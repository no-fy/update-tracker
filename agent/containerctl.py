#!/usr/bin/env python3
"""Controlling containers -- start, stop, restart, and reading their logs.

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

ACTIONS = ("start", "stop", "restart")
SAFE_ID = re.compile(r"^[a-fA-F0-9]{12,64}$")
MAX_TAIL = 2000


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


def run_action(client, container_id, action, timeout=None):
    # Re-checked here, not just in the UI: the API is reachable directly.
    if not actions_allowed():
        raise ActionError(
            "This agent was started with CUD_ALLOW_CONTAINER_ACTIONS=0, so it "
            "cannot start, stop or restart containers."
        )
    if action not in ACTIONS:
        raise ActionError("%r is not a supported action." % action)
    if not SAFE_ID.match(container_id or ""):
        raise ActionError("Not a valid container id.")
    client.container_action(container_id, action, timeout=timeout)


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
    if not SAFE_ID.match(container_id or ""):
        raise ActionError("Not a valid container id.")
    tail = max(1, min(int(tail or 200), MAX_TAIL))
    raw = client.container_logs(container_id, tail=tail)
    demuxed = _demux(raw)
    text = (demuxed if demuxed is not None else raw).decode("utf-8", "replace")
    return text.splitlines()[-tail:]
