#!/usr/bin/env python3
"""Actually installing OS updates -- one of two things here that write.

The other is containerctl.py. This one needs more than the Docker socket: it
runs the host's own package manager, which needs the host's namespaces
(--pid=host --privileged) as well as CUD_ALLOW_UPDATES not being turned off.
Without those extra flags an agent stays exactly as report-only as it always
was, regardless of the env var's default.

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


def in_host_pid_namespace():
    """True when PID 1 is the host's init rather than this container's.

    Compared by namespace link rather than by reading /proc/1/ns/mnt, which is
    root-only -- an unprivileged process would otherwise conclude "no host
    namespaces" when the real problem is that it is unprivileged.
    """
    try:
        return os.readlink("/proc/1/ns/mnt") != os.readlink("/proc/self/ns/mnt")
    except OSError:
        return False


def execution_mode():
    """How, if at all, this agent can reach the host's package manager."""
    if _host_root() in ("/", ""):
        return "direct"  # the agent is on the host itself
    if os.geteuid() != 0:
        return None
    if not in_host_pid_namespace():
        return None
    if not _which("nsenter"):
        return None
    return "nsenter"


def _which(binary):
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        candidate = os.path.join(directory, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _read_first_line(path):
    try:
        with open(path, "r") as handle:
            return handle.read().strip().split("\n")[0]
    except OSError:
        return ""


def same_machine():
    """Is the system we would write to the same one we are reading from?

    On Docker Desktop, --pid=host puts you in *its* Linux VM, while -v /:/host
    is the machine you actually care about. Installing then upgrades the wrong
    system entirely, so compare machine-ids before offering to.

    None means "cannot tell", which is not treated as a failure.
    """
    if execution_mode() != "nsenter":
        return True
    read_id = _read_first_line(os.path.join(_host_root(), "etc/machine-id"))
    if not read_id:
        return None
    try:
        result = subprocess.run(
            _wrap_for_host(["cat", "/etc/machine-id"], {}),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    run_id = (result.stdout or b"").decode("utf-8", "replace").strip()
    if not run_id:
        return None
    return read_id == run_id


def capability():
    """What the dashboard should show, without changing anything."""
    mode = execution_mode()
    matches = same_machine() if (updates_allowed() and mode) else True
    can_update = bool(updates_allowed() and mode and matches is not False)
    reason = _reason(mode)
    if reason is None and matches is False:
        reason = (
            "The packages listed are read from this host's filesystem, but "
            "commands would run on a different machine -- on Docker Desktop, "
            "--pid=host is Docker's own Linux VM, not the machine you mounted. "
            "Installing from here would upgrade the wrong system, so it is "
            "disabled. Run the agent on the machine itself to install updates."
        )
    return {
        "allowed": updates_allowed(),
        "mode": mode,
        "can_update": can_update,
        "same_machine": matches,
        "reason": reason,
    }


def _reason(mode):
    """Say which condition actually failed, not a generic 'add these flags'."""
    if not updates_allowed():
        return ("This agent was started with CUD_ALLOW_UPDATES=0, so it only "
                "reports updates and will not install them.")
    if mode is not None:
        return None
    if not in_host_pid_namespace():
        return ("This agent shares no namespaces with the host, so it cannot "
                "run the host's package manager. Re-run it with --pid=host "
                "--privileged.")
    if os.geteuid() != 0:
        return ("This agent has the host's namespaces but is running as uid %d. "
                "Entering them needs root, so it cannot install updates."
                % os.geteuid())
    if not _which("nsenter"):
        return ("nsenter is missing from this image, so the agent cannot run "
                "commands in the host's namespaces.")
    return "The agent cannot reach the host's package manager."


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
        # Re-checked here, not just in the UI: the API is reachable directly.
        able = capability()
        if not able["can_update"]:
            raise UpdateError(able["reason"] or "This agent cannot install updates.")
        mode = able["mode"]

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
