# Container update dashboard

A web dashboard that shows which Docker containers are running an outdated
image, across as many servers as you like.

- **One command per server, and the dashboard never logs in.** It hands you a
  `docker run` to paste on the host. The agent starts there, registers itself,
  and the dashboard verifies it by connecting back before saving anything. No
  SSH key ever reaches the dashboard, and no config file editing.
- **No pulls.** "Needs an update" is decided by asking the registry what digest
  the tag points at right now and comparing it to the digest the container is
  running. One HTTP request per unique image, not one image download.
- **Read-only, everywhere.** The agent only ever issues GETs to the Docker API.
  Nothing in this tool can start, stop, pull or recreate a container.
- **No dependencies.** Python 3.12+ standard library only, on both sides. No pip
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

The dashboard ships as a container image, which is the easiest way to run it.
Grab `docker-compose.yml` from this repo and:

```bash
docker compose up -d          # http://localhost:8500
```

That is the entire setup. The compose file binds the Docker socket, so this
machine's containers show up immediately — no group id, no `PUID`, nothing to
look up. It asks you to choose a username and password, and from there **Add
host** gives you a command to paste on each other machine you want to watch.

The image is published to the GitHub Container Registry for amd64 and arm64, so
it runs on a NAS or a Pi as happily as on a server:

```bash
docker pull ghcr.io/no-fy/update-tracker:latest
```

`docker compose run --rm dashboard check` prints the same information in the
terminal, which is what you want from cron. See [Running in a
container](#running-the-dashboard-in-a-container) for the volume, the Docker
socket and the password.

<details>
<summary>Running from a checkout instead</summary>

There is nothing to build and nothing to install — Python 3.12+ and the standard
library are the only requirements.

```bash
cd container-update-dashboard

./cud serve                    # http://localhost:8500, registers itself
./cud check                    # the same report, in the terminal

./cud add root@nas.lan         # optional: push an agent over SSH instead
```

The user running it needs read access to `/var/run/docker.sock`, which normally
means being in the `docker` group. See [Running as a
service](#running-the-dashboard-as-a-service) for the systemd unit.

</details>

## First run

The dashboard registers the Docker socket of the machine it runs on by itself,
so a fresh install already has something to show. It only does this when no
hosts are configured yet and the socket is actually there — it will not invent
a host that can never be read.

Then it asks you to pick a username and password before anything else. There is
no default login and no way to skip it from the UI: anyone who can reach the
page can see every container on every host you add. The password is stored as a
PBKDF2-SHA256 hash, never in the clear, and the API never returns it. Choosing
it signs you straight in — being asked for a password you just set would be a
silly first impression.

After that you get a proper sign-in page at `/login`, backed by a session
cookie (`HttpOnly`, `SameSite=Lax`, 12 hours by default —
`dashboard.session_hours` changes it). Sessions live in memory, so restarting
the dashboard signs everyone out.

HTTP basic auth still works for `curl`, cron and monitoring, but the server
never *asks* for it: no `WWW-Authenticate` header is ever sent, which is what
keeps browsers from popping up their own grey login box.

```bash
curl -u admin:… http://localhost:8500/api/state    # still fine
```

A plaintext `dashboard.password` still works if you would rather hand-edit the
config, and `$CUD_PASSWORD` still overrides both — setting either one skips the
first-run prompt but not the login page.

## Watching the machine the dashboard runs on

Bind the socket. That is the whole configuration:

```yaml
services:
  dashboard:
    image: ghcr.io/no-fy/update-tracker:latest
    ports:
      - "8500:8500"
    volumes:
      - cud-config:/config
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

It is already in `docker-compose.yml`, so `docker compose up -d` picks up this
machine with nothing else set. **No group id to look up, no `group_add`, no
`user:`, no environment variable.** Delete the socket line if you only want to
watch remote hosts.

There is no `PUID`/`PGID` to set either, and the container is not running as
root to get away with it. It starts as root, reads the group that owns the
socket you mounted — whatever number that is on your host — joins it, takes
ownership of the config volume, and then drops to an unprivileged user with
`su-exec`. The server itself runs as uid 10001 for its whole life:

```console
$ docker compose exec dashboard ps -o user,args
USER     COMMAND
cud      python3 /app/cud serve
```

If you would rather it never start as root, set `user:` yourself — the
entrypoint sees it is not root, changes nothing, and leaves the socket group to
you via `group_add`.

The `:ro` is not decoration: the dashboard only ever issues GETs, and mounting
read-only means a bug cannot become a container being stopped.

**From a checkout** there is nothing to configure either, beyond the user
running `./cud serve` being able to read the socket — normally
`sudo usermod -aG docker "$USER"`, then log out and back in.

**How to tell it worked.** The host list shows *This machine*, online, with
your containers under it.

| What you see | What it means |
|---|---|
| *This machine*, online | Working. |
| *This machine*, offline: `cannot connect to Docker at /var/run/docker.sock: [Errno 13] Permission denied` | You set `user:` yourself, so the entrypoint left permissions alone — add `group_add` with `stat -c %g /var/run/docker.sock`. |
| No local host at all | The socket was not mounted. The dashboard will not invent a host it cannot read. |

To watch a machine that is *not* the one running the dashboard, do not mount
anything — use **Add host** below.

## Adding a host from the dashboard

**Add host** gives you one command to run on the machine you want to watch. The
dashboard never logs into that machine, never holds an SSH key, and never has
credentials for anything but itself.

```bash
docker run -d --name container-update-agent --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -p 9713:9713 \
  -e CUD_AGENT_TOKEN=… \
  -e CUD_ENROLL_URL=http://your-dashboard:8500/api/enroll \
  -e CUD_ENROLL_TOKEN=… \
  ghcr.io/no-fy/update-tracker-agent:latest
```

The agent starts, registers itself with the dashboard, and the host appears.
You do not type an address anywhere: the dashboard takes it from where the
call came from.

How it stays honest:

- **Two separate secrets.** The enrolment token authorises one registration;
  the agent token is what the dashboard uses to poll afterwards. Neither is
  reusable as the other.
- **Single use, and it expires.** An hour by default. Registering burns it
  immediately — a replay gets *"already-used enrolment token"* even if the
  registration that burned it went on to fail.
- **A claim is not believed.** Before writing anything, the dashboard connects
  back to the agent with the agent token and requires a valid answer. A host
  that cannot be reached is not registered, and the token is spent regardless.
- **In memory only.** Pending enrolments never touch the disk, so a restart
  cancels them rather than leaving unclaimed secrets lying around.
- **The token is shown once**, to the person who generated it. It is not in
  `GET /api/enrollments`.

`POST /api/enroll` is the one endpoint that does not need the dashboard
password — the agent has no reason to know it. The enrolment token is its
credential, and the connect-back is the proof.

If the host is behind NAT or has several addresses, set
`-e CUD_ADVERTISE_ADDRESS=10.0.0.4` and the agent will ask to be registered on
that address instead of the one the dashboard sees.

Hosts without Docker still work the old way, from a terminal:
`./setup-host.py root@nas.lan` installs the agent as a systemd service over
SSH. That path is unchanged and is not exposed to the browser.

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
handle password logins), Python 3.12+, systemd, and a Docker socket. Root or
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
    "username": null,
    "password": null,
    "session_hours": 12,
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

- **`dashboard.password`** — set it and the dashboard asks for HTTP basic auth.
  Written as a PBKDF2 hash by the first-run prompt; a plaintext value still
  works if you set it by hand. Also readable from `$CUD_PASSWORD`.
- **`dashboard.username`** — the username that must accompany it. Left unset,
  any username is accepted, which is how this behaved before usernames existed.
- **`dashboard.session_hours`** — how long a sign-in lasts, 12 by default.
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

If you are not using the container, systemd runs it from a checkout:

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
docker compose up -d                  # http://localhost:8500
docker compose up -d --build          # build your working copy instead
```

`docker-compose.yml` points at `ghcr.io/no-fy/update-tracker:latest` and pulls
on every `up`, so bringing the stack up is also how you update it. Pin a release
tag (`:1.0.0`) instead if you would rather choose your own moment.

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
the volume for a bind mount — no `chown` needed, the entrypoint takes ownership
of it at startup.

`CUD_PASSWORD` is commented out in `docker-compose.yml`: setting it skips the
first-run prompt and fixes the password from the environment instead.

Adding a remote host from inside the container needs your SSH key mounted, as
above; `setup-host.py` uses key-based auth only. Running it from a checkout on
the host works equally well — the agent it installs is independent of how the
dashboard is deployed.

### How the image is built

`.github/workflows/docker.yml` runs the offline smoke test on Python 3.12, and
only then builds `linux/amd64` and `linux/arm64` for **both** images —
`ghcr.io/no-fy/update-tracker` and `ghcr.io/no-fy/update-tracker-agent` — and
pushes them. Pull requests build to prove the Dockerfiles still work but
publish nothing.

| Trigger | Tags |
|---|---|
| push to `main` | `latest`, `sha-<short>` |
| push of tag `v1.2.3` | `1.2.3`, `1.2`, `sha-<short>` |

Nothing needs configuring for this: it authenticates with the built-in
`GITHUB_TOKEN` via the workflow's `packages: write` permission — no secret to
add. The one manual step is on the very first run, since a new GHCR package is
private: open the package under your profile's *Packages*, then *Package
settings* → *Change visibility* → *Public* if you want unauthenticated pulls,
and link it to this repository so it inherits future access.

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
| POST | `/api/enrollments` | mint an enrolment and return the command to run |
| GET | `/api/enrollments` | pending and finished enrolments, tokens omitted |
| GET | `/api/enrollments/<id>` | one enrolment, to watch for the agent checking in |
| DELETE | `/api/enrollments/<id>` | cancel one |
| POST | `/api/enroll` | **the agent calls this** — no password, enrolment token instead |
| POST | `/api/login` | sign in, sets the session cookie |
| POST | `/api/logout` | sign out, clears it |
| GET | `/api/setup` | whether credentials still need choosing |
| POST | `/api/setup` | set the username and password, once |
| GET | `/api/meta` | version, config path, effective settings |
| GET | `/healthz` | unauthenticated liveness check |

`POST /api/setup` is refused once credentials exist, and `POST /api/enrollments`
is refused until they do — an unauthenticated dashboard will not hand out
enrolment tokens to whoever can reach the port.

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
Dockerfile.agent          the agent as a container, for enrolment
docker-compose.yml        socket bound, nothing else to fill in
docker-entrypoint.sh      joins the socket's group, then drops root
.github/workflows/        test, then build and push the image to ghcr.io
agent/agent.py            the agent — one stdlib-only file, also imported for local hosts
dashboard/registry.py     tag → digest, via the OCI distribution API
dashboard/collector.py    polls hosts, classifies containers
dashboard/server.py       web server and JSON API
dashboard/enroll.py       enrolment tokens, and the agent's self-registration
dashboard/static/         the UI and the sign-in page (no build step)
tests/                    fake Docker daemon + smoke test
```
