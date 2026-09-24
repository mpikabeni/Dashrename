FROM aiogram/telegram-bot-api:latest AS telegram_api

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TELEGRAM_HTTP_PORT=8081
ENV TELEGRAM_LOCAL=true
ENV TELEGRAM_WORK_DIR=/var/lib/telegram-bot-api
ENV TELEGRAM_TEMP_DIR=/var/lib/telegram-bot-api/temp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=telegram_api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
COPY . .
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh \
    && mkdir -p /var/lib/telegram-bot-api/temp /tmp/dash

EXPOSE 10000
EXPOSE 8081

CMD ["/app/start.sh"]
