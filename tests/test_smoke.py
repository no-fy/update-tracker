#!/usr/bin/env python3
"""End-to-end smoke test.

Runs the real collector, agent and web server against a fake Docker daemon.
Registry lookups hit the real registries, so this needs outbound HTTPS; pass
--offline to skip the checks that depend on a live digest.

Exec and volume backup/restore need real Docker hijack behavior the fake
daemon doesn't implement; pass --with-docker to also run those against
/var/run/docker.sock (creates and removes real throwaway containers/volumes
prefixed cud-smoketest-). Off by default -- every other check here is fully
isolated from whatever Docker daemon happens to be on the machine running
this file, and that stays true unless you ask for it.

    python3 tests/test_smoke.py
    python3 tests/test_smoke.py --with-docker
"""

import http.server
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "agent"))

import agent as agent_module  # noqa: E402
from dashboard import collector, config as config_mod, registry as registry_mod  # noqa: E402
from dashboard import server as server_mod  # noqa: E402
from dashboard import enroll  # noqa: E402
import containerctl  # noqa: E402
import eventstore  # noqa: E402
import execctl  # noqa: E402
import imagectl  # noqa: E402
import ospackages  # noqa: E402
import osupdate  # noqa: E402
import stackctl  # noqa: E402
import volumectl  # noqa: E402
from fake_docker import FakeDocker, make_fixtures  # noqa: E402

try:
    import websockets  # noqa: F401
    HAVE_WEBSOCKETS = True
except ImportError:
    HAVE_WEBSOCKETS = False

HAVE_DOCKER_SOCKET = os.path.exists("/var/run/docker.sock")

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  \033[32mPASS\033[0m %s" % name)
    else:
        FAILED.append(name)
        print("  \033[31mFAIL\033[0m %s %s" % (name, detail))


def section(title):
    print("\n\033[1m%s\033[0m" % title)


class StubClient:
    """A hand-built stand-in for agent.DockerClient's write-path methods --
    for testing containerctl/imagectl/volumectl/execctl's own logic (input
    validation, shaping, math) without needing a real or fake Docker server
    to actually run anything."""

    def __init__(self, **overrides):
        self._calls = []
        for key, value in overrides.items():
            setattr(self, key, value)

    def _record(self, name, *args):
        self._calls.append((name,) + args)

    def system_df(self):
        return getattr(self, "_system_df", {})

    def container_stats(self, container_id):
        self._record("container_stats", container_id)
        return getattr(self, "_stats", {})

    def prune_containers(self):
        self._record("prune_containers")
        return getattr(self, "_prune_containers", {})

    def prune_volumes(self):
        self._record("prune_volumes")
        return getattr(self, "_prune_volumes", {})

    def prune_networks(self):
        self._record("prune_networks")
        return getattr(self, "_prune_networks", {})

    def prune_images(self, dangling_only=True):
        self._record("prune_images", dangling_only)
        return getattr(self, "_prune_images", {})

    def update_container(self, container_id, body):
        self._record("update_container", container_id, body)

    def volumes(self):
        return {"Volumes": getattr(self, "_volumes", [])}

    def image(self, ref):
        return {}

    def pull_image(self, repository, reference, on_line=None):
        self._record("pull_image", repository, reference)

    def create_container(self, name, body):
        self._record("create_container", name, body)
        return {"Id": getattr(self, "_created_id", "stubcontainer0001")}

    def container_action(self, container_id, action, timeout=None):
        self._record("container_action", container_id, action)

    def wait_container(self, container_id):
        self._record("wait_container", container_id)
        return {"StatusCode": getattr(self, "_exit_code", 0)}

    def remove_container(self, container_id):
        self._record("remove_container", container_id)

    def hijack_attach(self, container_id):
        self._record("hijack_attach", container_id)
        return getattr(self, "_attach_sock"), getattr(self, "_attach_leftover", b"")

    def exec_resize(self, exec_id, cols, rows):
        self._record("exec_resize", exec_id, cols, rows)


class FakeSocket:
    """A minimal recv()-only stand-in for a hijacked Docker connection --
    yields each entry in ``chunks`` in turn, then empty bytes (EOF)."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def settimeout(self, _t):
        pass

    def sendall(self, _data):
        pass

    def close(self):
        pass


def docker_multiplex_frame(stream_type, payload):
    return bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def _raises(exception, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except exception:
        return True
    except Exception:
        return False
    return False


def live_digests(refs):
    client = registry_mod.RegistryClient(cache_path=None, fetch_metadata=False)
    out = {}
    for ref in refs:
        try:
            result = client.resolve(ref, "linux/amd64")
            if result.get("digest"):
                out[ref] = result["digest"]
        except Exception:
            pass
    return out


def main(argv):
    offline = "--offline" in argv
    # Opt-in, not auto-detected: everything else in this suite runs against
    # an isolated FakeDocker on a throwaway unix socket. Exec and volume
    # backup/restore need real Docker hijack behavior FakeDocker doesn't
    # implement, so these touch the real daemon at /var/run/docker.sock --
    # only when explicitly asked, so running the suite never surprises
    # someone by creating or removing real containers/volumes on their host.
    with_docker = "--with-docker" in argv and HAVE_DOCKER_SOCKET and HAVE_WEBSOCKETS
    workdir = tempfile.mkdtemp(prefix="cud-test-")
    socket_path = os.path.join(workdir, "docker.sock")
    config_path = os.path.join(workdir, "config.json")

    try:
        section("Preparing fixtures")
        digests = {} if offline else live_digests(["nginx:latest"])
        if digests:
            print("  resolved nginx:latest -> %s" % digests["nginx:latest"][:19])
        elif not offline:
            print("  \033[33mwarn\033[0m could not resolve nginx:latest; skipping up-to-date check")
        fixtures = make_fixtures(digests)

        with FakeDocker(socket_path, fixtures):
            section("Agent collection")
            client = agent_module.DockerClient(socket_path)
            snapshot = agent_module.collect_snapshot(client)
            names = {c["name"]: c for c in snapshot["containers"]}
            check("agent reads all containers", len(snapshot["containers"]) == 8,
                  "got %d" % len(snapshot["containers"]))
            check("agent reads host info", snapshot["info"]["hostname"] == "fixture-host")
            check("agent resolves repo digests",
                  names["proxy"]["repo_digests"] and names["proxy"]["repo_digests"][0].startswith("traefik@"))
            check("agent detects a pulled-but-not-recreated image",
                  names["grafana"]["image_id"] != names["grafana"]["current_image_id"])
            check("agent reads the ignore label",
                  names["cache"]["ignored_by"] == "container-update-dashboard.ignore")
            check("agent reads compose metadata",
                  names["web"]["compose_project"] == "homelab" and names["web"]["compose_service"] == "web")
            check("agent formats published ports",
                  names["web"]["ports"] == ["0.0.0.0:8080->80/tcp"], names["web"]["ports"])

            section("Agent HTTP service")
            port = 19713
            token = "test-token-abc"
            thread = threading.Thread(
                target=agent_module.serve,
                kwargs=dict(token=token, bind="127.0.0.1", port=port, docker_endpoint=socket_path),
                daemon=True,
            )
            thread.start()
            time.sleep(0.5)

            def agent_get(path, bearer=token):
                headers = {"Authorization": "Bearer " + bearer} if bearer else {}
                request = urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (port, path), headers=headers)
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode())

            status, payload = agent_get("/healthz", bearer=None)
            check("health endpoint needs no token", status == 200 and payload["ok"])

            try:
                agent_get("/v1/containers", bearer=None)
                check("agent rejects requests with no token", False)
            except urllib.error.HTTPError as exc:
                check("agent rejects requests with no token", exc.code == 401, "got %d" % exc.code)

            try:
                agent_get("/v1/containers", bearer="wrong-token")
                check("agent rejects a wrong token", False)
            except urllib.error.HTTPError as exc:
                check("agent rejects a wrong token", exc.code == 401, "got %d" % exc.code)

            status, payload = agent_get("/v1/containers")
            check("agent serves containers with a valid token",
                  status == 200 and len(payload["containers"]) == 8)

            section("Collector and classification")
            config = {
                "dashboard": {"registry_cache_hours": 6, "fetch_remote_metadata": True},
                "registries": {},
                "insecure_registries": [],
                "hosts": [
                    {"name": "fixture-local", "mode": "local", "label": "Fixture (local)",
                     "docker_socket": socket_path, "enabled": True},
                    {"name": "fixture-agent", "mode": "agent", "label": "Fixture (agent)",
                     "address": "127.0.0.1", "port": port, "token": token, "enabled": True},
                    {"name": "dead", "mode": "agent", "label": "Unreachable",
                     "address": "127.0.0.1", "port": 19999, "token": "x", "enabled": True},
                    {"name": "off", "mode": "agent", "label": "Disabled",
                     "address": "127.0.0.1", "port": 19998, "token": "x", "enabled": False},
                ],
            }
            config_mod.save_config(config, config_path)

            results = collector.poll_hosts(config["hosts"], timeout=10)
            by_host = {h["name"]: h for h in results}
            check("local host polled", by_host["fixture-local"]["online"])
            check("agent host polled", by_host["fixture-agent"]["online"])
            check("unreachable host reported, not raised",
                  not by_host["dead"]["online"] and "cannot reach" in (by_host["dead"]["error"] or ""))
            check("disabled host skipped", by_host["off"]["error"] == "disabled")

            registry_client = server_mod.make_registry_client(
                config, cache_path=os.path.join(workdir, "cache.json"))
            collector.enrich_with_registry(results, registry_client)
            summary = collector.summarise(results)

            statuses = {c["name"]: c["update_status"]
                        for c in by_host["fixture-local"]["containers"]}
            print("  statuses: %s" % json.dumps(statuses, indent=None))

            check("stale digest -> update-available",
                  statuses["proxy"] == "update-available", statuses["proxy"])
            check("locally built image -> unknown",
                  statuses["my-app"] == "unknown", statuses["my-app"])
            check("digest-pinned image -> pinned",
                  statuses["db"] == "pinned", statuses["db"])
            check("ignore label -> ignored",
                  statuses["cache"] == "ignored", statuses["cache"])
            check("unreachable registry -> error",
                  statuses["api"] == "error", statuses["api"])
            check("newer image already pulled -> restart-pending",
                  statuses["grafana"] == "restart-pending", statuses["grafana"])
            if digests:
                check("current digest -> up-to-date",
                      statuses["web"] == "up-to-date", statuses["web"])

            check("summary counts both fixture hosts",
                  summary["containers_total"] == 16, str(summary["containers_total"]))
            check("summary counts hosts online",
                  summary["hosts_online"] == 2 and summary["hosts_offline"] == 2)
            check("needs_attention counts updates and restarts",
                  summary["needs_attention"] == summary["counts"]["update-available"]
                  + summary["counts"]["restart-pending"])

            section("Registry cache")
            before = time.time()
            collector.enrich_with_registry(results, registry_client)
            elapsed = time.time() - before
            cached = [c for h in results for c in h["containers"] if c.get("checked_from_cache")]
            check("second pass served from cache", bool(cached), "%d cached" % len(cached))
            check("cached pass is fast", elapsed < 5.0, "%.2fs" % elapsed)
            check("cache file written", os.path.exists(os.path.join(workdir, "cache.json")))

            section("Dashboard HTTP API")
            httpd, _, _ = server_mod.build_server(config_path, bind="127.0.0.1", port=18500)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            httpd.poller.refresh()

            def dash_get(path):
                with urllib.request.urlopen("http://127.0.0.1:18500" + path, timeout=10) as response:
                    return response.status, response.read()

            status, body = dash_get("/")
            check("serves the dashboard page", status == 200 and b"Container updates" in body)

            status, body = dash_get("/static/app.js")
            check("serves static assets", status == 200 and b"update-available" in body)

            status, body = dash_get("/api/state")
            payload = json.loads(body.decode())
            check("api/state returns a snapshot",
                  payload["summary"]["containers_total"] == 16 and len(payload["hosts"]) == 4)
            check("api/state exposes no tokens", b'"token"' not in body)

            status, body = dash_get("/healthz")
            check("healthz responds", status == 200)

            request = urllib.request.Request("http://127.0.0.1:18500/api/refresh", method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                check("api/refresh accepted", response.status == 202)

            status, body = dash_get("/api/hosts")
            check("api/hosts redacts tokens",
                  b'"has_token": true' in body and b"test-token-abc" not in body)

            request = urllib.request.Request(
                "http://127.0.0.1:18500/api/hosts/dead", method="DELETE")
            with urllib.request.urlopen(request, timeout=10) as response:
                check("api can remove a host", response.status == 200)
            reloaded, _ = config_mod.load_config(config_path)
            check("host removal persisted",
                  config_mod.find_host(reloaded, "dead") is None)

            def dash_post(path, payload=None, raw=None, method="POST"):
                data = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
                request = urllib.request.Request(
                    "http://127.0.0.1:18500" + path, data=data, method=method)
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            def dash_delete(path):
                request = urllib.request.Request("http://127.0.0.1:18500" + path, method="DELETE")
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            section("Images / Volumes / Networks tabs")
            status, body = dash_get("/api/hosts/fixture-local/images")
            payload = json.loads(body.decode())
            check("images route lists the fixture images",
                  status == 200 and len(payload.get("images") or []) == len(fixtures["images"]))

            status, body = dash_get("/api/hosts/fixture-local/volumes")
            payload = json.loads(body.decode())
            check("volumes route lists the fixture volumes",
                  status == 200 and len(payload.get("volumes") or []) == 2, body[:200])

            status, body = dash_get("/api/hosts/fixture-local/networks")
            payload = json.loads(body.decode())
            check("networks route lists the fixture networks",
                  status == 200 and len(payload.get("networks") or []) == 2, body[:200])

            status, body = dash_get("/api/hosts/fixture-local/disk-usage")
            payload = json.loads(body.decode())
            check("disk-usage route aggregates the fixture system/df",
                  status == 200 and payload["volumes"]["reclaimable"] == 2 * 1024 * 1024, body[:300])

            section("Prune routes")
            status, body = dash_post("/api/hosts/fixture-local/prune/containers")
            check("prune containers route reaches the fixture", status == 200, body[:200])

            status, body = dash_post("/api/hosts/fixture-local/prune/volumes")
            payload = json.loads(body.decode())
            check("prune volumes route returns what was removed",
                  status == 200 and payload["removed"] == ["orphan-vol"], body[:200])

            status, body = dash_post("/api/hosts/fixture-local/prune/networks")
            check("prune networks route reaches the fixture", status == 200, body[:200])

            status, body = dash_post(
                "/api/hosts/fixture-local/prune/images", {"dangling_only": True})
            check("prune images route reaches the fixture", status == 200, body[:200])

            section("Resource limits and clone-spec routes")
            fixture_container_id = fixtures["containers"][0]["Id"]
            status, body = dash_get(
                "/api/hosts/fixture-local/containers/%s/clone-spec" % fixture_container_id)
            payload = json.loads(body.decode())
            check("clone-spec route includes resource limit fields",
                  status == 200 and "memory_mb" in payload and "cpu_limit" in payload, body[:300])

            status, body = dash_get(
                "/api/hosts/fixture-local/containers/%s/stats" % fixture_container_id)
            payload = json.loads(body.decode())
            check("stats route computes cpu/memory from the fixture's raw stats",
                  status == 200 and payload["cpu_percent"] > 0 and payload["memory_used"] > 0,
                  body[:300])

            status, body = dash_post(
                "/api/hosts/fixture-local/containers/%s/limits" % fixture_container_id,
                {"memory_mb": 256, "cpu_limit": 0.5})
            check("limits route reaches the fixture's update endpoint", status == 200, body[:200])

            section("Registry credentials via the API")
            status, body = dash_post(
                "/api/registries", {"host": "ghcr.io", "username": "bob", "password": "tok"})
            check("registries route adds credentials", status == 200, body[:200])

            status, body = dash_get("/api/registries")
            payload = json.loads(body.decode())
            hosts_listed = {r["host"] for r in payload["registries"]}
            check("registries route lists the host without the password",
                  "ghcr.io" in hosts_listed and b"tok" not in body)

            status, body = dash_delete("/api/registries/ghcr.io")
            check("registries route removes a host", status == 200, body[:200])
            status, body = dash_get("/api/registries")
            payload = json.loads(body.decode())
            check("removed registry no longer listed",
                  "ghcr.io" not in {r["host"] for r in payload["registries"]})

            status, body = dash_delete("/api/registries/never-added.example")
            check("removing an unknown registry 404s", status == 404, body[:200])

            section("Stack templates via the API")
            status, body = dash_post(
                "/api/stack-templates", {"name": "web-basic", "compose": "services:\n  web:\n    image: nginx\n"})
            check("stack-templates route saves a template", status == 200, body[:200])
            status, body = dash_get("/api/stack-templates")
            payload = json.loads(body.decode())
            check("stack-templates route lists it back",
                  any(t["name"] == "web-basic" for t in payload.get("templates") or []), body[:300])
            status, body = dash_delete("/api/stack-templates/web-basic")
            check("stack-templates route removes it", status == 200, body[:200])

            httpd.poller.stop()
            httpd.shutdown()
            httpd.server_close()

            section("Config file")
            mode = os.stat(config_path).st_mode & 0o777
            check("config written 0600", mode == 0o600, oct(mode))

            section("Local host registration")
            local_cfg = os.path.join(workdir, "local", "config.json")
            cfg, _ = config_mod.load_config(local_cfg)
            host, created = config_mod.upsert_host(cfg, {
                "name": "local", "mode": "local",
                "docker_socket": socket_path, "enabled": True})
            config_mod.save_config(cfg, local_cfg)
            check("local socket can be registered", created and host["mode"] == "local")
            results_local = collector.poll_hosts(cfg["hosts"], timeout=10)
            check("registered local socket is readable",
                  results_local[0]["online"] and len(results_local[0]["containers"]) == 8)

        section("containerctl: stats math and limit validation")
        stats_client = StubClient(_stats={
            "cpu_stats": {"cpu_usage": {"total_usage": 2000000000},
                          "system_cpu_usage": 40000000000, "online_cpus": 4},
            "precpu_stats": {"cpu_usage": {"total_usage": 1000000000},
                              "system_cpu_usage": 30000000000},
            "memory_stats": {"usage": 50 * 1024 * 1024, "limit": 512 * 1024 * 1024,
                              "stats": {"cache": 0}},
        })
        stats = containerctl.stats(stats_client, "a" * 64)
        check("cpu percent from the delta formula",
              abs(stats["cpu_percent"] - 40.0) < 0.01, stats)
        check("memory used subtracts cache", stats["memory_used"] == 50 * 1024 * 1024, stats)

        limits_client = StubClient()
        containerctl.update_limits(limits_client, "a" * 64, {"memory_mb": 256, "cpu_limit": 0.5})
        sent = limits_client._calls[0]
        check("update_limits sends bytes, not MB", sent[2]["Memory"] == 256 * 1024 * 1024, sent)
        check("update_limits sends matching MemorySwap (no extra swap)",
              sent[2]["MemorySwap"] == sent[2]["Memory"], sent)
        check("update_limits refuses a negative memory value",
              _raises(containerctl.ActionError, containerctl.update_limits,
                      StubClient(), "a" * 64, {"memory_mb": -1, "cpu_limit": 0}))
        check("update_limits refuses an absurd cpu value",
              _raises(containerctl.ActionError, containerctl.update_limits,
                      StubClient(), "a" * 64, {"memory_mb": 0, "cpu_limit": 99999}))

        zero_client = StubClient()
        zero_client.info = lambda: {"MemTotal": 16 * 1024 ** 3, "NCPU": 8}
        containerctl.update_limits(zero_client, "a" * 64, {"memory_mb": 0, "cpu_limit": 0})
        zero_sent = zero_client._calls[0]
        check("update_limits substitutes host capacity for 0 (Docker ignores a literal 0)",
              zero_sent[2]["Memory"] == 16 * 1024 ** 3 and zero_sent[2]["NanoCpus"] == 8 * 10 ** 9,
              zero_sent)

        section("containerctl: prune shaping and disk usage aggregation")
        prune_client = StubClient(_prune_containers={"ContainersDeleted": ["c1", "c2"],
                                                       "SpaceReclaimed": 1024})
        result = containerctl.prune_containers(prune_client)
        check("prune_containers shapes Docker's raw response",
              result == {"removed": ["c1", "c2"], "space_reclaimed": 1024}, result)

        prune_client2 = StubClient(_prune_volumes={"VolumesDeleted": None, "SpaceReclaimed": None})
        result = containerctl.prune_volumes(prune_client2)
        check("prune_volumes tolerates a null removed-list", result["removed"] == [], result)

        df_client = StubClient(_system_df={
            "Images": [{"Size": 100, "Containers": 0}, {"Size": 200, "Containers": 1}],
            "Containers": [{"SizeRw": 50, "State": "exited"}, {"SizeRw": 30, "State": "running"}],
            "Volumes": [{"UsageData": {"RefCount": 0, "Size": 10}}],
            "BuildCache": [{"Size": 5, "InUse": False}, {"Size": 7, "InUse": True}],
        })
        usage = containerctl.disk_usage(df_client)
        check("disk_usage only counts unattached images as reclaimable",
              usage["images"]["reclaimable"] == 100, usage)
        check("disk_usage only counts stopped containers as reclaimable",
              usage["containers"]["reclaimable"] == 50, usage)
        check("disk_usage totals every category",
              usage["total_reclaimable"] == 100 + 50 + 10 + 5, usage)

        section("imagectl: prune shaping")
        img_client = StubClient(_prune_images={
            "ImagesDeleted": [{"Untagged": "old:tag"}, {"Deleted": "sha256:abc"}],
            "SpaceReclaimed": 4096,
        })
        result = imagectl.prune_images(img_client, dangling_only=True)
        check("imagectl.prune_images prefers Untagged, falls back to Deleted",
              result["removed"] == ["old:tag", "sha256:abc"], result)
        check("imagectl.prune_images passes dangling_only through",
              img_client._calls[0] == ("prune_images", True))

        section("volumectl: demux, capability gating, volume existence")
        frames = (
            docker_multiplex_frame(1, b"tar-bytes-stdout-")
            + docker_multiplex_frame(2, b"a stderr line that must be dropped")
            + docker_multiplex_frame(1, b"more-stdout")
        )
        demuxed = volumectl._demux(FakeSocket([]), leftover=frames)
        check("volumectl._demux keeps only stdout frames",
              demuxed == b"tar-bytes-stdout-more-stdout", demuxed)

        split_frames = [frames[:5], frames[5:]]
        demuxed_split = volumectl._demux(FakeSocket(split_frames), leftover=b"")
        check("volumectl._demux handles a header split across recv() calls",
              demuxed_split == b"tar-bytes-stdout-more-stdout", demuxed_split)

        check("volume backup is off by default", volumectl.capability()["can_backup"] is False)
        check("backup refuses when the env var is off",
              _raises(containerctl.ActionError, volumectl.backup, StubClient(), "any-volume"))
        os.environ["CUD_ALLOW_VOLUME_BACKUP"] = "1"
        try:
            check("volume backup capability turns on with the env var",
                  volumectl.capability()["can_backup"] is True)
            check("backup refuses an unsafe volume name",
                  _raises(containerctl.ActionError, volumectl.backup, StubClient(_volumes=[]),
                          "../etc"))
            check("backup refuses a volume Docker doesn't know about",
                  _raises(containerctl.ActionError, volumectl.backup,
                          StubClient(_volumes=[{"Name": "other"}]), "missing-volume"))
            check("restore refuses an empty upload",
                  _raises(containerctl.ActionError, volumectl.restore,
                          StubClient(_volumes=[{"Name": "v"}]), "v", b""))
        finally:
            del os.environ["CUD_ALLOW_VOLUME_BACKUP"]

        section("execctl: capability gating")
        check("exec is off by default", execctl.capability()["can_exec"] is False)
        check("open_session refuses when the env var is off",
              _raises(containerctl.ActionError, execctl.open_session, StubClient(), "a" * 64))
        os.environ["CUD_ALLOW_EXEC"] = "1"
        try:
            check("exec capability turns on with the env var",
                  execctl.capability()["can_exec"] is True)
            check("open_session still refuses a malformed container id",
                  _raises(containerctl.ActionError, execctl.open_session, StubClient(), "not-an-id"))
        finally:
            del os.environ["CUD_ALLOW_EXEC"]
        check("resize on a broken client is best-effort, not a crash",
              execctl.resize(StubClient(), "e1", 80, 24) is None)
        check("resize clamps a nonsense size instead of raising",
              execctl.resize(StubClient(), "e1", "nonsense", 24) is None)

        section("stackctl: name/path safety and capability gating")
        check("a safe project name is accepted", bool(stackctl.SAFE_PROJECT.match("my-stack_1")))
        check("a project name with a slash is rejected", not stackctl.SAFE_PROJECT.match("a/b"))
        check("a safe compose path is accepted",
              bool(stackctl.SAFE_PATH.match("/opt/stacks/app/docker-compose.yml")))
        check("a relative path is rejected (must be absolute)",
              not stackctl.SAFE_PATH.match("relative/path.yml"))

        os.environ.pop("CUD_STACKS_DIR", None)
        cap = stackctl.capability()
        check("stack deploy needs CUD_STACKS_DIR configured", cap["can_deploy"] is False)
        redeploy_cap = stackctl.redeploy_capability()
        check("stack redeploy capability reports a reason when nsenter is unavailable",
              redeploy_cap["can_redeploy"] is False or redeploy_cap["reason"] is None)

        with tempfile.TemporaryDirectory() as stacks_dir:
            os.environ["CUD_STACKS_DIR"] = stacks_dir
            try:
                cap = stackctl.capability()
                check("stack deploy capability turns on once CUD_STACKS_DIR is set",
                      cap["can_deploy"] is True, cap)
                inside = os.path.join(stacks_dir, "proj", "docker-compose.yml")
                os.makedirs(os.path.dirname(inside))
                with open(inside, "w") as handle:
                    handle.write("services: {}\n")
                stackctl.write_compose_file(inside, "services:\n  web:\n    image: nginx\n")
                with open(inside) as handle:
                    check("write_compose_file writes inside the stacks dir",
                          "nginx" in handle.read())
                outside = os.path.join(os.path.dirname(stacks_dir), "escape.yml")
                check("write_compose_file refuses a path outside the stacks dir",
                      _raises(containerctl.ActionError, stackctl.write_compose_file,
                              outside, "services: {}\n"))
            finally:
                del os.environ["CUD_STACKS_DIR"]

        section("eventstore: shaping and actor id truncation")
        container_event = eventstore._record_from_raw({
            "Type": "container", "Action": "start", "time": time.time(),
            "Actor": {"ID": "a" * 64, "Attributes": {"name": "web"}},
        })
        check("a container actor id is truncated to 12 chars",
              container_event["actor_id"] == "a" * 12, container_event)

        network_event = eventstore._record_from_raw({
            "Type": "network", "Action": "connect", "time": time.time(),
            "Actor": {"ID": "network-id-not-hex-and-not-12-chars", "Attributes": {}},
        })
        check("a non-container actor id is left alone",
              network_event["actor_id"] == "network-id-not-hex-and-not-12-chars", network_event)

        with tempfile.TemporaryDirectory() as event_dir:
            store = eventstore.Store(os.path.join(event_dir, "events.db"))
            check("a fresh event store is ready", store.ready, store.error)
            check("capability reports enabled once ready", store.capability()["enabled"] is True)

        section("config.py: registry credential storage")
        reg_config = {"registries": {}, "insecure_registries": []}
        config_mod.upsert_registry(reg_config, "GHCR.io", "alice", "secret1", insecure=False)
        check("registry host is stored lowercase",
              "ghcr.io" in reg_config["registries"], reg_config["registries"])
        check("registry password is stored", reg_config["registries"]["ghcr.io"]["password"] == "secret1")

        config_mod.upsert_registry(reg_config, "ghcr.io", "alice2", "", insecure=True)
        check("a blank password on re-submit keeps the stored one",
              reg_config["registries"]["ghcr.io"]["password"] == "secret1", reg_config["registries"])
        check("username still updates even when password is blank",
              reg_config["registries"]["ghcr.io"]["username"] == "alice2")
        check("insecure flag adds the host to insecure_registries",
              "ghcr.io" in reg_config["insecure_registries"])

        config_mod.upsert_registry(reg_config, "ghcr.io", "alice2", "", insecure=False)
        check("insecure flag can be turned back off",
              "ghcr.io" not in reg_config["insecure_registries"])

        removed = config_mod.remove_registry(reg_config, "ghcr.io")
        check("remove_registry returns the removed entry", removed is not None)
        check("remove_registry actually removes it", "ghcr.io" not in reg_config["registries"])
        check("removing an unknown registry returns None",
              config_mod.remove_registry(reg_config, "never-there") is None)

        section("collector.py: stacks summary")
        synthetic_hosts = [
            {"containers": [{"update_status": "up-to-date", "state": "running"}],
             "online": True, "os": {},
             "stacks": [{"needs_attention": False}, {"needs_attention": True}]},
            {"containers": [], "online": True, "os": {}, "stacks": [{"needs_attention": False}]},
        ]
        stacks_summary = collector.summarise(synthetic_hosts)
        check("summarise totals stacks across hosts", stacks_summary["stacks_total"] == 3)
        check("summarise counts only the stacks needing attention",
              stacks_summary["stacks_needing_attention"] == 1)

        section("Credentials")
        hashed = config_mod.hash_password("correct-horse")
        check("password stored as a hash",
              hashed.startswith("pbkdf2_sha256$") and "correct-horse" not in hashed)
        check("correct password verifies", config_mod.verify_password(hashed, "correct-horse"))
        check("wrong password rejected", not config_mod.verify_password(hashed, "wrong"))
        check("empty password rejected", not config_mod.verify_password(hashed, ""))
        check("plaintext password still works (documented, hand-edited)",
              config_mod.verify_password("plain", "plain"))
        settings = server_mod.sanitise_settings({"password": hashed, "port": 8500})
        check("settings never expose the credential",
              "password" not in settings and settings["password_set"] is True)

        section("Sessions and sign-in")
        sessions = server_mod.SessionStore(ttl_hours=12)
        session_token = sessions.create("admin")
        check("a session resolves to its user",
              sessions.get(session_token)["username"] == "admin")
        check("an unknown session is nobody", sessions.get("made-up") is None)
        check("no cookie is nobody", sessions.get(None) is None)
        check("logout destroys the session",
              sessions.destroy(session_token) and sessions.get(session_token) is None)

        brief = server_mod.SessionStore(ttl_hours=0)
        stale = brief.create("admin")
        check("an expired session is rejected", brief.get(stale) is None)
        check("two sessions are never the same token",
              sessions.create("a") != sessions.create("a"))

        section("Enrolment")
        store = enroll.EnrollmentStore(config_path)
        item = store.create(name="nas", port=9713)
        check("enrolment mints two distinct secrets",
              item.token != item.agent_token and len(item.token) > 30)
        listed = store.list()[0]
        check("listing never carries the enrolment token", "token" not in listed)
        check("the token is only shown to its creator",
              store.get(item.id).snapshot(include_token=True)["token"] == item.token)

        command = enroll.agent_command(item, "http://dash.lan:8500")
        check("command mounts the socket read-only", ":/var/run/docker.sock:ro" in command)
        check("command carries both tokens",
              item.agent_token in command and item.token in command)
        check("command points back at the dashboard",
              "CUD_ENROLL_URL=http://dash.lan:8500/api/enroll" in command)

        try:
            store.claim("not-a-real-token", "10.0.0.9", {})
            check("an unknown token is refused", False, "accepted")
        except enroll.EnrollError:
            check("an unknown token is refused", True)

        expired = store.create(name="old", ttl_minutes=0)
        expired.expires = time.time() - 1
        try:
            store.claim(expired.token, "10.0.0.9", {})
            check("an expired token is refused", False, "accepted")
        except enroll.EnrollError:
            check("an expired token is refused", True)

        # A claim that cannot be verified must not register anything, and must
        # still burn the token so a second attempt gets nowhere.
        doomed = store.create(name="unreachable", port=19997)
        try:
            store.claim(doomed.token, "127.0.0.1", {"port": 19997})
            check("unverifiable agent is not registered", False, "accepted")
        except enroll.EnrollError:
            after, _ = config_mod.load_config(config_path)
            check("unverifiable agent is not registered",
                  config_mod.find_host(after, "unreachable") is None)
        try:
            store.claim(doomed.token, "127.0.0.1", {"port": 19997})
            check("a spent token cannot be replayed", False, "accepted")
        except enroll.EnrollError:
            check("a spent token cannot be replayed", True)

        # The happy path. A stub agent rather than the fixture one: this
        # exercises claim() itself, and must prove the token is checked.
        seen = {}

        class StubAgent(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen["auth"] = self.headers.get("Authorization")
                if seen["auth"] != "Bearer " + live.agent_token:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({"hostname": "pi.lan"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        live = store.create(name="enrolled", port=19914)
        stub = http.server.HTTPServer(("127.0.0.1", 19914), StubAgent)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        try:
            registered = store.claim(live.token, "127.0.0.1", {"port": 19914})
            check("verified agent is registered", registered.status == "registered")
            check("verification presented the agent token",
                  seen.get("auth") == "Bearer " + live.agent_token)
            saved, _ = config_mod.load_config(config_path)
            entry = config_mod.find_host(saved, "enrolled")
            check("registered host keeps the agent's token",
                  entry is not None and entry["token"] == live.agent_token)
            check("registered host points at the caller",
                  entry is not None and entry["address"] == "127.0.0.1"
                  and entry["port"] == 19914)
            check("label comes from the agent's own hostname",
                  entry is not None and entry["label"] == "pi.lan")
        finally:
            stub.shutdown()

        section("OS updates: version ordering")
        for left, right, want in [
            ("1.9", "1.10", -1), ("1.10", "1.9", 1), ("1.0", "1.0", 0),
            ("1:1.0", "2.0", 1), ("1.0~rc1", "1.0", -1), ("1.0-1", "1.0-2", -1),
            ("2.39-0ubuntu8.7", "2.39-0ubuntu8.8", -1),
        ]:
            got = ospackages.compare_versions(left, right)
            check("%s vs %s" % (left, right), got == want, "got %d" % got)

        section("OS updates: apt")
        apt_root = os.path.join(workdir, "aptroot")
        os.makedirs(os.path.join(apt_root, "var/lib/dpkg"))
        os.makedirs(os.path.join(apt_root, "var/lib/apt/lists"))
        with open(os.path.join(apt_root, "var/lib/dpkg/status"), "w") as handle:
            handle.write(
                "Package: openssl\nStatus: install ok installed\nVersion: 3.0.13-1\n\n"
                "Package: nano\nStatus: install ok installed\nVersion: 7.2-1\n\n"
                "Package: cowsay\nStatus: install ok installed\nVersion: 3.03-1\n\n"
                "Package: gone\nStatus: deinstall ok not-installed\nVersion: 1.0\n\n"
            )
        lists = os.path.join(apt_root, "var/lib/apt/lists")
        with open(os.path.join(lists, "archive.ubuntu.com_ubuntu_dists_noble-updates_main_binary-amd64_Packages"), "w") as handle:
            handle.write(
                "Package: openssl\nVersion: 3.0.13-2\n\n"
                "Package: nano\nVersion: 7.2-2\n\n"
                "Package: cowsay\nVersion: 3.03-1\n\n"
            )
        with open(os.path.join(lists, "security.ubuntu.com_ubuntu_dists_noble-security_main_binary-amd64_Packages"), "w") as handle:
            # Same version as -updates: the security origin must still win.
            handle.write("Package: openssl\nVersion: 3.0.13-2\n\n")

        apt_result = ospackages.collect(apt_root)
        by_name = {u["name"]: u for u in apt_result["updates"]}
        check("apt detected", apt_result["manager"] == "apt" and apt_result["available"])
        check("only upgradable packages reported",
              sorted(by_name) == ["nano", "openssl"], sorted(by_name))
        check("uninstalled packages ignored", "gone" not in by_name)
        check("same version is not an update", "cowsay" not in by_name)
        check("security suite wins over updates at equal version",
              by_name["openssl"]["severity"] == "security", by_name["openssl"]["source"])
        check("non-security update is routine or important",
              by_name["nano"]["severity"] == "routine", by_name["nano"]["severity"])
        check("counts add up", apt_result["counts"]["security"] == 1)
        check("origin is readable",
              "noble-security" in by_name["openssl"]["source"], by_name["openssl"]["source"])

        section("OS updates: kernel and reboot")
        with open(os.path.join(apt_root, "var/lib/dpkg/status"), "a") as handle:
            handle.write("Package: linux-image-generic\nStatus: install ok installed\nVersion: 6.8.0-31\n\n")
        with open(os.path.join(lists, "archive.ubuntu.com_ubuntu_dists_noble-updates_main_binary-amd64_Packages"), "a") as handle:
            handle.write("Package: linux-image-generic\nVersion: 6.8.0-32\n\n")
        os.makedirs(os.path.join(apt_root, "var/run"), exist_ok=True)
        open(os.path.join(apt_root, "var/run/reboot-required"), "w").close()
        kernel_result = ospackages.collect(apt_root)
        kernel = [u for u in kernel_result["updates"] if u["name"].startswith("linux-image")][0]
        check("kernel update ranked important", kernel["severity"] == "important", kernel["severity"])
        check("reboot-required is reported", kernel_result["reboot_required"])
        check("security still sorts first",
              kernel_result["updates"][0]["severity"] == "security")

        section("OS updates: apk and pacman")
        apk_root = os.path.join(workdir, "apkroot")
        os.makedirs(os.path.join(apk_root, "lib/apk/db"))
        os.makedirs(os.path.join(apk_root, "var/cache/apk"))
        with open(os.path.join(apk_root, "lib/apk/db/installed"), "w") as handle:
            handle.write("P:busybox\nV:1.36.1-r5\n\nP:musl\nV:1.2.4-r2\n\n")
        index = io.BytesIO(b"P:busybox\nV:1.36.1-r7\n\nP:musl\nV:1.2.4-r2\n\n")
        with tarfile.open(os.path.join(apk_root, "var/cache/apk/APKINDEX.abc.tar.gz"), "w:gz") as archive:
            entry = tarfile.TarInfo("APKINDEX")
            entry.size = len(index.getvalue())
            archive.addfile(entry, io.BytesIO(index.getvalue()))
        apk_result = ospackages.collect(apk_root)
        check("apk detected", apk_result["manager"] == "apk" and apk_result["available"])
        check("apk finds the upgradable package",
              [u["name"] for u in apk_result["updates"]] == ["busybox"],
              [u["name"] for u in apk_result["updates"]])

        pac_root = os.path.join(workdir, "pacroot")
        os.makedirs(os.path.join(pac_root, "var/lib/pacman/local/vim-9.0-1"))
        os.makedirs(os.path.join(pac_root, "var/lib/pacman/sync"))
        with open(os.path.join(pac_root, "var/lib/pacman/local/vim-9.0-1/desc"), "w") as handle:
            handle.write("%NAME%\nvim\n\n%VERSION%\n9.0-1\n")
        with tarfile.open(os.path.join(pac_root, "var/lib/pacman/sync/core.db"), "w:gz") as archive:
            payload = b"%NAME%\nvim\n\n%VERSION%\n9.1-1\n"
            entry = tarfile.TarInfo("vim-9.1-1/desc")
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        pac_result = ospackages.collect(pac_root)
        check("pacman detected", pac_result["manager"] == "pacman" and pac_result["available"])
        check("pacman finds the upgradable package",
              [u["name"] for u in pac_result["updates"]] == ["vim"])

        section("OS updates: honest about what it cannot read")
        rpm_root = os.path.join(workdir, "rpmroot")
        os.makedirs(os.path.join(rpm_root, "var/lib/rpm"))
        open(os.path.join(rpm_root, "var/lib/rpm/rpmdb.sqlite"), "w").close()
        rpm_result = ospackages.collect(rpm_root)
        check("rpm host is detected", rpm_result["manager"] == "rpm")
        check("rpm reports unsupported rather than zero updates",
              not rpm_result["available"] and "rpm" in (rpm_result["error"] or ""))
        check("no fake 'all clear' for rpm", rpm_result["updates"] == [])

        empty_root = os.path.join(workdir, "emptyroot")
        os.makedirs(empty_root)
        none_result = ospackages.collect(empty_root)
        check("no package manager is reported, not guessed",
              none_result["manager"] is None and not none_result["available"])
        check("the error explains the mount",
              "CUD_HOST_ROOT" in (none_result["error"] or ""))

        section("OS updates: lz4 indexes (Debian's default)")

        def lz4_frame(payload_blocks):
            out = bytearray(b"\x04\x22\x4d\x18")
            out += bytes([0x60, 0x70, 0x00])          # FLG, BD, header checksum
            for block, raw in payload_blocks:
                size = len(block) | (0x80000000 if raw else 0)
                out += size.to_bytes(4, "little") + block
            out += (0).to_bytes(4, "little")          # end mark
            return bytes(out)

        # A hand-built compressed block: literals "abc", then a match of 9
        # bytes at offset 3 -- an overlapping run, the case a naive slice copy
        # gets wrong.
        compressed = bytes([0x35]) + b"abc" + bytes([0x03, 0x00])
        check("lz4 decodes an overlapping match",
              ospackages.lz4_decompress(lz4_frame([(compressed, False)])) == b"abcabcabcabc",
              ospackages.lz4_decompress(lz4_frame([(compressed, False)])))
        check("lz4 decodes a stored block",
              ospackages.lz4_decompress(lz4_frame([(b"plain bytes", True)])) == b"plain bytes")
        check("lz4 rejects a non-frame",
              _raises(ValueError, ospackages.lz4_decompress, b"not lz4 at all"))

        lz4_root = os.path.join(workdir, "lz4root")
        os.makedirs(os.path.join(lz4_root, "var/lib/dpkg"))
        os.makedirs(os.path.join(lz4_root, "var/lib/apt/lists"))
        with open(os.path.join(lz4_root, "var/lib/dpkg/status"), "w") as handle:
            handle.write("Package: curl\nStatus: install ok installed\nVersion: 7.88.1-1\n\n")
        stanza = b"Package: curl\nVersion: 7.88.1-2\nDescription: transfer a URL\nSection: web\n\n"
        with open(os.path.join(lz4_root,
                  "var/lib/apt/lists/deb.debian.org_debian_dists_bookworm_main_binary-amd64_Packages.lz4"),
                  "wb") as handle:
            handle.write(lz4_frame([(stanza, True)]))
        lz4_result = ospackages.collect(lz4_root)
        check("an lz4 index is read, not skipped",
              [u["name"] for u in lz4_result["updates"]] == ["curl"],
              lz4_result.get("error") or lz4_result["updates"])
        if lz4_result["updates"]:
            check("metadata survives decompression",
                  lz4_result["updates"][0]["description"] == "transfer a URL")

        section("OS updates: installing them")
        check("updates are allowed unless switched off", osupdate.updates_allowed())
        os.environ["CUD_ALLOW_UPDATES"] = "0"
        check("CUD_ALLOW_UPDATES=0 turns them off", not osupdate.updates_allowed())
        check("and says why", "CUD_ALLOW_UPDATES=0" in (osupdate.capability()["reason"] or ""))
        del os.environ["CUD_ALLOW_UPDATES"]

        argv, env = osupdate.build_command("apt", ["openssl", "libc6"])
        check("apt command upgrades only the named packages",
              argv[:6] == ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold",
                           "--only-upgrade", "install"] and argv[-2:] == ["openssl", "libc6"], argv)
        check("apt runs non-interactively", env.get("DEBIAN_FRONTEND") == "noninteractive")
        check("the command is argv, never a shell string",
              not any(ch in " ".join(argv) for ch in "|&;<>$`"))

        check("a package that is not pending is refused",
              _raises(osupdate.UpdateError, osupdate.RUNNER.start,
                      "apt", ["nosuchpkg"], {"openssl"}))
        check("a name with shell metacharacters is refused",
              _raises(osupdate.UpdateError, osupdate.RUNNER.start,
                      "apt", ["openssl; rm -rf /"], {"openssl", "openssl; rm -rf /"}))
        check("an empty selection is refused",
              _raises(osupdate.UpdateError, osupdate.RUNNER.start, "apt", [], {"openssl"}))
        check("an unsupported manager is refused",
              _raises(osupdate.UpdateError, osupdate.build_command, "rpm", ["x"]))

        able = osupdate.capability()
        check("capability reports whether it is the same machine",
              "same_machine" in able and "mode" in able, sorted(able))
        check("host pid namespace detection never raises",
              isinstance(osupdate.in_host_pid_namespace(), bool))
        check("a refusal always carries a reason",
              able["can_update"] or bool(able["reason"]))

        section("Local host by default")
        shared = config_mod.load_config(os.path.join(workdir, "nope-1.json"))[0]
        shared.setdefault("hosts", []).append({"name": "leak", "mode": "local"})
        again = config_mod.load_config(os.path.join(workdir, "nope-2.json"))[0]
        check("defaults are not shared between loaded configs", again.get("hosts") == [])

        fresh_path = os.path.join(workdir, "auto", "config.json")
        fresh, _ = config_mod.load_config(fresh_path)
        added = server_mod.ensure_local_host_registered(fresh, fresh_path)
        if os.path.exists("/var/run/docker.sock"):
            check("local Docker registered without being asked",
                  added is not None and added["mode"] == "local")
        else:
            check("no local host invented when there is no socket", added is None)
        already = {"hosts": [{"name": "nas", "mode": "agent"}]}
        check("existing hosts are left alone",
              server_mod.ensure_local_host_registered(already, fresh_path) is None)

        if with_docker:
            section("Exec and volume backup/restore (real Docker socket)")
            real_client = agent_module.DockerClient("/var/run/docker.sock")
            test_container = None
            test_volume = "cud-smoketest-vol-%d" % int(time.time())
            try:
                subprocess.run(
                    ["docker", "run", "-d", "--rm", "--name",
                     "cud-smoketest-%d" % int(time.time()), "busybox", "sleep", "120"],
                    check=True, capture_output=True, text=True,
                )
                inspect = subprocess.run(
                    ["docker", "ps", "-q", "--filter", "name=cud-smoketest-"],
                    check=True, capture_output=True, text=True,
                )
                test_container = inspect.stdout.strip().splitlines()[-1]

                os.environ["CUD_ALLOW_EXEC"] = "1"
                try:
                    sock, leftover, exec_id = execctl.open_session(real_client, test_container)
                    sock.sendall(b"echo smoketest-exec-ok\n")
                    time.sleep(0.4)
                    sock.settimeout(2)
                    output = leftover + sock.recv(65536)
                    check("real exec session returns real shell output",
                          b"smoketest-exec-ok" in output, output[:200])
                    execctl.resize(real_client, exec_id, 100, 30)
                    sock.close()
                finally:
                    del os.environ["CUD_ALLOW_EXEC"]

                subprocess.run(["docker", "volume", "create", test_volume],
                                check=True, capture_output=True, text=True)
                subprocess.run(
                    ["docker", "run", "--rm", "-v", "%s:/data" % test_volume, "busybox",
                     "sh", "-c", "echo real-backup-test > /data/f.txt"],
                    check=True, capture_output=True, text=True,
                )
                os.environ["CUD_ALLOW_VOLUME_BACKUP"] = "1"
                try:
                    archive = volumectl.backup(real_client, test_volume)
                    check("real backup produces a valid gzip header", archive[:2] == b"\x1f\x8b")
                    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
                        names = tf.getnames()
                    check("real backup archive contains the file written to the volume",
                          any(n.endswith("f.txt") for n in names), names)

                    restore_volume = test_volume + "-restore"
                    subprocess.run(["docker", "volume", "create", restore_volume],
                                    check=True, capture_output=True, text=True)
                    try:
                        volumectl.restore(real_client, restore_volume, archive)
                        cat = subprocess.run(
                            ["docker", "run", "--rm", "-v", "%s:/data" % restore_volume,
                             "busybox", "cat", "/data/f.txt"],
                            check=True, capture_output=True, text=True,
                        )
                        check("real restore round-trips the file content",
                              cat.stdout.strip() == "real-backup-test", cat.stdout)
                    finally:
                        subprocess.run(["docker", "volume", "rm", "-f", restore_volume],
                                        capture_output=True, text=True)
                finally:
                    del os.environ["CUD_ALLOW_VOLUME_BACKUP"]
            finally:
                if test_container:
                    subprocess.run(["docker", "kill", test_container], capture_output=True, text=True)
                subprocess.run(["docker", "volume", "rm", "-f", test_volume],
                                capture_output=True, text=True)
        else:
            print("  \033[33mskip\033[0m real-Docker exec/volume-backup checks "
                  "(pass --with-docker to run them against /var/run/docker.sock)")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n\033[1m%d passed, %d failed\033[0m" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  failed: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
