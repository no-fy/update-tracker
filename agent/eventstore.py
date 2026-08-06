#!/usr/bin/env python3
"""Docker events -- container start/stop/die/oom/health, kept locally.

`docker events` is normally a live stream, but Docker's own API will bound
it instead: pass both `since` and `until` and the daemon sends whatever
happened in that window and closes the connection, rather than holding it
open waiting for more. That is what lets this poll -- the same reason
logstore.py polls container logs rather than following them, after an
earlier follow-based design proved unreliable on Docker Desktop's WSL2
backend (see logstore.py's docstring for the full story). Every few
seconds this asks for events since the last poll, up to now, and stores
whatever comes back.

On by default once a writable database path exists -- CUD_EVENTS=0 turns
it off outright. CUD_EVENTS_DB points the database somewhere else (default
/var/lib/container-update-agent/events.db, alongside the log history
database, so the same volume mount covers both).
"""

import json
import os
import queue
import sqlite3
import sys
import threading
import time

DEFAULT_DB_PATH = "/var/lib/container-update-agent/events.db"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_ROWS = 50000
PRUNE_INTERVAL_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
# Docker's own event types worth a dashboard's attention -- skips the
# noisier network/volume/builder chatter by default.
EVENT_FILTERS = {"type": ["container", "image"]}


def events_enabled():
    value = (os.environ.get("CUD_EVENTS") or "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    return True


def db_path():
    return os.environ.get("CUD_EVENTS_DB") or DEFAULT_DB_PATH


def retention_days():
    try:
        return float(os.environ.get("CUD_EVENTS_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS)
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def max_rows():
    try:
        return int(os.environ.get("CUD_EVENTS_MAX_ROWS") or DEFAULT_MAX_ROWS)
    except ValueError:
        return DEFAULT_MAX_ROWS


class Store(object):
    """Same shape as logstore.Store: one writer thread, short-lived read
    connections for queries."""

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
                "CREATE TABLE IF NOT EXISTS events ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL,"
                " type TEXT,"
                " action TEXT,"
                " actor_id TEXT,"
                " name TEXT,"
                " image TEXT,"
                " exit_code TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            conn.commit()
            conn.close()
            self.ready = True
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def capability(self):
        return {
            "enabled": events_enabled() and self.ready,
            "retention_days": retention_days(),
            "reason": None if self.ready else self.error,
        }

    def append(self, record):
        self._queue.put(record)

    def query(self, since=None, until=None, limit=200):
        try:
            limit = max(1, min(int(limit or 200), 2000))
        except (TypeError, ValueError):
            limit = 200
        clauses = ["1=1"]
        params = []
        if since not in (None, ""):
            clauses.append("ts >= ?")
            params.append(float(since))
        if until not in (None, ""):
            clauses.append("ts <= ?")
            params.append(float(until))
        sql = (
            "SELECT ts, type, action, actor_id, name, image, exit_code FROM events "
            "WHERE " + " AND ".join(clauses) + " ORDER BY ts DESC LIMIT ?"
        )
        params.append(limit)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [
            {"ts": row[0], "type": row[1], "action": row[2], "actor_id": row[3],
             "name": row[4], "image": row[5], "exit_code": row[6]}
            for row in rows
        ]

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
                            "INSERT INTO events (ts, type, action, actor_id, name, "
                            "image, exit_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [(r["ts"], r["type"], r["action"], r["actor_id"], r["name"],
                              r["image"], r["exit_code"]) for r in batch],
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
            with self._lock:
                conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                conn.execute(
                    "DELETE FROM events WHERE id NOT IN "
                    "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                    (max_rows(),),
                )
                conn.commit()
        except Exception:
            pass


STORE = None
_last_poll_ts = {"value": None}
_manager_started = False


def capability():
    if not events_enabled():
        return {"enabled": False, "retention_days": retention_days(),
                "reason": "This agent was started with CUD_EVENTS=0."}
    if STORE is None:
        return {"enabled": False, "retention_days": retention_days(),
                "reason": "Event history has not started."}
    return STORE.capability()


def _record_from_raw(raw_event):
    actor = raw_event.get("Actor") or {}
    attrs = actor.get("Attributes") or {}
    ts = raw_event.get("time")
    if ts is None:
        ts = (raw_event.get("timeNano") or 0) / 1e9
    event_type = raw_event.get("Type") or raw_event.get("status") or ""
    actor_id = actor.get("ID") or raw_event.get("id") or ""
    if event_type == "container":
        # Container IDs are the long hex string; the rest of this app
        # (and the dashboard's own container.id) always uses the short
        # 12-char form. Other actor types (an image ref, a network name,
        # ...) are not hex ids at all and must not be truncated the same way.
        actor_id = actor_id[:12]
    return {
        "ts": float(ts or time.time()),
        "type": event_type,
        "action": raw_event.get("Action") or raw_event.get("status") or "",
        "actor_id": actor_id,
        "name": attrs.get("name") or "",
        "image": attrs.get("image") or "",
        "exit_code": attrs.get("exitCode"),
    }


def _poll_once(client):
    now = time.time()
    since = _last_poll_ts["value"]
    if since is None:
        since = now - POLL_INTERVAL_SECONDS - 2
    try:
        raw = client.events(since, now, filters=EVENT_FILTERS)
    except Exception:
        return

    newest = since
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        record = _record_from_raw(event)
        if STORE is not None:
            STORE.append(record)
        if record["ts"] > newest:
            newest = record["ts"]
    _last_poll_ts["value"] = max(newest, now)


def _poll_loop(client_factory):
    while True:
        try:
            if events_enabled() and STORE is not None and STORE.ready:
                _poll_once(client_factory())
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


def init(docker_endpoint=None):
    """Start event capture. Safe to call even when it will not do
    anything -- it just leaves STORE unset and capability() explains why.
    """
    global STORE, _manager_started
    if not events_enabled() or _manager_started:
        return
    _manager_started = True

    import agent as agent_module

    STORE = Store(db_path())
    if not STORE.ready:
        sys.stderr.write(
            "event history disabled: %s (set CUD_EVENTS_DB to a writable path, "
            "or CUD_EVENTS=0 to silence this)\n" % STORE.error
        )
        return

    threading.Thread(target=STORE.run_writer, name="eventstore-writer", daemon=True).start()
    threading.Thread(
        target=_poll_loop,
        args=(lambda: agent_module.DockerClient(docker_endpoint),),
        name="eventstore-poll", daemon=True,
    ).start()
