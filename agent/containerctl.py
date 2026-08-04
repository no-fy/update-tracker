#!/usr/bin/env python3
"""Controlling containers -- start, stop, restart, pause, rename, remove,
recreate, and reading their logs.

This is the other thing in this project that writes, alongside osupdate.py.
Same convention: on by default, CUD_ALLOW_CONTAINER_ACTIONS=0 turns an agent
back into a pure reporter. Unlike OS updates, this needs nothing beyond the
Docker socket already mounted for reading containers -- there is no extra
namespace or privilege to grant, so an upgraded agent image gets this the
moment it starts unless the env var says otherwise.
"""

import os
import re
import struct
import threading
import time
import uuid

ACTIONS = ("start", "stop", "restart", "pause", "unpause")
SAFE_ID = re.compile(r"^[a-fA-F0-9]{12,64}$")
# Docker's own container-name rule (see NameRegexp in moby/moby).
SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
MAX_TAIL = 2000
MAX_LOG_LINES = 400


def actions_allowed():
    """On by default. Set CUD_ALLOW_CONTAINER_ACTIONS=0 for report-only."""
    value = (os.environ.get("CUD_ALLOW_CONTAINER_ACTIONS") or "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    return True


def capability():
    """What the dashboard should show, without changing anything."""
    allowed = actions_allowed()
    return {
        "allowed": allowed,
        "can_manage": allowed,
        "reason": None if allowed else (
            "This agent was started with CUD_ALLOW_CONTAINER_ACTIONS=0, so it "
            "only reports container state."
        ),
    }


class ActionError(Exception):
    """Refused before anything ran."""


def _require_allowed():
    if not actions_allowed():
        raise ActionError(
            "This agent was started with CUD_ALLOW_CONTAINER_ACTIONS=0, so it "
            "cannot change containers."
        )


def _require_id(container_id):
    if not SAFE_ID.match(container_id or ""):
        raise ActionError("Not a valid container id.")


def run_action(client, container_id, action, timeout=None):
    # Re-checked here, not just in the UI: the API is reachable directly.
    _require_allowed()
    if action not in ACTIONS:
        raise ActionError("%r is not a supported action." % action)
    _require_id(container_id)
    client.container_action(container_id, action, timeout=timeout)


def run_rename(client, container_id, new_name):
    _require_allowed()
    _require_id(container_id)
    new_name = (new_name or "").strip()
    if not SAFE_NAME.match(new_name):
        raise ActionError("%r is not a valid container name." % new_name)
    client.rename_container(container_id, new_name)


def run_remove(client, container_id, expected_name=None):
    """Remove a stopped container. Docker itself refuses a running one
    without force, which this never passes -- that safety is not ours to
    override from a web click.
    """
    _require_allowed()
    _require_id(container_id)
    if expected_name is not None:
        # The dashboard makes the user type the container's name before this
        # is even called; re-check it here too, since the API is reachable
        # directly and a stale page could otherwise remove the wrong thing.
        info = client.inspect_container(container_id) or {}
        actual = (info.get("Name") or "").lstrip("/")
        if actual != expected_name:
            raise ActionError(
                "That name does not match this container; refusing to remove it."
            )
    client.remove_container(container_id)


# ---- recreate: pull the image, then swap the container for a new one ------

def _split_image_ref(ref):
    """(repository, tag-or-digest) the way /images/create wants it."""
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, digest
    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon > last_slash:
        return ref[:last_colon], ref[last_colon + 1:]
    return ref, "latest"


def _primary_network(networks, network_mode):
    if not networks:
        return None, None
    if network_mode in networks:
        return network_mode, networks[network_mode]
    name = next(iter(networks))
    return name, networks[name]


def _endpoint_config(raw, old_container_id=None):
    """Only the parts of a NetworkSettings entry that are inputs, not the
    computed IP/gateway/MAC Docker filled in when reporting it back.

    Aliases also carries Docker's own auto-added alias -- the container's
    short id -- which belongs to the container being replaced, not the new
    one. Docker will add the new container's own id-alias itself.
    """
    raw = raw or {}
    out = {}
    aliases = raw.get("Aliases") or []
    if old_container_id:
        short_id = old_container_id[:12]
        aliases = [a for a in aliases if a != old_container_id and a != short_id]
    if aliases:
        out["Aliases"] = aliases
    if raw.get("Links"):
        out["Links"] = raw["Links"]
    if raw.get("DriverOpts"):
        out["DriverOpts"] = raw["DriverOpts"]
    ipam = raw.get("IPAMConfig") or {}
    ipam = {k: v for k, v in ipam.items() if v}
    if ipam:
        out["IPAMConfig"] = ipam
    return out


class RecreateJob(object):
    def __init__(self, job_id, container_id, name):
        self.id = job_id
        self.container_id = container_id
        self.name = name
        self.status = "running"
        self.lines = []
        self.new_container_id = None
        self.started = time.time()
        self.finished = None
        self._lock = threading.Lock()
        self._last_pull_status = {}

    def append(self, line):
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def pull_progress(self, event):
        layer_id = event.get("id")
        status = event.get("status")
        if not status:
            return
        key = layer_id or ""
        # Layer progress repeats dozens of times as bytes download; only log
        # it when the status actually changes, not every percentage tick.
        if self._last_pull_status.get(key) == status:
            return
        self._last_pull_status[key] = status
        self.append(("%s: %s" % (layer_id, status)) if layer_id else status)

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "container": self.container_id,
                "name": self.name,
                "status": self.status,
                "new_container": self.new_container_id,
                "lines": list(self.lines),
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


class RecreateRunner(object):
    """One recreate at a time per container."""

    def __init__(self):
        self._jobs = {}
        self._order = []
        self._active = set()
        self._lock = threading.Lock()

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, client, container_id, on_finish=None):
        _require_allowed()
        _require_id(container_id)

        with self._lock:
            if container_id in self._active:
                raise ActionError("A recreate is already running for this container.")
            self._active.add(container_id)

        info = client.inspect_container(container_id) or {}
        name = (info.get("Name") or "").lstrip("/") or container_id

        job = RecreateJob(uuid.uuid4().hex[:12], container_id, name)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)

        thread = threading.Thread(
            target=self._run, args=(job, client, container_id, info, on_finish),
            name="recreate-%s" % job.id, daemon=True,
        )
        thread.start()
        return job

    def _run(self, job, client, container_id, info, on_finish):
        old_name = job.name
        was_running = ((info.get("State") or {}).get("Running") is True)
        config = dict(info.get("Config") or {})
        host_config = info.get("HostConfig") or {}
        networks = (info.get("NetworkSettings") or {}).get("Networks") or {}

        try:
            image_ref = config.get("Image") or ""
            repo, reference = _split_image_ref(image_ref)
            job.append("Pulling %s:%s" % (repo, reference))
            try:
                client.pull_image(repo, reference, on_line=job.pull_progress)
            except Exception as exc:
                job.append("Pull failed: %s" % exc)
                raise

            if was_running:
                job.append("Stopping %s" % old_name)
                client.container_action(container_id, "stop")

            temp_name = "%s-recreate-%s" % (old_name, uuid.uuid4().hex[:8])
            job.append("Renaming %s out of the way" % old_name)
            client.rename_container(container_id, temp_name)

            primary_name, primary_net = _primary_network(
                networks, host_config.get("NetworkMode"))

            create_body = dict(config)
            create_body["HostConfig"] = host_config
            if primary_name:
                create_body["NetworkingConfig"] = {
                    "EndpointsConfig": {
                        primary_name: _endpoint_config(primary_net, container_id)
                    }
                }

            job.append("Creating %s from the refreshed image" % old_name)
            try:
                created = client.create_container(old_name, create_body)
            except Exception as exc:
                job.append("Create failed: %s -- rolling back" % exc)
                client.rename_container(container_id, old_name)
                if was_running:
                    client.container_action(container_id, "start")
                raise

            new_id = created.get("Id")
            job.new_container_id = new_id

            for extra_name, extra_net in networks.items():
                if extra_name == primary_name:
                    continue
                try:
                    job.append("Connecting network %s" % extra_name)
                    client.connect_network(
                        extra_name, new_id, _endpoint_config(extra_net, container_id))
                except Exception as exc:
                    # Best-effort: worth finishing the recreate over one
                    # secondary network failing to reattach.
                    job.append("Could not connect network %s: %s" % (extra_name, exc))

            job.append("Starting %s" % old_name)
            try:
                client.container_action(new_id, "start")
            except Exception as exc:
                job.append("Start failed: %s -- rolling back" % exc)
                try:
                    client.remove_container(new_id)
                except Exception:
                    pass
                client.rename_container(container_id, old_name)
                if was_running:
                    client.container_action(container_id, "start")
                raise

            job.append("Removing the old container")
            client.remove_container(container_id)

            job.append("Done.")
            job.status = "ok"
        except Exception as exc:
            if job.status == "running":
                job.append("%s: %s" % (type(exc).__name__, exc))
            job.status = "failed"
        finally:
            job.finished = time.time()
            with self._lock:
                self._active.discard(container_id)
            if on_finish:
                try:
                    on_finish(job)
                except Exception:
                    pass


RECREATE_RUNNER = RecreateRunner()


def _demux(raw):
    """Docker frames stdout/stderr with an 8-byte header per chunk unless the
    container has a TTY, in which case the stream is already plain bytes.
    Try to parse it as framed; fall back to raw if that does not add up.
    """
    out = bytearray()
    i, n = 0, len(raw)
    while i + 8 <= n:
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        chunk = raw[i + 8:i + 8 + size]
        if len(chunk) != size:
            return None
        out += chunk
        i += 8 + size
    if i != n:
        return None
    return bytes(out)


def fetch_logs(client, container_id, tail=200):
    _require_id(container_id)
    tail = max(1, min(int(tail or 200), MAX_TAIL))
    raw = client.container_logs(container_id, tail=tail)
    demuxed = _demux(raw)
    text = (demuxed if demuxed is not None else raw).decode("utf-8", "replace")
    return text.splitlines()[-tail:]
