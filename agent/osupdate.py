#!/usr/bin/env python3
"""Actually installing OS updates -- the one thing here that writes.

Everything else in this project is read-only by design. This is not, so it is
off unless switched on: the agent refuses to run anything unless started with
CUD_ALLOW_UPDATES=1. An agent that was never given that stays exactly as
read-only as it always was.

Running the host's package manager from inside a container needs the host's
namespaces, which means the agent must be started with --privileged and
--pid=host. Without them this reports why it cannot act rather than half-doing
something.

Package names are never interpolated into a shell. The command is built as an
argv list, and every name is checked against the set the scanner actually found
as upgradable, so nothing outside that list can be passed through.
"""

import os
import re
import subprocess
import threading
import time
import uuid

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
MAX_LINES = 600
DEFAULT_TIMEOUT = 1800


def updates_allowed():
    """On by default. Set CUD_ALLOW_UPDATES=0 for an agent that only reports."""
    value = (os.environ.get("CUD_ALLOW_UPDATES") or "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    return True


class UpdateError(Exception):
    """Refused before anything ran."""


def _host_root():
    return os.environ.get("CUD_HOST_ROOT") or "/"


def execution_mode():
    """How, if at all, this agent can reach the host's package manager."""
    if _host_root() in ("/", ""):
        return "direct"  # the agent is on the host itself
    if os.path.exists("/proc/1/ns/mnt") and _which("nsenter"):
        return "nsenter"
    return None


def _which(binary):
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        candidate = os.path.join(directory, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def capability():
    """What the dashboard should show, without trying anything."""
    mode = execution_mode()
    return {
        "allowed": updates_allowed(),
        "mode": mode,
        "can_update": bool(updates_allowed() and mode),
        "reason": _reason(mode),
    }


def _reason(mode):
    if not updates_allowed():
        return ("This agent was started with CUD_ALLOW_UPDATES=0, so it only "
                "reports updates and will not install them.")
    if mode is None:
        return ("The agent cannot reach the host's package manager. Re-run it "
                "with --privileged --pid=host so it can run commands in the "
                "host's namespaces.")
    return None


def build_command(manager, packages):
    """The argv to run. No shell, ever."""
    if manager == "apt":
        return (
            ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold",
             "--only-upgrade", "install"] + packages,
            {"DEBIAN_FRONTEND": "noninteractive"},
        )
    if manager == "apk":
        return ["apk", "upgrade"] + packages, {}
    if manager == "pacman":
        return ["pacman", "-S", "--noconfirm"] + packages, {}
    raise UpdateError("Updating %s hosts is not supported." % manager)


def _wrap_for_host(argv, env):
    """Run argv on the host rather than inside this container."""
    if execution_mode() == "direct":
        return argv
    prefix = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--"]
    if env:
        prefix += ["env"] + ["%s=%s" % (key, value) for key, value in sorted(env.items())]
    return prefix + argv


class Job(object):
    def __init__(self, job_id, packages, manager):
        self.id = job_id
        self.packages = list(packages)
        self.manager = manager
        self.status = "running"
        self.returncode = None
        self.lines = []
        self.started = time.time()
        self.finished = None
        self._lock = threading.Lock()

    def append(self, line):
        with self._lock:
            self.lines.append(line.rstrip())
            if len(self.lines) > MAX_LINES:
                del self.lines[: len(self.lines) - MAX_LINES]

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "packages": self.packages,
                "manager": self.manager,
                "status": self.status,
                "returncode": self.returncode,
                "lines": list(self.lines),
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


class Runner(object):
    """One update at a time. Package managers take their own locks anyway."""

    def __init__(self):
        self._jobs = {}
        self._order = []
        self._active = None
        self._lock = threading.Lock()

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit=10):
        with self._lock:
            return [self._jobs[i].snapshot() for i in self._order[-limit:] if i in self._jobs]

    def start(self, manager, requested, upgradable, on_finish=None):
        if not updates_allowed():
            raise UpdateError(_reason(execution_mode()))
        mode = execution_mode()
        if mode is None:
            raise UpdateError(_reason(mode))

        upgradable = set(upgradable or [])
        packages = []
        for name in requested or []:
            name = (name or "").strip()
            if not SAFE_NAME.match(name):
                raise UpdateError("%r is not a valid package name." % name)
            if name not in upgradable:
                # The dashboard can only ask for things this agent already
                # reported as upgradable. Anything else is a bug or an attempt.
                raise UpdateError("%s is not a pending update on this host." % name)
            packages.append(name)
        if not packages:
            raise UpdateError("No packages were selected.")

        with self._lock:
            if self._active is not None:
                raise UpdateError("An update is already running on this host.")
            job = Job(uuid.uuid4().hex[:12], packages, manager)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._active = job.id

        thread = threading.Thread(
            target=self._run, args=(job, manager, packages, on_finish),
            name="os-update-%s" % job.id, daemon=True,
        )
        thread.start()
        return job

    def _run(self, job, manager, packages, on_finish):
        try:
            argv, env = build_command(manager, packages)
            command = _wrap_for_host(argv, env)
            job.append("$ " + " ".join(command))
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=dict(os.environ, **env) if execution_mode() == "direct" else None,
            )
            deadline = time.time() + DEFAULT_TIMEOUT
            for raw in process.stdout:
                job.append(raw.decode("utf-8", "replace"))
                if time.time() > deadline:
                    process.kill()
                    job.append("timed out after %d seconds" % DEFAULT_TIMEOUT)
                    break
            process.wait()
            job.returncode = process.returncode
            job.status = "ok" if process.returncode == 0 else "failed"
        except FileNotFoundError as exc:
            job.append(str(exc))
            job.returncode = -1
            job.status = "failed"
        except Exception as exc:
            job.append("%s: %s" % (type(exc).__name__, exc))
            job.returncode = -1
            job.status = "failed"
        finally:
            job.finished = time.time()
            with self._lock:
                self._active = None
            if on_finish:
                try:
                    on_finish(job)
                except Exception:
                    pass


RUNNER = Runner()
