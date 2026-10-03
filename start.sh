#!/bin/sh
set -eu

: "${BOT_TOKEN:?BOT_TOKEN is required}"
: "${TELEGRAM_API_ID:?TELEGRAM_API_ID is required}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is required}"

PORT="${PORT:-10000}"

mkdir -p /app/data /app/tgdata /app/tgtmp

echo "======================================"
echo " DASH RENAMER"
echo " Local Bot API: ${PORT}"
echo " Maximum file: 2 GiB"
echo "======================================"

telegram-bot-api \
  --api-id="${TELEGRAM_API_ID}" \
  --api-hash="${TELEGRAM_API_HASH}" \
  --local \
  --http-port="${PORT}" \
  --dir="/app/tgdata" \
  --temp-dir="/app/tgtmp" &

API_PID=$!

cleanup() {
  echo "Stopping services..."
  kill "${API_PID}" 2>/dev/null || true
  wait "${API_PID}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "Waiting for Local Bot API..."
i=0
while [ "$i" -lt 30 ]; do
  if python3 - "$PORT" <<'PY'
import sys, urllib.request
port = sys.argv[1]
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
except Exception:
    # Any HTTP response means the server is listening. A connection refusal
    # raises before this point.
    pass
else:
    raise SystemExit(0)
raise SystemExit(1)
PY
  then
    break
  fi

  if ! kill -0 "${API_PID}" 2>/dev/null; then
    echo "ERROR: Local Bot API stopped unexpectedly."
    wait "${API_PID}" || true
    exit 1
  fi

  i=$((i + 1))
  sleep 1
done

if ! kill -0 "${API_PID}" 2>/dev/null; then
  echo "ERROR: Local Bot API is not running."
  exit 1
fi

echo "Local Bot API is running."
echo "Starting Dash Renamer..."
exec python3 /app/bot.py
