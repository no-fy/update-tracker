#!/usr/bin/env python3
"""Polls every configured host and decides what needs an update.

Hosts are polled in parallel; registry lookups for the resulting images are
then done once per unique image reference, so twenty containers running the
same image cost one HTTP round trip.
"""

import concurrent.futures
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))

import agent as agent_module  # noqa: E402  (single-file agent, reused verbatim)

from . import registry as registry_mod  # noqa: E402

# Ordered by how much attention each state deserves.
STATUS_ORDER = [
    "error",
    "update-available",
    "restart-pending",
    "unknown",
    "pinned",
    "ignored",
    "up-to-date",
]
NEEDS_ATTENTION = {"update-available", "restart-pending"}


class HostResult(dict):
    pass


def _http_get_json(url, token=None, timeout=20, verify_tls=True):
    headers = {"Accept": "application/json", "User-Agent": "container-update-dashboard/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    context = None
    if url.startswith("https://") and not verify_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def poll_host(host, timeout=20, include_stopped=True):
    """Fetch one host's snapshot. Never raises -- failures become host errors."""
    name = host.get("name") or host.get("address") or "unnamed"
    started = time.time()
    result = HostResult(
        name=name,
        label=host.get("label") or name,
        mode=host.get("mode", "agent"),
        address=host.get("address"),
        port=host.get("port"),
        enabled=host.get("enabled", True),
        online=False,
        error=None,
        info={},
        containers=[],
        agent_version=None,
        poll_seconds=0.0,
    )

    if not host.get("enabled", True):
        result["error"] = "disabled"
        result["poll_seconds"] = 0.0
        return result

    try:
        if host.get("mode") == "local":
            client = agent_module.DockerClient(host.get("docker_socket"), timeout=timeout)
            snapshot = agent_module.collect_snapshot(client, include_stopped=include_stopped)
        else:
            scheme = "https" if host.get("tls") else "http"
            address = host.get("address")
            if not address:
                raise ValueError("host has no address")
            url = "%s://%s:%s/v1/containers" % (scheme, address, host.get("port", 9713))
            snapshot = _http_get_json(
                url,
                token=host.get("token"),
                timeout=timeout,
                verify_tls=host.get("verify_tls", True),
            )
        result["online"] = True
        result["info"] = snapshot.get("info") or {}
        result["agent_version"] = snapshot.get("agent_version")
        containers = snapshot.get("containers") or []
        if not include_stopped:
            containers = [c for c in containers if c.get("state") == "running"]
        result["containers"] = containers
    except agent_module.DockerError as exc:
        result["error"] = str(exc)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            result["error"] = "agent rejected the token (re-run setup-host.py for this host)"
        else:
            result["error"] = "agent returned HTTP %s" % exc.code
    except urllib.error.URLError as exc:
        result["error"] = "cannot reach agent: %s" % exc.reason
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)

    result["poll_seconds"] = round(time.time() - started, 2)
    return result


def poll_hosts(hosts, timeout=20, max_workers=8, include_stopped=True):
    if not hosts:
        return []
    workers = max(1, min(max_workers, len(hosts)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(poll_host, host, timeout=timeout, include_stopped=include_stopped)
            for host in hosts
        ]
        return [future.result() for future in futures]


def _classify(container, ref, local_digest, lookup):
    """Turn the raw facts about one container into a status + explanation."""
    if container.get("ignored_by"):
        return "ignored", "Excluded by label %s" % container["ignored_by"]

    if container.get("image_id") and container.get("current_image_id") \
            and container["image_id"] != container["current_image_id"]:
        return (
            "restart-pending",
            "A newer image is already pulled for this tag; the container is still "
            "running the old one. Recreate it to pick the new image up.",
        )

    if ref is None:
        return "unknown", "Image reference could not be parsed"

    if ref.is_pinned:
        return "pinned", "Pinned to a digest, so the tag can never move"

    if not local_digest:
        return (
            "unknown",
            "No registry digest recorded locally -- the image was most likely built "
            "on this host rather than pulled.",
        )

    if lookup is None:
        return "unknown", "Not checked yet"

    if lookup.get("error"):
        kind = lookup.get("error_kind") or "error"
        if kind == "notfound":
            return "unknown", lookup["error"]
        return "error", lookup["error"]

    remote = lookup.get("digest")
    if not remote:
        return "unknown", "Registry returned no digest"

    if remote != local_digest:
        return "update-available", "Registry tag points at a different image"

    return "up-to-date", "Running the image the tag currently points at"


def enrich_with_registry(host_results, registry_client, max_workers=6):
    """Attach registry status to every container across every host."""
    wanted = {}  # (image_ref, platform) -> list of container dicts

    for host in host_results:
        platform = registry_mod.normalise_arch((host.get("info") or {}).get("architecture"))
        for container in host["containers"]:
            container["_platform"] = platform
            image_ref = container.get("image_ref") or ""
            try:
                ref = registry_mod.parse_image_ref(image_ref)
            except registry_mod.RegistryError:
                ref = None
            container["_ref"] = ref
            container["registry"] = ref.registry if ref else None
            container["repository"] = ref.repository if ref else None
            container["tag"] = ref.tag if ref else None
            container["image_display"] = ref.display if ref else (image_ref or "unknown")
            container["local_digest"] = (
                registry_mod.local_digest_for(ref, container.get("repo_digests")) if ref else None
            )
            needs_lookup = (
                ref is not None
                and not ref.is_pinned
                and container["local_digest"]
                and not container.get("ignored_by")
                and container.get("image_id") == container.get("current_image_id")
            )
            if needs_lookup:
                wanted.setdefault((image_ref, platform), []).append(container)

    lookups = {}
    if wanted:
        workers = max(1, min(max_workers, len(wanted)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(registry_client.resolve, image_ref, platform): (image_ref, platform)
                for (image_ref, platform) in wanted
            }
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                try:
                    lookups[key] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    lookups[key] = {
                        "digest": None,
                        "error": "%s: %s" % (type(exc).__name__, exc),
                        "error_kind": "error",
                    }

    for host in host_results:
        for container in host["containers"]:
            ref = container.pop("_ref", None)
            platform = container.pop("_platform", None)
            lookup = lookups.get((container.get("image_ref") or "", platform))
            status, detail = _classify(container, ref, container.get("local_digest"), lookup)
            container["update_status"] = status
            container["detail"] = detail
            container["remote_digest"] = (lookup or {}).get("digest")
            container["remote_created"] = (lookup or {}).get("remote_created")
            container["checked_from_cache"] = bool((lookup or {}).get("cached"))
            container["host"] = host["name"]
    return host_results


def summarise(host_results):
    counts = {status: 0 for status in STATUS_ORDER}
    total = 0
    running = 0
    for host in host_results:
        host_counts = {status: 0 for status in STATUS_ORDER}
        for container in host["containers"]:
            status = container.get("update_status", "unknown")
            counts[status] = counts.get(status, 0) + 1
            host_counts[status] = host_counts.get(status, 0) + 1
            total += 1
            if container.get("state") == "running":
                running += 1
        host["counts"] = host_counts
        host["needs_attention"] = sum(host_counts.get(s, 0) for s in NEEDS_ATTENTION)

    return {
        "containers_total": total,
        "containers_running": running,
        "hosts_total": len(host_results),
        "hosts_online": sum(1 for h in host_results if h.get("online")),
        "hosts_offline": sum(1 for h in host_results if not h.get("online")),
        "needs_attention": sum(counts.get(s, 0) for s in NEEDS_ATTENTION),
        "counts": counts,
    }


class Poller:
    """Owns the current snapshot and refreshes it on a timer or on demand."""

    def __init__(self, config_loader, registry_client_factory):
        self.config_loader = config_loader
        self.registry_client_factory = registry_client_factory
        self.lock = threading.Lock()
        self.snapshot = {
            "generated_at": None,
            "duration_seconds": None,
            "hosts": [],
            "summary": summarise([]),
            "status": "never-run",
        }
        self.refreshing = threading.Event()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def get(self):
        with self.lock:
            snapshot = dict(self.snapshot)
        snapshot["refreshing"] = self.refreshing.is_set()
        return snapshot

    def refresh(self):
        if self.refreshing.is_set():
            return self.get()
        self.refreshing.set()
        started = time.time()
        try:
            config, _ = self.config_loader()
            settings = config.get("dashboard", {})
            hosts = config.get("hosts", [])
            results = poll_hosts(
                hosts,
                timeout=settings.get("poll_timeout_seconds", 20),
                max_workers=settings.get("max_parallel_hosts", 8),
                include_stopped=settings.get("include_stopped", True),
            )
            client = self.registry_client_factory(config)
            enrich_with_registry(
                results,
                client,
                max_workers=settings.get("max_parallel_registry_lookups", 6),
            )
            snapshot = {
                "generated_at": time.time(),
                "duration_seconds": round(time.time() - started, 2),
                "hosts": results,
                "summary": summarise(results),
                "status": "ok",
            }
            with self.lock:
                self.snapshot = snapshot
        finally:
            self.refreshing.clear()
        return self.get()

    def refresh_async(self):
        if self.refreshing.is_set():
            return False
        threading.Thread(target=self._safe_refresh, daemon=True).start()
        return True

    def _safe_refresh(self):
        try:
            self.refresh()
        except Exception as exc:  # pragma: no cover - keep the loop alive
            sys.stderr.write("refresh failed: %s: %s\n" % (type(exc).__name__, exc))

    def start_background(self, interval_minutes):
        if self._thread:
            return

        def loop():
            self._safe_refresh()
            while not self._stop.is_set():
                self._wake.wait(timeout=max(60.0, interval_minutes * 60))
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._safe_refresh()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
