#!/bin/bash
# Writes every BidPilot link to ~/.bidpilot/links.txt and links.html (both
# chmod 600, overwritten each run) plus a copy on the Desktop, and shows a
# macOS notification with the chat address. Run by deploy/run_server.sh on
# every server (re)start and by start_all.sh. The owner token is read from
# ~/.bidpilot/owner_token and only ever written to these local files.
set -uo pipefail
STATE="${BIDPILOT_STATE:-$HOME/.bidpilot}"
DESKTOP="${BIDPILOT_DESKTOP:-$HOME/Desktop}"
NOTIFY="${BIDPILOT_NOTIFY:-1}"
TOKEN="$( { tr -d "[:space:]" < "$STATE/owner_token"; } 2>/dev/null || true)"
CHAT="$( { tr -d "[:space:]" < "$STATE/current_chat_origin"; } 2>/dev/null || true)"
OWNER="http://127.0.0.1:8765/?owner=$TOKEN"
STAMP="$(date '+%a %d %b %Y, %H:%M')"
umask 077
NAMES=("Founder dashboard (this Mac only)" "Calendar" "Studio tools"
       "Public chat page" "Hashmeta iframe URL" "Public quote page"
       "QwenWork callback" "Hashmeta live site")
if [[ -n "$CHAT" ]]; then
  CHATP="$CHAT/inquire"; IFR="$CHAT/inquire"; QUOTE="$CHAT/quote"
else
  CHATP="not set, run start_all.sh"; IFR="$CHATP"; QUOTE="$CHATP"
fi
URLS=("$OWNER" "$OWNER#calendar" "$OWNER#tools" "$CHATP" "$IFR" "$QUOTE"
      "https://geek-twisty-fade.ngrok-free.dev/qwenwork/callback"
      "https://alex-studio.geo.hashmeta.tech/request-a-quote")
{
  echo "BidPilot links ($STAMP)"
  for i in "${!NAMES[@]}"; do printf '%-34s %s\n' "${NAMES[$i]}:" "${URLS[$i]}"; done
} > "$STATE/links.txt.tmp" && mv "$STATE/links.txt.tmp" "$STATE/links.txt"
{
  cat <<HTML
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BidPilot links</title><style>
body{margin:0;background:#f7f6f3;color:#1f1e1b;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:720px;margin:40px auto;padding:0 16px}h1{font-size:22px;margin:0 0 4px}p{color:#6b6862;margin:0 0 20px}
ul{list-style:none;padding:0;margin:0;background:#fff;border:1px solid #e4e1db;border-radius:10px}
li{padding:12px 16px;border-top:1px solid #eeebe5}li:first-child{border-top:0}
b{display:block;font-size:13px;color:#6b6862;font-weight:600}a{color:#2453c7;word-break:break-all}
</style></head><body><main><h1>BidPilot links</h1><p>Updated $STAMP. Rewritten every time BidPilot restarts.</p><ul>
HTML
  for i in "${!NAMES[@]}"; do
    u="${URLS[$i]}"
    if [[ "$u" == http* ]]; then printf '<li><b>%s</b><a href="%s">%s</a></li>\n' "${NAMES[$i]}" "$u" "$u"
    else printf '<li><b>%s</b>%s</li>\n' "${NAMES[$i]}" "$u"; fi
  done
  echo '</ul></main></body></html>'
} > "$STATE/links.html.tmp" && mv "$STATE/links.html.tmp" "$STATE/links.html"
chmod 600 "$STATE/links.txt" "$STATE/links.html"
if [[ -d "$DESKTOP" ]]; then
  cp "$STATE/links.txt" "$DESKTOP/BidPilot links.txt" 2>/dev/null && chmod 600 "$DESKTOP/BidPilot links.txt"
fi
if [[ "$NOTIFY" == 1 ]]; then
  /usr/bin/osascript -e "display notification \"Chat: ${CHAT:-not set}\" with title \"BidPilot restarted\" subtitle \"All links: ~/.bidpilot/links.html\"" >/dev/null 2>&1 || true
fi
