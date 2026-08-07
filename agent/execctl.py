#!/usr/bin/env python3
"""Interactive shell into a running container.

Docker's own exec-create + hijacked-start protocol (agent.py's DockerClient
owns the actual bytes-on-the-wire part; this module is policy, the same
split as containerctl.py). agent.py's WebSocket server bridges the hijacked
raw stream this module opens to a browser terminal.

A shell is a materially bigger trust boundary than start/stop/restart -- it's
arbitrary code execution as whatever user the container runs as, and on a
privileged or host-mounted container that's very close to a host shell. So
unlike the container lifecycle actions, this does NOT share
CUD_ALLOW_CONTAINER_ACTIONS or its default-on posture: it needs its own
explicit opt-in, off unless asked for.
"""

import os

import containerctl

SAFE_ID = containerctl.SAFE_ID
DEFAULT_SHELL = ["/bin/sh"]
MAX_COLS = 1000
MAX_ROWS = 1000


def exec_allowed():
    value = (os.environ.get("CUD_ALLOW_EXEC") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def capability():
    allowed = exec_allowed()
    return {
        "allowed": allowed,
        "can_exec": allowed,
        "reason": None if allowed else (
            "Exec is off by default -- it's a full shell in the container, a "
            "bigger trust boundary than start/stop/restart. Set "
            "CUD_ALLOW_EXEC=1 on the agent to enable it."
        ),
    }


def _require_allowed():
    if not exec_allowed():
        raise containerctl.ActionError(
            "Exec is off by default -- it's a full shell in the container, a "
            "bigger trust boundary than start/stop/restart. Set "
            "CUD_ALLOW_EXEC=1 on the agent to enable it."
        )


def _require_id(container_id):
    if not SAFE_ID.match(container_id or ""):
        raise containerctl.ActionError("Not a valid container id.")


def open_session(client, container_id, cmd=None):
    """Create an exec session and hijack its connection. Returns the raw
    socket, ready for duplex I/O, plus any stream bytes that arrived before
    the caller starts reading (rare, but possible for a fast-printing shell
    prompt)."""
    _require_allowed()
    _require_id(container_id)
    try:
        created = client.create_exec(container_id, cmd or DEFAULT_SHELL)
    except Exception as exc:
        raise containerctl.ActionError(str(exc))
    exec_id = (created or {}).get("Id")
    if not exec_id:
        raise containerctl.ActionError("Docker did not return an exec id.")
    try:
        sock, leftover = client.hijack_exec_start(exec_id)
    except Exception as exc:
        raise containerctl.ActionError(str(exc))
    # The interactive session can sit idle for a long time between
    # keystrokes -- the short request timeout used to create/hijack it would
    # otherwise fire as a spurious read error on an idle terminal.
    sock.settimeout(None)
    return sock, leftover, exec_id


def resize(client, exec_id, cols, rows):
    try:
        cols = max(1, min(MAX_COLS, int(cols)))
        rows = max(1, min(MAX_ROWS, int(rows)))
    except (TypeError, ValueError):
        return
    try:
        client.exec_resize(exec_id, cols, rows)
    except Exception:
        pass  # best-effort -- a failed resize should not kill the session
