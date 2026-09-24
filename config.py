# config.py

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN manquant.")

NEXA_CHANNEL = os.getenv(
    "NEXA_CHANNEL",
    "@Nexa_CG"
).strip()

NEXA_URL = os.getenv(
    "NEXA_URL",
    "https://t.me/Nexa_CG"
).strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    ""
).strip()

WEBHOOK_PATH = "telegram"

TEMP_ROOT = os.getenv(
    "TEMP_ROOT",
    "/tmp/dash"
)

Path(TEMP_ROOT).mkdir(
    parents=True,
    exist_ok=True
)

MAX_PROCESS_SECONDS = int(
    os.getenv(
        "MAX_PROCESS_SECONDS",
        "7200"
    )
)

# ============================================================
# IMPORTANT :
# AUCUNE LIMITE 20 MB DANS L'APPLICATION.
#
# Pour les gros fichiers, DASH doit utiliser le Local Bot API.
# ============================================================

TELEGRAM_API_ID = os.getenv(
    "TELEGRAM_API_ID",
    ""
).strip()

TELEGRAM_API_HASH = os.getenv(
    "TELEGRAM_API_HASH",
    ""
).strip()

TELEGRAM_LOCAL_API = (
    os.getenv(
        "TELEGRAM_LOCAL_API",
        "true"
    ).lower()
    in {
        "1",
        "true",
        "yes",
        "on"
    }
)

TELEGRAM_API_BASE_URL = os.getenv(
    "TELEGRAM_API_BASE_URL",
    "http://127.0.0.1:8081/bot"
).strip()

TELEGRAM_API_FILE_BASE_URL = os.getenv(
    "TELEGRAM_API_FILE_BASE_URL",
    "http://127.0.0.1:8081/file/bot"
).strip()

DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    str(BASE_DIR / "dash.db")
)

B2_ENABLED = (
    os.getenv(
        "B2_ENABLED",
        "false"
    ).lower()
    in {
        "1",
        "true",
        "yes",
        "on"
    }
)

B2_KEY_ID = os.getenv(
    "B2_KEY_ID",
    ""
).strip()

B2_APPLICATION_KEY = os.getenv(
    "B2_APPLICATION_KEY",
    ""
).strip()

B2_BUCKET = os.getenv(
    "B2_BUCKET",
    ""
).strip()

B2_ENDPOINT = os.getenv(
    "B2_ENDPOINT",
    ""
).strip()

B2_REGION = os.getenv(
    "B2_REGION",
    "us-west-002"
).strip()

B2_SOURCE_PREFIX = os.getenv(
    "B2_SOURCE_PREFIX",
    "dash/source"
).strip("/")

B2_OUTPUT_PREFIX = os.getenv(
    "B2_OUTPUT_PREFIX",
    "dash/output"
).strip("/")

B2_THUMBNAIL_PREFIX = os.getenv(
    "B2_THUMBNAIL_PREFIX",
    "dash/thumbnails"
).strip("/")

FFMPEG_BIN = os.getenv(
    "FFMPEG_BIN",
    "ffmpeg"
).strip()

FFPROBE_BIN = os.getenv(
    "FFPROBE_BIN",
    "ffprobe"
).strip()

THUMBNAIL_WIDTH = int(
    os.getenv(
        "THUMBNAIL_WIDTH",
        "640"
    )
)

THUMBNAIL_HEIGHT = int(
    os.getenv(
        "THUMBNAIL_HEIGHT",
        "360"
    )
)

BOT_NAME = os.getenv(
    "BOT_NAME",
    "DASH FILEBOT"
).strip()

COMPANY_NAME = os.getenv(
    "COMPANY_NAME",
    "NEXA"
).strip()

WELCOME_IMAGE = os.getenv(
    "WELCOME_IMAGE",
    str(BASE_DIR / "dash.png")
)

BROADCAST_DELAY = float(
    os.getenv(
        "BROADCAST_DELAY",
        "0.05"
    )
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def local_api_enabled() -> bool:
    return (
        TELEGRAM_LOCAL_API
        and bool(TELEGRAM_API_ID)
        and bool(TELEGRAM_API_HASH)
    )


def webhook_url() -> str:
    if not WEBHOOK_URL:
        return ""

    return (
        WEBHOOK_URL.rstrip("/")
        + "/"
        + WEBHOOK_PATH
    )
