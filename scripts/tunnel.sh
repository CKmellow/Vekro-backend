#!/usr/bin/env bash
# Public HTTPS tunnel to the local backend so Daraja can post STK callbacks (docs/demo-modes.md).
# The hostname comes from DARAJA_CALLBACK_URL in .env (an ngrok static domain), so the tunnel and
# the URL sent to Daraja always agree. Needs `ngrok config add-authtoken <token>` once.
set -euo pipefail

cd "$(dirname "$0")/.."
port="${APP_PORT:-8000}"
callback="$(grep -E '^DARAJA_CALLBACK_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
host="$(printf '%s' "$callback" | sed -E 's#^https://([^/]+)/.*$#\1#')"

if [[ -z "$callback" || "$host" == "$callback" ]]; then
  echo "Set DARAJA_CALLBACK_URL=https://<your-static-domain>/payments/mpesa/callback in .env" >&2
  exit 2
fi
if ! command -v ngrok >/dev/null; then
  echo "ngrok is not installed (https://ngrok.com/download)." >&2
  exit 2
fi

echo "Tunnel https://${host} -> http://localhost:${port}"
exec ngrok http "$port" --url "https://${host}"
