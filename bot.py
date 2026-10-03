import os
import sqlite3
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# 2 GiB. Telegram Local Bot API supports uploads up to 2000 MB.
MAX_BYTES = 2 * 1024 * 1024 * 1024

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
TMP_DIR = DATA_DIR / "tmp"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "dash.sqlite3"

LOCAL_API = os.getenv("TELEGRAM_LOCAL_API", "true").lower() in {"1", "true", "yes"}
API_BASE_URL = os.getenv(
    "TELEGRAM_API_BASE_URL", "http://127.0.0.1:8081/bot"
)
API_FILE_BASE_URL = os.getenv(
    "TELEGRAM_API_FILE_BASE_URL",
    "http://127.0.0.1:8081/file/bot",
)

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv"
}

# Short-lived in-memory sessions. The permanent thumbnail is stored in SQLite.
sessions = {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_thumbnails (
            user_id INTEGER PRIMARY KEY,
            file_id TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def set_thumbnail(user_id: int, file_id: str):
    conn = db()
    conn.execute(
        """
        INSERT INTO user_thumbnails(user_id, file_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET file_id=excluded.file_id,
                      updated_at=excluded.updated_at
        """,
        (user_id, file_id, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_thumbnail(user_id: int):
    conn = db()
    row = conn.execute(
        "SELECT file_id FROM user_thumbnails WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def clean_name(name: str) -> str:
    name = Path(name or "file").name.replace("\x00", "").strip()
    return name[:240] or "file"


def extension(name: str) -> str:
    suffix = Path(name).suffix
    return suffix[1:].upper() if suffix else "N/A"


def is_video_document(name: str, mime: str) -> bool:
    suffix = Path(name).suffix.lower()
    return mime.startswith("video/") or suffix in VIDEO_EXTENSIONS


def get_media(message):
    if message.video:
        m = message.video
        return {
            "kind": "video",
            "file_id": m.file_id,
            "name": clean_name(m.file_name or "video.mp4"),
            "size": m.file_size or 0,
            "mime": m.mime_type or "video/mp4",
        }

    if message.document:
        m = message.document
        return {
            "kind": "document",
            "file_id": m.file_id,
            "name": clean_name(m.file_name or "file"),
            "size": m.file_size or 0,
            "mime": m.mime_type or "application/octet-stream",
        }

    return None


def action_keyboard(session_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ Renommer", callback_data=f"rename|{session_id}"
                ),
                InlineKeyboardButton(
                    "ℹ️ Infos", callback_data=f"info|{session_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📁 Document", callback_data=f"output|document|{session_id}"
                ),
                InlineKeyboardButton(
                    "🎬 Vidéo", callback_data=f"output|video|{session_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Annuler", callback_data=f"cancel|{session_id}"
                )
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⚡ DASH RENAMER\n\n"
        "Envoie une vidéo ou un fichier jusqu'à 2 Go.\n\n"
        "📸 Chaque photo envoyée devient automatiquement ta miniature "
        "permanente et remplace la précédente."
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.effective_message.photo[-1]

    set_thumbnail(user_id, photo.file_id)

    await update.effective_message.reply_text(
        "🖼️ MINIATURE ENREGISTRÉE\n\n"
        "Cette photo est maintenant ta miniature permanente.\n"
        "Une prochaine photo la remplacera automatiquement."
    )


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    media = get_media(message)

    if not media:
        return

    size = media["size"]

    if size > MAX_BYTES:
        await message.reply_text(
            "❌ FICHIER TROP VOLUMINEUX\n\n"
            f"📦 Taille : {human_size(size)}\n"
            "🚫 Maximum autorisé : 2.00 GB"
        )
        return

    session_id = f"{message.chat_id}:{message.message_id}"

    sessions[session_id] = {
        **media,
        "chat_id": message.chat_id,
        "user_id": update.effective_user.id,
        "waiting_name": False,
        "created_at": time.time(),
    }

    title = "🎬 MEDIA INFO" if media["kind"] == "video" else "📄 MEDIA INFO"

    text = (
        f"{title}\n\n"
        f"📁 OLD FILE NAME\n{media['name']}\n\n"
        f"🏷 EXTENSION\n{extension(media['name'])}\n\n"
        f"💾 FILE SIZE\n{human_size(size)}\n\n"
        f"🧬 MIME TYPE\n{media['mime']}\n\n"
        "✏️ Choisis une action."
    )

    await message.reply_text(
        text,
        reply_markup=action_keyboard(session_id),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_id = update.effective_user.id

    pending = [
        (sid, session)
        for sid, session in sessions.items()
        if session["user_id"] == user_id and session["waiting_name"]
    ]

    if not pending:
        return

    session_id, session = pending[-1]
    new_name = clean_name(message.text)

    if "." not in new_name:
        old_suffix = Path(session["name"]).suffix
        new_name += old_suffix

    session["name"] = new_name
    session["waiting_name"] = False

    await message.reply_text(
        "🎯 NOM ENREGISTRÉ\n\n"
        f"📁 {new_name}\n\n"
        "📤 Comment veux-tu recevoir le fichier ?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📁 Document",
                        callback_data=f"output|document|{session_id}",
                    ),
                    InlineKeyboardButton(
                        "🎬 Vidéo",
                        callback_data=f"output|video|{session_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "❌ Annuler",
                        callback_data=f"cancel|{session_id}",
                    )
                ],
            ]
        ),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")

    if parts[0] == "cancel":
        session_id = parts[1]
        sessions.pop(session_id, None)
        await query.message.edit_text("❌ Opération annulée.")
        return

    if parts[0] == "rename":
        session_id = parts[1]
        session = sessions.get(session_id)

        if not session:
            await query.answer("Session expirée.", show_alert=True)
            return

        session["waiting_name"] = True

        await query.message.reply_text(
            "✏️ PLEASE ENTER THE NEW FILE NAME\n\n"
            "Envoie maintenant le nouveau nom avec son extension."
        )
        return

    if parts[0] == "info":
        session_id = parts[1]
        session = sessions.get(session_id)

        if not session:
            await query.answer("Session expirée.", show_alert=True)
            return

        await query.message.reply_text(
            "ℹ️ MEDIA INFO\n\n"
            f"📁 {session['name']}\n"
            f"💾 {human_size(session['size'])}\n"
            f"🏷 {extension(session['name'])}\n"
            f"🧬 {session['mime']}"
        )
        return

    if parts[0] == "output":
        output = parts[1]
        session_id = parts[2]
        session = sessions.get(session_id)

        if not session:
            await query.answer("Session expirée.", show_alert=True)
            return

        # This is Telegram media-type selection, not transcoding.
        # Only a document identified as a video can be sent as a video.
        if output == "video" and session["kind"] == "document":
            if not is_video_document(session["name"], session["mime"]):
                await query.answer(
                    "Ce fichier n'est pas identifié comme une vidéo.",
                    show_alert=True,
                )
                return

        await process_send(query.message, session_id, output)


async def process_send(message, session_id: str, output: str):
    session = sessions.get(session_id)

    if not session:
        await message.reply_text("❌ Session expirée.")
        return

    status = await message.reply_text("📤 Préparation du fichier...")

    local_path = None
    try:
        tg_file = await message.get_bot().get_file(session["file_id"])

        # With the Local Bot API, getFile returns a local path.
        # download_to_drive() can therefore copy it to our own working path.
        local_path = TMP_DIR / f"{message.chat_id}_{session_id.replace(':', '_')}"
        result = await tg_file.download_to_drive(custom_path=local_path)

        if result and Path(result).exists():
            local_path = Path(result)

        actual_size = local_path.stat().st_size
        if actual_size > MAX_BYTES:
            raise RuntimeError("Le fichier dépasse la limite de 2 Go.")

        new_name = clean_name(session["name"])

        await status.edit_text(
            "📤 Uploading file...\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "Préparation terminée.\n"
            "━━━━━━━━━━━━━━━━"
        )

        # In this first clean version, video/document are two Telegram
        # output types for the same bytes. No transcoding is performed.
        with local_path.open("rb") as stream:
            if output == "video":
                await message.get_bot().send_video(
                    chat_id=message.chat_id,
                    video=stream,
                    caption=f"📁 {new_name}",
                    supports_streaming=True,
                )
            else:
                await message.get_bot().send_document(
                    chat_id=message.chat_id,
                    document=stream,
                    caption=f"📁 {new_name}",
                )

        await status.edit_text("✅ Fichier envoyé avec succès.")

    except Exception as exc:
        await status.edit_text(
            "❌ Erreur pendant le traitement.\n\n"
            f"{type(exc).__name__}: {exc}"
        )

    finally:
        if local_path:
            try:
                Path(local_path).unlink(missing_ok=True)
            except Exception:
                pass

        sessions.pop(session_id, None)


async def ignored_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Audio, voice and animations are intentionally not processed.
    return


def build_application():
    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_API:
        builder = (
            builder
            .base_url(API_BASE_URL)
            .base_file_url(API_FILE_BASE_URL)
            .local_mode(True)
        )

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.ALL, media_handler)
    )
    app.add_handler(
        MessageHandler(
            filters.AUDIO | filters.VOICE | filters.ANIMATION,
            ignored_media,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    return app


def main():
    app = build_application()

    port = int(os.getenv("PORT", "8080"))
    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if render_url:
        webhook_url = f"{render_url.rstrip('/')}/telegram"
        print(f"Starting webhook on port {port}: {webhook_url}")

        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=webhook_url,
        )
    else:
        print("Starting polling")
        app.run_polling()


if __name__ == "__main__":
    main()
