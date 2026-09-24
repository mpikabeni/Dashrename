#!/bin/sh
set -eu

if [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ]; then
  echo "ERREUR: TELEGRAM_API_ID et TELEGRAM_API_HASH sont requis pour TELEGRAM_LOCAL_API=true."
  exit 1
fi

mkdir -p /var/lib/telegram-bot-api/temp /tmp/dash

echo "========================================"
echo " DASH + TELEGRAM LOCAL BOT API"
echo "========================================"
echo "Demarrage du Local Bot API sur 127.0.0.1:8081..."

telegram-bot-api \
  --api-id="${TELEGRAM_API_ID}" \
  --api-hash="${TELEGRAM_API_HASH}" \
  --local \
  --http-port=8081 \
  --http-ip-address=127.0.0.1 \
  --dir=/var/lib/telegram-bot-api \
  --temp-dir=/var/lib/telegram-bot-api/temp \
  --verbosity=1 &

API_PID=$!

cleanup() {
  echo "Arret du Local Bot API..."
  kill "$API_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

READY=0
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:8081/bot${BOT_TOKEN}/getMe" >/tmp/dash_api_check.json 2>/dev/null; then
    READY=1
    break
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "ERREUR: le processus Telegram Local Bot API s'est arrete."
    exit 1
  fi
  echo "Attente du Local Bot API... ($i/60)"
  sleep 2
done

if [ "$READY" -ne 1 ]; then
  echo "ERREUR: Local Bot API non disponible apres 120 secondes."
  cat /tmp/dash_api_check.json 2>/dev/null || true
  exit 1
fi

echo "Local Bot API pret."
cat /tmp/dash_api_check.json || true
echo "Demarrage de Dash..."

exec python bot.py
