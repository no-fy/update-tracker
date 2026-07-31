#!/usr/bin/env python3
"""End-to-end smoke test.

Runs the real collector, agent and web server against a fake Docker daemon.
Registry lookups hit the real registries, so this needs outbound HTTPS; pass
--offline to skip the checks that depend on a live digest.

    python3 tests/test_smoke.py
"""

import http.server
import json
import os
import shutil
import stat
import sys
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
from fake_docker import FakeDocker, make_fixtures  # noqa: E402

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

            httpd.poller.stop()
            httpd.shutdown()
            httpd.server_close()

            section("Config file")
            mode = os.stat(config_path).st_mode & 0o777
            check("config written 0600", mode == 0o600, oct(mode))

            section("Setup script argument parsing")
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "setup_host", os.path.join(ROOT, "setup-host.py"))
            setup = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(setup)
            check("parses user@host", setup.parse_target("root@nas.lan") == ("root@nas.lan", "nas.lan", None))
            check("parses user@host:port",
                  setup.parse_target("deploy@10.0.0.5:2222") == ("deploy@10.0.0.5", "10.0.0.5", 2222))
            check("parses a bare host", setup.parse_target("nas") == ("nas", "nas", None))
            check("builds an install script",
                  "systemctl restart" in setup.build_install_script(
                      "print('x')", {"token": "t"}, {"socket_group": "docker"}, "cudagent", "/usr/bin/python3"))

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

        live = store.create(name="enrolled", port=19714)
        stub = http.server.HTTPServer(("127.0.0.1", 19714), StubAgent)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        try:
            registered = store.claim(live.token, "127.0.0.1", {"port": 19714})
            check("verified agent is registered", registered.status == "registered")
            check("verification presented the agent token",
                  seen.get("auth") == "Bearer " + live.agent_token)
            saved, _ = config_mod.load_config(config_path)
            entry = config_mod.find_host(saved, "enrolled")
            check("registered host keeps the agent's token",
                  entry is not None and entry["token"] == live.agent_token)
            check("registered host points at the caller",
                  entry is not None and entry["address"] == "127.0.0.1"
                  and entry["port"] == 19714)
            check("label comes from the agent's own hostname",
                  entry is not None and entry["label"] == "pi.lan")
        finally:
            stub.shutdown()

        section("Local host by default")
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

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n\033[1m%d passed, %d failed\033[0m" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  failed: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
