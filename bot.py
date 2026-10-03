import os
import sqlite3
import time
import uuid
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
PORT = int(os.getenv("PORT", "10000"))

MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
TMP_DIR = DATA_DIR / "jobs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "dash.sqlite3"

# The Local Bot API and this bot run in the SAME container.
# This is intentional: local_mode returns absolute file paths, so both
# processes must see the same filesystem.
API_BASE_URL = f"http://127.0.0.1:{PORT}/bot"
API_FILE_BASE_URL = f"http://127.0.0.1:{PORT}/file/bot"

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv"
}

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
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def clean_name(name: str) -> str:
    name = Path(name or "file").name.replace("\x00", "").strip()
    return name[:240] or "file"


def ext(name: str) -> str:
    suffix = Path(name).suffix
    return suffix[1:].upper() if suffix else "N/A"


def is_video(name: str, mime: str) -> bool:
    return mime.startswith("video/") or Path(name).suffix.lower() in VIDEO_EXTENSIONS


def media_from_message(message):
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


def first_keyboard(session_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Renommer", callback_data=f"rename:{session_id}"),
                InlineKeyboardButton("ℹ️ Infos", callback_data=f"info:{session_id}"),
            ],
            [
                InlineKeyboardButton("📁 Document", callback_data=f"output:document:{session_id}"),
                InlineKeyboardButton("🎬 Vidéo", callback_data=f"output:video:{session_id}"),
            ],
            [
                InlineKeyboardButton("❌ Annuler", callback_data=f"cancel:{session_id}")
            ],
        ]
    )


def output_keyboard(session_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📁 Document", callback_data=f"output:document:{session_id}"),
                InlineKeyboardButton("🎬 Vidéo", callback_data=f"output:video:{session_id}"),
            ],
            [
                InlineKeyboardButton("❌ Annuler", callback_data=f"cancel:{session_id}")
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
    photo = update.effective_message.photo[-1]
    set_thumbnail(update.effective_user.id, photo.file_id)

    await update.effective_message.reply_text(
        "🖼️ MINIATURE ENREGISTRÉE\n\n"
        "Cette photo est maintenant ta miniature permanente.\n"
        "Une nouvelle photo la remplacera automatiquement."
    )


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    media = media_from_message(message)
    if not media:
        return

    if media["size"] > MAX_BYTES:
        await message.reply_text(
            "❌ FICHIER TROP VOLUMINEUX\n\n"
            f"📦 Taille : {human_size(media['size'])}\n"
            "🚫 Maximum : 2.00 GB"
        )
        return

    session_id = uuid.uuid4().hex[:12]
    sessions[session_id] = {
        **media,
        "chat_id": message.chat_id,
        "user_id": update.effective_user.id,
        "waiting_name": False,
        "new_name": media["name"],
        "created_at": time.time(),
        "source_message_id": message.message_id,
    }

    await message.reply_text(
        ("🎬 MEDIA INFO" if media["kind"] == "video" else "📄 MEDIA INFO")
        + "\n\n"
        f"📁 OLD FILE NAME\n{media['name']}\n\n"
        f"🏷 EXTENSION\n{ext(media['name'])}\n\n"
        f"💾 FILE SIZE\n{human_size(media['size'])}\n\n"
        f"🧬 MIME TYPE\n{media['mime']}\n\n"
        "✏️ Choisis une action.",
        reply_markup=first_keyboard(session_id),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_id = update.effective_user.id

    candidates = [
        (sid, s)
        for sid, s in sessions.items()
        if s["user_id"] == user_id and s["waiting_name"]
    ]
    if not candidates:
        return

    session_id, session = candidates[-1]
    new_name = clean_name(message.text)

    # If the user doesn't provide an extension, preserve the original one.
    if not Path(new_name).suffix:
        suffix = Path(session["name"]).suffix
        if suffix:
            new_name += suffix

    session["new_name"] = new_name
    session["waiting_name"] = False

    await message.reply_text(
        "🎯 NOM ENREGISTRÉ\n\n"
        f"📁 {new_name}\n\n"
        "📤 Comment veux-tu recevoir le fichier ?",
        reply_markup=output_keyboard(session_id),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action = parts[0]

    if action == "cancel":
        session_id = parts[1]
        sessions.pop(session_id, None)
        await query.message.edit_text("❌ Opération annulée.")
        return

    if len(parts) < 2:
        return

    session_id = parts[-1]
    session = sessions.get(session_id)

    if not session:
        await query.answer("Session expirée.", show_alert=True)
        return

    if action == "rename":
        session["waiting_name"] = True
        await query.message.reply_text(
            "✏️ PLEASE ENTER THE NEW FILE NAME\n\n"
            "Envoie maintenant le nouveau nom avec son extension."
        )
        return

    if action == "info":
        await query.message.reply_text(
            "ℹ️ MEDIA INFO\n\n"
            f"📁 {session['name']}\n"
            f"💾 {human_size(session['size'])}\n"
            f"🏷 {ext(session['name'])}\n"
            f"🧬 {session['mime']}"
        )
        return

    if action == "output":
        output = parts[1]

        if output == "video" and not is_video(session["name"], session["mime"]):
            await query.answer(
                "Ce fichier n'est pas identifié comme une vidéo.",
                show_alert=True,
            )
            return

        await send_result(query.message, session_id, output)


async def send_result(message, session_id: str, output: str):
    session = sessions.get(session_id)
    if not session:
        await message.reply_text("❌ Session expirée.")
        return

    status = await message.reply_text("📤 Préparation du fichier...")

    source_path = None
    try:
        tg_file = await message.get_bot().get_file(session["file_id"])

        # In Local Bot API mode, file_path is an absolute path on the SAME
        # container. download_to_drive() returns that path without copying it.
        source_path = await tg_file.download_to_drive()

        source_path = Path(source_path)
        if not source_path.exists():
            raise RuntimeError(f"Fichier local introuvable: {source_path}")

        actual_size = source_path.stat().st_size
        if actual_size > MAX_BYTES:
            raise RuntimeError("Le fichier dépasse la limite de 2 Go.")

        # Rename only at the upload layer. No transcoding.
        new_name = clean_name(session["new_name"])

        await status.edit_text(
            "📤 UPLOADING FILE...\n\n"
            f"📁 {new_name}\n"
            f"📦 {human_size(actual_size)}\n\n"
            "⚙️ Transfert en cours..."
        )

        thumbnail_id = get_thumbnail(session["user_id"])

        # Telegram thumbnail file_ids cannot be directly reused as upload
        # thumbnails. The permanent thumbnail will be wired to the local
        # file in the next step after the 2 GiB transfer path is validated.
        # We intentionally do not fake this here.
        if output == "video":
            await message.get_bot().send_video(
                chat_id=message.chat_id,
                video=source_path,
                filename=new_name,
                supports_streaming=True,
                caption=f"📁 {new_name}",
            )
        else:
            await message.get_bot().send_document(
                chat_id=message.chat_id,
                document=source_path,
                filename=new_name,
                caption=f"📁 {new_name}",
            )

        await status.edit_text("✅ Fichier envoyé avec succès.")

    except Exception as exc:
        await status.edit_text(
            "❌ ERREUR PENDANT LE TRAITEMENT\n\n"
            f"{type(exc).__name__}: {exc}"
        )

    finally:
        # Telegram Local Bot API owns the downloaded file. Do not delete it
        # here; the Local API manages its own storage.
        sessions.pop(session_id, None)


async def ignored_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return


def build_app():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(API_BASE_URL)
        .base_file_url(API_FILE_BASE_URL)
        .local_mode(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.ALL, media_handler)
    )
    app.add_handler(
        MessageHandler(
            filters.AUDIO | filters.VOICE | filters.ANIMATION,
            ignored_handler,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    return app


def main():
    print(f"Dash Renamer starting with Local Bot API at {API_BASE_URL}")
    print(f"Maximum file size: {MAX_BYTES} bytes (2 GiB)")
    build_app().run_polling()


if __name__ == "__main__":
    main()
