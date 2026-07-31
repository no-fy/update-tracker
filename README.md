# Container update dashboard

A web dashboard that shows which Docker containers are running an outdated
image, across as many servers as you like.

- **One command per server.** `./setup-host.py root@nas.lan` connects over SSH,
  checks the box can run the agent, installs it as a hardened systemd service
  with a freshly generated token, starts it, verifies the dashboard can actually
  reach it, and registers it. No manual config file editing.
- **No pulls.** "Needs an update" is decided by asking the registry what digest
  the tag points at right now and comparing it to the digest the container is
  running. One HTTP request per unique image, not one image download.
- **Read-only, everywhere.** The agent only ever issues GETs to the Docker API.
  Nothing in this tool can start, stop, pull or recreate a container.
- **No dependencies.** Python 3.8+ standard library only, on both sides. No pip
  install, no node, no build step, no database.

```
                          ┌──────────────────────┐
   browser  ────────────► │  dashboard (:8500)   │ ──► registry API (HTTPS)
                          │  local Docker socket │     digest for each tag
                          └───────┬──────────────┘
                                  │  HTTP + bearer token
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
                agent :9713   agent :9713   agent :9713
                (nas.lan)     (vps)         (pi)
```

## Quick start

```bash
cd container-update-dashboard

./cud add --local              # the machine the dashboard runs on
./cud add root@nas.lan         # a remote server, over SSH
./cud add deploy@10.0.0.5:2222 # non-standard SSH port

./cud serve                    # http://localhost:8500
```

`./cud check` prints the same information in the terminal, which is what you
want from cron.

## What the statuses mean

| Status | Meaning |
|---|---|
| **Update available** | The registry tag now points at a different image than the one the container is running. |
| **Restart pending** | A newer image for that tag is *already pulled* on the host; the container is still on the old one. Recreate it. |
| **Up to date** | The container runs exactly the image its tag currently points at. |
| **Pinned** | The image is referenced by digest (`image@sha256:…`), so the tag can never move. Nothing to check. |
| **Ignored** | Excluded by label (see below). |
| **Unknown** | No registry digest recorded locally — almost always an image built on the host rather than pulled. Also covers tags that no longer exist in the registry. |
| **Check failed** | The registry could not be asked: unreachable, rate limited, or the image is private and no credentials are configured. |

To exclude a container, label it. Watchtower's opt-out label is honoured too:

```yaml
labels:
  container-update-dashboard.ignore: "true"
  # or: com.centurylinklabs.watchtower.enable: "false"
```

## Adding servers

```bash
./setup-host.py root@nas.lan                 # usual case
./setup-host.py root@nas.lan --dry-run       # check the host, install nothing
./setup-host.py root@nas.lan --name storage  # override the short name
./setup-host.py root@nas.lan --port 9800     # different agent port
./setup-host.py root@nas.lan --address 10.0.0.4  # connect on a different address than SSH
./setup-host.py root@nas.lan --uninstall     # remove the agent and unregister
```

Requirements on the remote host: SSH access (key-based — the script does not
handle password logins), Python 3.8+, systemd, and a Docker socket. Root or
sudo is needed to install the service; if sudo asks for a password you will be
prompted.

What it installs:

| Path | What |
|---|---|
| `/opt/container-update-agent/agent.py` | the agent, a single file |
| `/etc/container-update-agent/config.json` | token, port, socket path (mode 0600) |
| `/etc/systemd/system/container-update-agent.service` | the unit |

The service runs as a dedicated `cudagent` system user in the Docker socket's
group where possible (root only if the socket is root-owned), with
`NoNewPrivileges`, `ProtectSystem=strict`, an empty capability bounding set and
the rest of the usual systemd hardening.

If the install succeeds but verification fails, the script checks whether the
agent answers on the host itself and looks for ufw/firewalld, so you get told
"a firewall is blocking port 9713" rather than "connection refused".

### The local host

`./cud add --local` registers the dashboard's own Docker socket directly — no
agent, no port. The dashboard process needs read access to
`/var/run/docker.sock` (be in the `docker` group).

## Configuration

Everything lives in `config/config.json`, written mode 0600 because it holds
agent tokens.

```json
{
  "dashboard": {
    "bind": "0.0.0.0",
    "port": 8500,
    "password": null,
    "refresh_interval_minutes": 30,
    "registry_cache_hours": 6,
    "include_stopped": true
  },
  "registries": {
    "ghcr.io": { "username": "you", "password": "ghp_…" }
  },
  "insecure_registries": ["registry.lan:5000"],
  "hosts": [
    { "name": "local", "mode": "local", "docker_socket": "/var/run/docker.sock", "enabled": true },
    { "name": "nas", "mode": "agent", "address": "nas.lan", "port": 9713, "token": "…", "enabled": true }
  ]
}
```

Notable settings:

- **`dashboard.password`** — set it and the dashboard asks for HTTP basic auth
  (any username). Also readable from `$CUD_PASSWORD`. There is no auth by
  default, so treat an unset password as "LAN only".
- **`registries`** — credentials for private images, keyed by registry host.
  Use `docker.io` for Docker Hub. Without these, private images report
  *Check failed*.
- **`insecure_registries`** — registries to reach over plain HTTP. `localhost`
  and `127.0.0.1` are already treated this way.
- **`registry_cache_hours`** — successful digest lookups are cached this long
  (default 6h) in `config/registry-cache.json`; failures are re-tried after 20
  minutes. This is what keeps you clear of Docker Hub's rate limits.
- **`include_stopped`** — set false to hide exited containers.

Use `CUD_CONFIG=/path/to/config.json` or `--config` to keep the config
elsewhere.

## Running the dashboard as a service

```ini
# /etc/systemd/system/container-update-dashboard.service
[Unit]
Description=Container update dashboard
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/container-update-dashboard
ExecStart=/usr/bin/python3 /opt/container-update-dashboard/cud serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

A copy of this unit is in `contrib/container-update-dashboard.service`.

## Running the dashboard in a container

```bash
docker compose up -d --build          # http://localhost:8500
```

The image is the CLI, so host management is the same commands with
`docker compose run --rm`:

```bash
docker compose run --rm dashboard hosts
docker compose run --rm dashboard check --updates-only
docker compose run --rm -v ~/.ssh:/home/cud/.ssh:ro dashboard add root@nas.lan
```

`config.json` (which holds the agent tokens) and the registry cache live on the
`cud-config` volume, so replacing the container keeps your registered hosts and
your place under the registry rate limits. To edit the config by hand, either
`docker compose cp dashboard:/config/config.json .` and copy it back, or swap
the volume for a bind mount — in which case `chown 10001 ./config` first, since
the process runs as a non-root user.

Two things are commented out in `docker-compose.yml` because most setups do not
need them:

- **The Docker socket.** Only `cud add --local` — watching the containers on
  the machine the dashboard itself runs on — needs it. Uncomment the socket
  mount *and* `group_add` with the host's docker gid
  (`stat -c %g /var/run/docker.sock`). A dashboard that only polls remote
  agents needs no Docker access at all.
- **`CUD_PASSWORD`.** There is no auth by default. Set it if the port is
  reachable beyond a trusted LAN.

Adding a remote host from inside the container needs your SSH key mounted, as
above; `setup-host.py` uses key-based auth only. Running it from a checkout on
the host works equally well — the agent it installs is independent of how the
dashboard is deployed.

For a nightly report by mail instead:

```cron
0 7 * * * cd /opt/container-update-dashboard && ./cud check --updates-only
```

`./cud check --exit-code` exits 1 when anything needs an update, for monitoring.

## HTTP API

Everything the UI uses. All endpoints sit behind `dashboard.password` when set;
tokens are never returned.

| Method | Path | What |
|---|---|---|
| GET | `/api/state` | the current snapshot: hosts, containers, statuses, summary |
| POST | `/api/refresh` | trigger a refresh (returns immediately) |
| GET | `/api/hosts` | configured hosts, tokens redacted |
| POST | `/api/hosts/<name>/enabled` | `{"enabled": false}` to pause a host |
| DELETE | `/api/hosts/<name>` | unregister a host |
| GET | `/api/meta` | version, config path, effective settings |
| GET | `/healthz` | unauthenticated liveness check |

The agent's own API is `/healthz` (open) plus `/v1/containers` and `/v1/info`
(bearer token).

## Security notes

- The agent listens on `0.0.0.0:9713` by default and requires its bearer token.
  It refuses to start without one unless you pass `--no-auth`.
- Traffic between dashboard and agent is plain HTTP. On a trusted LAN that is
  fine. Across the internet, either restrict the port to the dashboard's IP,
  put the agent behind a reverse proxy with TLS (`"tls": true` on the host
  entry), or bind the agent to loopback (`--bind 127.0.0.1`) and point the host
  entry at an SSH tunnel.
- The agent exposes container names, image names, ports and labels — not
  environment variables, not secrets, not container contents.

## Testing

```bash
python3 tests/test_smoke.py            # full pipeline against a fake Docker daemon
python3 tests/test_smoke.py --offline  # skip checks needing live registries
```

The test starts a fake Docker Engine API on a unix socket with fixtures that hit
every status branch, runs the real agent, collector and web server against it,
and checks the classifications, the token auth, the cache and the JSON API.

`python3 tests/fake_docker.py /tmp/fake.sock` runs that fake daemon on its own,
which is a convenient way to look at the UI without any containers:

```bash
python3 tests/fake_docker.py /tmp/fake.sock &
./cud add --local --name demo --docker-socket /tmp/fake.sock
./cud serve
```

## Layout

```
cud                       CLI: serve, check, hosts, add, remove
setup-host.py             SSH provisioner for a remote host
Dockerfile                the dashboard as a container
docker-compose.yml        socket mount and password, commented out
agent/agent.py            the agent — one stdlib-only file, also imported for local hosts
dashboard/registry.py     tag → digest, via the OCI distribution API
dashboard/collector.py    polls hosts, classifies containers
dashboard/server.py       web server and JSON API
dashboard/static/         the UI (no build step)
tests/                    fake Docker daemon + smoke test
```
