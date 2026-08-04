#!/usr/bin/env python3
"""Container-update agent.

A collector that reports the containers on one Docker host, with optional
capability to manage them (start/stop/restart, read logs) and to install OS
package updates. It is a single stdlib-only file so it can be scp'd to any
machine with Python 3.12+ and run under systemd. Reporting is always GETs
against the container, image and info endpoints; the write paths live in
containerctl.py and osupdate.py and are each gated by their own env var.

It is also importable: the dashboard uses ``DockerClient`` and
``collect_snapshot`` directly for the host it runs on, so local and remote hosts
go through exactly the same collection code.

Run:
    agent.py --config /etc/container-update-agent/config.json
    agent.py --token SECRET --port 9713
"""

import argparse
import hmac
import http.client
import http.server
import json
import os
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AGENT_VERSION = "1.0.0"
API_VERSION = "v1.41"
DEFAULT_PORT = 9713
DEFAULT_SOCKET = "/var/run/docker.sock"

# Labels that opt a container out of update checks.
IGNORE_LABELS = {
    "container-update-dashboard.ignore": ("true", "1", "yes"),
    "com.centurylinklabs.watchtower.enable": ("false", "0", "no"),
}
COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
COMPOSE_WORKDIR = "com.docker.compose.project.working_dir"
COMPOSE_CONFIG = "com.docker.compose.project.config_files"


class DockerError(Exception):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a unix domain socket."""

    def __init__(self, socket_path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            raise DockerError(
                "cannot connect to Docker at %s: %s" % (self.socket_path, exc)
            ) from exc
        self.sock = sock


class DockerClient:
    """Minimal Docker Engine API client.

    Accepts a unix socket path or a ``tcp://host:port`` / ``http://`` endpoint
    (DOCKER_HOST style). No TLS client-cert support -- for remote daemons use
    the agent rather than exposing the Docker socket.

    Mostly GETs. The three write calls (``container_action``, and the raw POST
    underneath it) only ever reach the container lifecycle endpoints, and only
    when containerctl.py has already decided the action is allowed.
    """

    def __init__(self, endpoint=None, timeout=15):
        self.endpoint = endpoint or os.environ.get("DOCKER_HOST") or DEFAULT_SOCKET
        self.timeout = timeout

    def _connect(self):
        ep = self.endpoint
        if ep.startswith("unix://"):
            return _UnixHTTPConnection(ep[len("unix://"):], self.timeout)
        if ep.startswith("/"):
            return _UnixHTTPConnection(ep, self.timeout)
        parsed = urllib.parse.urlparse(ep)
        host = parsed.hostname or "localhost"
        port = parsed.port or (2376 if parsed.scheme == "https" else 2375)
        if parsed.scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=self.timeout)
        return http.client.HTTPConnection(host, port, timeout=self.timeout)

    def get(self, path):
        conn = self._connect()
        try:
            conn.request("GET", path, headers={"Host": "docker", "Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                raise DockerError("not found: %s" % path)
            if resp.status >= 400:
                raise DockerError(
                    "docker API %s returned %s: %s"
                    % (path, resp.status, body.decode("utf-8", "replace")[:200])
                )
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError("docker API request failed: %s" % exc) from exc
        finally:
            conn.close()

    def ping(self):
        self.get("/_ping")
        return True

    def info(self):
        return self.get("/%s/info" % API_VERSION)

    def version(self):
        return self.get("/%s/version" % API_VERSION)

    def containers(self, all_containers=True):
        return self.get(
            "/%s/containers/json?all=%d" % (API_VERSION, 1 if all_containers else 0)
        )

    def image(self, ref):
        return self.get("/%s/images/%s/json" % (API_VERSION, urllib.parse.quote(ref, safe="")))

    def post(self, path):
        conn = self._connect()
        try:
            conn.request("POST", path, headers={"Host": "docker", "Content-Length": "0"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                raise DockerError("not found: %s" % path)
            if resp.status >= 400:
                raise DockerError(
                    "docker API %s returned %s: %s"
                    % (path, resp.status, body.decode("utf-8", "replace")[:200])
                )
            if not body:
                return None
            try:
                return json.loads(body.decode("utf-8"))
            except ValueError:
                return None
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError("docker API request failed: %s" % exc) from exc
        finally:
            conn.close()

    def raw_get(self, path):
        conn = self._connect()
        try:
            conn.request("GET", path, headers={"Host": "docker"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                raise DockerError("not found: %s" % path)
            if resp.status >= 400:
                raise DockerError(
                    "docker API %s returned %s: %s"
                    % (path, resp.status, body.decode("utf-8", "replace")[:200])
                )
            return body
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError("docker API request failed: %s" % exc) from exc
        finally:
            conn.close()

    def container_action(self, container_id, action, timeout=None):
        path = "/%s/containers/%s/%s" % (
            API_VERSION, urllib.parse.quote(container_id, safe=""), action)
        if timeout is not None:
            path += "?t=%d" % int(timeout)
        self.post(path)

    def container_logs(self, container_id, tail=200):
        path = "/%s/containers/%s/logs?stdout=1&stderr=1&tail=%d" % (
            API_VERSION, urllib.parse.quote(container_id, safe=""), int(tail))
        return self.raw_get(path)


def _format_ports(raw_ports):
    out = []
    seen = set()
    for port in raw_ports or []:
        private = port.get("PrivatePort")
        public = port.get("PublicPort")
        proto = port.get("Type", "tcp")
        if public:
            text = "%s:%s->%s/%s" % (port.get("IP") or "0.0.0.0", public, private, proto)
        else:
            text = "%s/%s" % (private, proto)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _ignored_by_label(labels):
    for key, truthy in IGNORE_LABELS.items():
        value = labels.get(key)
        if value is not None and str(value).strip().lower() in truthy:
            return key
    return None


_OS_CACHE = {"at": 0.0, "data": None}
_OS_CACHE_LOCK = threading.Lock()
OS_CACHE_SECONDS = float(os.environ.get("CUD_OS_CACHE_SECONDS") or 900)


def os_updates(force=False):
    """Pending OS package updates, cached -- a full scan costs about a second."""
    with _OS_CACHE_LOCK:
        fresh = _OS_CACHE["data"] is not None and \
            (time.time() - _OS_CACHE["at"]) < OS_CACHE_SECONDS
        if fresh and not force:
            return _OS_CACHE["data"]
    try:
        import ospackages
        data = ospackages.collect()
    except Exception as exc:  # never take the agent down over this
        data = {
            "available": False, "manager": None, "supported": False,
            "updates": [], "counts": {}, "reboot_required": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
    with _OS_CACHE_LOCK:
        _OS_CACHE["at"] = time.time()
        _OS_CACHE["data"] = data
    return data


def _osupdate():
    import osupdate
    return osupdate


def _containerctl():
    import containerctl
    return containerctl


def collect_snapshot(client, include_stopped=True):
    """Return ``{"info": {...}, "containers": [...]}`` for one Docker host."""
    try:
        info = client.info() or {}
    except DockerError:
        info = {}
    host_info = {
        "hostname": info.get("Name"),
        "docker_version": info.get("ServerVersion"),
        "os": info.get("OperatingSystem"),
        "kernel": info.get("KernelVersion"),
        "architecture": info.get("Architecture"),
        "cpus": info.get("NCPU"),
        "memory_bytes": info.get("MemTotal"),
        "containers_total": info.get("Containers"),
        "containers_running": info.get("ContainersRunning"),
        "images_total": info.get("Images"),
    }

    image_cache = {}

    def image_details(ref):
        """Look up an image by id or by tag, memoised for this snapshot."""
        if ref not in image_cache:
            try:
                image_cache[ref] = client.image(ref) or {}
            except DockerError:
                image_cache[ref] = {}
        return image_cache[ref]

    containers = []
    for raw in client.containers(all_containers=include_stopped) or []:
        labels = raw.get("Labels") or {}
        image_id = raw.get("ImageID") or ""
        details = image_details(image_id) if image_id else {}
        repo_tags = details.get("RepoTags") or []
        repo_digests = details.get("RepoDigests") or []

        image_ref = raw.get("Image") or ""
        if not image_ref or image_ref.startswith("sha256:"):
            image_ref = repo_tags[0] if repo_tags else image_ref

        # The image the *tag* currently points at may differ from the image the
        # container was started from -- that means "pulled but not recreated".
        current_image_id = image_id
        if image_ref and not image_ref.startswith("sha256:") and "<none>" not in image_ref:
            current_image_id = image_details(image_ref).get("Id") or image_id

        names = [n.lstrip("/") for n in (raw.get("Names") or []) if n]
        containers.append(
            {
                "id": (raw.get("Id") or "")[:12],
                "name": names[0] if names else (raw.get("Id") or "")[:12],
                "names": names,
                "image_ref": image_ref,
                "image_id": image_id,
                "current_image_id": current_image_id,
                "repo_digests": repo_digests,
                "repo_tags": repo_tags,
                "image_created": details.get("Created"),
                "image_size": details.get("Size"),
                "state": raw.get("State"),
                "status": raw.get("Status"),
                "created": raw.get("Created"),
                "ports": _format_ports(raw.get("Ports")),
                "compose_project": labels.get(COMPOSE_PROJECT),
                "compose_service": labels.get(COMPOSE_SERVICE),
                "compose_workdir": labels.get(COMPOSE_WORKDIR),
                "compose_config": labels.get(COMPOSE_CONFIG),
                "ignored_by": _ignored_by_label(labels),
            }
        )

    containers.sort(key=lambda c: (c.get("compose_project") or "~", c["name"]))
    return {
        "agent_version": AGENT_VERSION,
        "collected_at": time.time(),
        "info": host_info,
        "containers": containers,
        "container_actions": _containerctl().capability(),
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "container-update-agent/" + AGENT_VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), token)

    def do_GET(self):  # noqa: N802 - stdlib naming
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if path in ("/healthz", "/"):
            self._send(200, {"ok": True, "agent_version": AGENT_VERSION})
            return

        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="container-update-agent"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        client = DockerClient(self.server.docker_endpoint)
        try:
            if path == "/v1/containers":
                self._send(200, collect_snapshot(client))
            elif path == "/v1/info":
                self._send(200, {"agent_version": AGENT_VERSION, "info": client.info()})
            elif path == "/v1/os":
                payload = dict(os_updates())
                payload["updating"] = _osupdate().capability()
                self._send(200, payload)
            elif path.startswith("/v1/os/job/"):
                job = _osupdate().RUNNER.get(path[len("/v1/os/job/"):])
                if job is None:
                    self._send(404, {"error": "no such job"})
                else:
                    self._send(200, job.snapshot())
            elif path.startswith("/v1/containers/") and path.endswith("/logs"):
                container_id = path[len("/v1/containers/"):-len("/logs")]
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                tail = int((query.get("tail") or ["200"])[0])
                try:
                    lines = _containerctl().fetch_logs(client, container_id, tail=tail)
                except _containerctl().ActionError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                self._send(200, {"container": container_id, "lines": lines})
            else:
                self._send(404, {"error": "no such endpoint", "path": path})
        except DockerError as exc:
            self._send(503, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})


    def do_POST(self):  # noqa: N802 - stdlib naming
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="container-update-agent"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except ValueError:
            body = {}

        if path.startswith("/v1/containers/"):
            rest = path[len("/v1/containers/"):]
            container_id, _, action = rest.rpartition("/")
            if action in _containerctl().ACTIONS:
                client = DockerClient(self.server.docker_endpoint)
                try:
                    _containerctl().run_action(
                        client, container_id, action, timeout=body.get("timeout"))
                except _containerctl().ActionError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                except DockerError as exc:
                    self._send(503, {"error": str(exc)})
                    return
                self._send(200, {"ok": True, "container": container_id, "action": action})
                return

        if path != "/v1/os/update":
            self._send(404, {"error": "no such endpoint", "path": path})
            return

        osupdate = _osupdate()
        snapshot = os_updates()
        upgradable = {u["name"] for u in (snapshot.get("updates") or [])}
        requested = body.get("packages")
        if body.get("severity"):
            requested = [u["name"] for u in (snapshot.get("updates") or [])
                         if u.get("severity") == body["severity"]]
        elif requested is None:
            requested = sorted(upgradable)

        try:
            job = osupdate.RUNNER.start(
                snapshot.get("manager"),
                requested,
                upgradable,
                # The package list is stale the moment an update succeeds.
                on_finish=lambda job: os_updates(force=True),
            )
        except osupdate.UpdateError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(202, job.snapshot())


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def serve(token, bind="0.0.0.0", port=DEFAULT_PORT, docker_endpoint=None,
          verbose=False, certfile=None, keyfile=None):
    if ":" in bind:
        _Server.address_family = socket.AF_INET6
    httpd = _Server((bind, port), _Handler)
    httpd.token = token
    httpd.docker_endpoint = docker_endpoint
    httpd.verbose = verbose
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile or certfile)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    scheme = "https" if certfile else "http"
    sys.stderr.write(
        "container-update-agent %s listening on %s://%s:%s (docker=%s)\n"
        % (AGENT_VERSION, scheme, bind, port, docker_endpoint or DEFAULT_SOCKET)
    )
    sys.stderr.flush()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


def announce(enroll_url, enroll_token, port, attempts=12, delay=5.0):
    """Tell the dashboard we exist, so it can register this host itself.

    Runs in the background while the HTTP service comes up, because the
    dashboard verifies us by connecting back before it saves anything. Retries
    for a minute: the dashboard may still be starting, or the network may not
    be up yet on a freshly booted box.
    """
    payload = json.dumps({
        "token": enroll_token,
        "port": port,
        "hostname": socket.gethostname(),
        "address": os.environ.get("CUD_ADVERTISE_ADDRESS") or None,
    }).encode("utf-8")

    for attempt in range(attempts):
        time.sleep(delay if attempt else 1.0)
        request = urllib.request.Request(
            enroll_url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
            sys.stderr.write("enrolled with the dashboard as %r\n" % body.get("name"))
            return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            sys.stderr.write("enrolment refused (HTTP %s): %s\n" % (exc.code, detail))
            if exc.code < 500:
                return False  # a bad or spent token will not get better
        except (urllib.error.URLError, OSError, ValueError) as exc:
            sys.stderr.write("enrolment attempt %d failed: %s\n" % (attempt + 1, exc))
    sys.stderr.write("giving up on enrolment; the agent keeps serving normally\n")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Docker container update agent")
    parser.add_argument("--config", help="JSON config file")
    parser.add_argument("--token", help="shared bearer token (required unless --no-auth)")
    parser.add_argument("--no-auth", action="store_true", help="disable token auth (loopback only)")
    parser.add_argument("--bind", default=None, help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="port (default %d)" % DEFAULT_PORT)
    parser.add_argument("--docker", default=None, help="docker socket path or tcp:// endpoint")
    parser.add_argument("--tls-cert", default=None, help="serve HTTPS with this certificate")
    parser.add_argument("--tls-key", default=None, help="private key for --tls-cert")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--check", action="store_true", help="print one snapshot and exit")
    args = parser.parse_args(argv)

    cfg = {}
    if args.config:
        try:
            with open(args.config) as handle:
                cfg = json.load(handle)
        except FileNotFoundError:
            sys.stderr.write("config not found: %s\n" % args.config)
            return 2
        except ValueError as exc:
            sys.stderr.write("invalid config %s: %s\n" % (args.config, exc))
            return 2

    token = (args.token or cfg.get("token") or os.environ.get("AGENT_TOKEN")
             or os.environ.get("CUD_AGENT_TOKEN"))
    bind = args.bind or cfg.get("bind") or "0.0.0.0"
    port = args.port or cfg.get("port") or DEFAULT_PORT
    docker_endpoint = args.docker or cfg.get("docker_socket") or None
    certfile = args.tls_cert or cfg.get("tls_cert")
    keyfile = args.tls_key or cfg.get("tls_key")

    if args.check:
        snapshot = collect_snapshot(DockerClient(docker_endpoint))
        json.dump(snapshot, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not token and not args.no_auth:
        sys.stderr.write("refusing to start without a token (use --token or --no-auth)\n")
        return 2

    enroll_url = os.environ.get("CUD_ENROLL_URL")
    enroll_token = os.environ.get("CUD_ENROLL_TOKEN")
    if enroll_url and enroll_token:
        threading.Thread(
            target=announce,
            args=(enroll_url, enroll_token, int(port)),
            daemon=True,
        ).start()

    return serve(
        token if not args.no_auth else None,
        bind=bind,
        port=int(port),
        docker_endpoint=docker_endpoint,
        verbose=args.verbose or bool(cfg.get("verbose")),
        certfile=certfile,
        keyfile=keyfile,
    )


if __name__ == "__main__":
    sys.exit(main())
