FROM aiogram/telegram-bot-api:latest

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TELEGRAM_WORK_DIR=/app/telegram-data \
    TELEGRAM_TEMP_DIR=/app/telegram-tmp \
    TELEGRAM_HTTP_PORT=8081 \
    TELEGRAM_LOCAL=1

RUN apk add --no-cache python3 py3-pip curl ca-certificates \
    && mkdir -p /app /app/data /app/telegram-data /app/telegram-tmp

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY bot.py .
COPY dash.png .
COPY start.sh .
RUN chmod +x /app/start.sh

EXPOSE 10000

CMD ["/app/start.sh"]
