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
import http.cookies
import http.server
import secrets
import time
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
        aiagent,
        aihelper,
        collector,
        config as config_mod,
        enroll as enroll_mod,
        execws,
        registry as registry_mod,
    )
else:
    from . import (
        aiagent,
        aihelper,
        collector,
        config as config_mod,
        enroll as enroll_mod,
        execws,
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


SESSION_COOKIE = "cud_session"


class SessionStore(object):
    """Signed-in browsers, in memory.

    A restart signs everyone out, which is the right trade for a tool with no
    database: nothing about a session is worth persisting.
    """

    def __init__(self, ttl_hours=12):
        self.ttl = float(ttl_hours) * 3600
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self, username):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._sessions[token] = {"username": username, "expires": time.time() + self.ttl}
        return token

    def get(self, token):
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            if session["expires"] < time.time():
                del self._sessions[token]
                return None
            return session

    def destroy(self, token):
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def clear(self):
        with self._lock:
            self._sessions.clear()

    def _prune(self):
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s["expires"] < now]:
            del self._sessions[token]


def sanitise_settings(settings):
    """Everything about the dashboard except the credential itself."""
    out = {k: v for k, v in (settings or {}).items() if k != "password"}
    out["password_set"] = bool(settings.get("password"))
    return out


# User-editable preferences, as opposed to the rest of `dashboard.*` (bind,
# port, password, ...) which are configuration, not day-to-day settings.
SETTINGS_SCHEMA = {
    "skip_confirmations": bool,
    "include_stopped": bool,
    "refresh_interval_minutes": float,
    "log_tail_lines": int,
    "log_refresh_seconds": int,
    "log_auto_refresh": bool,
    "openrouter_api_key": str,
    "openrouter_model": str,
}
SETTINGS_DEFAULTS = {
    "skip_confirmations": False,
    "include_stopped": True,
    "refresh_interval_minutes": 30.0,
    "log_tail_lines": 300,
    "log_refresh_seconds": 5,
    "log_auto_refresh": True,
    "openrouter_api_key": "",
    "openrouter_model": "",
}
# Like dashboard.password, this is never returned to the browser -- current_settings()
# reports whether one is set instead of the value itself.
SETTINGS_SECRET_KEYS = {"openrouter_api_key"}


def resolved_openrouter_key(config):
    """config.json first, then the env var -- same precedence as dashboard.password."""
    return config.get("dashboard", {}).get("openrouter_api_key") or aihelper.api_key()


def resolved_openrouter_model(config):
    return config.get("dashboard", {}).get("openrouter_model") or aihelper.model()


def current_settings(config):
    stored = config.get("dashboard", {})
    out = {
        key: stored.get(key, default)
        for key, default in SETTINGS_DEFAULTS.items()
        if key not in SETTINGS_SECRET_KEYS
    }
    out["openrouter_api_key_set"] = bool(stored.get("openrouter_api_key"))
    out["ai_assistant_available"] = aihelper.available(resolved_openrouter_key(config))
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

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _session(self):
        return self.server.sessions.get(self._cookie(SESSION_COOKIE))

    def _authorised(self):
        password = self.server.password
        if not password:
            return True
        if self._session():
            return True
        # Basic is still accepted so `curl -u`, cron jobs and monitoring keep
        # working. Browsers never see a challenge for it, so they never get the
        # native popup -- they get the login page instead.
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
        """401 without WWW-Authenticate: no browser popup, just a JSON no."""
        self._json(401, {"error": "not signed in"})

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _set_session_cookie(self, token):
        cookie = http.cookies.SimpleCookie()
        cookie[SESSION_COOKIE] = token
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["httponly"] = True
        # Lax keeps the cookie off cross-site POSTs, which is the CSRF cover
        # this app needs. Secure is not set: the dashboard is usually plain
        # HTTP on a LAN, and a Secure cookie would simply never be sent.
        cookie[SESSION_COOKIE]["samesite"] = "Lax"
        cookie[SESSION_COOKIE]["max-age"] = int(self.server.sessions.ttl)
        self.send_header("Set-Cookie", cookie[SESSION_COOKIE].OutputString())

    def _clear_session_cookie(self):
        self.send_header(
            "Set-Cookie",
            "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % SESSION_COOKIE,
        )

    def _json_with_session(self, status, payload, token=None, clear=False):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token:
            self._set_session_cookie(token)
        if clear:
            self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _read_raw_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _raw(self, status, body, content_type="application/octet-stream", filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(body)

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

        # The login page needs its stylesheet before anyone is signed in, and
        # nothing in here is a secret.
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path == "/login":
            if not self.server.password:
                self._redirect("/")  # nothing to log into yet
            elif self._authorised():
                self._redirect("/")
            else:
                self._serve_static("login.html")
            return

        if not self._authorised():
            # A browser asking for a page gets the page; an API caller gets JSON.
            if path == "/" or "text/html" in (self.headers.get("Accept") or ""):
                self._redirect("/login")
            else:
                self._challenge()
            return

        if path == "/":
            self._serve_static("index.html")
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

        if path == "/api/settings":
            config, _ = self.server.load_config()
            self._json(200, current_settings(config))
            return

        if path == "/api/registries":
            config, _ = self.server.load_config()
            insecure = set(config.get("insecure_registries") or [])
            registries = [
                {"host": host, "username": entry.get("username") or "", "insecure": host in insecure}
                for host, entry in sorted((config.get("registries") or {}).items())
            ]
            self._json(200, {"registries": registries})
            return

        if path == "/api/ai/models":
            try:
                models = aihelper.list_models()
            except Exception as exc:
                self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
                return
            self._json(200, {"models": models})
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

        if path == "/api/events":
            self._handle_all_events()
            return

        if path.startswith("/api/hosts/") and path.endswith("/events"):
            self._handle_host_events(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/events")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/images"):
            self._handle_host_resource(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/images")]),
                collector.host_images)
            return

        if path.startswith("/api/hosts/") and path.endswith("/volumes"):
            self._handle_host_resource(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/volumes")]),
                collector.host_volumes)
            return

        if path.startswith("/api/hosts/") and path.endswith("/networks"):
            self._handle_host_resource(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/networks")]),
                collector.host_networks)
            return

        if path.startswith("/api/hosts/") and "/volumes/" in path and path.endswith("/backup"):
            self._handle_volume_backup(path)
            return

        if path.startswith("/api/hosts/") and path.endswith("/disk-usage"):
            self._handle_host_resource(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/disk-usage")]),
                collector.host_disk_usage)
            return

        if path.startswith("/api/hosts/") and "/containers/" in path and path.endswith("/clone-spec"):
            self._handle_container_clone_spec(path)
            return

        if path.startswith("/api/hosts/") and "/containers/" in path and path.endswith("/stats"):
            self._handle_container_stats(path)
            return

        if path.startswith("/api/hosts/") and "/images/job/" in path:
            self._handle_image_job(path)
            return

        if path.startswith("/api/hosts/") and "/stacks/job/" in path:
            self._handle_stack_job(path)
            return

        if path.startswith("/api/hosts/") and path.endswith("/stacks/file"):
            self._handle_stack_file_read(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/stacks/file")]))
            return

        if path == "/api/stack-templates":
            self._handle_list_stack_templates()
            return

        if path.startswith("/api/hosts/") and "/containers/" in path and path.endswith("/logs/history"):
            self._handle_container_logs_history(path)
            return

        if path.startswith("/api/hosts/") and "/containers/" in path and path.endswith("/logs"):
            self._handle_container_logs(path)
            return

        if path.startswith("/api/hosts/") and "/recreate/job/" in path:
            self._handle_container_recreate_job(path)
            return

        if path.startswith("/api/hosts/") and "/os/job/" in path:
            rest = path[len("/api/hosts/"):]
            name, _, job_id = rest.partition("/os/job/")
            config, _ = self.server.load_config()
            host = config_mod.find_host(config, urllib.parse.unquote(name))
            if not host:
                self._json(404, {"error": "no such host"})
                return
            try:
                job = collector.get_os_job(host, urllib.parse.unquote(job_id))
            except Exception as exc:
                self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
                return
            if job is None:
                self._json(404, {"error": "no such job"})
                return
            self._json(200, job)
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

        if path == "/api/login":
            self._handle_login()
            return

        if not self._authorised():
            self._challenge()
            return

        if path == "/api/logout":
            self.server.sessions.destroy(self._cookie(SESSION_COOKIE))
            self._json_with_session(200, {"ok": True}, clear=True)
            return

        if path == "/api/refresh":
            started = self.server.poller.refresh_async()
            self._json(202, {"started": started, "refreshing": True})
            return

        if path == "/api/setup":
            self._handle_setup()
            return

        if path == "/api/settings":
            self._handle_update_settings()
            return

        if path == "/api/enrollments":
            self._handle_new_enrollment()
            return

        if path == "/api/registries":
            self._handle_save_registry()
            return

        if path == "/api/ai/chat":
            self._handle_ai_chat()
            return

        if path.startswith("/api/hosts/") and path.endswith("/os/update"):
            self._handle_os_update(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/os/update")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/os/refresh"):
            self._handle_os_refresh(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/os/refresh")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/containers/create"):
            self._handle_container_create(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/containers/create")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/prune/containers"):
            self._handle_prune(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/prune/containers")]),
                collector.prune_containers)
            return

        if path.startswith("/api/hosts/") and path.endswith("/prune/volumes"):
            self._handle_prune(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/prune/volumes")]),
                collector.prune_volumes)
            return

        if path.startswith("/api/hosts/") and path.endswith("/prune/networks"):
            self._handle_prune(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/prune/networks")]),
                collector.prune_networks)
            return

        if path.startswith("/api/hosts/") and path.endswith("/prune/images"):
            name = urllib.parse.unquote(path[len("/api/hosts/"):-len("/prune/images")])
            body = self._read_body()
            self._handle_prune(
                name,
                lambda host: collector.prune_images(
                    host, dangling_only=body.get("dangling_only", True)))
            return

        if path.startswith("/api/hosts/") and "/volumes/" in path and path.endswith("/restore"):
            self._handle_volume_restore(path)
            return

        if path.startswith("/api/hosts/") and path.endswith("/volumes"):
            self._handle_create_volume(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/volumes")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/networks"):
            self._handle_create_network(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/networks")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/images/pull"):
            self._handle_image_pull(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/images/pull")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/images/build"):
            self._handle_image_build(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/images/build")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/stacks"):
            self._handle_stack_deploy(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/stacks")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/stacks/redeploy"):
            self._handle_stack_redeploy(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/stacks/redeploy")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/stacks/validate"):
            self._handle_stack_validate(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/stacks/validate")]))
            return

        if path.startswith("/api/hosts/") and path.endswith("/stacks/file"):
            self._handle_stack_file_write(
                urllib.parse.unquote(path[len("/api/hosts/"):-len("/stacks/file")]))
            return

        if path == "/api/stack-templates":
            self._handle_save_stack_template()
            return

        if path.startswith("/api/hosts/") and "/containers/" in path:
            tail = path.rsplit("/", 1)[-1]
            if tail in ("start", "stop", "restart", "pause", "unpause"):
                self._handle_container_action(path, tail)
                return
            if tail == "rename":
                self._handle_container_rename(path)
                return
            if tail == "recreate":
                self._handle_container_recreate(path)
                return
            if tail == "limits":
                self._handle_container_limits(path)
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

    # -- signing in --------------------------------------------------------

    def _handle_login(self):
        if not self.server.password:
            self._json(409, {"error": "no credentials are configured yet"})
            return

        body = self._read_body()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""

        expected_user = self.server.username
        user_ok = True if not expected_user else hmac.compare_digest(username, expected_user)
        # Check the password either way, so a wrong username and a wrong
        # password take the same time to fail.
        password_ok = config_mod.verify_password(self.server.password, password)

        if not (user_ok and password_ok):
            self._json(401, {"error": "That username and password do not match."})
            return

        token = self.server.sessions.create(username or "admin")
        self._json_with_session(200, {"ok": True, "username": username or "admin"}, token=token)

    def _handle_os_update(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        body = self._read_body()
        try:
            job = collector.start_os_update(
                host,
                packages=body.get("packages"),
                severity=body.get("severity"),
            )
        except collector.OsUpdateRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(202, job)

    def _handle_os_refresh(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        try:
            job = collector.refresh_os_lists(host)
        except collector.OsUpdateRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(202, job)

    def _split_container_path(self, path, suffix):
        """"/api/hosts/<name>/containers/<id><suffix>" -> (name, id)."""
        rest = path[len("/api/hosts/"):-len(suffix)] if suffix else path[len("/api/hosts/"):]
        name, _, container_id = rest.partition("/containers/")
        return urllib.parse.unquote(name), urllib.parse.unquote(container_id)

    def _handle_container_action(self, path, action):
        name, container_id = self._split_container_path(path, "/" + action)
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        body = self._read_body()
        try:
            result = collector.container_action(
                host, container_id, action, timeout=body.get("timeout"))
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(200, result)

    def _handle_container_logs(self, path):
        name, container_id = self._split_container_path(path, "/logs")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tail = int((query.get("tail") or ["200"])[0])
        try:
            result = collector.container_logs(host, container_id, tail=tail)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_container_logs_history(self, path):
        name, container_id = self._split_container_path(path, "/logs/history")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            result = collector.container_logs_history(
                host, container_id,
                since=(query.get("since") or [None])[0],
                until=(query.get("until") or [None])[0],
                limit=(query.get("limit") or [None])[0],
            )
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_all_events(self):
        config, _ = self.server.load_config()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        hosts = config.get("hosts", [])
        host_filter = (query.get("host") or [None])[0]
        if host_filter:
            hosts = [h for h in hosts if h.get("name") == host_filter]
        hosts = [h for h in hosts if h.get("enabled", True)]

        try:
            events = collector.all_events(
                hosts,
                since=(query.get("since") or [None])[0],
                until=(query.get("until") or [None])[0],
                limit=(query.get("limit") or [None])[0],
            )
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, {"events": events})

    def _handle_host_resource(self, name, fetcher):
        """Shared by the Images/Volumes/Networks routes -- each is just one
        host, one Docker API list, no filtering to parse from the query string."""
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            result = fetcher(host)
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_host_events(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            result = collector.host_events(
                host,
                since=(query.get("since") or [None])[0],
                until=(query.get("until") or [None])[0],
                limit=(query.get("limit") or [None])[0],
            )
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_container_clone_spec(self, path):
        name, container_id = self._split_container_path(path, "/clone-spec")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            result = collector.container_clone_spec(host, container_id)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_prune(self, name, runner):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            result = runner(host)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(200, result)

    def _split_volume_path(self, path, suffix):
        rest = path[len("/api/hosts/"):-len(suffix)]
        name, _, volume_name = rest.partition("/volumes/")
        return urllib.parse.unquote(name), urllib.parse.unquote(volume_name)

    def _handle_volume_backup(self, path):
        name, volume_name = self._split_volume_path(path, "/backup")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            data = collector.volume_backup(host, volume_name)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._raw(200, data, content_type="application/gzip",
                  filename="%s.tar.gz" % volume_name)

    def _handle_volume_restore(self, path):
        name, volume_name = self._split_volume_path(path, "/restore")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        tar_bytes = self._read_raw_body()
        try:
            result = collector.volume_restore(host, volume_name, tar_bytes)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_container_stats(self, path):
        name, container_id = self._split_container_path(path, "/stats")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            result = collector.container_stats(host, container_id)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_container_create(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        body = self._read_body()
        start = bool(body.pop("start", True)) if isinstance(body, dict) else True
        try:
            result = collector.create_container(host, body, start=start)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(201, result)

    def _handle_create_volume(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            result = collector.create_volume(host, self._read_body())
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(201, result)

    def _handle_create_network(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            result = collector.create_network(host, self._read_body())
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(201, result)

    def _handle_image_pull(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        body = self._read_body()
        repository = (body.get("repository") or "").strip()
        reference = (body.get("reference") or "latest").strip()
        if not repository:
            self._json(400, {"error": "An image repository is required."})
            return
        try:
            job = collector.pull_image(host, repository, reference)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(202, job)

    def _handle_image_build(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        body = self._read_body()
        try:
            job = collector.build_image(
                host, body.get("dockerfile") or "", tag=(body.get("tag") or "").strip() or None)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(202, job)

    def _handle_image_job(self, path):
        rest = path[len("/api/hosts/"):]
        name, _, tail = rest.partition("/images/job/")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, urllib.parse.unquote(name))
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            job = collector.image_job(host, urllib.parse.unquote(tail))
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        if job is None:
            self._json(404, {"error": "no such job"})
            return
        self._json(200, job)

    def _handle_stack_deploy(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        body = self._read_body()
        try:
            job = collector.deploy_stack(host, body.get("project"), body.get("compose"))
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(202, job)

    def _handle_stack_redeploy(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        body = self._read_body()
        try:
            job = collector.redeploy_stack(host, body.get("project"), body.get("path"))
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(202, job)

    def _handle_stack_validate(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        body = self._read_body()
        try:
            result = collector.validate_stack(host, body.get("project"), body.get("compose"))
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_stack_file_read(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = (query.get("path") or [""])[0]
        try:
            result = collector.read_stack_file(host, path)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_stack_file_write(self, name):
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        body = self._read_body()
        try:
            result = collector.write_stack_file(host, body.get("path"), body.get("content") or "")
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(200, result)

    def _handle_list_stack_templates(self):
        config, _ = self.server.load_config()
        self._json(200, {"templates": config.get("stack_templates", [])})

    def _handle_save_stack_template(self):
        body = self._read_body()
        name = (body.get("name") or "").strip()
        compose = body.get("compose") or ""
        if not name:
            self._json(400, {"error": "A template name is required."})
            return
        if not compose.strip():
            self._json(400, {"error": "A compose file is required."})
            return
        config, config_path = self.server.load_config()
        entry = config_mod.upsert_stack_template(config, name, compose)
        config_mod.save_config(config, config_path)
        self._json(200, entry)

    def _handle_stack_job(self, path):
        rest = path[len("/api/hosts/"):]
        name, _, tail = rest.partition("/stacks/job/")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, urllib.parse.unquote(name))
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            job = collector.stack_job(host, urllib.parse.unquote(tail))
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        if job is None:
            self._json(404, {"error": "no such job"})
            return
        self._json(200, job)

    def _handle_container_rename(self, path):
        name, container_id = self._split_container_path(path, "/rename")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        body = self._read_body()
        new_name = (body.get("name") or "").strip()
        if not new_name:
            self._json(400, {"error": "A new name is required."})
            return
        try:
            result = collector.container_rename(host, container_id, new_name)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(200, result)

    def _handle_container_limits(self, path):
        name, container_id = self._split_container_path(path, "/limits")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        body = self._read_body()
        try:
            result = collector.container_update_limits(host, container_id, body)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(200, result)

    def _handle_container_recreate(self, path):
        name, container_id = self._split_container_path(path, "/recreate")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        try:
            job = collector.container_recreate(host, container_id)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._json(202, job)

    def _handle_ai_chat(self):
        config, _ = self.server.load_config()
        api_key = resolved_openrouter_key(config)
        model_override = resolved_openrouter_model(config)
        if not aihelper.available(api_key):
            self._json(400, {"error": "The AI assistant is not configured on this dashboard "
                              "(set an OpenRouter API key in Settings)."})
            return

        body = self._read_body()
        messages = body.get("messages") or []
        confirm = body.get("confirm")

        try:
            if confirm:
                result = aiagent.resume_turn(
                    messages, body.get("pending") or {}, bool(confirm.get("approved")),
                    config, self.server.poller, api_key, model_override,
                )
            else:
                result = aiagent.run_turn(
                    messages, config, self.server.poller, api_key, model_override)
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return

        if result.get("status") == "error":
            self._json(502, {"error": result.get("error")})
            return
        if confirm and confirm.get("approved"):
            # A write tool may have just run -- catch the dashboard up.
            self.server.poller.refresh_async()
        self._json(200, result)

    def _handle_container_recreate_job(self, path):
        rest = path[len("/api/hosts/"):]
        name, _, tail = rest.partition("/containers/")
        container_id, _, job_id = tail.partition("/recreate/job/")
        name = urllib.parse.unquote(name)
        container_id = urllib.parse.unquote(container_id)
        job_id = urllib.parse.unquote(job_id)

        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return
        try:
            job = collector.container_recreate_job(host, container_id, job_id)
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        if job is None:
            self._json(404, {"error": "no such job"})
            return
        self._json(200, job)

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

        # Take effect immediately, and sign this browser in: having just chosen
        # the password, being asked for it is a silly first impression.
        self.server.username = username
        self.server.password = settings["password"]
        token = self.server.sessions.create(username)
        self._json_with_session(200, {"ok": True, "username": username}, token=token)

    def _handle_update_settings(self):
        body = self._read_body()
        config, config_path = self.server.load_config()
        settings = config.setdefault("dashboard", {})
        for key, value in body.items():
            if key not in SETTINGS_SCHEMA:
                continue
            if key == "openrouter_api_key" and not (value or "").strip():
                # The field is never pre-filled with the current key, so a
                # blank submission means "unchanged", not "clear it".
                continue
            expected = SETTINGS_SCHEMA[key]
            try:
                settings[key] = bool(value) if expected is bool else expected(value)
            except (TypeError, ValueError):
                self._json(400, {"error": "invalid value for %s" % key})
                return
        config_mod.save_config(config, config_path)
        self.server.poller.wake()
        self._json(200, current_settings(config))

    def _handle_save_registry(self):
        body = self._read_body()
        host = (body.get("host") or "").strip()
        if not host:
            self._json(400, {"error": "A registry host is required."})
            return
        config, config_path = self.server.load_config()
        config_mod.upsert_registry(
            config, host, body.get("username"), body.get("password"),
            insecure=bool(body.get("insecure")),
        )
        config_mod.save_config(config, config_path)
        self.server.poller.wake()
        self._json(200, {"ok": True, "host": host.strip().lower()})

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

        if path.startswith("/api/registries/"):
            host = urllib.parse.unquote(path[len("/api/registries/"):])
            config, config_path = self.server.load_config()
            removed = config_mod.remove_registry(config, host)
            if removed is None:
                self._json(404, {"error": "no such registry: %s" % host})
                return
            config_mod.save_config(config, config_path)
            self.server.poller.wake()
            self._json(200, {"removed": True, "host": host})
            return

        if path.startswith("/api/stack-templates/"):
            name = urllib.parse.unquote(path[len("/api/stack-templates/"):])
            config, config_path = self.server.load_config()
            removed = config_mod.remove_stack_template(config, name)
            if not removed:
                self._json(404, {"error": "no such template: %s" % name})
                return
            config_mod.save_config(config, config_path)
            self._json(200, {"removed": removed})
            return

        if path.startswith("/api/hosts/") and "/containers/" in path:
            self._handle_container_remove(path)
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

    def _handle_container_remove(self, path):
        name, container_id = self._split_container_path(path, "")
        config, _ = self.server.load_config()
        host = config_mod.find_host(config, name)
        if not host:
            self._json(404, {"error": "no such host: %s" % name})
            return

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        expected_name = (query.get("expected_name") or [None])[0]
        try:
            result = collector.container_remove(host, container_id, expected_name=expected_name)
        except collector.ContainerActionRefused as exc:
            self._json(400, {"error": str(exc)})
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                self._json(exc.code, json.loads(detail))
            except ValueError:
                self._json(exc.code, {"error": detail[:300] or "agent refused"})
            return
        except Exception as exc:
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.server.poller.refresh_async()
        self._json(200, result)


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

    local_host = next(
        (h for h in config.get("hosts", []) if h.get("mode") == "local"), None)
    if local_host is not None:
        import eventstore
        import logstore
        import osupdate
        logstore.init(local_host.get("docker_socket"))
        eventstore.init(local_host.get("docker_socket"))
        osupdate.start_auto_refresh(
            on_finish=lambda job: collector.agent_module.os_updates(force=True))

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
    httpd.sessions = SessionStore(ttl_hours=float(settings.get("session_hours", 12)))
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

    execws.serve(
        httpd.load_config, httpd.sessions, httpd.password, SESSION_COOKIE,
        bind=host, port=port + 1,
    )

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
