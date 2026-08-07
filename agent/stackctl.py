#!/usr/bin/env python3
"""Deploying a docker-compose stack.

Runs the real `docker compose` CLI in the host's own namespaces, the same
way osupdate.py runs the host's package manager, rather than reimplementing
compose's YAML semantics -- profiles, build contexts, env-file
interpolation and everything else compose does are things this project has
no business getting subtly wrong.

That needs a directory that is the *same path* both inside this container
and on the host itself: when the compose command runs via nsenter, it does
so in the host's mount namespace, where only genuinely host-side paths
exist -- a container-only path like /var/lib/container-update-agent means
nothing there. Set CUD_STACKS_DIR to a host path and bind-mount it onto the
identical path:

    -v /opt/cud-stacks:/opt/cud-stacks
    -e CUD_STACKS_DIR=/opt/cud-stacks

Off entirely until CUD_STACKS_DIR is set. There is no way to confirm from
in here that the host-side bind mount actually matches -- this only checks
what it can (the directory being writable) and explains the rest in the
capability reason.
"""

import os
import re
import subprocess
import threading
import time
import uuid

import containerctl
import osupdate

SAFE_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_LOG_LINES = 400
DEFAULT_TIMEOUT = 1800


def stacks_dir():
    return os.environ.get("CUD_STACKS_DIR") or ""


def _namespace_reason():
    if not osupdate.in_host_pid_namespace():
        return ("This agent shares no namespaces with the host, so it cannot run "
                "docker compose there. Re-run it with --pid=host --privileged.")
    if os.geteuid() != 0:
        return ("This agent has the host's namespaces but is running as uid %d. "
                "Entering them needs root." % os.geteuid())
    if not osupdate._which("nsenter"):
        return "nsenter is missing from this image."
    return "This agent cannot reach the host's docker compose."


def capability():
    """What the dashboard should show, without changing anything."""
    directory = stacks_dir()
    if not directory:
        return {
            "allowed": False, "can_deploy": False,
            "reason": "CUD_STACKS_DIR is not set, so there is nowhere to write compose "
                      "files that the host's own docker compose can also see.",
        }
    if not containerctl.actions_allowed():
        return {
            "allowed": False, "can_deploy": False,
            "reason": "This agent was started with CUD_ALLOW_CONTAINER_ACTIONS=0.",
        }
    mode = osupdate.execution_mode()
    if not mode:
        return {"allowed": True, "can_deploy": False, "reason": _namespace_reason()}
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".cud-write-test")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as exc:
        return {
            "allowed": True, "can_deploy": False,
            "reason": "CUD_STACKS_DIR (%s) is not writable: %s" % (directory, exc),
        }
    return {"allowed": True, "can_deploy": True, "reason": None, "mode": mode}


class Job(object):
    def __init__(self, job_id, project):
        self.id = job_id
        self.project = project
        self.status = "running"
        self.lines = []
        self.started = time.time()
        self.finished = None
        self._lock = threading.Lock()

    def append(self, line):
        with self._lock:
            self.lines.append(line.rstrip())
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "project": self.project,
                "status": self.status,
                "lines": list(self.lines),
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


class Runner(object):
    """One deploy at a time -- docker compose itself would serialise
    concurrent runs against the same project anyway."""

    def __init__(self):
        self._jobs = {}
        self._order = []
        self._active = None
        self._lock = threading.Lock()

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def start_deploy(self, project, compose_text):
        able = capability()
        if not able["can_deploy"]:
            raise containerctl.ActionError(
                able["reason"] or "Stack deploy is not available on this host.")

        project = (project or "").strip().lower()
        if not SAFE_PROJECT.match(project):
            raise containerctl.ActionError(
                "%r is not a valid stack name -- lowercase letters, digits, - and _ only."
                % project)
        if not (compose_text or "").strip():
            raise containerctl.ActionError("A compose file is required.")

        with self._lock:
            if self._active is not None:
                raise containerctl.ActionError("A deploy is already running on this host.")
            job = Job(uuid.uuid4().hex[:12], project)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._active = job.id

        thread = threading.Thread(
            target=self._run, args=(job, project, compose_text),
            name="stack-deploy-%s" % job.id, daemon=True,
        )
        thread.start()
        return job

    def _run(self, job, project, compose_text):
        try:
            directory = os.path.join(stacks_dir(), project)
            os.makedirs(directory, exist_ok=True)
            compose_path = os.path.join(directory, "docker-compose.yml")
            with open(compose_path, "w") as handle:
                handle.write(compose_text)
            job.append("Wrote %s" % compose_path)

            argv = ["docker", "compose", "-f", compose_path, "-p", project,
                     "up", "-d", "--remove-orphans"]
            command = osupdate._wrap_for_host(argv, {})
            job.append("$ " + " ".join(command))

            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            deadline = time.time() + DEFAULT_TIMEOUT
            for raw in process.stdout:
                job.append(raw.decode("utf-8", "replace"))
                if time.time() > deadline:
                    process.kill()
                    job.append("timed out after %d seconds" % DEFAULT_TIMEOUT)
                    break
            process.wait()
            job.status = "ok" if process.returncode == 0 else "failed"
        except Exception as exc:
            if job.status == "running":
                job.append("%s: %s" % (type(exc).__name__, exc))
            job.status = "failed"
        finally:
            job.finished = time.time()
            with self._lock:
                self._active = None


RUNNER = Runner()
