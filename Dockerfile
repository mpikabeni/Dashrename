FROM aiogram/telegram-bot-api:latest

USER root

RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY bot.py .
COPY start.sh .
RUN chmod +x /app/start.sh

ENV DATA_DIR=/app/data
ENV TELEGRAM_WORK_DIR=/app/tgdata
ENV TELEGRAM_TEMP_DIR=/app/tgtmp

RUN mkdir -p /app/data /app/tgdata /app/tgtmp

EXPOSE 10000

CMD ["/app/start.sh"]
