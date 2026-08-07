#!/usr/bin/env python3
"""A fake Docker Engine API on a unix socket.

Serves just the three endpoints the agent reads, backed by fixture data, so the
collector and dashboard can be exercised end to end without a Docker daemon.
"""

import http.server
import json
import os
import socket
import socketserver
import threading
import time
import urllib.parse


def make_fixtures(live_digests=None):
    """Build a container/image set that hits every classification branch."""
    live = live_digests or {}
    now = time.time()

    def image(image_id, tags, digests, created_days_ago):
        return {
            "Id": image_id,
            "RepoTags": tags,
            "RepoDigests": digests,
            "Created": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - created_days_ago * 86400)
            ),
            "Size": 142 * 1024 * 1024,
        }

    stale = "sha256:" + "0" * 64
    images = {
        # Up to date: the local digest is whatever the registry serves right now.
        "sha256:aa" + "1" * 62: image(
            "sha256:aa" + "1" * 62, ["nginx:latest"],
            ["nginx@" + live.get("nginx:latest", stale)], 3),
        # Update available: deliberately wrong digest.
        "sha256:bb" + "2" * 62: image(
            "sha256:bb" + "2" * 62, ["traefik:v3.0"],
            ["traefik@" + stale], 240),
        # Locally built -- no repo digests at all.
        "sha256:cc" + "3" * 62: image(
            "sha256:cc" + "3" * 62, ["my-app:dev"], [], 1),
        # Pinned by digest.
        "sha256:dd" + "4" * 62: image(
            "sha256:dd" + "4" * 62, ["postgres:16"],
            ["postgres@sha256:" + "d" * 64], 60),
        # Ignored by label.
        "sha256:ee" + "5" * 62: image(
            "sha256:ee" + "5" * 62, ["redis:7"], ["redis@" + stale], 20),
        # Registry that does not exist -> lookup error.
        "sha256:ff" + "6" * 62: image(
            "sha256:ff" + "6" * 62, ["registry.invalid/team/api:2.1"],
            ["registry.invalid/team/api@" + stale], 12),
        # Restart pending: the tag now points here, the container still runs 77.
        "sha256:88" + "7" * 62: image(
            "sha256:88" + "7" * 62, ["grafana/grafana:11.1.0"],
            ["grafana/grafana@" + stale], 2),
        "sha256:77" + "7" * 62: image(
            "sha256:77" + "7" * 62, [], ["grafana/grafana@" + stale], 45),
    }

    def container(name, image_ref, image_id, state="running", labels=None, ports=None,
                  created_days_ago=10):
        return {
            "Id": (image_id[7:19] + "0" * 12)[:64],
            "Names": ["/" + name],
            "Image": image_ref,
            "ImageID": image_id,
            "State": state,
            "Status": "Up 3 days" if state == "running" else "Exited (0) 2 days ago",
            "Created": int(now - created_days_ago * 86400),
            "Labels": labels or {},
            "Ports": ports or [],
        }

    compose = {
        "com.docker.compose.project": "homelab",
        "com.docker.compose.project.working_dir": "/srv/homelab",
    }

    containers = [
        container("web", "nginx:latest", "sha256:aa" + "1" * 62,
                  labels=dict(compose, **{"com.docker.compose.service": "web"}),
                  ports=[{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}]),
        container("proxy", "traefik:v3.0", "sha256:bb" + "2" * 62,
                  labels=dict(compose, **{"com.docker.compose.service": "proxy"}),
                  ports=[{"IP": "0.0.0.0", "PrivatePort": 443, "PublicPort": 443, "Type": "tcp"}]),
        container("my-app", "my-app:dev", "sha256:cc" + "3" * 62),
        container("db", "postgres:16@sha256:" + "d" * 64, "sha256:dd" + "4" * 62),
        container("cache", "redis:7", "sha256:ee" + "5" * 62,
                  labels={"container-update-dashboard.ignore": "true"}),
        container("api", "registry.invalid/team/api:2.1", "sha256:ff" + "6" * 62),
        container("grafana", "grafana/grafana:11.1.0", "sha256:77" + "7" * 62),
        container("old-worker", "traefik:v3.0", "sha256:bb" + "2" * 62, state="exited"),
    ]

    info = {
        "Name": "fixture-host",
        "ServerVersion": "27.1.1",
        "OperatingSystem": "Debian GNU/Linux 12 (bookworm)",
        "KernelVersion": "6.1.0-23-amd64",
        "Architecture": "x86_64",
        "NCPU": 4,
        "MemTotal": 8 * 1024 ** 3,
        "Containers": len(containers),
        "ContainersRunning": sum(1 for c in containers if c["State"] == "running"),
        "Images": len(images),
        "Warnings": ["WARNING: No swap limit support"],
    }

    volumes = [
        {"Name": "homelab_db-data", "Driver": "local",
         "Mountpoint": "/var/lib/docker/volumes/homelab_db-data/_data",
         "CreatedAt": "2026-01-01T00:00:00Z", "Labels": {}, "Scope": "local"},
        {"Name": "orphan-vol", "Driver": "local",
         "Mountpoint": "/var/lib/docker/volumes/orphan-vol/_data",
         "CreatedAt": "2026-01-02T00:00:00Z", "Labels": {}, "Scope": "local"},
    ]

    networks = [
        {"Id": "net1", "Name": "homelab_default", "Driver": "bridge", "Scope": "local",
         "IPAM": {"Config": [{"Subnet": "172.20.0.0/16", "Gateway": "172.20.0.1"}]}},
        {"Id": "net2", "Name": "bridge", "Driver": "bridge", "Scope": "local",
         "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}},
    ]

    system_df = {
        "LayersSize": 500 * 1024 * 1024,
        "Images": [
            {"Id": iid, "Size": 100 * 1024 * 1024,
             "Containers": 1 if any(c["ImageID"] == iid for c in containers) else 0}
            for iid in images
        ],
        "Containers": [
            {"Id": c["Id"], "SizeRw": 1024 * 1024, "State": c["State"]} for c in containers
        ],
        "Volumes": [
            {"Name": "homelab_db-data", "UsageData": {"RefCount": 1, "Size": 5 * 1024 * 1024}},
            {"Name": "orphan-vol", "UsageData": {"RefCount": 0, "Size": 2 * 1024 * 1024}},
        ],
        "BuildCache": [
            {"ID": "c1", "Size": 10 * 1024 * 1024, "InUse": False},
        ],
    }

    return {
        "info": info, "containers": containers, "images": images,
        "volumes": volumes, "networks": networks, "system_df": system_df,
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        data = self.server.fixtures
        path = urllib.parse.urlparse(self.path).path

        if path.endswith("/_ping"):
            self._json(200, {})
            return
        if path.endswith("/info"):
            self._json(200, data["info"])
            return
        if path.endswith("/system/df"):
            self._json(200, data["system_df"])
            return
        if path.endswith("/volumes"):
            self._json(200, {"Volumes": data["volumes"]})
            return
        if path.endswith("/networks"):
            self._json(200, data["networks"])
            return
        if "/images/json" in path:
            self._json(200, list(data["images"].values()))
            return
        if "/containers/json" in path:
            self._json(200, data["containers"])
            return
        if "/containers/" in path and path.endswith("/json"):
            cid = path.split("/containers/", 1)[1][: -len("/json")]
            for container in data["containers"]:
                if container["Id"] == cid:
                    self._json(200, self._inspect(container, data))
                    return
            self._json(404, {"message": "no such container: " + cid})
            return
        if "/containers/" in path and path.endswith("/stats"):
            self._json(200, {
                "cpu_stats": {"cpu_usage": {"total_usage": 2000000000},
                              "system_cpu_usage": 40000000000, "online_cpus": 4},
                "precpu_stats": {"cpu_usage": {"total_usage": 1000000000},
                                  "system_cpu_usage": 30000000000},
                "memory_stats": {"usage": 50 * 1024 * 1024, "limit": 512 * 1024 * 1024,
                                  "stats": {"cache": 0}},
            })
            return
        if "/images/" in path and path.endswith("/json"):
            ref = urllib.parse.unquote(path.split("/images/", 1)[1][: -len("/json")])
            if ref in data["images"]:
                self._json(200, data["images"][ref])
                return
            for image in data["images"].values():
                if ref in (image.get("RepoTags") or []):
                    self._json(200, image)
                    return
            self._json(404, {"message": "no such image: " + ref})
            return
        self._json(404, {"message": "unsupported: " + path})

    def _inspect(self, container, data):
        image = data["images"].get(container["ImageID"], {})
        return {
            "Id": container["Id"],
            "Name": "/" + container["Names"][0].lstrip("/"),
            "State": {"Status": container["State"]},
            "Config": {
                "Image": container["Image"], "Cmd": None,
                "Env": ["PATH=/usr/bin", "APP_ENV=fixture"],
            },
            "HostConfig": {
                "Binds": [], "PortBindings": {}, "RestartPolicy": {"Name": "unless-stopped"},
                "Memory": 0, "NanoCpus": 0,
            },
            "NetworkSettings": {"Networks": {"homelab_default": {}}},
            "Mounts": [],
        }

    def do_POST(self):  # noqa: N802
        data = self.server.fixtures
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        if path.endswith("/containers/prune"):
            self._json(200, {"ContainersDeleted": [], "SpaceReclaimed": 0})
            return
        if path.endswith("/volumes/prune"):
            self._json(200, {"VolumesDeleted": ["orphan-vol"], "SpaceReclaimed": 2097152})
            return
        if path.endswith("/networks/prune"):
            self._json(200, {"NetworksDeleted": []})
            return
        if path.endswith("/images/prune"):
            self._json(200, {"ImagesDeleted": [], "SpaceReclaimed": 0})
            return
        if path.endswith("/update"):
            self._json(200, {"Warnings": []})
            return
        self._json(404, {"message": "unsupported: " + path})


class _UnixServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        if os.path.exists(self.server_address):
            os.unlink(self.server_address)
        socketserver.TCPServer.server_bind(self)

    def get_request(self):
        conn, _ = self.socket.accept()
        return conn, ("fake-docker", 0)


class FakeDocker:
    def __init__(self, socket_path, fixtures=None):
        self.socket_path = socket_path
        self.fixtures = fixtures or make_fixtures()
        self.httpd = None
        self.thread = None

    def start(self):
        self.httpd = _UnixServer(self.socket_path, _Handler)
        self.httpd.fixtures = self.fixtures
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-docker.sock"
    server = FakeDocker(path).start()
    print("fake docker on %s -- ctrl-c to stop" % path)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
