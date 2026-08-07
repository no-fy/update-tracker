#!/usr/bin/env python3
"""Backing up and restoring what's actually inside a volume.

Export config (clone-spec, compose file download) already covers
configuration; Docker has no API to read or write a volume's contents
directly, so this goes through a throwaway helper container instead --
create it (not started), mount the volume, run tar against it, and either
read its stdout (backup) or feed its stdin (restore) over the container's
own attach/hijack connection.

Restore overwrites whatever is already in the volume, which is a bigger and
less reversible trust boundary than anything containerctl.py gates behind
CUD_ALLOW_CONTAINER_ACTIONS, so -- same reasoning as execctl.py's shell
access -- this needs its own explicit opt-in rather than sharing that one.
"""

import os

import containerctl

SAFE_VOLUME = containerctl.SAFE_RESOURCE_NAME
HELPER_IMAGE = "busybox:latest"
WAIT_TIMEOUT = 300


def backup_allowed():
    value = (os.environ.get("CUD_ALLOW_VOLUME_BACKUP") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def capability():
    allowed = backup_allowed()
    return {
        "allowed": allowed,
        "can_backup": allowed,
        "reason": None if allowed else (
            "Volume backup/restore is off by default -- restore overwrites a "
            "volume's existing data. Set CUD_ALLOW_VOLUME_BACKUP=1 on the "
            "agent to enable it."
        ),
    }


def _require_allowed():
    if not backup_allowed():
        raise containerctl.ActionError(
            "Volume backup/restore is off by default -- restore overwrites a "
            "volume's existing data. Set CUD_ALLOW_VOLUME_BACKUP=1 on the "
            "agent to enable it."
        )


def _require_volume(client, name):
    if not SAFE_VOLUME.match(name or ""):
        raise containerctl.ActionError("Not a valid volume name.")
    # Docker silently *creates* a named volume the first time something
    # binds to it -- fine for a real mount, a footgun here, since a typo'd
    # name would quietly back up (or worse, restore into) a blank volume
    # instead of failing loudly.
    try:
        existing = client.volumes() or {}
    except Exception as exc:
        raise containerctl.ActionError(str(exc))
    names = {v.get("Name") for v in (existing.get("Volumes") or [])}
    if name not in names:
        raise containerctl.ActionError("No such volume: %r" % name)


def _ensure_helper_image(client):
    try:
        client.image(HELPER_IMAGE)
        return
    except Exception:
        pass
    try:
        client.pull_image("busybox", "latest")
    except Exception as exc:
        raise containerctl.ActionError(
            "Could not pull the %s helper image needed for volume backup: %s"
            % (HELPER_IMAGE, exc)
        )


def _wait_for_exit(client, container_id):
    result = client.wait_container(container_id) or {}
    return result.get("StatusCode")


def _demux(sock, leftover):
    """Split Docker's stdout/stderr-multiplexed hijacked stream, keeping only
    stdout frames, until the far end closes (the helper process exiting).

    Not the plain ``GET .../logs`` endpoint: on at least one real deployment
    target (Docker Desktop's WSL2 socket proxy) that path was observed to
    mangle bytes that aren't valid UTF-8 -- silent corruption of exactly the
    binary tar data this needs. The hijacked connection used for the exec
    and restore paths round-trips arbitrary bytes correctly, so backup reads
    through the same kind of connection instead, just in the other
    direction.
    """
    buf = bytearray(leftover)
    out = bytearray()
    while True:
        while len(buf) >= 8:
            length = int.from_bytes(buf[4:8], "big")
            if len(buf) < 8 + length:
                break
            if buf[0] == 1:  # stdout frame; stderr (2) is dropped
                out.extend(buf[8:8 + length])
            del buf[:8 + length]
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(out)


def backup(client, volume_name):
    """Tar+gzip a volume's contents and return the bytes."""
    _require_allowed()
    _require_volume(client, volume_name)
    _ensure_helper_image(client)

    created = client.create_container(None, {
        "Image": HELPER_IMAGE,
        "Cmd": ["tar", "-czf", "-", "-C", "/data", "."],
        "AttachStdout": True, "AttachStderr": True, "Tty": False,
        "HostConfig": {"Binds": ["%s:/data:ro" % volume_name]},
    })
    container_id = created["Id"]
    sock = None
    try:
        sock, leftover = client.hijack_attach(container_id)
        client.container_action(container_id, "start")
        sock.settimeout(WAIT_TIMEOUT)
        data = _demux(sock, leftover)
        status = _wait_for_exit(client, container_id)
        if status != 0:
            raise containerctl.ActionError(
                "tar exited with status %s while backing up %r" % (status, volume_name))
        return data
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        try:
            client.remove_container(container_id)
        except Exception:
            pass


def restore(client, volume_name, tar_bytes):
    """Extract a tar+gzip archive into a volume, replacing what's there."""
    _require_allowed()
    _require_volume(client, volume_name)
    if not tar_bytes:
        raise containerctl.ActionError("The uploaded archive is empty.")
    _ensure_helper_image(client)

    created = client.create_container(None, {
        "Image": HELPER_IMAGE,
        "Cmd": ["tar", "-xzf", "-", "-C", "/data"],
        "AttachStdin": True, "OpenStdin": True, "StdinOnce": True,
        "AttachStdout": True, "AttachStderr": True, "Tty": False,
        "HostConfig": {"Binds": ["%s:/data:rw" % volume_name]},
    })
    container_id = created["Id"]
    sock = None
    try:
        sock, _leftover = client.hijack_attach(container_id)
        client.container_action(container_id, "start")
        sock.sendall(tar_bytes)
        try:
            sock.shutdown(1)  # SHUT_WR -- tar needs EOF on stdin to finish
        except OSError:
            pass
        # Drain and discard any output so Docker's side never blocks on a
        # full buffer; we only care about the exit status via wait().
        sock.settimeout(WAIT_TIMEOUT)
        try:
            while sock.recv(65536):
                pass
        except Exception:
            pass

        status = _wait_for_exit(client, container_id)
        if status != 0:
            raise containerctl.ActionError(
                "tar exited with status %s while restoring %r -- the volume "
                "may be partially overwritten" % (status, volume_name))
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        try:
            client.remove_container(container_id)
        except Exception:
            pass
