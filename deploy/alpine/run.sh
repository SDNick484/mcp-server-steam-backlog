#!/bin/sh
# Runs mcp-server-steam-backlog with the settings in /etc/mcp-server-steam-backlog/env.
# /usr/local/bin/mcp-server-steam-backlog calls this as user mcp-steam-backlog, so
#   mcp-server-steam-backlog doctor
#   mcp-server-steam-backlog call suggest_picks count=3
# work from any shell on this machine. With no arguments the CLI's default is `serve`
# over stdio.
#
# A variable already in the caller's environment beats the file, so one-off overrides work:
#   BACKLOG_DRY_RUN=1 mcp-server-steam-backlog call set_lane games='["Hades"]' lane=done
set -eu
ENV_FILE="${MCP_STEAM_BACKLOG_ENV:-/etc/mcp-server-steam-backlog/env}"
if [ -r "$ENV_FILE" ]; then
	caller=$(export -p)  # the caller's exports, quoted so the shell can re-read them
	set -a               # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
	eval "$caller"       # ...then put the caller's back on top
fi
exec /opt/mcp-server-steam-backlog/venv/bin/mcp-server-steam-backlog "$@"
