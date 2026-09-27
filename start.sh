#!/bin/sh
set -eu

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID est obligatoire pour le Local Bot API}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH est obligatoire pour le Local Bot API}"
: "${BOT_TOKEN:?BOT_TOKEN est obligatoire}"

mkdir -p /app/data /app/telegram-data /app/telegram-tmp

echo "[Dash] Démarrage du Telegram Local Bot API..."

telegram-bot-api \
  --api-id="$TELEGRAM_API_ID" \
  --api-hash="$TELEGRAM_API_HASH" \
  --local \
  --http-port=8081 \
  --dir=/app/telegram-data \
  --temp-dir=/app/telegram-tmp \
  > /app/telegram-api.log 2>&1 &

API_PID=$!

cleanup() {
  echo "[Dash] Arrêt du Local Bot API..."
  kill "$API_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8081/bot"$BOT_TOKEN"/getMe >/tmp/dash-getme.json 2>/dev/null; then
    echo "[Dash] Local Bot API prêt."
    break
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "[Dash] Le Local Bot API s'est arrêté. Logs :"
    cat /app/telegram-api.log || true
    exit 1
  fi
  sleep 1
done

if ! curl -fsS http://127.0.0.1:8081/bot"$BOT_TOKEN"/getMe >/tmp/dash-getme.json 2>/dev/null; then
  echo "[Dash] Impossible de joindre le Local Bot API après 60 secondes."
  cat /app/telegram-api.log || true
  exit 1
fi

export TELEGRAM_LOCAL_API=true
export TELEGRAM_API_BASE_URL="http://127.0.0.1:8081/bot"
export TELEGRAM_API_FILE_BASE_URL="http://127.0.0.1:8081/file/bot"

echo "[Dash] Démarrage du bot Dash Renamer..."
python3 bot.py
