FROM aiogram/telegram-bot-api:latest AS telegram_api
FROM python:3.13-slim
WORKDIR /app
COPY --from=telegram_api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py start.sh ./
RUN chmod +x start.sh && mkdir -p /app/data
ENV TELEGRAM_LOCAL_API=true
EXPOSE 10000
CMD ["./start.sh"]
