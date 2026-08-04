#!/usr/bin/env python3
"""Historical container logs -- optional, persisted locally in SQLite.

`docker logs` only shows what the log driver still has on disk, which
rotates away. This polls every running container's recent output every few
seconds and stores lines it has not seen yet into a small SQLite database
(Python's own sqlite3, so this stays dependency-free), so "what did this
print at 2am last Tuesday" survives log rotation and container restarts.

This deliberately polls rather than follows. An earlier version used
Docker's `follow=1` log stream, one long-lived connection per running
container; on Docker Desktop's WSL2 backend that combination proved
unreliable -- `since`/`tail` combined with `follow` could hang indefinitely,
and even a bare follow connection could wedge after enough of them piled up.
Polling the same non-streaming endpoint the rest of this project already
relies on is slower to notice new output (up to one poll interval of lag)
but it is the same request/response call every other feature here makes,
with no long-lived connection to leak or get stuck.

On by default once a writable database path exists -- CUD_LOG_HISTORY=0
turns it off outright. CUD_LOG_DB points the database somewhere else
(default /var/lib/container-update-agent/logs.db, which needs a volume
mounted there to survive an agent restart). Nothing here executes anything;
it only reads the same logs `docker logs` does.
"""

import datetime
import os
import queue
import sqlite3
import struct
import sys
import threading
import time

DEFAULT_DB_PATH = "/var/lib/container-update-agent/logs.db"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_ROWS_PER_CONTAINER = 200000
PRUNE_INTERVAL_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
CAPTURE_TAIL = 1000


def history_enabled():
    value = (os.environ.get("CUD_LOG_HISTORY") or "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    return True


def db_path():
    return os.environ.get("CUD_LOG_DB") or DEFAULT_DB_PATH


def retention_days():
    try:
        return float(os.environ.get("CUD_LOG_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS)
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def max_rows_per_container():
    try:
        return int(os.environ.get("CUD_LOG_MAX_ROWS_PER_CONTAINER")
                    or DEFAULT_MAX_ROWS_PER_CONTAINER)
    except ValueError:
        return DEFAULT_MAX_ROWS_PER_CONTAINER


def _parse_docker_timestamp(raw):
    """Docker's `timestamps=1` prefixes each line with an RFC3339Nano
    timestamp and a space. Returns (unix_ts_or_None, remaining_text)."""
    prefix, sep, rest = raw.partition(" ")
    if not sep:
        return None, raw
    try:
        body = prefix[:-1] if prefix.endswith("Z") else prefix
        if "." in body:
            date_part, frac = body.split(".", 1)
            frac = (frac + "000000")[:6]
            dt = datetime.datetime.strptime(date_part, "%Y-%m-%dT%H:%M:%S")
            dt = dt.replace(microsecond=int(frac))
        else:
            dt = datetime.datetime.strptime(body, "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp(), rest
    except (ValueError, IndexError):
        return None, raw


def _demux(raw):
    """Same framing rule as containerctl.fetch_logs, duplicated here so this
    module has no import-order dependency on it."""
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


class Store(object):
    """A single writer thread owns writes; queries open their own
    short-lived read connections -- SQLite is fine with that as long as
    there is exactly one writer.
    """

    def __init__(self, path):
        self.path = path
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self.ready = False
        self.error = None
        self._init_schema()

    def _init_schema(self):
        try:
            directory = os.path.dirname(self.path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS logs ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " container_id TEXT NOT NULL,"
                " container_name TEXT,"
                " ts REAL NOT NULL,"
                " line TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_container_ts ON logs(container_id, ts)"
            )
            conn.commit()
            conn.close()
            self.ready = True
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def capability(self):
        return {
            "enabled": history_enabled() and self.ready,
            "retention_days": retention_days(),
            "reason": None if self.ready else self.error,
        }

    def append(self, container_id, container_name, ts, line):
        self._queue.put((container_id, container_name, ts, line))

    def query(self, container_id, since=None, until=None, limit=500):
        try:
            limit = max(1, min(int(limit or 500), 5000))
        except (TypeError, ValueError):
            limit = 500
        clauses = ["container_id = ?"]
        params = [container_id]
        if since not in (None, ""):
            clauses.append("ts >= ?")
            params.append(float(since))
        if until not in (None, ""):
            clauses.append("ts <= ?")
            params.append(float(until))
        sql = (
            "SELECT ts, line FROM logs WHERE " + " AND ".join(clauses) +
            " ORDER BY ts DESC LIMIT ?"
        )
        params.append(limit)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        rows.reverse()
        return [{"ts": row[0], "line": row[1]} for row in rows]

    def run_writer(self):
        conn = sqlite3.connect(self.path, timeout=10)
        last_prune = 0.0
        while True:
            try:
                batch = [self._queue.get(timeout=5)]
            except queue.Empty:
                batch = []
            while len(batch) < 500:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if batch:
                try:
                    with self._lock:
                        conn.executemany(
                            "INSERT INTO logs (container_id, container_name, ts, line) "
                            "VALUES (?, ?, ?, ?)",
                            batch,
                        )
                        conn.commit()
                except Exception:
                    pass
            now = time.time()
            if now - last_prune > PRUNE_INTERVAL_SECONDS:
                self._prune(conn)
                last_prune = now

    def _prune(self, conn):
        try:
            cutoff = time.time() - retention_days() * 86400
            cap = max_rows_per_container()
            with self._lock:
                conn.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
                for (container_id,) in conn.execute(
                    "SELECT DISTINCT container_id FROM logs"
                ).fetchall():
                    conn.execute(
                        "DELETE FROM logs WHERE container_id = ? AND id NOT IN "
                        "(SELECT id FROM logs WHERE container_id = ? "
                        " ORDER BY id DESC LIMIT ?)",
                        (container_id, container_id, cap),
                    )
                conn.commit()
        except Exception:
            pass


STORE = None
_last_ts = {}
_manager_started = False


def capability():
    if not history_enabled():
        return {"enabled": False, "retention_days": retention_days(),
                "reason": "This agent was started with CUD_LOG_HISTORY=0."}
    if STORE is None:
        return {"enabled": False, "retention_days": retention_days(),
                "reason": "Log history has not started."}
    return STORE.capability()


def _poll_once(client):
    try:
        containers = client.containers(all_containers=False) or []
    except Exception:
        return

    running_ids = set()
    for raw in containers:
        container_id = (raw.get("Id") or "")[:12]
        if not container_id:
            continue
        running_ids.add(container_id)
        names = [n.lstrip("/") for n in (raw.get("Names") or []) if n]
        name = names[0] if names else container_id

        try:
            body = client.container_logs(container_id, tail=CAPTURE_TAIL, timestamps=True)
        except Exception:
            continue
        demuxed = _demux(body)
        text = (demuxed if demuxed is not None else body).decode("utf-8", "replace")

        last_ts = _last_ts.get(container_id)
        newest = last_ts
        for line in text.splitlines():
            if not line:
                continue
            ts, message = _parse_docker_timestamp(line)
            if ts is not None and last_ts is not None and ts <= last_ts:
                continue
            if STORE is not None:
                STORE.append(container_id, name, ts if ts is not None else time.time(), message)
            if ts is not None and (newest is None or ts > newest):
                newest = ts
        if newest is not None:
            _last_ts[container_id] = newest

    # A container that stopped is no longer "seen" -- if it (or a same-named
    # replacement) starts again, its log content should not look pre-deduped.
    for container_id in list(_last_ts.keys()):
        if container_id not in running_ids:
            _last_ts.pop(container_id, None)


def _poll_loop(client_factory):
    while True:
        try:
            if history_enabled() and STORE is not None and STORE.ready:
                _poll_once(client_factory())
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


def init(docker_endpoint=None):
    """Start log history capture. Safe to call even when it will not do
    anything -- it just leaves STORE unset and capability() explains why.
    """
    global STORE, _manager_started
    if not history_enabled() or _manager_started:
        return
    _manager_started = True

    import agent as agent_module

    STORE = Store(db_path())
    if not STORE.ready:
        sys.stderr.write(
            "log history disabled: %s (set CUD_LOG_DB to a writable path, "
            "or CUD_LOG_HISTORY=0 to silence this)\n" % STORE.error
        )
        return

    threading.Thread(target=STORE.run_writer, name="logstore-writer", daemon=True).start()
    threading.Thread(
        target=_poll_loop,
        args=(lambda: agent_module.DockerClient(docker_endpoint),),
        name="logstore-poll", daemon=True,
    ).start()
