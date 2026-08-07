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

import json
import os
import re
import subprocess
import threading
import time
import uuid

import containerctl
import osupdate

SAFE_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Docker itself put this string in a container's own compose-config-files
# label; still worth checking it looks like a path before it goes near a
# subprocess argv.
SAFE_PATH = re.compile(r"^/[^\x00]+$")
MAX_LOG_LINES = 400
DEFAULT_TIMEOUT = 1800


def stacks_dir():
    return os.environ.get("CUD_STACKS_DIR") or ""


def _host_root():
    return os.environ.get("CUD_HOST_ROOT") or "/"


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


def redeploy_capability():
    """Redeploying an *existing* stack doesn't need CUD_STACKS_DIR at all --
    nsenter runs docker compose against the compose file's already-known
    host path directly, from inside the host's own mount namespace, whether
    this dashboard wrote that file or not."""
    if not containerctl.actions_allowed():
        return {"can_redeploy": False,
                "reason": "This agent was started with CUD_ALLOW_CONTAINER_ACTIONS=0."}
    mode = osupdate.execution_mode()
    if not mode:
        return {"can_redeploy": False, "reason": _namespace_reason()}
    return {"can_redeploy": True, "reason": None, "mode": mode}


def read_compose_file(path):
    """Read a compose file via the read-only host view -- available
    whenever CUD_HOST_ROOT is set, the same mount OS updates already read
    package databases through. Works for any stack's file, not just ones
    under CUD_STACKS_DIR, since this never writes anything."""
    path = (path or "").strip()
    if not SAFE_PATH.match(path):
        raise containerctl.ActionError("Not a valid compose file path.")
    host_root = _host_root()
    full = path if host_root in ("/", "") else os.path.join(host_root, path.lstrip("/"))
    try:
        with open(full, "r", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise containerctl.ActionError("Could not read %s: %s" % (path, exc))


def write_compose_file(path, content):
    """Only within CUD_STACKS_DIR -- the only place this agent actually has
    write access to the host filesystem. A stack this dashboard did not
    create is offered as view-only for exactly this reason."""
    directory = stacks_dir()
    if not directory:
        raise containerctl.ActionError(
            "CUD_STACKS_DIR is not set, so nothing here is writable.")
    path = (path or "").strip()
    if not SAFE_PATH.match(path):
        raise containerctl.ActionError("Not a valid compose file path.")
    real_dir = os.path.realpath(directory)
    real_path = os.path.realpath(path)
    if real_path != real_dir and not real_path.startswith(real_dir + os.sep):
        raise containerctl.ActionError(
            "%s is outside CUD_STACKS_DIR -- this dashboard only edits files it wrote "
            "itself, so a stack deployed some other way is view-only here." % path)
    try:
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "w") as handle:
            handle.write(content)
    except OSError as exc:
        raise containerctl.ActionError("Could not write %s: %s" % (path, exc))


def validate(project, compose_text):
    """Ask the real docker compose to resolve the file -- catches YAML
    errors, undefined services and missing env vars the same way an actual
    deploy would, without deploying anything. Needs CUD_STACKS_DIR for the
    same reason deploying does: nsenter needs a host-visible path to point
    docker compose at."""
    able = capability()
    if not able["can_deploy"]:
        raise containerctl.ActionError(
            able["reason"] or "Stack validation is not available on this host.")
    project = (project or "stack-validate").strip().lower() or "stack-validate"
    if not SAFE_PROJECT.match(project):
        raise containerctl.ActionError(
            "%r is not a valid stack name -- lowercase letters, digits, - and _ only."
            % project)
    if not (compose_text or "").strip():
        raise containerctl.ActionError("A compose file is required.")

    directory = os.path.join(stacks_dir(), ".validate", project)
    os.makedirs(directory, exist_ok=True)
    compose_path = os.path.join(directory, "docker-compose.yml")
    try:
        with open(compose_path, "w") as handle:
            handle.write(compose_text)

        argv = ["docker", "compose", "-f", compose_path, "-p", project,
                 "config", "--format", "json"]
        command = osupdate._wrap_for_host(argv, {})
        try:
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=60,
            )
        except subprocess.SubprocessError as exc:
            raise containerctl.ActionError("Could not run docker compose config: %s" % exc)
    finally:
        try:
            os.remove(compose_path)
        except OSError:
            pass

    output = (result.stdout or b"").decode("utf-8", "replace")
    if result.returncode != 0:
        return {"valid": False, "errors": output.strip().splitlines() or
                ["docker compose config failed with no output."], "ports": []}

    try:
        resolved = json.loads(output)
    except ValueError:
        return {"valid": True, "errors": [],
                "warning": "docker compose accepted the file but did not return JSON "
                           "config to check ports against -- update the host's compose "
                           "plugin for this.", "ports": []}

    ports = []
    for service_name, service in (resolved.get("services") or {}).items():
        for port_spec in service.get("ports") or []:
            if not isinstance(port_spec, dict):
                continue
            published = port_spec.get("published")
            if published:
                ports.append({
                    "service": service_name,
                    "host_port": str(published),
                    "container_port": port_spec.get("target"),
                    "protocol": port_spec.get("protocol") or "tcp",
                })

    return {"valid": True, "errors": [], "ports": ports}


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

    def _claim(self, project):
        with self._lock:
            if self._active is not None:
                raise containerctl.ActionError(
                    "A deploy or redeploy is already running on this host.")
            job = Job(uuid.uuid4().hex[:12], project)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._active = job.id
        return job

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

        job = self._claim(project)
        thread = threading.Thread(
            target=self._run_deploy, args=(job, project, compose_text),
            name="stack-deploy-%s" % job.id, daemon=True,
        )
        thread.start()
        return job

    def start_redeploy(self, project, compose_path):
        able = redeploy_capability()
        if not able["can_redeploy"]:
            raise containerctl.ActionError(
                able["reason"] or "Stack redeploy is not available on this host.")

        project = (project or "").strip().lower()
        if not SAFE_PROJECT.match(project):
            raise containerctl.ActionError("%r is not a valid stack name." % project)
        compose_path = (compose_path or "").strip()
        if not SAFE_PATH.match(compose_path):
            raise containerctl.ActionError("Not a valid compose file path.")

        job = self._claim(project)
        thread = threading.Thread(
            target=self._run_redeploy, args=(job, project, compose_path),
            name="stack-redeploy-%s" % job.id, daemon=True,
        )
        thread.start()
        return job

    def _stream(self, job, argv):
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
        if process.returncode != 0:
            raise containerctl.ActionError(
                "docker compose exited with status %d" % process.returncode)

    def _finish(self, job, ok):
        job.status = "ok" if ok else "failed"
        job.finished = time.time()
        with self._lock:
            self._active = None

    def _run_deploy(self, job, project, compose_text):
        try:
            directory = os.path.join(stacks_dir(), project)
            os.makedirs(directory, exist_ok=True)
            compose_path = os.path.join(directory, "docker-compose.yml")
            with open(compose_path, "w") as handle:
                handle.write(compose_text)
            job.append("Wrote %s" % compose_path)
            self._stream(job, ["docker", "compose", "-f", compose_path, "-p", project,
                                "up", "-d", "--remove-orphans"])
        except Exception as exc:
            job.append("%s: %s" % (type(exc).__name__, exc))
            self._finish(job, False)
            return
        self._finish(job, True)

    def _run_redeploy(self, job, project, compose_path):
        try:
            self._stream(job, ["docker", "compose", "-f", compose_path, "-p", project, "pull"])
            self._stream(job, ["docker", "compose", "-f", compose_path, "-p", project,
                                "up", "-d", "--remove-orphans"])
        except Exception as exc:
            job.append("%s: %s" % (type(exc).__name__, exc))
            self._finish(job, False)
            return
        self._finish(job, True)


RUNNER = Runner()
