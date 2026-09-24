# ============================================================
# DASH - Telegram Local Bot API + Python
# ============================================================

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ------------------------------------------------------------
# Dépendances système
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# Compiler Telegram Local Bot API
# ------------------------------------------------------------
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

# Vérification obligatoire
RUN test -x /usr/local/bin/telegram-bot-api \
    && /usr/local/bin/telegram-bot-api --help >/dev/null 2>&1 || true

# ------------------------------------------------------------
# Revenir au projet Dash
# ------------------------------------------------------------
WORKDIR /app

# ------------------------------------------------------------
# Python
# ------------------------------------------------------------
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------
# Projet
# ------------------------------------------------------------
COPY . .

# ------------------------------------------------------------
# Dossiers nécessaires
# ------------------------------------------------------------
RUN mkdir -p \
    /tmp/dash \
    /var/lib/telegram-bot-api \
    /var/lib/telegram-bot-api/temp

# ------------------------------------------------------------
# Vérifier que le binaire existe
# ------------------------------------------------------------
RUN ls -lh /usr/local/bin/telegram-bot-api \
    && /usr/local/bin/telegram-bot-api --help >/dev/null 2>&1 || true

# ------------------------------------------------------------
# Script de démarrage
# ------------------------------------------------------------
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh

# Render utilise ce port pour Dash
EXPOSE 10000

# Local Telegram Bot API
EXPOSE 8081

CMD ["/app/start.sh"]
