# config.py

import os
from pathlib import Path


# ============================================================
# DASH CONFIGURATION
# CREATED BY NEXA
# ============================================================

BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# TELEGRAM
# ============================================================

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    ""
).strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN est manquant dans les variables Render."
    )


NEXA_CHANNEL = os.getenv(
    "NEXA_CHANNEL",
    "@Nexa_CG"
).strip()


NEXA_URL = os.getenv(
    "NEXA_URL",
    "https://t.me/Nexa_CG"
).strip()


# ============================================================
# ADMIN
# ============================================================

def parse_admin_ids(value):
    result = set()

    for item in value.split(","):
        item = item.strip()

        if item.isdigit():
            result.add(int(item))

    return result


ADMIN_IDS = parse_admin_ids(
    os.getenv(
        "ADMIN_IDS",
        ""
    )
)


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ============================================================
# SERVER
# ============================================================

PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)


WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    ""
).strip()


WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH",
    "telegram"
).strip("/")


# ============================================================
# 1. DANS config.py
# REMPLACE LA PARTIE MAX_DOWNLOAD_BYTES PAR CECI
# ============================================================

TELEGRAM_API_BASE_URL = os.getenv(
    "TELEGRAM_API_BASE_URL",
    ""
).strip()

TELEGRAM_API_FILE_BASE_URL = os.getenv(
    "TELEGRAM_API_FILE_BASE_URL",
    ""
).strip()

# IMPORTANT :
# Ne bloque plus les fichiers à 20 MB côté application.
# La taille réellement possible dépend du Bot API utilisé.
MAX_DOWNLOAD_BYTES = int(
    os.getenv(
        "MAX_DOWNLOAD_BYTES",
        str(2 * 1024 * 1024 * 1024)
    )
)



# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    ""
).strip()


DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    str(BASE_DIR / "dash.db")
)


# ============================================================
# BACKBLAZE B2
# ============================================================

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
    "dash/source/"
).strip("/")


B2_OUTPUT_PREFIX = os.getenv(
    "B2_OUTPUT_PREFIX",
    "dash/output/"
).strip("/")


B2_THUMBNAIL_PREFIX = os.getenv(
    "B2_THUMBNAIL_PREFIX",
    "dash/thumbnails/"
).strip("/")


def b2_is_configured():
    return all([
        B2_ENABLED,
        B2_KEY_ID,
        B2_APPLICATION_KEY,
        B2_BUCKET,
        B2_ENDPOINT,
    ])


# ============================================================
# PROCESSING
# ============================================================

FFMPEG_BIN = os.getenv(
    "FFMPEG_BIN",
    "ffmpeg"
).strip()


FFPROBE_BIN = os.getenv(
    "FFPROBE_BIN",
    "ffprobe"
).strip()


# ============================================================
# THUMBNAILS
# ============================================================

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


THUMBNAIL_QUALITY = int(
    os.getenv(
        "THUMBNAIL_QUALITY",
        "4"
    )
)


# ============================================================
# BOT INFORMATION
# ============================================================

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


# ============================================================
# CLEANUP
# ============================================================

CLEANUP_ENABLED = (
    os.getenv(
        "CLEANUP_ENABLED",
        "true"
    ).lower()
    in {
        "1",
        "true",
        "yes",
        "on"
    }
)


CLEANUP_HOURS = int(
    os.getenv(
        "CLEANUP_HOURS",
        "6"
    )
)


# ============================================================
# BROADCAST
# ============================================================

BROADCAST_DELAY = float(
    os.getenv(
        "BROADCAST_DELAY",
        "0.05"
    )
)


# ============================================================
# SECURITY
# ============================================================

MAX_FILENAME_LENGTH = int(
    os.getenv(
        "MAX_FILENAME_LENGTH",
        "180"
    )
)


ALLOWED_FILENAME_CHARS = (
    r"[^a-zA-Z0-9À-ÿ._() \-]"
)


# ============================================================
# HELPERS
# ============================================================

def get_webhook_url():

    if not WEBHOOK_URL:
        return ""

    return (
        WEBHOOK_URL.rstrip("/")
        + "/"
        + WEBHOOK_PATH
    )


def validate_config():

    errors = []

    if not BOT_TOKEN:
        errors.append(
            "BOT_TOKEN"
        )

    if not NEXA_CHANNEL:
        errors.append(
            "NEXA_CHANNEL"
        )

    if errors:
        raise RuntimeError(
            "Variables manquantes : "
            + ", ".join(errors)
        )

    return True


validate_config()
