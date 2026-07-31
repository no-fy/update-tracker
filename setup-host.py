#!/usr/bin/env python3
"""Add a Docker host to the dashboard.

    ./setup-host.py root@nas.lan
    ./setup-host.py deploy@10.0.0.5 --name vps --port 9713
    ./setup-host.py --local
    ./setup-host.py root@nas.lan --uninstall

For a remote host this connects over SSH, checks the machine can run the agent,
installs it under /opt/container-update-agent as a systemd service with a freshly
generated token, starts it, verifies the dashboard can actually reach it, and
records the host in config/config.json.

It only ever needs your existing SSH access -- no ports opened in advance, no
Docker socket exposed, and the agent it installs is read-only.
"""

import argparse
import base64
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dashboard import config as config_mod  # noqa: E402

AGENT_SOURCE = os.path.join(HERE, "agent", "agent.py")
INSTALL_DIR = "/opt/container-update-agent"
CONFIG_DIR = "/etc/container-update-agent"
SERVICE_NAME = "container-update-agent"
DEFAULT_PORT = 9713
AGENT_USER = "cudagent"


class SetupError(Exception):
    pass


def info(message):
    print("  %s" % message)


def step(message):
    print("\n\033[1m==>\033[0m %s" % message)


def ok(message):
    print("  \033[32mok\033[0m   %s" % message)


def warn(message):
    print("  \033[33mwarn\033[0m %s" % message)


def fail(message):
    print("  \033[31mfail\033[0m %s" % message)


# ---------------------------------------------------------------------------
# SSH plumbing
# ---------------------------------------------------------------------------

def parse_target(target):
    """``[user@]host[:port]`` -> (ssh_destination, hostname, ssh_port)."""
    ssh_port = None
    rest = target
    if rest.count(":") == 1 and not rest.endswith(":"):
        rest, _, maybe_port = rest.rpartition(":")
        if maybe_port.isdigit():
            ssh_port = int(maybe_port)
        else:
            rest = target
    hostname = rest.rpartition("@")[2]
    return rest, hostname, ssh_port


# Extra `ssh` options, set by the dashboard when it drives an install over the
# web: the browser cannot answer a host key prompt. Empty for terminal use, so
# the CLI keeps SSH's normal strict behaviour.
EXTRA_SSH_OPTS = []


def ssh_command(destination, ssh_port, identity, tty=False, extra_opts=()):
    cmd = ["ssh", "-o", "ConnectTimeout=10"]
    cmd += list(EXTRA_SSH_OPTS)
    if not tty:
        cmd += ["-o", "BatchMode=yes"]
    else:
        cmd += ["-t"]
    if ssh_port:
        cmd += ["-p", str(ssh_port)]
    if identity:
        cmd += ["-i", identity]
    cmd += list(extra_opts)
    cmd.append(destination)
    return cmd


def run_remote_script(destination, ssh_port, identity, script, sudo=False, check=True):
    """Pipe a bash script to the remote host and capture its output.

    Only usable when no password prompt can appear -- stdin is the script.
    """
    remote = "sudo -n -H bash -s" if sudo else "bash -s"
    cmd = ssh_command(destination, ssh_port, identity) + [remote]
    proc = subprocess.run(
        cmd,
        input=script.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise SetupError(
            "remote command failed (exit %d)\n%s" % (proc.returncode, (stderr or stdout).strip())
        )
    return stdout, stderr, proc.returncode


def run_remote_interactive(destination, ssh_port, identity, script, sudo=True):
    """Run a script on the remote host where sudo may prompt for a password.

    The script cannot be piped in this case -- sudo would read the password
    from the same stdin and swallow it. So it is uploaded first, then run over
    a second connection with the terminal left attached.
    """
    remote_path = "/tmp/.cud-setup-%s.sh" % secrets.token_hex(6)
    quoted = shlex.quote(remote_path)

    upload = ssh_command(destination, ssh_port, identity) + [
        "cat > %s && chmod 700 %s" % (quoted, quoted)
    ]
    proc = subprocess.run(
        upload, input=script.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        raise SetupError(
            "could not upload the install script: %s"
            % (proc.stderr or b"").decode("utf-8", "replace").strip()
        )

    runner = "sudo -H bash %s" % quoted if sudo else "bash %s" % quoted
    command = "%s; rc=$?; rm -f %s; exit $rc" % (runner, quoted)
    # stdio is inherited so the sudo prompt reaches the user's terminal.
    proc = subprocess.run(
        ssh_command(destination, ssh_port, identity, tty=True) + [command], check=False
    )
    return "", "", proc.returncode


PROBE_SCRIPT = r"""
set -u
emit() { printf '%s=%s\n' "$1" "$2"; }
emit uid "$(id -u)"
emit user "$(id -un)"
emit hostname "$(hostname 2>/dev/null || echo unknown)"
emit arch "$(uname -m 2>/dev/null || echo unknown)"
for candidate in python3 python3.14 python3.13 python3.12 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver="$("$candidate" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
    if [ -n "$ver" ]; then emit python "$(command -v "$candidate")"; emit python_version "$ver"; break; fi
  fi
done
emit systemd "$(command -v systemctl >/dev/null 2>&1 && echo yes || echo no)"
sock=""
for s in /var/run/docker.sock /run/docker.sock; do
  if [ -S "$s" ]; then sock="$s"; break; fi
done
emit docker_socket "$sock"
if [ -n "$sock" ]; then
  emit socket_group "$(stat -c '%G' "$sock" 2>/dev/null || echo unknown)"
  emit socket_gid "$(stat -c '%g' "$sock" 2>/dev/null || echo unknown)"
fi
emit docker_cli "$(command -v docker >/dev/null 2>&1 && echo yes || echo no)"
if command -v docker >/dev/null 2>&1; then
  emit docker_ok "$(docker info --format '{{.ServerVersion}}' 2>/dev/null || echo '')"
fi
if [ "$(id -u)" != "0" ]; then
  if sudo -n true 2>/dev/null; then emit sudo passwordless; else emit sudo password; fi
else
  emit sudo root
fi
"""


def probe_remote(destination, ssh_port, identity):
    stdout, stderr, code = run_remote_script(
        destination, ssh_port, identity, PROBE_SCRIPT, check=False
    )
    if code != 0 and not stdout.strip():
        message = stderr.strip() or "ssh exited %d" % code
        raise SetupError(
            "cannot connect to %s over SSH.\n       %s\n"
            "       Check that `ssh %s` works first (key-based login is required)."
            % (destination, message, destination)
        )
    facts = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            facts[key.strip()] = value.strip()
    return facts


# ---------------------------------------------------------------------------
# The remote install
# ---------------------------------------------------------------------------

INSTALL_SCRIPT = r"""
set -eu

INSTALL_DIR={install_dir}
CONFIG_DIR={config_dir}
SERVICE={service}
AGENT_USER={agent_user}
RUN_AS={run_as}
PYTHON={python}
SOCKET_GROUP={socket_group}

install -d -m 0755 "$INSTALL_DIR"
install -d -m 0750 "$CONFIG_DIR"

printf '%s' {agent_b64} | base64 -d > "$INSTALL_DIR/agent.py"
chmod 0755 "$INSTALL_DIR/agent.py"

printf '%s' {config_b64} | base64 -d > "$CONFIG_DIR/config.json"
chmod 0600 "$CONFIG_DIR/config.json"

if [ "$RUN_AS" != "root" ]; then
  if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$AGENT_USER" 2>/dev/null \
      || useradd --system --no-create-home --shell /sbin/nologin "$AGENT_USER"
  fi
  if [ -n "$SOCKET_GROUP" ] && [ "$SOCKET_GROUP" != "unknown" ]; then
    usermod -aG "$SOCKET_GROUP" "$AGENT_USER"
  fi
  chown -R "$AGENT_USER":"$AGENT_USER" "$CONFIG_DIR"
  chmod 0700 "$CONFIG_DIR"
fi

cat > /etc/systemd/system/"$SERVICE".service <<UNIT
[Unit]
Description=Container update agent (read-only Docker reporter)
Documentation=https://github.com/local/container-update-dashboard
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS
ExecStart=$PYTHON $INSTALL_DIR/agent.py --config $CONFIG_DIR/config.json
Restart=on-failure
RestartSec=5

# This service only reads the Docker API; keep it boxed in.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
ReadOnlyPaths=$INSTALL_DIR $CONFIG_DIR

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 1
state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
echo "SERVICE_STATE=$state"
if [ "$state" != "active" ]; then
  journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null || true
  exit 3
fi
"""

UNINSTALL_SCRIPT = r"""
set -u
SERVICE={service}
systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
rm -f /etc/systemd/system/"$SERVICE".service
systemctl daemon-reload >/dev/null 2>&1 || true
rm -rf {install_dir} {config_dir}
if id -u {agent_user} >/dev/null 2>&1; then userdel {agent_user} >/dev/null 2>&1 || true; fi
echo removed
"""


def build_install_script(agent_source, agent_config, facts, run_as, python):
    return INSTALL_SCRIPT.format(
        install_dir=shlex.quote(INSTALL_DIR),
        config_dir=shlex.quote(CONFIG_DIR),
        service=shlex.quote(SERVICE_NAME),
        agent_user=shlex.quote(AGENT_USER),
        run_as=shlex.quote(run_as),
        python=shlex.quote(python),
        socket_group=shlex.quote(facts.get("socket_group", "")),
        agent_b64=shlex.quote(base64.b64encode(agent_source.encode("utf-8")).decode()),
        config_b64=shlex.quote(
            base64.b64encode(json.dumps(agent_config, indent=2).encode("utf-8")).decode()
        ),
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_agent(address, port, token, tls=False, timeout=10):
    scheme = "https" if tls else "http"
    url = "%s://%s:%s/v1/containers" % (scheme, address, port)
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def diagnose_unreachable(destination, ssh_port, identity, port):
    """The agent started but we cannot reach it -- work out why."""
    script = (
        "set -u\n"
        "echo listening=$(ss -ltn 2>/dev/null | grep -c ':%d ' || echo 0)\n"
        "echo local_probe=$(curl -s -o /dev/null -w '%%{http_code}' "
        "--max-time 5 http://127.0.0.1:%d/healthz 2>/dev/null || echo none)\n"
        "if command -v ufw >/dev/null 2>&1; then echo ufw=$(ufw status 2>/dev/null | head -1); fi\n"
        "if command -v firewall-cmd >/dev/null 2>&1; then echo firewalld=$(firewall-cmd --state 2>/dev/null); fi\n"
        % (port, port)
    )
    try:
        stdout, _, _ = run_remote_script(destination, ssh_port, identity, script, check=False)
    except SetupError:
        return []
    notes = []
    facts = dict(
        line.partition("=")[::2] for line in stdout.splitlines() if "=" in line
    )
    if facts.get("local_probe", "").strip() in ("200", "401"):
        notes.append("The agent answers on the host itself, so a firewall is blocking port %d." % port)
    if facts.get("ufw"):
        notes.append("ufw is present: %s -- try `sudo ufw allow %d/tcp`." % (facts["ufw"].strip(), port))
    if facts.get("firewalld", "").strip() == "running":
        notes.append(
            "firewalld is running -- try "
            "`sudo firewall-cmd --add-port=%d/tcp --permanent && sudo firewall-cmd --reload`." % port
        )
    if not notes:
        notes.append(
            "Check that port %d is reachable from this machine "
            "(cloud security groups are the usual culprit)." % port
        )
    return notes


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def add_local(args, config, config_path):
    step("Configuring the local Docker host")
    sys.path.insert(0, os.path.join(HERE, "agent"))
    import agent as agent_module

    socket_path = args.docker_socket or os.environ.get("DOCKER_HOST") or "/var/run/docker.sock"
    client = agent_module.DockerClient(socket_path)
    try:
        snapshot = agent_module.collect_snapshot(client)
    except agent_module.DockerError as exc:
        fail(str(exc))
        print(
            "\nThe dashboard cannot read the local Docker socket. Either run it as a user in\n"
            "the `docker` group, or skip the local host and add remote ones only."
        )
        return 1
    ok("read %d containers from %s" % (len(snapshot["containers"]), socket_path))

    host = {
        "name": args.name or "local",
        "mode": "local",
        "label": args.label or (snapshot["info"].get("hostname") or "This machine"),
        "docker_socket": socket_path,
        "enabled": True,
    }
    _, created = config_mod.upsert_host(config, host)
    config_mod.save_config(config, config_path)
    ok("%s host '%s' in %s" % ("added" if created else "updated", host["name"], config_path))
    return 0


def add_remote(args, config, config_path):
    destination, hostname, ssh_port = parse_target(args.target)
    ssh_port = args.ssh_port or ssh_port
    name = args.name or hostname.split(".")[0]
    address = args.address or hostname
    port = args.port or DEFAULT_PORT

    step("Connecting to %s over SSH" % destination)
    facts = probe_remote(destination, ssh_port, args.identity)
    ok("connected as %s (uid %s) on %s" % (facts.get("user"), facts.get("uid"), facts.get("hostname")))

    step("Checking the host can run the agent")
    problems = []

    python = facts.get("python")
    if python and _version_at_least(facts.get("python_version"), (3, 12)):
        ok("python %s at %s" % (facts.get("python_version"), python))
    else:
        problems.append("Python 3.12+ is required but was not found (install python3).")
        fail("no usable python3")

    if facts.get("docker_socket"):
        ok("docker socket %s (group %s)" % (facts["docker_socket"], facts.get("socket_group")))
    else:
        problems.append("No Docker socket found at /var/run/docker.sock or /run/docker.sock.")
        fail("no docker socket")

    if facts.get("docker_ok"):
        ok("docker engine %s responding" % facts["docker_ok"])

    if facts.get("systemd") == "yes":
        ok("systemd available")
    else:
        problems.append("systemctl not found -- this installer needs systemd.")
        fail("no systemd")

    sudo_mode = facts.get("sudo")
    needs_sudo = facts.get("uid") != "0"
    if not needs_sudo:
        ok("running as root")
    elif sudo_mode == "passwordless":
        ok("passwordless sudo available")
    else:
        warn("sudo will prompt for a password")

    if problems:
        print("\nCannot continue:")
        for problem in problems:
            print("  - %s" % problem)
        return 1

    if args.dry_run:
        print("\nDry run: would install the agent on %s and listen on %s:%d" % (destination, address, port))
        return 0

    # A dedicated unprivileged user works whenever the socket has a real group.
    socket_group = facts.get("socket_group", "")
    run_as = AGENT_USER if socket_group and socket_group not in ("root", "unknown") else "root"

    token = args.token or secrets.token_urlsafe(32)
    agent_config = {
        "token": token,
        "bind": args.bind,
        "port": port,
        "docker_socket": facts.get("docker_socket") or "/var/run/docker.sock",
    }

    with open(AGENT_SOURCE) as handle:
        agent_source = handle.read()

    step("Installing the agent on %s" % destination)
    info("%s/agent.py, service %s, running as %s" % (INSTALL_DIR, SERVICE_NAME, run_as))
    script = build_install_script(agent_source, agent_config, facts, run_as, python)
    if needs_sudo and sudo_mode == "password":
        info("sudo will ask for your password")
        stdout, stderr, code = run_remote_interactive(
            destination, ssh_port, args.identity, script, sudo=True
        )
    else:
        stdout, stderr, code = run_remote_script(
            destination, ssh_port, args.identity, script, sudo=needs_sudo, check=False
        )
    if code != 0:
        fail("install failed (exit %d)" % code)
        for line in (stdout + stderr).strip().splitlines()[-20:]:
            info(line)
        print("\nInspect it with: ssh %s 'sudo journalctl -u %s -n 50'" % (destination, SERVICE_NAME))
        return 1
    ok("systemd service active")

    step("Verifying the dashboard can reach the agent at %s:%d" % (address, port))
    try:
        snapshot = verify_agent(address, port, token, tls=False)
    except urllib.error.HTTPError as exc:
        fail("agent replied HTTP %s" % exc.code)
        return 1
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        fail("no answer from %s:%d (%s)" % (address, port, getattr(exc, "reason", exc)))
        for note in diagnose_unreachable(destination, ssh_port, args.identity, port):
            info(note)
        print(
            "\nThe agent is installed and running. Fix the network path and re-run this "
            "command to finish registration, or add the host manually with --skip-verify."
        )
        if not args.skip_verify:
            return 1
        snapshot = {"containers": [], "info": {}}
    else:
        ok("agent reported %d containers" % len(snapshot.get("containers") or []))

    host = {
        "name": name,
        "mode": "agent",
        "label": args.label or (snapshot.get("info", {}).get("hostname") or name),
        "address": address,
        "port": port,
        "token": token,
        "tls": False,
        "verify_tls": True,
        "ssh": destination + (":%d" % ssh_port if ssh_port else ""),
        "enabled": True,
    }
    _, created = config_mod.upsert_host(config, host)
    config_mod.save_config(config, config_path)
    ok("%s host '%s' in %s" % ("added" if created else "updated", name, config_path))

    print("\nDone. Start the dashboard with:  ./cud serve")
    return 0


def uninstall_remote(args, config, config_path):
    destination, hostname, ssh_port = parse_target(args.target)
    ssh_port = args.ssh_port or ssh_port
    step("Removing the agent from %s" % destination)
    facts = probe_remote(destination, ssh_port, args.identity)
    needs_sudo = facts.get("uid") != "0"
    script = UNINSTALL_SCRIPT.format(
        service=shlex.quote(SERVICE_NAME),
        install_dir=shlex.quote(INSTALL_DIR),
        config_dir=shlex.quote(CONFIG_DIR),
        agent_user=shlex.quote(AGENT_USER),
    )
    if needs_sudo and facts.get("sudo") == "password":
        run_remote_interactive(destination, ssh_port, args.identity, script, sudo=True)
    else:
        run_remote_script(
            destination, ssh_port, args.identity, script, sudo=needs_sudo, check=False
        )
    ok("agent removed from %s" % destination)

    name = args.name or hostname.split(".")[0]
    if config_mod.remove_host(config, name):
        config_mod.save_config(config, config_path)
        ok("removed host '%s' from %s" % (name, config_path))
    else:
        warn("no host named '%s' in the config -- nothing to unregister" % name)
    return 0


def _version_at_least(version_string, minimum):
    match = re.match(r"(\d+)\.(\d+)", version_string or "")
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= minimum


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Install the container-update agent on a host and register it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", nargs="?", help="[user@]host[:ssh_port]")
    parser.add_argument("--local", action="store_true", help="register this machine's Docker socket")
    parser.add_argument("--uninstall", action="store_true", help="remove the agent and unregister")
    parser.add_argument("--name", help="short name for the host (default: its hostname)")
    parser.add_argument("--label", help="display name in the dashboard")
    parser.add_argument("--address", help="address the dashboard should connect to (default: the SSH host)")
    parser.add_argument("--port", type=int, help="agent port (default %d)" % DEFAULT_PORT)
    parser.add_argument("--bind", default="0.0.0.0", help="address the agent listens on (default 0.0.0.0)")
    parser.add_argument("--ssh-port", type=int, help="SSH port")
    parser.add_argument("-i", "--identity", help="SSH private key")
    parser.add_argument("--token", help="use this token instead of generating one")
    parser.add_argument("--docker-socket", help="Docker socket path (with --local)")
    parser.add_argument("--config", help="dashboard config file")
    parser.add_argument("--dry-run", action="store_true", help="check the host, change nothing")
    parser.add_argument("--skip-verify", action="store_true",
                        help="register the host even if it is not reachable yet")
    args = parser.parse_args(argv)

    if not args.local and not args.target:
        parser.error("give a target such as root@nas.lan, or --local")

    config, config_path = config_mod.load_config(args.config)

    try:
        if args.local:
            return add_local(args, config, config_path)
        if args.uninstall:
            return uninstall_remote(args, config, config_path)
        return add_remote(args, config, config_path)
    except SetupError as exc:
        print("\n\033[31merror\033[0m %s" % exc)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
