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

    def images(self, all_images=True):
        return self.get("/%s/images/json?all=%d" % (API_VERSION, 1 if all_images else 0))

    def volumes(self):
        return self.get("/%s/volumes" % API_VERSION)

    def networks(self):
        return self.get("/%s/networks" % API_VERSION)

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

    def container_logs(self, container_id, tail=200, timestamps=False):
        path = "/%s/containers/%s/logs?stdout=1&stderr=1&tail=%d%s" % (
            API_VERSION, urllib.parse.quote(container_id, safe=""), int(tail),
            "&timestamps=1" if timestamps else "")
        return self.raw_get(path)

    def events(self, since, until, filters=None):
        """Bounded, not a live stream: passing `until` makes Docker send
        whatever matches and close the connection, rather than holding it
        open -- the same reason logstore.py polls instead of following."""
        path = "/%s/events?since=%d&until=%d" % (API_VERSION, int(since), int(until))
        if filters:
            path += "&filters=" + urllib.parse.quote(json.dumps(filters), safe="")
        return self.raw_get(path)

    def delete(self, path):
        conn = self._connect()
        try:
            conn.request("DELETE", path, headers={"Host": "docker"})
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

    def post_json(self, path, payload=None):
        """Like post(), but sends a JSON body -- create/rename/connect need one."""
        conn = self._connect()
        try:
            data = json.dumps(payload if payload is not None else {}).encode("utf-8")
            headers = {
                "Host": "docker",
                "Content-Type": "application/json",
                "Content-Length": str(len(data)),
            }
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                raise DockerError("not found: %s" % path)
            if resp.status >= 400:
                raise DockerError(
                    "docker API %s returned %s: %s"
                    % (path, resp.status, body.decode("utf-8", "replace")[:300])
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

    def inspect_container(self, container_id):
        return self.get(
            "/%s/containers/%s/json" % (API_VERSION, urllib.parse.quote(container_id, safe="")))

    def remove_container(self, container_id):
        self.delete(
            "/%s/containers/%s" % (API_VERSION, urllib.parse.quote(container_id, safe="")))

    def container_stats(self, container_id):
        """One-shot resource snapshot (stream=0) -- not the live stream."""
        return self.get(
            "/%s/containers/%s/stats?stream=0" % (
                API_VERSION, urllib.parse.quote(container_id, safe="")))

    def update_container(self, container_id, body):
        return self.post_json(
            "/%s/containers/%s/update" % (
                API_VERSION, urllib.parse.quote(container_id, safe="")),
            body,
        )

    def rename_container(self, container_id, new_name):
        self.post_json(
            "/%s/containers/%s/rename?name=%s" % (
                API_VERSION, urllib.parse.quote(container_id, safe=""),
                urllib.parse.quote(new_name, safe=""))
        )

    def create_container(self, name, body):
        path = "/%s/containers/create" % API_VERSION
        if name:
            path += "?name=%s" % urllib.parse.quote(name, safe="")
        return self.post_json(path, body)

    def create_volume(self, body):
        return self.post_json("/%s/volumes/create" % API_VERSION, body)

    def create_network(self, body):
        return self.post_json("/%s/networks/create" % API_VERSION, body)

    def connect_network(self, network_id, container_id, endpoint_config=None):
        self.post_json(
            "/%s/networks/%s/connect" % (API_VERSION, urllib.parse.quote(network_id, safe="")),
            {"Container": container_id, "EndpointConfig": endpoint_config or {}},
        )

    def pull_image(self, repository, reference, on_line=None):
        path = "/%s/images/create?fromImage=%s&tag=%s" % (
            API_VERSION, urllib.parse.quote(repository, safe=""),
            urllib.parse.quote(reference, safe=""))
        conn = self._connect()
        try:
            conn.request("POST", path, headers={"Host": "docker", "Content-Length": "0"})
            resp = conn.getresponse()
            buf = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line and on_line:
                        try:
                            on_line(json.loads(line.decode("utf-8", "replace")))
                        except ValueError:
                            pass
            if resp.status >= 400:
                raise DockerError("docker API pull returned %s" % resp.status)
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError("docker API request failed: %s" % exc) from exc
        finally:
            conn.close()

    def build_image(self, tar_bytes, tag=None, on_line=None):
        """Build from a tar context (just a Dockerfile, for the paste/upload
        flow this agent offers) -- same streamed-NDJSON shape as pull_image."""
        query = "?rm=1&forcerm=1"
        if tag:
            query += "&t=%s" % urllib.parse.quote(tag, safe="")
        path = "/%s/build%s" % (API_VERSION, query)
        conn = self._connect()
        try:
            headers = {
                "Host": "docker",
                "Content-Type": "application/x-tar",
                "Content-Length": str(len(tar_bytes)),
            }
            conn.request("POST", path, body=tar_bytes, headers=headers)
            resp = conn.getresponse()
            buf = b""
            error = None
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if on_line:
                        on_line(event)
                    if event.get("error"):
                        error = event["error"]
            if resp.status >= 400 and not error:
                error = "docker API build returned %s" % resp.status
            if error:
                raise DockerError(error)
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError("docker API request failed: %s" % exc) from exc
        finally:
            conn.close()


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


def _logstore():
    import logstore
    return logstore


def _eventstore():
    import eventstore
    return eventstore


def _imagectl():
    import imagectl
    return imagectl


def _stackctl():
    import stackctl
    return stackctl


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
                # Already in the same /containers/json response Docker sends for
                # everything above -- free cross-referencing for the Volumes and
                # Networks tabs, no extra API call per container.
                "mounts": [
                    {"type": m.get("Type"), "name": m.get("Name"), "source": m.get("Source"),
                     "destination": m.get("Destination"), "rw": m.get("RW")}
                    for m in (raw.get("Mounts") or [])
                ],
                "networks": sorted((raw.get("NetworkSettings") or {}).get("Networks") or {}),
            }
        )

    containers.sort(key=lambda c: (c.get("compose_project") or "~", c["name"]))
    return {
        "agent_version": AGENT_VERSION,
        "collected_at": time.time(),
        "info": host_info,
        "containers": containers,
        "container_actions": _containerctl().capability(),
        "log_history": _logstore().capability(),
        "event_history": _eventstore().capability(),
        "stack_deploy": _stackctl().capability(),
        "stack_redeploy": _stackctl().redeploy_capability(),
    }


def shape_images(raw_list):
    """Trim Docker's own /images/json response to what the Images tab needs.
    Which containers use an image is cross-referenced dashboard-side from
    the container list this agent already reports -- not duplicated here."""
    out = []
    for img in raw_list or []:
        tags = [t for t in (img.get("RepoTags") or []) if t and t != "<none>:<none>"]
        out.append({
            "id": (img.get("Id") or "").replace("sha256:", "")[:12],
            "full_id": img.get("Id"),
            "tags": tags,
            "dangling": not tags,
            "digests": img.get("RepoDigests") or [],
            "created": img.get("Created"),
            "size": img.get("Size"),
            "labels": img.get("Labels") or {},
        })
    return out


def shape_volumes(raw_list):
    out = []
    for vol in raw_list or []:
        out.append({
            "name": vol.get("Name"),
            "driver": vol.get("Driver"),
            "mountpoint": vol.get("Mountpoint"),
            "created": vol.get("CreatedAt"),
            "scope": vol.get("Scope"),
            "labels": vol.get("Labels") or {},
        })
    return out


def shape_networks(raw_list):
    out = []
    for net in raw_list or []:
        ipam = (net.get("IPAM") or {}).get("Config") or []
        out.append({
            "id": (net.get("Id") or "")[:12],
            "name": net.get("Name"),
            "driver": net.get("Driver"),
            "scope": net.get("Scope"),
            "internal": bool(net.get("Internal")),
            "subnets": [c.get("Subnet") for c in ipam if c.get("Subnet")],
            "gateways": [c.get("Gateway") for c in ipam if c.get("Gateway")],
            "created": net.get("Created"),
            "labels": net.get("Labels") or {},
        })
    return out


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
            elif path == "/v1/events":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                store = _eventstore().STORE
                if store is None:
                    self._send(200, {"events": [], "enabled": False})
                else:
                    events = store.query(
                        since=(query.get("since") or [None])[0],
                        until=(query.get("until") or [None])[0],
                        limit=(query.get("limit") or [None])[0],
                    )
                    self._send(200, {"events": events, "enabled": True})
            elif path == "/v1/images":
                self._send(200, {"images": shape_images(client.images() or [])})
            elif path == "/v1/volumes":
                payload = client.volumes() or {}
                self._send(200, {"volumes": shape_volumes(payload.get("Volumes") or [])})
            elif path == "/v1/networks":
                self._send(200, {"networks": shape_networks(client.networks() or [])})
            elif path.startswith("/v1/containers/") and path.endswith("/logs/history"):
                container_id = path[len("/v1/containers/"):-len("/logs/history")]
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                store = _logstore().STORE
                if store is None:
                    self._send(200, {"container": container_id, "lines": [], "enabled": False})
                else:
                    lines = store.query(
                        container_id,
                        since=(query.get("since") or [None])[0],
                        until=(query.get("until") or [None])[0],
                        limit=(query.get("limit") or [None])[0],
                    )
                    self._send(200, {"container": container_id, "lines": lines, "enabled": True})
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
            elif "/recreate/job/" in path:
                job_id = path.rsplit("/", 1)[-1]
                job = _containerctl().RECREATE_RUNNER.get(job_id)
                if job is None:
                    self._send(404, {"error": "no such job"})
                else:
                    self._send(200, job.snapshot())
            elif path.startswith("/v1/containers/") and path.endswith("/clone-spec"):
                container_id = path[len("/v1/containers/"):-len("/clone-spec")]
                try:
                    self._send(200, _containerctl().clone_spec(client, container_id))
                except _containerctl().ActionError as exc:
                    self._send(400, {"error": str(exc)})
            elif path.startswith("/v1/containers/") and path.endswith("/stats"):
                container_id = path[len("/v1/containers/"):-len("/stats")]
                try:
                    self._send(200, _containerctl().stats(client, container_id))
                except _containerctl().ActionError as exc:
                    self._send(400, {"error": str(exc)})
            elif "/images/job/" in path:
                job_id = path.rsplit("/", 1)[-1]
                job = _imagectl().RUNNER.get(job_id)
                if job is None:
                    self._send(404, {"error": "no such job"})
                else:
                    self._send(200, job.snapshot())
            elif "/stacks/job/" in path:
                job_id = path.rsplit("/", 1)[-1]
                job = _stackctl().RUNNER.get(job_id)
                if job is None:
                    self._send(404, {"error": "no such job"})
                else:
                    self._send(200, job.snapshot())
            elif path == "/v1/stacks/file":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                file_path = (query.get("path") or [""])[0]
                try:
                    content = _stackctl().read_compose_file(file_path)
                except _containerctl().ActionError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                self._send(200, {"path": file_path, "content": content})
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

        if path == "/v1/containers/create":
            client = DockerClient(self.server.docker_endpoint)
            containerctl = _containerctl()
            try:
                container_id = containerctl.run_create(
                    client, body, start=bool(body.get("start", True)))
            except containerctl.ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            except DockerError as exc:
                self._send(503, {"error": str(exc)})
                return
            self._send(201, {"ok": True, "container": container_id})
            return

        if path == "/v1/volumes":
            client = DockerClient(self.server.docker_endpoint)
            containerctl = _containerctl()
            try:
                result = containerctl.create_volume(client, body)
            except containerctl.ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            except DockerError as exc:
                self._send(503, {"error": str(exc)})
                return
            self._send(201, result)
            return

        if path == "/v1/networks":
            client = DockerClient(self.server.docker_endpoint)
            containerctl = _containerctl()
            try:
                result = containerctl.create_network(client, body)
            except containerctl.ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            except DockerError as exc:
                self._send(503, {"error": str(exc)})
                return
            self._send(201, result)
            return

        if path == "/v1/images/pull":
            client = DockerClient(self.server.docker_endpoint)
            try:
                job = _imagectl().RUNNER.start_pull(
                    client, (body.get("repository") or "").strip(),
                    (body.get("reference") or "latest").strip())
            except _containerctl().ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(202, job.snapshot())
            return

        if path == "/v1/images/build":
            client = DockerClient(self.server.docker_endpoint)
            try:
                job = _imagectl().RUNNER.start_build(
                    client, body.get("dockerfile") or "",
                    tag=(body.get("tag") or "").strip() or None)
            except _containerctl().ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(202, job.snapshot())
            return

        if path == "/v1/stacks":
            try:
                job = _stackctl().RUNNER.start_deploy(body.get("project"), body.get("compose"))
            except _containerctl().ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(202, job.snapshot())
            return

        if path == "/v1/stacks/redeploy":
            try:
                job = _stackctl().RUNNER.start_redeploy(body.get("project"), body.get("path"))
            except _containerctl().ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(202, job.snapshot())
            return

        if path == "/v1/stacks/validate":
            try:
                result = _stackctl().validate(body.get("project"), body.get("compose"))
            except _containerctl().ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, result)
            return

        if path == "/v1/stacks/file":
            try:
                _stackctl().write_compose_file(body.get("path"), body.get("content") or "")
            except _containerctl().ActionError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, {"ok": True})
            return

        if path.startswith("/v1/containers/"):
            rest = path[len("/v1/containers/"):]
            container_id, _, action = rest.rpartition("/")
            containerctl = _containerctl()

            if action in containerctl.ACTIONS:
                client = DockerClient(self.server.docker_endpoint)
                try:
                    containerctl.run_action(
                        client, container_id, action, timeout=body.get("timeout"))
                except containerctl.ActionError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                except DockerError as exc:
                    self._send(503, {"error": str(exc)})
                    return
                self._send(200, {"ok": True, "container": container_id, "action": action})
                return

            if action == "rename":
                client = DockerClient(self.server.docker_endpoint)
                try:
                    containerctl.run_rename(client, container_id, body.get("name"))
                except containerctl.ActionError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                except DockerError as exc:
                    self._send(503, {"error": str(exc)})
                    return
                self._send(200, {"ok": True, "container": container_id, "name": body.get("name")})
                return

            if action == "limits":
                client = DockerClient(self.server.docker_endpoint)
                try:
                    containerctl.update_limits(client, container_id, body)
                except containerctl.ActionError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                except DockerError as exc:
                    self._send(503, {"error": str(exc)})
                    return
                self._send(200, {"ok": True, "container": container_id})
                return

            if action == "recreate":
                client = DockerClient(self.server.docker_endpoint)
                try:
                    job = containerctl.RECREATE_RUNNER.start(client, container_id)
                except containerctl.ActionError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                except DockerError as exc:
                    self._send(503, {"error": str(exc)})
                    return
                self._send(202, job.snapshot())
                return

        if path == "/v1/os/refresh":
            osupdate = _osupdate()
            manager = os_updates().get("manager")
            try:
                job = osupdate.RUNNER.start_refresh(
                    manager, on_finish=lambda job: os_updates(force=True))
            except osupdate.UpdateError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(202, job.snapshot())
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

    def do_DELETE(self):  # noqa: N802 - stdlib naming
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="container-update-agent"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not path.startswith("/v1/containers/"):
            self._send(404, {"error": "no such endpoint", "path": path})
            return

        container_id = path[len("/v1/containers/"):]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        expected_name = (query.get("expected_name") or [None])[0]

        client = DockerClient(self.server.docker_endpoint)
        try:
            _containerctl().run_remove(client, container_id, expected_name=expected_name)
        except _containerctl().ActionError as exc:
            self._send(400, {"error": str(exc)})
            return
        except DockerError as exc:
            self._send(503, {"error": str(exc)})
            return
        self._send(200, {"ok": True, "container": container_id, "removed": True})


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
    _logstore().init(docker_endpoint)
    _eventstore().init(docker_endpoint)
    _osupdate().start_auto_refresh(on_finish=lambda job: os_updates(force=True))
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
