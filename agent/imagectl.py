#!/usr/bin/env python3
"""Pulling and building images.

Same capability gate as containerctl.py (CUD_ALLOW_CONTAINER_ACTIONS) --
anyone who can already start/stop/recreate a container through this agent
can already run arbitrary code on this host via the Docker socket, so a
separate opt-out for "may this agent also pull or build an image" would not
change the actual trust boundary, only add a second flag nobody would set
independently of the first.
"""

import io
import re
import tarfile
import threading
import time
import uuid

import containerctl

MAX_LOG_LINES = 400
# Loose on purpose: Docker's own daemon is the real validator for image
# names/tags, this just keeps obviously-wrong input from reaching it.
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$")


class Job(object):
    def __init__(self, job_id, kind, target):
        self.id = job_id
        self.kind = kind  # "pull" or "build"
        self.target = target
        self.status = "running"
        self.lines = []
        self.started = time.time()
        self.finished = None
        self._lock = threading.Lock()
        self._last_status = {}

    def append(self, line):
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def on_line(self, event):
        if event.get("stream"):
            self.append(event["stream"].rstrip("\n"))
            return
        status = event.get("status")
        if not status:
            return
        layer_id = event.get("id")
        key = layer_id or ""
        # Layer progress repeats dozens of times as bytes download; only log
        # it when the status actually changes, not every percentage tick.
        if self._last_status.get(key) == status:
            return
        self._last_status[key] = status
        self.append(("%s: %s" % (layer_id, status)) if layer_id else status)

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "target": self.target,
                "status": self.status,
                "lines": list(self.lines),
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


class Runner(object):
    """One pull/build at a time -- same reasoning as osupdate.Runner: the
    daemon serialises image operations on its own anyway."""

    def __init__(self):
        self._jobs = {}
        self._order = []
        self._active = None
        self._lock = threading.Lock()

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def _start(self, kind, target, run):
        if not containerctl.actions_allowed():
            raise containerctl.ActionError(
                "This agent was started with CUD_ALLOW_CONTAINER_ACTIONS=0, so it "
                "cannot pull or build images.")
        with self._lock:
            if self._active is not None:
                raise containerctl.ActionError(
                    "A pull or build is already running on this host.")
            job = Job(uuid.uuid4().hex[:12], kind, target)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._active = job.id
        thread = threading.Thread(
            target=self._run, args=(job, run), name="%s-%s" % (kind, job.id), daemon=True)
        thread.start()
        return job

    def _run(self, job, run):
        try:
            run(job)
            job.status = "ok"
        except Exception as exc:
            job.append("%s: %s" % (type(exc).__name__, exc))
            job.status = "failed"
        finally:
            job.finished = time.time()
            with self._lock:
                self._active = None

    def start_pull(self, client, repository, reference):
        if not SAFE_REF.match(repository) or not SAFE_REF.match(reference):
            raise containerctl.ActionError("Not a valid image reference.")

        def run(job):
            job.append("Pulling %s:%s" % (repository, reference))
            client.pull_image(repository, reference, on_line=job.on_line)
            job.append("Done.")

        return self._start("pull", "%s:%s" % (repository, reference), run)

    def start_build(self, client, dockerfile_text, tag=None):
        if tag and not SAFE_REF.match(tag):
            raise containerctl.ActionError("Not a valid image tag.")
        if not (dockerfile_text or "").strip():
            raise containerctl.ActionError("A Dockerfile is required.")
        tar_bytes = _build_context_tar(dockerfile_text)

        def run(job):
            job.append("Building" + (" " + tag if tag else ""))
            client.build_image(tar_bytes, tag=tag, on_line=job.on_line)
            job.append("Done.")

        return self._start("build", tag or "(untagged)", run)


RUNNER = Runner()


def prune_images(client, dangling_only=True):
    if not containerctl.actions_allowed():
        raise containerctl.ActionError(
            "This agent was started with CUD_ALLOW_CONTAINER_ACTIONS=0, so it "
            "cannot remove images.")
    try:
        raw = client.prune_images(dangling_only=dangling_only)
    except Exception as exc:
        raise containerctl.ActionError(str(exc))
    removed = [
        (entry.get("Untagged") or entry.get("Deleted") or "")
        for entry in (raw.get("ImagesDeleted") or [])
    ]
    return {
        "removed": [r for r in removed if r],
        "space_reclaimed": raw.get("SpaceReclaimed") or 0,
    }


def _build_context_tar(dockerfile_text):
    """A build context of exactly one file -- the Dockerfile itself, for the
    paste/upload flow this agent offers. No other files are ever sent."""
    data = dockerfile_text.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="Dockerfile")
        info.size = len(data)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
