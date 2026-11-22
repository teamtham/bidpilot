#!/bin/bash
# Make this checkout the one the live BidPilot server runs, and (re)install the
# launchd agent. The folder is kept in ONE place, ~/.bidpilot/app_dir; the
# agent, start_all.sh and the links file all read it from there.
#
#   deploy/install_launchd.sh                 # run this checkout
#   deploy/install_launchd.sh --db PATH       # also set the live database file
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/.bidpilot"; mkdir -p "$STATE/logs"
if [[ "${1:-}" == "--db" ]]; then
  [[ -f "${2:-}" ]] || { echo "No database at ${2:-<missing>}"; exit 1; }
  printf '%s\n' "$(cd "$(dirname "$2")" && pwd)/$(basename "$2")" > "$STATE/db_path"
fi
printf '%s\n' "$HERE" > "$STATE/app_dir"
AGENT="$HOME/Library/LaunchAgents/com.bidpilot.server.plist"
sed "s#__HOME__#$HOME#g" "$HERE/deploy/launchd/com.bidpilot.server.plist" > "$AGENT.tmp"
plutil -lint "$AGENT.tmp" >/dev/null && mv "$AGENT.tmp" "$AGENT"
U="gui/$(id -u)"
launchctl bootout "$U/com.bidpilot.server" 2>/dev/null || true
sleep 1
launchctl bootstrap "$U" "$AGENT"
echo "Live server now runs: $HERE"
echo "Database: $(cat "$STATE/db_path" 2>/dev/null || echo "$HERE/bidpilot.db (default)")"
