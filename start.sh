#!/bin/sh
set -eu

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID est obligatoire pour le Local Bot API}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH est obligatoire pour le Local Bot API}"
: "${BOT_TOKEN:?BOT_TOKEN est obligatoire}"

API_BIN="/usr/local/bin/telegram-bot-api"

if [ ! -x "$API_BIN" ]; then
  echo "❌ Binaire Telegram Local Bot API introuvable: $API_BIN"
  echo "Contenu de /usr/local/bin:"
  ls -la /usr/local/bin || true
  exit 1
fi

mkdir -p /app/data /app/telegram-data /app/telegram-tmp /app/telegram-files
chmod 777 /app/data /app/telegram-data /app/telegram-tmp /app/telegram-files

# Le serveur officiel utilise l'utilisateur telegram-bot-api dans son image de base.
# On rend les répertoires applicatifs accessibles pour éviter les erreurs de temp-dir.
if id telegram-bot-api >/dev/null 2>&1; then
  chown -R telegram-bot-api:telegram-bot-api /app/telegram-data /app/telegram-tmp /app/telegram-files || true
fi

# Le bot Python, lui, doit pouvoir lire les fichiers téléchargés.
chmod -R a+rwX /app/telegram-data /app/telegram-tmp /app/telegram-files

echo "=========================================="
echo "🚀 Démarrage de Dash Renamer"
echo "=========================================="
echo "📦 Telegram Bot API: $API_BIN"
echo "📁 Data: /app/telegram-data"
echo "📁 Temp: /app/telegram-tmp"
echo ""

echo "🚀 Démarrage du Telegram Local Bot API..."

"$API_BIN" \
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
  wait "$API_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "⏳ Attente du Local Bot API..."
READY=0

for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:8081/bot${BOT_TOKEN}/getMe" >/tmp/dash-getme.json 2>/dev/null; then
    READY=1
    echo "✅ Local Bot API prêt."
    cat /tmp/dash-getme.json || true
    break
  fi

  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "❌ Le Local Bot API s'est arrêté. Logs :"
    cat /app/telegram-api.log || true
    exit 1
  fi

  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "❌ Impossible de joindre le Local Bot API après 90 secondes."
  cat /app/telegram-api.log || true
  exit 1
fi

export TELEGRAM_LOCAL_API=true
export TELEGRAM_API_BASE_URL="http://127.0.0.1:8081/bot"
export TELEGRAM_API_FILE_BASE_URL="http://127.0.0.1:8081/file/bot"

echo "🤖 Démarrage du bot Dash Renamer..."
exec python3 bot.py
