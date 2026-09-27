#!/bin/sh
set -e

echo "=========================================="
echo "🚀 Démarrage de Dash Renamer"
echo "=========================================="

mkdir -p /app/telegram-data
mkdir -p /app/telegram-tmp
mkdir -p /app/telegram-files
mkdir -p /app/data

chmod 777 /app/telegram-data
chmod 777 /app/telegram-tmp
chmod 777 /app/telegram-files
chmod 777 /app/data

echo "📁 Dossiers Telegram préparés."

echo "🚀 Démarrage du Telegram Local Bot API..."

telegram-bot-api \
  --dir=/app/telegram-data \
  --files-dir=/app/telegram-files \
  --temp-dir=/app/telegram-tmp \
  --http-port=8081 \
  --local &

TELEGRAM_PID=$!

echo "⏳ Attente du Local Bot API..."

for i in $(seq 1 30); do
    if wget -q -O /dev/null http://127.0.0.1:8081/ 2>/dev/null; then
        echo "✅ Local Bot API prêt."
        break
    fi

    sleep 1
done

if ! kill -0 "$TELEGRAM_PID" 2>/dev/null; then
    echo "❌ Le Local Bot API n'a pas démarré."
    exit 1
fi

echo "🤖 Démarrage de Dash Renamer..."

exec python bot.py
