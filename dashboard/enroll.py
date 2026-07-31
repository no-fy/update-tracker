#!/usr/bin/env python3
"""Enrolment: the remote host comes to us.

The dashboard never holds an SSH key and never opens a shell anywhere. It mints
a single-use, expiring enrolment token together with the bearer token the agent
will use, and hands you one `docker run` to paste on the host. The agent
registers itself when it starts; the dashboard verifies it by connecting back
before writing anything to the config.

Enrolments live in memory only. A restart cancels the pending ones, which is
the safe direction: no unclaimed secret is ever written to disk.
"""

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dashboard import config as config_mod
else:
    from . import config as config_mod

DEFAULT_AGENT_PORT = 9713
DEFAULT_TTL_MINUTES = 60
AGENT_IMAGE = os.environ.get("CUD_AGENT_IMAGE", "ghcr.io/no-fy/update-tracker-agent:latest")
CONTAINER_NAME = "container-update-agent"


class EnrollError(Exception):
    """Something the caller can fix, reported as a 4xx."""


def _now():
    return time.time()


class Enrollment(object):
    def __init__(self, name=None, port=DEFAULT_AGENT_PORT, ttl_minutes=DEFAULT_TTL_MINUTES):
        self.id = secrets.token_hex(8)
        self.token = secrets.token_urlsafe(32)
        self.agent_token = secrets.token_urlsafe(32)
        self.name = name or None
        self.port = int(port or DEFAULT_AGENT_PORT)
        self.created = _now()
        self.expires = self.created + float(ttl_minutes) * 60
        self.status = "pending"
        self.host = None
        self.error = None

    def expired(self):
        return self.status == "pending" and _now() > self.expires

    def snapshot(self, include_token=False):
        out = {
            "id": self.id,
            "name": self.name,
            "port": self.port,
            "status": "expired" if self.expired() else self.status,
            "created": self.created,
            "expires": self.expires,
            "expires_in": max(0, int(self.expires - _now())),
            "host": self.host,
            "error": self.error,
        }
        # The token is shown once, to the person who asked for it. It is not
        # part of the listing, so a shoulder-surfed dashboard leaks nothing.
        if include_token:
            out["token"] = self.token
        return out


def agent_command(enrollment, dashboard_url, socket_path="/var/run/docker.sock"):
    """The line to paste on the remote host.

    `/:/host:ro` is what lets the agent read the host's package databases for
    OS updates. Drop that line and the `CUD_HOST_ROOT` one to watch containers
    only -- everything else keeps working.
    """
    return (
        "docker run -d --name {name} --restart unless-stopped \\\n"
        "  -v {socket}:{socket}:ro \\\n"
        "  -v /:/host:ro \\\n"
        "  -p {port}:{port} \\\n"
        "  -e CUD_AGENT_TOKEN={agent_token} \\\n"
        "  -e CUD_HOST_ROOT=/host \\\n"
        "  -e CUD_ENROLL_URL={url}/api/enroll \\\n"
        "  -e CUD_ENROLL_TOKEN={token} \\\n"
        "  {image}"
    ).format(
        name=CONTAINER_NAME,
        socket=socket_path,
        port=enrollment.port,
        agent_token=enrollment.agent_token,
        url=dashboard_url.rstrip("/"),
        token=enrollment.token,
        image=AGENT_IMAGE,
    )


def verify_agent(address, port, token, timeout=8):
    """Prove the thing that called us is the agent we just minted a token for."""
    url = "http://%s:%d/v1/info" % (address, int(port))
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class EnrollmentStore(object):
    def __init__(self, config_path, on_registered=None):
        self.config_path = config_path
        self.on_registered = on_registered
        self._items = {}
        self._by_token = {}
        self._lock = threading.Lock()

    def create(self, name=None, port=DEFAULT_AGENT_PORT, ttl_minutes=DEFAULT_TTL_MINUTES):
        name = (name or "").strip() or None
        if name and not all(c.isalnum() or c in "-_." for c in name):
            raise EnrollError("A name may only contain letters, digits, dot, dash and underscore.")
        try:
            port = int(port or DEFAULT_AGENT_PORT)
        except (TypeError, ValueError):
            raise EnrollError("That port is not a number.")
        if not 1 <= port <= 65535:
            raise EnrollError("That port is out of range.")

        enrollment = Enrollment(name=name, port=port, ttl_minutes=ttl_minutes)
        with self._lock:
            self._purge_locked()
            self._items[enrollment.id] = enrollment
            self._by_token[enrollment.token] = enrollment
        return enrollment

    def _purge_locked(self):
        for item in list(self._items.values()):
            if item.expired():
                item.status = "expired"
            done = item.status in ("expired", "registered")
            if done and _now() - item.created > 24 * 3600:
                self._items.pop(item.id, None)
                self._by_token.pop(item.token, None)

    def get(self, enrollment_id):
        with self._lock:
            return self._items.get(enrollment_id)

    def list(self):
        with self._lock:
            self._purge_locked()
            items = sorted(self._items.values(), key=lambda e: e.created, reverse=True)
            return [item.snapshot() for item in items]

    def delete(self, enrollment_id):
        with self._lock:
            item = self._items.pop(enrollment_id, None)
            if item:
                self._by_token.pop(item.token, None)
            return item is not None

    # -- the callback ------------------------------------------------------

    def claim(self, token, source_address, payload):
        """Called by the agent itself. Single use, and it must answer back."""
        with self._lock:
            enrollment = self._by_token.get(token or "")
            if enrollment is None:
                raise EnrollError("Unknown or already-used enrolment token.")
            if enrollment.expired():
                enrollment.status = "expired"
                raise EnrollError("That enrolment token has expired. Generate a new command.")
            if enrollment.status != "pending":
                raise EnrollError("That enrolment token has already been used.")
            # Burn it now: a second caller with the same token gets nothing,
            # even if the verification below ends up failing.
            enrollment.status = "claiming"
            self._by_token.pop(token, None)

        address = (payload.get("address") or "").strip() or source_address
        port = payload.get("port") or enrollment.port
        try:
            info = verify_agent(address, port, enrollment.agent_token)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            enrollment.status = "failed"
            enrollment.error = (
                "The agent registered from %s but the dashboard could not reach it back "
                "on %s:%s (%s). Check the port is published and no firewall is in the way."
                % (source_address, address, port, getattr(exc, "reason", exc))
            )
            raise EnrollError(enrollment.error)

        hostname = info.get("hostname") or payload.get("hostname") or address
        name = enrollment.name or str(hostname).split(".")[0]
        host = {
            "name": name,
            "mode": "agent",
            "label": hostname,
            "address": address,
            "port": int(port),
            "token": enrollment.agent_token,
            "tls": False,
            "verify_tls": True,
            "enabled": True,
        }
        config, config_path = config_mod.load_config(self.config_path)
        config_mod.upsert_host(config, host)
        config_mod.save_config(config, config_path)

        enrollment.status = "registered"
        enrollment.host = {k: v for k, v in host.items() if k != "token"}
        if self.on_registered:
            try:
                self.on_registered(enrollment)
            except Exception:
                pass
        return enrollment
