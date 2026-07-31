#!/bin/sh
# Make a mounted Docker socket usable without anyone having to configure a
# group id, then get out of the way.
#
# The container starts as root, does two things that need root -- join the
# group that owns the socket, and take ownership of the config volume -- and
# then drops to an unprivileged user for the entire life of the process. The
# server itself never runs as root.
#
# If you override the user (`user:` in compose, `--user` on the CLI) there is
# nothing to adjust and nothing is attempted; supply `group_add` yourself.
set -e

APP_USER="${APP_USER:-cud}"
SOCKET="${CUD_DOCKER_SOCKET:-/var/run/docker.sock}"

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

if [ -S "$SOCKET" ]; then
    SOCK_GID="$(stat -c %g "$SOCKET" 2>/dev/null || echo "")"
    if [ -n "$SOCK_GID" ] && [ "$SOCK_GID" != "0" ]; then
        GROUP_NAME="$(getent group "$SOCK_GID" 2>/dev/null | cut -d: -f1)"
        if [ -z "$GROUP_NAME" ]; then
            GROUP_NAME=dockersock
            addgroup -g "$SOCK_GID" "$GROUP_NAME" 2>/dev/null || true
        fi
        addgroup "$APP_USER" "$GROUP_NAME" 2>/dev/null || true
    elif [ "$SOCK_GID" = "0" ]; then
        # Docker Desktop and rootful setups where the socket is root:root.
        addgroup "$APP_USER" root 2>/dev/null || true
    fi
fi

# A bind-mounted config directory arrives owned by whoever made it on the host,
# which is usually root. Without this the first write fails with EACCES.
CONFIG_DIR="$(dirname "${CUD_CONFIG:-/config/config.json}")"
if [ -d "$CONFIG_DIR" ]; then
    chown -R "$APP_USER" "$CONFIG_DIR" 2>/dev/null || true
fi

exec su-exec "$APP_USER" "$@"
