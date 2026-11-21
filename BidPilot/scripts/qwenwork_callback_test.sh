#!/bin/bash
# Usage: curltest.sh <base-url> <inquiry_id> [job_id]
URL="$1/qwenwork/callback"; INQ="$2"; JOB="${3:-curl-test-$(date +%s)}"
SECRET=$(tr -d '\n' < ~/.bidpilot/qwenwork_callback_secret)
BODY="{\"job_id\":\"$JOB\",\"status\":\"completed\",\"inquiry_id\":\"$INQ\",\"result\":{\"draft_body\":\"Test draft from curl\"}}"
TS=$(date +%s)
SIG="sha256=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')"
curl -sS -X POST "$URL" -H "Content-Type: application/json" \
  -H "X-BidPilot-Timestamp: $TS" -H "X-BidPilot-Signature: $SIG" -d "$BODY" -w "\nHTTP %{http_code}\n"
