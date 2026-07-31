#!/usr/bin/env python3
"""Run setup-host.py's remote install from the dashboard, as a background job.

The installer is a CLI: it prints progress and returns an exit code. Rather
than reimplement it, this captures its stdout line by line so the browser can
watch the same output the terminal would show.

One job runs at a time. That keeps config.json writes serialised and matches
what a person would do anyway.
"""

import contextlib
import importlib.util
import os
import re
import threading
import time
import uuid

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dashboard import config as config_mod, keystore
else:
    from . import config as config_mod, keystore

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
UNSAFE_TARGET = re.compile(r"[\s\x00-\x1f]")

# The browser cannot answer a host key prompt, and `ssh -o BatchMode=yes` fails
# rather than asking. Trust on first use is the only workable policy here; it is
# what a person typing `ssh` for the first time does by hand.
WEB_SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "IdentitiesOnly=yes"]

MAX_LINES = 400


def load_setup_module():
    path = os.path.join(REPO_ROOT, "setup-host.py")
    spec = importlib.util.spec_from_file_location("setup_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProvisionError(Exception):
    """Something the user can fix, reported back as a 400."""


class _Args(object):
    """The argparse namespace setup-host.py expects."""

    def __init__(self, **kwargs):
        self.target = None
        self.name = None
        self.label = None
        self.address = None
        self.port = None
        self.bind = "0.0.0.0"
        self.ssh_port = None
        self.identity = None
        self.token = None
        self.docker_socket = None
        self.config = None
        self.dry_run = False
        self.skip_verify = False
        self.local = False
        self.uninstall = False
        for key, value in kwargs.items():
            setattr(self, key, value)


class _LineWriter(object):
    """A file-like object that feeds whole lines to a job."""

    def __init__(self, job):
        self.job = job
        self._buffer = ""

    def write(self, text):
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            self.job.append(line)
        return len(text)

    def flush(self):
        if self._buffer.strip():
            self.job.append(self._buffer)
            self._buffer = ""


class Job(object):
    def __init__(self, job_id, target, name=None):
        self.id = job_id
        self.target = target
        self.name = name
        self.status = "running"
        self.error = None
        self.host = None
        self.started = time.time()
        self.finished = None
        self.lines = []
        self._lock = threading.Lock()

    def append(self, line):
        clean = ANSI.sub("", line).rstrip()
        with self._lock:
            self.lines.append(clean)
            if len(self.lines) > MAX_LINES:
                del self.lines[: len(self.lines) - MAX_LINES]

    def finish(self, status, error=None, host=None):
        with self._lock:
            self.status = status
            self.error = error
            self.host = host
            self.finished = time.time()

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "target": self.target,
                "name": self.name,
                "status": self.status,
                "error": self.error,
                "host": self.host,
                "lines": list(self.lines),
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


def validate_key(material):
    if not material or not material.strip():
        raise ProvisionError("An SSH private key is required.")
    text = material.strip()
    if "PRIVATE KEY" not in text.split("\n")[0].upper():
        raise ProvisionError(
            "That does not look like a private key. Paste the key file itself "
            "(the one without a .pub extension), not the public key."
        )
    if "ENCRYPTED" in text[:400].upper() or "Proc-Type: 4,ENCRYPTED" in text:
        raise ProvisionError(
            "That key is passphrase-protected. The installer runs unattended and "
            "cannot be prompted, so it needs a key with no passphrase."
        )
    return text


def validate_target(target):
    target = (target or "").strip()
    if not target:
        raise ProvisionError("A host is required, such as root@nas.lan.")
    if UNSAFE_TARGET.search(target):
        raise ProvisionError("That host contains whitespace or control characters.")
    if len(target) > 255:
        raise ProvisionError("That host is too long.")
    return target


class Provisioner(object):
    def __init__(self, config_path, key_dir=None, on_finish=None):
        self.config_path = config_path
        self.keys = keystore.KeyStore(key_dir or keystore.default_key_dir(config_path))
        self.on_finish = on_finish
        self._jobs = {}
        self._order = []
        self._active = None
        self._lock = threading.Lock()

    # -- job bookkeeping ---------------------------------------------------

    def busy(self):
        with self._lock:
            return self._active is not None

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit=10):
        with self._lock:
            ids = self._order[-limit:]
            return [self._jobs[i].snapshot() for i in ids if i in self._jobs]

    # -- running -----------------------------------------------------------

    def start(self, params):
        target = validate_target(params.get("target"))
        name = (params.get("name") or "").strip() or None
        stored_key = (params.get("stored_key") or "").strip() or None

        if stored_key:
            if not self.keys.has(stored_key):
                raise ProvisionError("No stored key named %r." % stored_key)
            key_material = None
        else:
            key_material = validate_key(params.get("ssh_key"))

        with self._lock:
            if self._active is not None:
                raise ProvisionError(
                    "Another host is being set up right now. Wait for it to finish."
                )
            job = Job(uuid.uuid4().hex[:12], target, name)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._active = job.id

        thread = threading.Thread(
            target=self._run,
            args=(job, params, key_material, stored_key),
            name="provision-%s" % job.id,
            daemon=True,
        )
        thread.start()
        return job

    def _run(self, job, params, key_material, stored_key):
        key_name = keystore.slugify(job.name or job.target)
        remember = bool(params.get("remember_key"))
        identity = None
        try:
            if stored_key:
                identity = self.keys.path_for(stored_key)
            else:
                identity = self.keys.save(key_name, key_material)

            module = load_setup_module()
            module.EXTRA_SSH_OPTS = list(WEB_SSH_OPTS)
            # add_remote falls back to an interactive sudo prompt when the
            # remote sudo wants a password. There is no terminal here, so fail
            # with something actionable instead of hanging on a hidden prompt.
            module.run_remote_interactive = _no_interactive_sudo(module)

            args = _Args(
                target=job.target,
                name=job.name,
                label=(params.get("label") or "").strip() or None,
                address=(params.get("address") or "").strip() or None,
                port=_int_or_none(params.get("port")),
                ssh_port=_int_or_none(params.get("ssh_port")),
                identity=identity,
                skip_verify=bool(params.get("skip_verify")),
            )

            config, config_path = config_mod.load_config(self.config_path)
            writer = _LineWriter(job)
            with contextlib.redirect_stdout(writer):
                code = module.add_remote(args, config, config_path)
            writer.flush()

            if code == 0:
                config, _ = config_mod.load_config(self.config_path)
                host = _find_new_host(config, args, job)
                job.finish("ok", host=host)
            else:
                job.finish("failed", error=_last_meaningful_line(job) or "Setup failed.")
        except Exception as exc:  # surfaced to the browser, not swallowed
            job.append(str(exc))
            job.finish("failed", error=str(exc))
        finally:
            if identity and not remember and not stored_key:
                self.keys.delete(key_name)
            with self._lock:
                self._active = None
            if self.on_finish:
                try:
                    self.on_finish(job)
                except Exception:
                    pass


def _no_interactive_sudo(module):
    def _fail(*_args, **_kwargs):
        raise module.SetupError(
            "sudo on that host asks for a password, which cannot be typed from "
            "the dashboard. Connect as root, or give the user passwordless sudo, "
            "or run ./setup-host.py for this host from a terminal."
        )

    return _fail


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _find_new_host(config, args, job):
    from . import server as _server  # local import: avoids a cycle at module load

    name = args.name or job.target.split("@")[-1].split(":")[0].split(".")[0]
    host = config_mod.find_host(config, name)
    return _server.sanitise_host(host) if host else None


def _last_meaningful_line(job):
    for line in reversed(job.snapshot()["lines"]):
        text = line.strip()
        if text and not text.startswith("=="):
            return text
    return None
