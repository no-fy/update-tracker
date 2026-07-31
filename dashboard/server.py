#!/usr/bin/env python3
"""The dashboard web server.

Serves the single-page UI plus a small JSON API. Stdlib only -- no framework,
no build step. State is held in memory and refreshed by a background thread;
the browser just reads the latest snapshot.
"""

import argparse
import base64
import functools
import hmac
import http.server
import json
import mimetypes
import os
import posixpath
import socket
import socketserver
import sys
import threading
import urllib.parse

if __package__ in (None, ""):  # allow `python3 dashboard/server.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dashboard import (
        collector,
        config as config_mod,
        enroll as enroll_mod,
        registry as registry_mod,
    )
else:
    from . import (
        collector,
        config as config_mod,
        enroll as enroll_mod,
        registry as registry_mod,
    )

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = "1.0.0"


def make_registry_client(config, cache_path=None):
    settings = config.get("dashboard", {})
    return registry_mod.RegistryClient(
        credentials=config.get("registries") or {},
        cache_path=cache_path or config_mod.default_cache_path(),
        ttl_hours=float(settings.get("registry_cache_hours", 6)),
        failure_ttl_minutes=float(settings.get("registry_failure_cache_minutes", 20)),
        insecure_registries=config.get("insecure_registries") or [],
        timeout=int(settings.get("poll_timeout_seconds", 20)),
        fetch_metadata=bool(settings.get("fetch_remote_metadata", True)),
    )


def sanitise_host(host):
    out = {k: v for k, v in host.items() if k != "token"}
    out["has_token"] = bool(host.get("token"))
    return out


def sanitise_settings(settings):
    """Everything about the dashboard except the credential itself."""
    out = {k: v for k, v in (settings or {}).items() if k != "password"}
    out["password_set"] = bool(settings.get("password"))
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "container-update-dashboard/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -----------------------------------------------------------

    def _json(self, status, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self):
        password = self.server.password
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
            except Exception:
                return False
            user, _, supplied = decoded.partition(":")
            expected_user = self.server.username
            # No configured username means any username, which is how this
            # behaved before usernames existed.
            if expected_user and not hmac.compare_digest(user, expected_user):
                return False
            return config_mod.verify_password(password, supplied)
        return False

    def _challenge(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Container updates"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _serve_static(self, rel_path):
        rel_path = posixpath.normpath("/" + rel_path).lstrip("/")
        full = os.path.join(STATIC_DIR, rel_path)
        if not os.path.abspath(full).startswith(os.path.abspath(STATIC_DIR)) \
                or not os.path.isfile(full):
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/healthz":
            self._json(200, {"ok": True, "version": VERSION})
            return

        if not self._authorised():
            self._challenge()
            return

        if path == "/":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path == "/api/state":
            self._json(200, self.server.poller.get())
            return

        if path == "/api/hosts":
            config, _ = self.server.load_config()
            self._json(200, {"hosts": [sanitise_host(h) for h in config.get("hosts", [])]})
            return

        if path == "/api/meta":
            config, config_path = self.server.load_config()
            self._json(
                200,
                {
                    "version": VERSION,
                    "config_path": config_path,
                    "settings": sanitise_settings(config.get("dashboard", {})),
                    "registries_configured": sorted((config.get("registries") or {}).keys()),
                },
            )
            return

        if path == "/api/setup":
            self._json(
                200,
                {
                    "needs_setup": not self.server.password,
                    "username": self.server.username,
                    "can_add_hosts": bool(self.server.password),
                    "env_password": bool(os.environ.get("CUD_PASSWORD")),
                },
            )
            return

        if path == "/api/enrollments":
            self._json(200, {"enrollments": self.server.enrollments.list()})
            return

        if path.startswith("/api/enrollments/"):
            item = self.server.enrollments.get(
                urllib.parse.unquote(path[len("/api/enrollments/"):])
            )
            if not item:
                self._json(404, {"error": "no such enrolment"})
                return
            self._json(200, item.snapshot())
            return

        self._json(404, {"error": "no such endpoint", "path": path})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # The agent calls this one, and it has no dashboard password. Its
        # single-use enrolment token is the credential, and the dashboard still
        # verifies the agent by connecting back before believing any of it.
        if path == "/api/enroll":
            self._handle_enroll_callback()
            return

        if not self._authorised():
            self._challenge()
            return

        if path == "/api/refresh":
            started = self.server.poller.refresh_async()
            self._json(202, {"started": started, "refreshing": True})
            return

        if path == "/api/setup":
            self._handle_setup()
            return

        if path == "/api/enrollments":
            self._handle_new_enrollment()
            return

        if path.startswith("/api/hosts/") and path.endswith("/enabled"):
            name = urllib.parse.unquote(path[len("/api/hosts/"):-len("/enabled")])
            body = self._read_body()
            config, config_path = self.server.load_config()
            host = config_mod.find_host(config, name)
            if not host:
                self._json(404, {"error": "no such host: %s" % name})
                return
            host["enabled"] = bool(body.get("enabled", True))
            config_mod.save_config(config, config_path)
            self.server.poller.refresh_async()
            self._json(200, {"host": sanitise_host(host)})
            return

        self._json(404, {"error": "no such endpoint", "path": path})

    # -- first-run setup ---------------------------------------------------

    def _handle_setup(self):
        """Set the username and password on a dashboard that has neither."""
        if self.server.password:
            self._json(409, {"error": "credentials are already configured"})
            return

        body = self._read_body()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""

        if not username:
            self._json(400, {"error": "A username is required."})
            return
        if len(password) < 8:
            self._json(400, {"error": "The password must be at least 8 characters."})
            return

        config, config_path = self.server.load_config()
        settings = config.setdefault("dashboard", {})
        settings["username"] = username
        settings["password"] = config_mod.hash_password(password)
        config_mod.save_config(config, config_path)

        # Take effect immediately: the next request is already authenticated.
        self.server.username = username
        self.server.password = settings["password"]
        self._json(200, {"ok": True, "username": username})

    # -- adding hosts ------------------------------------------------------

    def _dashboard_url(self):
        """The URL the remote host should call back on.

        Taken from the Host header, because that is by definition an address
        that reached this dashboard from somewhere -- a better guess than
        whatever the server happens to be bound to.
        """
        host = self.headers.get("Host")
        if not host:
            bound, port = self.server.server_address[:2]
            if bound in ("0.0.0.0", "::", ""):
                bound = socket.gethostname()
            host = "%s:%s" % (bound, port)
        return "http://%s" % host

    def _handle_new_enrollment(self):
        if not self.server.password:
            self._json(
                403,
                {
                    "error": "Set a username and password before adding hosts.",
                    "needs_setup": True,
                },
            )
            return

        body = self._read_body()
        try:
            enrollment = self.server.enrollments.create(
                name=body.get("name"),
                port=body.get("port") or enroll_mod.DEFAULT_AGENT_PORT,
                ttl_minutes=body.get("ttl_minutes") or enroll_mod.DEFAULT_TTL_MINUTES,
            )
        except enroll_mod.EnrollError as exc:
            self._json(400, {"error": str(exc)})
            return

        url = (body.get("dashboard_url") or "").strip() or self._dashboard_url()
        payload = enrollment.snapshot(include_token=True)
        payload["command"] = enroll_mod.agent_command(enrollment, url)
        payload["dashboard_url"] = url
        self._json(201, payload)

    def _handle_enroll_callback(self):
        body = self._read_body()
        source = self.client_address[0]
        try:
            enrollment = self.server.enrollments.claim(body.get("token"), source, body)
        except enroll_mod.EnrollError as exc:
            self._json(400, {"error": str(exc)})
            return
        self.server.poller.refresh_async()
        self._json(200, {"ok": True, "name": enrollment.host.get("name")})

    def do_DELETE(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorised():
            self._challenge()
            return

        if path.startswith("/api/enrollments/"):
            item_id = urllib.parse.unquote(path[len("/api/enrollments/"):])
            removed = self.server.enrollments.delete(item_id)
            self._json(200 if removed else 404, {"removed": removed, "id": item_id})
            return

        if path.startswith("/api/hosts/"):
            name = urllib.parse.unquote(path[len("/api/hosts/"):])
            config, config_path = self.server.load_config()
            removed = config_mod.remove_host(config, name)
            if not removed:
                self._json(404, {"error": "no such host: %s" % name})
                return
            config_mod.save_config(config, config_path)
            self.server.poller.refresh_async()
            self._json(200, {"removed": sanitise_host(removed)})
            return

        self._json(404, {"error": "no such endpoint", "path": path})


class DashboardServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def ensure_local_host_registered(config, config_path):
    """Watch the machine we run on, without being asked.

    Only when nothing at all is configured yet, and only when the socket is
    actually there -- registering a host that can never be read would just
    produce a permanent error row.
    """
    if config.get("hosts"):
        return None
    socket_path = os.environ.get("DOCKER_HOST") or "/var/run/docker.sock"
    if socket_path.startswith("unix://"):
        socket_path = socket_path[len("unix://"):]
    if not os.path.exists(socket_path):
        return None
    host, _ = config_mod.ensure_local_host(config)
    host["docker_socket"] = socket_path
    config_mod.save_config(config, config_path)
    return host


def build_server(config_path=None, bind=None, port=None, verbose=False):
    config, config_path = config_mod.load_config(config_path)
    ensure_local_host_registered(config, config_path)
    settings = config.get("dashboard", {})

    loader = functools.partial(config_mod.load_config, config_path)
    factory = functools.partial(
        make_registry_client, cache_path=config_mod.default_cache_path(config_path)
    )
    poller = collector.Poller(loader, factory)

    bind = bind or settings.get("bind", "0.0.0.0")
    port = int(port or settings.get("port", 8500))
    if ":" in bind:
        DashboardServer.address_family = socket.AF_INET6

    httpd = DashboardServer((bind, port), Handler)
    httpd.poller = poller
    httpd.load_config = loader
    httpd.password = settings.get("password") or os.environ.get("CUD_PASSWORD")
    httpd.username = settings.get("username")
    httpd.enrollments = enroll_mod.EnrollmentStore(
        config_path, on_registered=lambda item: poller.refresh_async()
    )
    httpd.verbose = verbose
    return httpd, config, config_path


def run(config_path=None, bind=None, port=None, verbose=False):
    httpd, config, config_path = build_server(config_path, bind, port, verbose)
    settings = config.get("dashboard", {})
    interval = float(settings.get("refresh_interval_minutes", 30))

    host, port = httpd.server_address[:2]
    shown = "localhost" if host in ("0.0.0.0", "::", "") else host
    print("Container update dashboard %s" % VERSION)
    print("  config    %s" % config_path)
    print("  hosts     %d configured" % len(config.get("hosts", [])))
    print("  refresh   every %g minutes" % interval)
    print("  listening http://%s:%s" % (shown, port))
    if not httpd.password:
        print("  note      no password set -- the dashboard will ask you to pick one")
    sys.stdout.flush()

    httpd.poller.start_background(interval)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.poller.stop()
        httpd.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Container update dashboard")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    return run(args.config, args.bind, args.port, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
