# Container update dashboard

A web dashboard that shows which Docker containers are running an outdated
image, across as many servers as you like.

- **One command per server, and the dashboard never logs in.** It hands you a
  `docker run` to paste on the host. The agent starts there, registers itself,
  and the dashboard verifies it by connecting back before saving anything. No
  SSH key ever reaches the dashboard, and no config file editing.
- **OS updates too.** Pending package updates for each host, read straight from
  the package manager's own database and ranked security / important / routine.
- **No pulls.** "Needs an update" is decided by asking the registry what digest
  the tag points at right now and comparing it to the digest the container is
  running. One HTTP request per unique image, not one image download.
- **A container manager, not just a watcher.** Start, stop and restart any
  container from the dashboard, and read its recent logs, on this machine or
  any agent-hosted one. `CUD_ALLOW_CONTAINER_ACTIONS=0` makes an agent
  report-only.
- **OS updates you can actually install.** Pending packages are listed with
  what they are and how big they are, and you can install a selection — or
  every security update — from the dashboard. `CUD_ALLOW_UPDATES=0` makes an
  agent report-only.
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

There is no `PUID`/`PGID` to set either. The container starts as root, reads
the group that owns the socket you mounted — whatever number that is on your
host — joins it, takes ownership of the config volume, and then drops to an
unprivileged user with `su-exec`.

**The exception is installing OS updates.** Entering the host's namespaces
needs root, so when the container has them (`pid: host`) and updates are not
switched off, it stays root and says so in the log:

```
note: staying root so OS updates can run on the host (set CUD_ALLOW_UPDATES=0 to drop to cud)
```

Set `CUD_ALLOW_UPDATES=0`, or remove `pid: host`, and it drops to uid 10001 for
the rest of its life as before. Setting `user:` yourself also works — the
entrypoint sees it is not root, changes nothing, and leaves the socket group to
you via `group_add`.

The `:ro` here is about the socket *file* — the container cannot rename or
delete it — not about what travels over it. Once connected, a Unix socket
carries whatever the protocol on both ends allows, `:ro` bind mount or not;
what actually decides whether this dashboard can stop a container is
`CUD_ALLOW_CONTAINER_ACTIONS`, on by default. Set it to `0` for a dashboard
that only reports.

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

The host needs Docker and a reachable port; that is the whole requirement.
There is no SSH path any more, from the browser or anywhere else — the
dashboard holds no credentials for your machines at all.

## Managing containers

The lifecycle actions and a **Logs** button sit right on each container's row
— no expanding, no menu, unless the host's agent has this turned off:

| Action | What it does |
|---|---|
| **Start / Stop / Restart** | The usual lifecycle. |
| **Pause / Unpause** | Freezes the container's processes in place without stopping it. |
| **Rename** | An inline field, not a prompt — type the new name and save. |
| **Remove** | Only offered once a container is stopped. |
| **Recreate with latest image** | Only shown when the container is **update available** or **restart pending** — this is the button that closes the loop the dashboard started with. It pulls the tag's current image, then swaps the container for a new one built from the same config (env, labels, ports, volumes, restart policy, networks and aliases), streaming progress the same way an OS update does. |

Clicking a container still expands it, now just for **Details** (image,
digests, ports, compose info) and whatever the **Logs** button opens.

- **On by default.** Unlike OS updates, this needs nothing beyond the Docker
  socket the agent already reads to report containers in the first place —
  there is no extra namespace or privilege to grant, so an agent gets this the
  moment it starts. Set `-e CUD_ALLOW_CONTAINER_ACTIONS=0` for an agent that
  only ever reports; the dashboard explains why the buttons are absent instead
  of failing when you press them.
- **Re-checked on the agent, not just hidden in the UI.** Every action above is
  re-validated server-side — including the name on Remove — so a stale page or
  a direct API call can't do anything the UI itself wouldn't allow.
- **A plain yes/no before Stop, Restart, Remove and Recreate** — nothing to
  type, just a confirm dialog styled like the rest of the app instead of the
  browser's own. **Settings** (the gear icon) can turn these off entirely for
  anyone signed in, so the action runs the moment you click it.
- **Recreate is best-effort, not a compose replacement.** It reuses the
  container's own inspected config, which is enough for containers started
  directly with `docker run` and for most compose-managed ones too, but it is
  not `docker compose up -d` — deploy-time compose concepts (`depends_on`,
  build contexts, profiles) aren't replayed, only what Docker itself
  persisted about the running container. For anything compose manages, running
  compose's own recreate on the host remains the most faithful path; this
  covers the common case of "pull the same image, keep everything else."
  If Docker refuses to remove the old container after the new one is already
  running, or the new one fails to start, the recreate rolls back: the
  original container is restored under its original name.
- **A restart or recreate clears the "restart pending"/"update available"
  status** the same way installing an OS update does — the dashboard refreshes
  shortly after any of these finish.

### Logs

The **Logs** panel shows a live tail by default, auto-refreshing on the
interval set in **Settings** (5 seconds by default; **Refresh** always works
on demand regardless). **1h / 24h / 7d** switch to stored history instead —
see below.

### Log history

Docker itself only shows what its log driver still has on disk, which
rotates away. If the agent has this on, it separately keeps its own copy:

- **Polled, not followed.** Every few seconds, the agent asks Docker for each
  running container's recent output the same way the live tail does, and
  keeps whatever lines it has not already stored. An earlier version used a
  long-lived `follow` connection per container instead; on Docker Desktop's
  WSL2 backend that combination of flags proved unreliable enough (`since` or
  `tail` alongside `follow` could simply hang) that polling — the same
  request/response call every other feature here already makes, with nothing
  long-lived to leak or wedge — won out over lower latency.
- **On by default, once there's somewhere to put it.** Stored in SQLite
  (Python's own `sqlite3` — still no dependencies) at `CUD_LOG_DB`, default
  `/var/lib/container-update-agent/logs.db`. That path needs a volume mounted
  under it to survive an agent restart; without one, history capture simply
  does not start, and the dashboard says why instead of pretending to have
  history it doesn't. `CUD_LOG_HISTORY=0` turns it off outright.
- **Pruned automatically.** `CUD_LOG_RETENTION_DAYS` (default 7) and
  `CUD_LOG_MAX_ROWS_PER_CONTAINER` (default 200,000) are both enforced, so a
  chatty container can't fill the disk between prunes.

### AI assistant

An **Ask AI** button floats in the corner on every page — one assistant for
the whole dashboard, not a chat per container. It can see every host,
container, log and pending OS update the dashboard already tracks and,
with your approval, act: start, stop, restart, pause, unpause, rename,
remove or recreate a container, or install OS updates. It's backed by
[OpenRouter](https://openrouter.ai):

- **Configured from Settings.** The gear icon has an OpenRouter API key field
  — paste one from [openrouter.ai/keys](https://openrouter.ai/keys) and the
  Ask AI button appears. The key is write-only: `GET /api/settings` reports
  `openrouter_api_key_set: true/false`, never the key itself, the same way it
  reports `password_set` instead of the password. Saving with the field left
  blank keeps whatever key is already configured — it does not clear it.
  `CUD_OPENROUTER_API_KEY` still works too, as a fallback for when nothing is
  set in Settings (same precedence as `CUD_PASSWORD`).
- **Pick any model.** The model field autocompletes from OpenRouter's live
  catalog (`GET /api/ai/models`, cached in memory for an hour). Leave it blank
  for the default, **Claude Haiku** (`anthropic/claude-haiku-4.5`) — cheap,
  since most of what this assistant does is look things up and describe them,
  not a task that needs a frontier model. `CUD_OPENROUTER_MODEL` sets a
  different default the same way the key env var does.
- **Nothing runs without confirmation.** The model can call read-only tools
  (list hosts, list containers, read logs, check OS updates) freely, but the
  moment it wants to change anything, the server stops and hands control back
  to the browser, which shows the exact same confirm dialog the row buttons
  use — including honoring **Skip confirmation dialogs** in Settings, if
  that's turned on. Every write tool ends up calling the same collector
  functions the buttons already do, which re-validate the request
  server-side regardless of what the model asked for — the assistant has no
  more power than the UI does, it just has a chat in front of it.
  Removing a container normally makes you type its name back; from the
  assistant, the confirm dialog stands in for that instead.
  See [dashboard/aiagent.py](dashboard/aiagent.py) for the tool list and
  the confirm-then-execute loop.
- **Stateless, like the rest of this app has no server-side sessions.** The
  browser holds the whole conversation, tool calls and all, and resends it
  each turn (`POST /api/ai/chat`); nothing is kept in memory between
  requests beyond the current dashboard snapshot the tools already read from.
- **Replies render as markdown** — headings, lists, bold/italic, inline code
  and fenced code blocks — via a small renderer built into `app.js` rather
  than a library, same reasoning as the rest of the project.
- **Shows what it cost.** Requests ask OpenRouter for cost accounting, so
  each reply is followed by its token count and price.
- **Raw HTTP against OpenRouter's OpenAI-compatible endpoint**, no SDK. Same
  reason as everywhere else in this project: no dependencies beyond the
  standard library, on either side.
- **What you ask, and whatever it looks up to answer, leaves the machine.**
  Every message, and any logs/container/OS-update data the assistant reads
  along the way, is sent to OpenRouter and from there to whichever model
  you've picked — this is the one feature in the whole project that talks to
  anything other than the hosts you configured. Don't turn it on if that data
  shouldn't leave this machine.
- **The key sits in `config.json` like everything else here**, mode 0600 —
  there's no separate secrets store, consistent with how agent tokens and the
  dashboard password are already kept.

## Settings

The gear icon opens dashboard-wide preferences, saved to `config.json` and
shared by everyone signed in:

| Setting | What it does |
|---|---|
| Skip confirmation dialogs | Stop, restart, remove and recreate run immediately, no dialog at all — including ones the AI assistant proposes. |
| Include stopped containers | Off hides exited containers from the list entirely. |
| Background refresh | How often the dashboard re-polls every host, in minutes. |
| Log lines fetched | How many lines a live tail request pulls at once. |
| Auto-refresh open log panels, and how often | Off means logs only update when you press Refresh. |
| OpenRouter API key | Turns on the **Ask AI** button. Write-only — never shown back once saved. |
| OpenRouter model | Which model **Ask AI** uses. Blank means the built-in default (Claude Haiku). |

## OS package updates

The dashboard has two tabs. **Containers** is the image view; **OS updates**
lists pending packages per host. Each tab has its own status filter, host
filter and search — the search box follows whichever tab you are on and keeps a
separate query for each, so switching never applies a search meant for the
other list. On the containers tab each host still shows a one-line OS summary,
and *See N packages* jumps straight to that host on the OS tab.

Each host reports its pending OS package updates, ranked so the ones that
matter are not buried:

| Rank | What lands here |
|---|---|
| **security** | The update is published in a security suite — `bookworm-security`, `noble-security` and friends. |
| **important** | Kernel, glibc/musl, systemd, OpenSSL, OpenSSH, Docker, containerd, dbus, grub. An update here usually means a reboot or a service restart. |
| **routine** | Everything else. |

A package published to both `-updates` and `-security` at the same version is
counted as security, which is what `apt` itself would tell you. Debian, Ubuntu,
Raspberry Pi OS and Proxmox all get this; Alpine ships security fixes in its
normal repositories, so there is no suite to key off and only the package name
ranks it.

| Package manager | Support |
|---|---|
| **apt / dpkg** | Full — installed versions, candidates, security suites, `reboot-required`. |
| **apk** | Installed versions against the cached `APKINDEX`. |
| **pacman** | Installed versions against the synced databases. |
| **rpm** (dnf/yum) | Detected and reported as unsupported. The rpm database is a binary format this agent cannot read without librpm, and answering "no updates" would be worse than saying so. |

Reading the list executes nothing: the package manager's files are parsed
directly. Debian compresses those indexes with **lz4**, which the standard
library does not implement, so the agent decodes it itself — without that, a
Debian or Proxmox host reports zero updates while `apt` sees dozens. `.gz`,
`.xz` and `.bz2` indexes are read too.

Reading still means the agent has to see the host's filesystem:

```
-v /:/host:ro -e CUD_HOST_ROOT=/host
```

That is in the command **Add host** gives you, and in `docker-compose.yml` for
the dashboard's own machine. Drop both and everything else keeps working —
containers are still tracked, OS updates just say they cannot be read.

**A read-only mount of `/` is still the whole filesystem**, `/etc/shadow`
included. If that is more trust than you want to extend, mount only the
databases instead — same results, nothing else visible:

```bash
# Debian, Ubuntu, Raspberry Pi OS, Proxmox
-v /var/lib/dpkg:/host/var/lib/dpkg:ro \
-v /var/lib/apt/lists:/host/var/lib/apt/lists:ro \
-e CUD_HOST_ROOT=/host
```

The only thing lost is the `reboot-required` flag, which lives in `/var/run`.

**"Available" means what the host last fetched.** These files only change when
something runs `apt update` and friends, so a box that has not refreshed in a
month will honestly report old data — the dashboard shows how stale the lists
are rather than implying freshness it cannot check. Two things keep that from
being something you have to remember to do yourself:

- **The agent refreshes them on its own**, every `CUD_OS_REFRESH_HOURS` hours
  (default 6, same idea as Ubuntu's own `apt-daily.timer`, just from inside
  the agent so it works the same way on every distro this supports). Set it
  to `0` to turn this off if you'd rather control refreshes yourself.
- **A "Refresh package lists" button** sits next to Install on every host, for
  "I know something changed, check now" — same 30-minute-ceiling, one-job,
  streamed-output job as installing, just running `apt-get update` (or
  `apk update` / `pacman -Sy`) instead of an upgrade. Needs the same
  `--pid=host --privileged` access installing does, since it is the same
  mechanism running a different command.

A scan of the lists themselves costs about two seconds on a normal Debian box,
so the agent caches *that* for 15 minutes (`CUD_OS_CACHE_SECONDS`) — this is
just how often the already-fetched files get re-read, not how often they get
fetched.

### Installing them

Tick the packages you want and press **Install selected**, or **Install N
security updates** to take the whole security set at once. The package
manager's output streams into the page, and the list refreshes when the job
finishes. Clicking a row opens what the package is, its section and priority,
download and installed size, and its homepage.

This is one of two things in the tool that write anything (the other being
[container management](#managing-containers) above), so it is worth being
precise about what it needs and what it will not do:

- **It runs the host's package manager, in the host's namespaces.** That is why
  the enrolment command carries `--pid=host --privileged`. Without them the
  agent says so rather than half-working.
- **It only ever passes package names it already reported as upgradable.** Each
  name is checked against that set and against
  `^[A-Za-z0-9][A-Za-z0-9._+-]*$`, and the command is built as an argv list —
  there is no shell in the path, so a metacharacter has nothing to do.
- **It upgrades, it does not install new things.** apt runs with
  `--only-upgrade`, keeping existing config files (`--force-confold`).
- **One job per host at a time**, non-interactive, with a 30-minute ceiling.

To take this away from an agent, add `-e CUD_ALLOW_UPDATES=0`, or drop
`--pid=host --privileged`. Either makes it report-only, and the dashboard
explains why the buttons are absent instead of failing when you press them.

**Docker Desktop cannot do this, and the agent will tell you so.** On Mac and
Windows, `--pid=host` puts you inside Docker's own Linux VM, while `-v /:/host`
is the machine you actually mean — so the packages listed and the machine that
would be changed are two different systems. The agent compares
`/etc/machine-id` on both sides and refuses when they differ, rather than
upgrading the wrong box. Reporting still works fine; only installing is
disabled. On a normal Linux host the two are the same machine and it just
works.

`docker compose up -d` gives the dashboard's own machine the same ability
through `pid: host` and `privileged: true` in `docker-compose.yml`. Remove
those two lines for a dashboard that only reports.

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

## Managing an agent

The agent is a container on the host, so there is no service to learn:

```bash
docker logs container-update-agent          # did it enrol? is it serving?
docker restart container-update-agent       # after a reboot, if not --restart
docker rm -f container-update-agent         # remove it entirely
docker pull ghcr.io/no-fy/update-tracker-agent:latest   # update it
```

Requirements on the host: Docker, a reachable port for the agent (9713 by
default), and network access from the dashboard to that port. No Python, no
systemd, no SSH, and no login of any kind for the dashboard.

Removing a host is two steps, because the dashboard cannot reach into a machine
it has no credentials for: `./cud remove nas` (or the delete control in the UI)
forgets it here, and `docker rm -f container-update-agent` on the host stops it
there. `cud remove` reminds you of the second one.

If an agent's token is ever compromised, remove the host and enrol it again —
that mints a fresh token and the old one stops being accepted.

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
```

`config.json` (which holds the agent tokens) and the registry cache live on the
`cud-config` volume, so replacing the container keeps your registered hosts and
your place under the registry rate limits. To edit the config by hand, either
`docker compose cp dashboard:/config/config.json .` and copy it back, or swap
the volume for a bind mount — no `chown` needed, the entrypoint takes ownership
of it at startup.

`CUD_PASSWORD` is commented out in `docker-compose.yml`: setting it skips the
first-run prompt and fixes the password from the environment instead.

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
| POST | `/api/hosts/<name>/os/update` | install updates: `{"packages": [...]}` or `{"severity": "security"}` |
| POST | `/api/hosts/<name>/os/refresh` | refresh that host's package lists (`apt-get update` and friends) |
| GET | `/api/hosts/<name>/os/job/<id>` | that install's or refresh's status and output (`"kind": "install"\|"refresh"`) |
| POST | `/api/hosts/<name>/containers/<id>/start` | start a stopped container |
| POST | `/api/hosts/<name>/containers/<id>/stop` | stop a running container |
| POST | `/api/hosts/<name>/containers/<id>/restart` | restart a container |
| POST | `/api/hosts/<name>/containers/<id>/pause` | freeze a running container |
| POST | `/api/hosts/<name>/containers/<id>/unpause` | resume a paused container |
| POST | `/api/hosts/<name>/containers/<id>/rename` | `{"name": "..."}` |
| DELETE | `/api/hosts/<name>/containers/<id>?expected_name=...` | remove a stopped container; refused if the name doesn't match |
| POST | `/api/hosts/<name>/containers/<id>/recreate` | pull the current image and recreate the container (202 + job) |
| GET | `/api/hosts/<name>/containers/<id>/recreate/job/<job_id>` | that recreate's status and log |
| GET | `/api/hosts/<name>/containers/<id>/logs?tail=200` | recent stdout/stderr lines |
| GET | `/api/hosts/<name>/containers/<id>/logs/history?since=&until=&limit=` | stored log lines, if the agent has history on |
| POST | `/api/ai/chat` | the assistant: `{"messages", "confirm"?, "pending"?}` → `{"status": "final"\|"needs_confirmation"\|"error", ...}` — refused unless an OpenRouter key is configured |
| GET | `/api/ai/models` | OpenRouter's model catalog, for the Settings model picker |
| GET | `/api/settings` | current dashboard-wide preferences |
| POST | `/api/settings` | update one or more of them |
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

The agent's own API is `/healthz` (open) plus `/v1/containers`, `/v1/info`,
`/v1/os`, `POST /v1/os/update`, `POST /v1/os/refresh`, `/v1/os/job/<id>`,
`POST /v1/containers/<id>/{start,stop,restart,pause,unpause,rename,recreate}`,
`DELETE /v1/containers/<id>`, `/v1/containers/<id>/logs`,
`/v1/containers/<id>/logs/history` and
`/v1/containers/<id>/recreate/job/<job_id>` (bearer token).

## Security notes

- The agent listens on `0.0.0.0:9713` by default and requires its bearer token.
  It refuses to start without one unless you pass `--no-auth`.
- Traffic between dashboard and agent is plain HTTP. On a trusted LAN that is
  fine. Across the internet, either restrict the port to the dashboard's IP,
  put the agent behind a reverse proxy with TLS (`"tls": true` on the host
  entry), or bind the agent to loopback (`--bind 127.0.0.1`) and point the host
  entry at an SSH tunnel.
- The agent exposes container names, image names, ports and labels, and now
  recent log output too — not environment variables, not secrets, not the
  filesystem inside a container.
- Anyone who can sign in to the dashboard can start, stop, restart, pause,
  rename, remove or recreate any container on any host it watches. That is
  the trade this project now makes deliberately — see [Managing
  containers](#managing-containers) for the opt-out.

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
Dockerfile                the dashboard as a container
Dockerfile.agent          the agent as a container, for enrolment
docker-compose.yml        socket bound, nothing else to fill in
docker-entrypoint.sh      joins the socket's group, then drops root
.github/workflows/        test, then build and push the image to ghcr.io
agent/agent.py            the agent, also imported for local hosts
agent/ospackages.py       reads apt/apk/pacman databases (incl. lz4 indexes)
agent/osupdate.py         installs updates, guarded and never via a shell
dashboard/registry.py     tag → digest, via the OCI distribution API
dashboard/collector.py    polls hosts, classifies containers
dashboard/server.py       web server and JSON API
dashboard/enroll.py       enrolment tokens, and the agent's self-registration
dashboard/static/         the UI and the sign-in page (no build step)
tests/                    fake Docker daemon + smoke test
```
