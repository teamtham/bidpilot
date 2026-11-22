#!/bin/bash
# Start (or reuse) everything BidPilot needs on this Mac:
#   1. the Cloudflare quick tunnel for the buyer chat (public address)
#   2. the ngrok tunnel for the QwenWork callback (fixed address)
#   3. the BidPilot server, with --public-origin set to the chat address
# Healthy pieces are reused. Everything runs detached from this terminal and
# logs to ~/.bidpilot/logs/. Safe to run again at any time.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/.bidpilot"; LOGS="$STATE/logs"; mkdir -p "$LOGS"
ORIGIN_FILE="$STATE/current_chat_origin"
NGROK_DOMAIN="geek-twisty-fade.ngrok-free.dev"
NGROK_POLICY="$STATE/ngrok-callback-policy.yml"
AGENT="com.bidpilot.server"

alive() { curl -s -m 10 -o /dev/null -w "%{http_code}" "$1" 2>/dev/null; }

# 1. Chat tunnel: reuse the recorded address if cloudflared is running and it answers.
origin=""; [[ -s "$ORIGIN_FILE" ]] && origin="$(tr -d '[:space:]' < "$ORIGIN_FILE")"
if [[ -n "$origin" ]] && pgrep -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765" >/dev/null \
   && [[ "$(alive "$origin/inquire")" == "200" || "$(alive "$origin/inquire")" == "502" ]]; then
  echo "Chat tunnel: reusing $origin"
  new_origin="$origin"
else
  pkill -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765" 2>/dev/null || true
  : > "$LOGS/cloudflared.log"
  nohup cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765 >> "$LOGS/cloudflared.log" 2>&1 < /dev/null &
  disown || true
  new_origin=""
  for _ in $(seq 1 45); do
    new_origin="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOGS/cloudflared.log" | head -1)"
    [[ -n "$new_origin" ]] && break; sleep 1
  done
  [[ -z "$new_origin" ]] && { echo "Could not start the chat tunnel; see $LOGS/cloudflared.log"; exit 1; }
  echo "Chat tunnel: started $new_origin"
fi
changed=0; [[ "$new_origin" != "$origin" ]] && changed=1
printf '%s\n' "$new_origin" > "$ORIGIN_FILE"
BIDPILOT_NOTIFY=0 /bin/bash "$HERE/deploy/write_links.sh"   # refresh links with the new address now

# 2. QwenWork callback tunnel (fixed ngrok address, callback path only).
if pgrep -f "ngrok http 8765" >/dev/null; then
  echo "QwenWork callback: reusing https://$NGROK_DOMAIN"
elif command -v ngrok >/dev/null; then
  nohup ngrok http 8765 --url "https://$NGROK_DOMAIN" --traffic-policy-file "$NGROK_POLICY" \
    --log stdout >> "$LOGS/ngrok.log" 2>&1 < /dev/null &
  disown || true
  echo "QwenWork callback: started https://$NGROK_DOMAIN"
else
  echo "QwenWork callback: ngrok is not installed, skipped"
fi

# 3. Server: launchd keeps it alive; restart it when the chat address changed.
APP_DIR="$( { tr -d '\n' < "$STATE/app_dir"; } 2>/dev/null || true)"
if [[ -n "$APP_DIR" && "$APP_DIR" != "$HERE/BidPilot" && "$APP_DIR" != "$HERE" ]]; then
  echo "Note: the live server runs $APP_DIR (set in ~/.bidpilot/app_dir), not this folder."
  echo "      To run this folder instead: $HERE/deploy/install_launchd.sh"
fi
if launchctl print "gui/$(id -u)/$AGENT" >/dev/null 2>&1; then
  if [[ $changed == 1 || "$(alive http://127.0.0.1:8765/inquire)" != "200" ]]; then
    launchctl kickstart -k "gui/$(id -u)/$AGENT"; echo "Server: restarted by launchd"
  else
    echo "Server: running (launchd)"
  fi
else
  if [[ $changed == 1 || "$(alive http://127.0.0.1:8765/inquire)" != "200" ]]; then
    pkill -f "server.py --port 8765" 2>/dev/null || true; sleep 1
    nohup /bin/bash "$HERE/deploy/run_server.sh" >> "$LOGS/server.log" 2>&1 < /dev/null &
    disown || true
    echo "Server: started (not managed by launchd)"
  else
    echo "Server: running"
  fi
fi
for _ in $(seq 1 20); do [[ "$(alive http://127.0.0.1:8765/inquire)" == "200" ]] && break; sleep 1; done

echo
echo "================================================================"
echo " Chat address:   $new_origin"
echo " Iframe URL:     $new_origin/inquire"
[[ $changed == 1 ]] && echo " (NEW address: update the Hashmeta request-a-quote iframe)"
echo " Owner link:     http://127.0.0.1:8765/?owner=$(tr -d '[:space:]' < "$STATE/owner_token" 2>/dev/null)"
echo " Logs:           $LOGS"
echo " All links:      ~/.bidpilot/links.html (and links.txt, Desktop copy)"
echo "================================================================"
