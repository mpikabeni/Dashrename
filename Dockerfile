FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY dash.png .
COPY start.sh .

RUN mkdir -p \
    /app/telegram-data \
    /app/telegram-tmp \
    /app/telegram-files \
    /app/data

RUN chmod +x /app/start.sh

EXPOSE 8081

CMD ["/app/start.sh"]
