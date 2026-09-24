#!/bin/sh

set -eu

echo "========================================"
echo " DASH + TELEGRAM LOCAL BOT API"
echo "========================================"

# ------------------------------------------------------------
# Vérification des variables
# ------------------------------------------------------------

if [ -z "${BOT_TOKEN:-}" ]; then
    echo "ERREUR: BOT_TOKEN est manquant."
    exit 1
fi

if [ -z "${TELEGRAM_API_ID:-}" ]; then
    echo "ERREUR: TELEGRAM_API_ID est manquant."
    exit 1
fi

if [ -z "${TELEGRAM_API_HASH:-}" ]; then
    echo "ERREUR: TELEGRAM_API_HASH est manquant."
    exit 1
fi

# ------------------------------------------------------------
# Vérification du binaire
# ------------------------------------------------------------

if [ ! -x "/usr/local/bin/telegram-bot-api" ]; then
    echo "ERREUR: /usr/local/bin/telegram-bot-api introuvable."
    echo "Contenu de /usr/local/bin :"
    ls -la /usr/local/bin || true
    exit 1
fi

echo "Telegram Bot API trouve."

# ------------------------------------------------------------
# Dossiers
# ------------------------------------------------------------

mkdir -p /var/lib/telegram-bot-api
mkdir -p /var/lib/telegram-bot-api/temp
mkdir -p /tmp/dash

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

export TELEGRAM_LOCAL_API=true
export TELEGRAM_API_BASE_URL="http://127.0.0.1:8081/bot"
export TELEGRAM_API_FILE_BASE_URL="http://127.0.0.1:8081/file/bot"

# ------------------------------------------------------------
# Démarrer Telegram Local Bot API
# ------------------------------------------------------------

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

# ------------------------------------------------------------
# Nettoyage
# ------------------------------------------------------------

cleanup() {
    echo "Arret du Local Bot API..."
    kill "$API_PID" 2>/dev/null || true
}

trap cleanup INT TERM EXIT

# ------------------------------------------------------------
# Attendre le serveur Telegram
# ------------------------------------------------------------

READY=0

for i in $(seq 1 90); do

    if curl -fsS \
        "http://127.0.0.1:8081/bot${BOT_TOKEN}/getMe" \
        >/tmp/dash_api_check.json 2>/dev/null
    then
        READY=1
        break
    fi

    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo ""
        echo "ERREUR: Telegram Local Bot API s'est arrete."
        echo ""

        if [ -f /tmp/dash_api_check.json ]; then
            cat /tmp/dash_api_check.json
        fi

        exit 1
    fi

    echo "Attente du Local Bot API... ($i/90)"

    sleep 2

done

# ------------------------------------------------------------
# Vérification finale
# ------------------------------------------------------------

if [ "$READY" -ne 1 ]; then

    echo ""
    echo "ERREUR: Local Bot API non disponible apres 180 secondes."
    echo ""

    if [ -f /tmp/dash_api_check.json ]; then
        cat /tmp/dash_api_check.json
    fi

    exit 1
fi

echo ""
echo "========================================"
echo " LOCAL BOT API PRET"
echo "========================================"

cat /tmp/dash_api_check.json || true

echo ""
echo "========================================"
echo " DEMARRAGE DE DASH"
echo "========================================"

# ------------------------------------------------------------
# Démarrer Dash
# ------------------------------------------------------------

exec python bot.py
