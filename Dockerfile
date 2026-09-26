FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ============================================================
# DEPENDANCES SYSTEME
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    cmake \
    g++ \
    make \
    pkg-config \
    zlib1g-dev \
    libssl-dev \
    gperf \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# TELEGRAM LOCAL BOT API
# ============================================================
WORKDIR /tmp

RUN git clone --recursive --depth 1 \
    https://github.com/tdlib/telegram-bot-api.git

WORKDIR /tmp/telegram-bot-api

RUN mkdir -p build \
    && cd build \
    && cmake \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        .. \
    && cmake --build . --target install -j2 \
    && strip /usr/local/bin/telegram-bot-api

# Verification
RUN test -x /usr/local/bin/telegram-bot-api

# ============================================================
# PROJET DASH
# ============================================================
WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ============================================================
# DOSSIERS
# ============================================================
RUN mkdir -p \
    /tmp/dash \
    /var/lib/telegram-bot-api \
    /var/lib/telegram-bot-api/temp

# ============================================================
# VERIFICATION TELEGRAM API
# ============================================================
RUN ls -lh /usr/local/bin/telegram-bot-api

# ============================================================
# START SCRIPT
# ============================================================
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh

# Render
EXPOSE 10000

# Telegram Local Bot API
EXPOSE 8081

CMD ["/app/start.sh"]
