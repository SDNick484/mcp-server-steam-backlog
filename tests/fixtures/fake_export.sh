#!/bin/sh
# A stand-in for run-export.sh in the contract tests (backlog_server.py --export-cmd).
# It takes the export lock and prints the exporter's real messages (contract §4.7), then
# rewrites the library atomically with a new exportedAt, as the real exporter does (§5.1).
# It reaches nothing on the network and needs no keys.
set -eu
OUT="${EXPORT_DIR:?EXPORT_DIR is set by backlog_server.py}"
exec 9>"$OUT/.export.lock"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  echo "$(date '+%F %T') skipped: another export is running"
  exit 75
fi
echo "$(date '+%F %T') starting export"
sleep 0.3
echo "7656119XXXXXXXXXX: 49 games"
echo "Steam Deck compatibility: fetching 3 games..."
echo "  3/3"
sleep 0.3
echo "ProtonDB: everything cached."
python3 - "$OUT/steam_backlog.json" <<'PY'
import datetime, json, os, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["exportedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
with open(p + ".tmp", "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
os.replace(p + ".tmp", p)
PY
echo "Wrote $OUT/steam_backlog.json: 49 games, 30 never launched, 45 with HLTB times."
