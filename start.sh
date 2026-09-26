#!/bin/sh
set -eu

echo "========================================"
echo " DASH + TELEGRAM LOCAL BOT API"
echo "========================================"

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "ERREUR: BOT_TOKEN manquant."
  exit 1
fi

if [ -z "${TELEGRAM_API_ID:-}" ]; then
  echo "ERREUR: TELEGRAM_API_ID manquant."
  exit 1
fi

if [ -z "${TELEGRAM_API_HASH:-}" ]; then
  echo "ERREUR: TELEGRAM_API_HASH manquant."
  exit 1
fi

if [ ! -x /usr/local/bin/telegram-bot-api ]; then
  echo "ERREUR: /usr/local/bin/telegram-bot-api introuvable."
  exit 1
fi

mkdir -p /var/lib/telegram-bot-api/temp /tmp/dash

# Telegram demande de sortir du Bot API cloud avant d'utiliser
# une instance locale avec le même bot.
echo "Deconnexion du Bot API cloud..."
curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/logOut" || true
echo ""

echo "Demarrage du Local Bot API sur 127.0.0.1:8081..."

/usr/local/bin/telegram-bot-api \
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
for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:8081/bot${BOT_TOKEN}/getMe" >/tmp/dash_api_check.json 2>/dev/null; then
    READY=1
    break
  fi

  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "ERREUR: Telegram Local Bot API s'est arrete."
    exit 1
  fi

  echo "Attente du Local Bot API... ($i/90)"
  sleep 2
done

if [ "$READY" -ne 1 ]; then
  echo "ERREUR: Local Bot API non disponible apres 180 secondes."
  cat /tmp/dash_api_check.json 2>/dev/null || true
  exit 1
fi

echo "========================================"
echo " LOCAL BOT API PRET"
echo "========================================"
cat /tmp/dash_api_check.json || true
echo ""
echo "Demarrage de Dash..."

export TELEGRAM_LOCAL_API=true
export TELEGRAM_API_BASE_URL="http://127.0.0.1:8081/bot"
export TELEGRAM_API_FILE_BASE_URL="http://127.0.0.1:8081/file/bot"

exec python bot.py
