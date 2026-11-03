#!/bin/sh
# Install mcp-server-steam-backlog on Alpine (e.g. the LXC that runs steam-backlog-triage):
# an OpenRC service over Streamable HTTP, plus a command on PATH.
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# The MCP siblings' deploy/alpine layout, so they can share a container. The service runs as
# its own unprivileged user, which can't read /home/steam (the backlog's keys and data): it
# reaches the backlog only through backlog_server.py's HTTP API.
# It installs the service but doesn't enable or start it.
#
#   /opt/mcp-server-steam-backlog/venv       the code                          root:root                0755
#   /opt/mcp-server-steam-backlog/run.sh     loads the settings, runs the CLI  root:root                0755
#   /usr/local/bin/mcp-server-steam-backlog  runs run.sh as mcp-steam-backlog  root:root                0755
#   /etc/mcp-server-steam-backlog/env        every setting                     root:mcp-steam-backlog  0640
#   /var/lib/mcp-server-steam-backlog/       the user's home and working dir   mcp-steam-backlog       0700
#   /var/log/mcp-server-steam-backlog/       the service's server.log          mcp-steam-backlog       0750
#   /etc/init.d/mcp-server-steam-backlog     the OpenRC script                 root:root                0755
# Re-running upgrades the code and keeps the settings.
set -eu

SRC="${1:-git+https://github.com/SDNick484/mcp-server-steam-backlog}"
USER_NAME=mcp-steam-backlog
PREFIX=/opt/mcp-server-steam-backlog
ETC=/etc/mcp-server-steam-backlog
STATE=/var/lib/mcp-server-steam-backlog
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# python3 and its venv; git for the default git+https source; su-exec so the command on PATH
# can drop to the user without re-quoting arguments; openrc for the service. Every compiled
# dependency ships a musl wheel, so no compiler is needed (the smoke test proves it).
apk add --no-cache python3 py3-pip git su-exec openrc >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h "$STATE" -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
# Every commit carries the same version number, so pip would call an installed copy "already
# satisfied" and keep it. Reinstall the package itself (not its dependencies) from SOURCE.
"$PREFIX/venv/bin/pip" install --quiet --force-reinstall --no-deps "$SRC"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"
install -m 0755 "$HERE/mcp-server-steam-backlog" /usr/local/bin/mcp-server-steam-backlog

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/env" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/env.example" "$ETC/env"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/log/mcp-server-steam-backlog
install -m 0755 "$HERE/mcp-server-steam-backlog.initd" /etc/init.d/mcp-server-steam-backlog

echo "Installed $("$PREFIX/venv/bin/mcp-server-steam-backlog" --version)."
echo "Next: check BACKLOG_URL in $ETC/env (default http://127.0.0.1:8484), then:"
echo "  mcp-server-steam-backlog doctor"
echo "  mcp-server-steam-backlog call overview"
echo "  rc-update add mcp-server-steam-backlog default && rc-service mcp-server-steam-backlog restart"
echo "It listens on 127.0.0.1:8486/mcp/backlog. For a Claude connector, see the README's"
echo "'Remote access' section (CF_ACCESS_* and MCP_PUBLIC_HOSTS in $ETC/env)."
