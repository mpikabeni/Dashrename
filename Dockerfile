FROM aiogram/telegram-bot-api:latest

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TELEGRAM_WORK_DIR=/app/telegram-data \
    TELEGRAM_TEMP_DIR=/app/telegram-tmp \
    TELEGRAM_HTTP_PORT=8081 \
    TELEGRAM_LOCAL=1

# The base image already contains the official Telegram Bot API binary
# at /usr/local/bin/telegram-bot-api. We explicitly reset the inherited
# entrypoint so Dash can run the API server and the Python bot together.
RUN apk add --no-cache python3 py3-pip curl ca-certificates \
    && mkdir -p /app/data /app/telegram-data /app/telegram-tmp /app/telegram-files \
    && chmod 777 /app/data /app/telegram-data /app/telegram-tmp /app/telegram-files \
    && test -x /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY bot.py .
COPY dash.png .
COPY start.sh .
RUN chmod +x /app/start.sh

EXPOSE 10000

ENTRYPOINT ["/bin/sh"]
CMD ["/app/start.sh"]
