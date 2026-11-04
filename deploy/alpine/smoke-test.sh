#!/bin/sh
# CI: install in a stock python:3.12-alpine container and drive the command on PATH and the
# OpenRC service against the simulator. Proves the dependencies install on musl, re-running
# upgrades the code, the layout and file modes, that the command drops to user
# mcp-steam-backlog and reads /etc/mcp-server-steam-backlog/env, that the caller's environment
# beats that file, that OpenRC runs the service as the user, that an MCP call over HTTP reaches
# the backlog, and that Access settings in the env file turn the Access check on.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

report() {
	status=$?
	if [ "$status" != 0 ]; then
		tail_log=$(tail -n 30 "${STEP_LOG:-/dev/null}" 2>/dev/null | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
		echo "::error title=Alpine smoke test failed (exit $status)::last step: ${STEP:-?}%0A$tail_log"
	fi
}
trap report EXIT

STEP='install'
apk add --no-cache curl >/dev/null
sh deploy/alpine/install.sh /src

STEP='upgrade'
cp -r /src /tmp/src2
echo '# smoke-test upgrade marker' >>/tmp/src2/src/steam_backlog_mcp/__init__.py
sh /tmp/src2/deploy/alpine/install.sh /tmp/src2
grep -q 'smoke-test upgrade marker' /opt/mcp-server-steam-backlog/venv/lib/python3*/site-packages/steam_backlog_mcp/__init__.py
sh deploy/alpine/install.sh /src  # back to the code under test

ETC=/etc/mcp-server-steam-backlog
STATE=/var/lib/mcp-server-steam-backlog
STEP='file modes'
[ "$(stat -c '%a %U:%G' $ETC)" = "750 root:mcp-steam-backlog" ]
[ "$(stat -c '%a %U:%G' $ETC/env)" = "640 root:mcp-steam-backlog" ]
[ "$(stat -c '%a %U' $STATE)" = "700 mcp-steam-backlog" ]
[ "$(command -v mcp-server-steam-backlog)" = /usr/local/bin/mcp-server-steam-backlog ]
[ "$(stat -c '%a %U' /var/log/mcp-server-steam-backlog)" = "750 mcp-steam-backlog" ]
[ -x /etc/init.d/mcp-server-steam-backlog ]

STEP='simulator, as the user'
STEP_LOG=/tmp/sim.log
mcp-server-steam-backlog simulate --port 8484 >"$STEP_LOG" 2>&1 &
for _ in $(seq 100); do curl -fsS http://127.0.0.1:8484/healthz >/dev/null 2>&1 && break; sleep 0.1; done
curl -fsS http://127.0.0.1:8484/healthz

STEP='doctor, settings from env'
STEP_LOG=''
grep -q '^BACKLOG_URL=http://127.0.0.1:8484$' $ETC/env
mcp-server-steam-backlog doctor

STEP='call'
mcp-server-steam-backlog call suggest_picks count=1 | grep -q '"name": "Portal"'
out=$(mcp-server-steam-backlog call set_lane 'games=["Hades"]' lane=done)
echo "$out" | grep -q '"outcome": "done"'

STEP='caller beats env'
if BACKLOG_URL=http://127.0.0.1:9 mcp-server-steam-backlog doctor; then false; fi
out=$(BACKLOG_DRY_RUN=1 mcp-server-steam-backlog call set_lane 'games=["Hades"]' lane=none)
echo "$out" | grep -q '"outcome": "dry_run"'

STEP='run as the user directly'
su-exec mcp-steam-backlog mcp-server-steam-backlog call overview | grep -q '"games": 49'

STEP='the user cannot read the backlog data'
mkdir -p /home/steam && chmod 700 /home/steam
if su-exec mcp-steam-backlog ls /home/steam >/dev/null 2>&1; then false; fi

STEP='service start'
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
STEP_LOG=/var/log/mcp-server-steam-backlog/server.log
rc-service mcp-server-steam-backlog start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8486/healthz && break; sleep 0.2; done
rc-service mcp-server-steam-backlog status

STEP='service user'
# shellcheck disable=SC2009
pid=$(ps -o pid,args | grep '[m]cp-server-steam-backlog serve --http' | awk 'NR==1 {print $1}')
[ -n "$pid" ] && [ "$(stat -c %U "/proc/$pid")" = mcp-steam-backlog ]

STEP='mcp over http'
URL=http://127.0.0.1:8486/mcp/backlog
H='-H Content-Type:application/json -H Accept:application/json,text/event-stream'
# shellcheck disable=SC2086
SID=$(curl -fsS -D - -o /dev/null $H "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
	| tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id" {print $2}')
[ -n "$SID" ]
# shellcheck disable=SC2086
curl -fsS $H -H "mcp-session-id: $SID" "$URL" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
# shellcheck disable=SC2086
out=$(curl -fsS $H -H "mcp-session-id: $SID" "$URL" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"backlog_get_overview","arguments":{}}}')
echo "$out" | tail -c 400
echo "$out" | grep -q '"games":49'

STEP='access from the env file'
cat >>$ETC/env <<'ENV'
MCP_PUBLIC_HOSTS=mcp.example.com
CF_ACCESS_TEAM_DOMAIN=example.cloudflareaccess.com
CF_ACCESS_AUD=smoke-test-aud
ENV
rc-service mcp-server-steam-backlog restart
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8486/healthz && break; sleep 0.2; done
# shellcheck disable=SC2086
code=$(curl -sS -o /dev/null -w '%{http_code}' $H "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
[ "$code" = 403 ]
STEP_LOG=''

rc-service mcp-server-steam-backlog stop
echo "Alpine smoke test passed"
