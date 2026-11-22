#!/bin/bash
# Runs the BidPilot server in the foreground (launchd keeps it alive).
# The public chat address comes from ~/.bidpilot/current_chat_origin, which
# start_all.sh writes; the owner token stays in ~/.bidpilot/owner_token.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
# QwenWork OpenAPI event runtime: buyer chat/brief/compose run in cloud sessions.
# Missing OAuth token keeps the server up; the dashboard shows "not connected".
export BIDPILOT_QWENWORK_EVENT_RUNTIME=1
# Buyer chats queue for the QwenWork cloud runner (not the local Gemini adapter).
export BIDPILOT_STUDIO_ASSISTANT=qwenwork
ORIGIN_FILE="$HOME/.bidpilot/current_chat_origin"
PY="${BIDPILOT_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
ARGS=(-u -B server.py --port 8765
      --embed-origin "https://alex-studio.geo.hashmeta.tech,https://sh5gnpnc.qwenwork.page"
      --qwenwork-secret-file "$HOME/.bidpilot/qwenwork_callback_secret")
# The one live database: BIDPILOT_DB, else ~/.bidpilot/db_path (set by
# deploy/install_launchd.sh --db), else bidpilot.db next to server.py.
DB="${BIDPILOT_DB:-$( { tr -d '\n' < "$HOME/.bidpilot/db_path"; } 2>/dev/null || true)}"
[[ -n "$DB" ]] && ARGS+=(--db "$DB")
if [[ -s "$ORIGIN_FILE" ]]; then
  ARGS+=(--public-origin "$(tr -d '[:space:]' < "$ORIGIN_FILE")")
fi
# Write every link the owner needs, and announce them, each time it starts.
/bin/bash "$HERE/deploy/write_links.sh" || true
cd "$HERE"
exec "$PY" "${ARGS[@]}"
