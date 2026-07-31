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
    from dashboard import collector, config as config_mod, registry as registry_mod
else:
    from . import collector, config as config_mod, registry as registry_mod

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
            supplied = decoded.partition(":")[2]
            return hmac.compare_digest(supplied, password)
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
                    "settings": config.get("dashboard", {}),
                    "registries_configured": sorted((config.get("registries") or {}).keys()),
                },
            )
            return

        self._json(404, {"error": "no such endpoint", "path": path})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorised():
            self._challenge()
            return

        if path == "/api/refresh":
            started = self.server.poller.refresh_async()
            self._json(202, {"started": started, "refreshing": True})
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

    def do_DELETE(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorised():
            self._challenge()
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


def build_server(config_path=None, bind=None, port=None, verbose=False):
    config, config_path = config_mod.load_config(config_path)
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
        print("  note      no password set (dashboard.password in config adds one)")
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
