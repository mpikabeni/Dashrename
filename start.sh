#!/bin/sh
set -eu
mkdir -p /app/data
if [ "${TELEGRAM_LOCAL_API:-true}" = "true" ]; then
  : "${TELEGRAM_API_ID:?TELEGRAM_API_ID required}"
  : "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH required}"
  telegram-bot-api --api-id="${TELEGRAM_API_ID}" --api-hash="${TELEGRAM_API_HASH}" --local --http-port=8081 --http-ip-address=127.0.0.1 --dir=/app/data &
  i=0
  until python -c 'import socket; s=socket.create_connection(("127.0.0.1",8081),2); s.close()' >/dev/null 2>&1; do
    i=$((i+1)); [ "$i" -ge 40 ] && { echo 'Local API failed to start'; exit 1; }
    sleep 2
  done
fi
exec python bot.py
