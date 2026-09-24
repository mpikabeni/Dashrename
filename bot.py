import os
import re
import json
import shutil
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# 2. DANS bot.py
# REMPLACE L'IMPORT DE config PAR CELUI-CI
# ============================================================

from config import (
    BOT_TOKEN,
    NEXA_CHANNEL,
    NEXA_URL,
    ADMIN_IDS,
    TEMP_ROOT,
    MAX_DOWNLOAD_BYTES,
    MAX_PROCESS_SECONDS,
    TELEGRAM_API_BASE_URL,
    TELEGRAM_API_FILE_BASE_URL,
)

# ============================================================
# DASH FILEBOT
# CREATED BY NEXA
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "@Nexa_CG").strip()
NEXA_URL = os.getenv("NEXA_URL", "https://t.me/Nexa_CG").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

TEMP_ROOT = os.getenv("TEMP_ROOT", tempfile.gettempdir())
MAX_DOWNLOAD_BYTES = int(
    os.getenv("MAX_DOWNLOAD_BYTES", str(20 * 1024 * 1024))
)
MAX_PROCESS_SECONDS = int(
    os.getenv("MAX_PROCESS_SECONDS", "1800")
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN est manquant.")

# ============================================================
# DATABASE
# ============================================================

DB_FILE = os.getenv(
    "DASH_DB",
    str(Path(__file__).resolve().parent / "dash.db")
)

import sqlite3


def db():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    connection = db()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            old_name TEXT,
            new_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.commit()
    connection.close()


def save_user(user):
    connection = db()

    connection.execute("""
        INSERT INTO users (
            user_id,
            username,
            first_name
        )
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=CURRENT_TIMESTAMP
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
    ))

    connection.commit()
    connection.close()


def get_users():
    connection = db()
    rows = connection.execute(
        "SELECT user_id FROM users"
    ).fetchall()
    connection.close()
    return [row["user_id"] for row in rows]


def save_history(user_id, old_name, new_name):
    connection = db()

    connection.execute("""
        INSERT INTO history (
            user_id,
            old_name,
            new_name
        )
        VALUES (?, ?, ?)
    """, (
        user_id,
        old_name,
        new_name,
    ))

    connection.commit()
    connection.close()


def user_count():
    connection = db()
    value = connection.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]
    connection.close()
    return value


# ============================================================
# GLOBAL STATE
# ============================================================

sessions = {}

stats = {
    "files": 0,
    "renames": 0,
    "errors": 0,
    "broadcasts": 0,
}

# ============================================================
# HELPERS
# ============================================================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_size(size):
    if not size:
        return "Inconnue"

    if size < 1024:
        return f"{size} B"

    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"

    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"

    return f"{size / 1024 ** 3:.2f} GB"


def format_duration(seconds):
    if seconds is None:
        return None

    seconds = int(seconds)

    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    return f"{minutes:02d}:{seconds:02d}"


def safe_filename(name, fallback="file"):
    name = Path(name or fallback).name

    name = re.sub(
        r"[\x00-\x1f\x7f]",
        "",
        name
    )

    name = name.replace("/", "_")
    name = name.replace("\\", "_")
    name = name.strip(" .")

    if not name:
        name = fallback

    return name[:180]


def temp_dir(prefix="dash_"):
    return tempfile.mkdtemp(
        prefix=prefix,
        dir=TEMP_ROOT
    )


def cleanup(path):
    if path:
        shutil.rmtree(
            path,
            ignore_errors=True
        )


def extension(filename):
    return Path(filename).suffix.lower()


async def safe_edit(message, text, **kwargs):
    try:
        await message.edit_text(
            text,
            **kwargs
        )
    except TelegramError:
        pass


# ============================================================
# NEXA MEMBERSHIP
# ============================================================


async def is_nexa_member(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            NEXA_CHANNEL,
            user_id
        )

        return member.status in {
            "creator",
            "administrator",
            "member",
        }

    except TelegramError as error:
        print(
            "NEXA MEMBERSHIP ERROR:",
            repr(error)
        )
        return False


def subscription_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Rejoindre NEXA",
                url=NEXA_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Vérifier",
                callback_data="verify_nexa"
            )
        ]
    ])


async def require_nexa(update, context):
    user_id = update.effective_user.id

    if is_admin(user_id):
        return True

    if await is_nexa_member(
        context,
        user_id
    ):
        return True

    text = (
        "🔐 <b>Accès réservé aux membres de NEXA</b>\n\n"
        "Pour utiliser Dash, rejoins d'abord notre canal "
        "NEXA puis appuie sur <b>Vérifier</b>."
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=subscription_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=subscription_keyboard()
        )

    return False


# ============================================================
# FILE DETECTION
# ============================================================


def get_file_info(message):
    if message.document:
        file = message.document

        return {
            "kind": "document",
            "file_id": file.file_id,
            "name": file.file_name or "document",
            "size": file.file_size or 0,
            "mime": file.mime_type or "",
            "width": None,
            "height": None,
            "duration": None,
        }

    if message.video:
        file = message.video

        return {
            "kind": "video",
            "file_id": file.file_id,
            "name": file.file_name or "video.mp4",
            "size": file.file_size or 0,
            "mime": file.mime_type or "video",
            "width": file.width,
            "height": file.height,
            "duration": file.duration,
        }

    if message.audio:
        file = message.audio

        return {
            "kind": "audio",
            "file_id": file.file_id,
            "name": file.file_name or "audio.mp3",
            "size": file.file_size or 0,
            "mime": file.mime_type or "audio",
            "width": None,
            "height": None,
            "duration": file.duration,
        }

    if message.animation:
        file = message.animation

        return {
            "kind": "animation",
            "file_id": file.file_id,
            "name": file.file_name or "animation.gif",
            "size": file.file_size or 0,
            "mime": "animation",
            "width": file.width,
            "height": file.height,
            "duration": file.duration,
        }

    return None


# ============================================================
# USER SESSION
# ============================================================


def get_session(user_id):
    return sessions.get(user_id)


def clear_session(user_id):
    sessions.pop(user_id, None)


# ============================================================
# KEYBOARDS
# ============================================================


def file_keyboard(data):
    rows = [
        [
            InlineKeyboardButton(
                "✏️ Renommer",
                callback_data="rename"
            ),
            InlineKeyboardButton(
                "ℹ️ Infos",
                callback_data="info"
            ),
        ]
    ]

    if data["kind"] == "video":

        rows.append([
            InlineKeyboardButton(
                "🖼️ Miniature",
                callback_data="thumbnail"
            ),
            InlineKeyboardButton(
                "📦 Compresser",
                callback_data="compress"
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            "🗑️ Annuler",
            callback_data="cancel"
        )
    ])

    return InlineKeyboardMarkup(rows)


def rename_type_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📄 Document",
                callback_data="rename_type:document"
            ),
            InlineKeyboardButton(
                "🎬 Vidéo",
                callback_data="rename_type:video"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Annuler",
                callback_data="cancel"
            )
        ]
    ])


def thumbnail_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🖼️ Envoyer une image",
                callback_data="thumb_upload"
            )
        ],
        [
            InlineKeyboardButton(
                "🎞️ Extraire une image",
                callback_data="thumb_extract"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Annuler",
                callback_data="cancel"
            )
        ]
    ])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 Statistiques",
                callback_data="admin_stats"
            ),
            InlineKeyboardButton(
                "👥 Utilisateurs",
                callback_data="admin_users"
            )
        ],
        [
            InlineKeyboardButton(
                "📢 Annonce",
                callback_data="admin_broadcast"
            ),
            InlineKeyboardButton(
                "🧹 Nettoyage",
                callback_data="admin_cleanup"
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Configuration",
                callback_data="admin_config"
            )
        ]
    ])


# ============================================================
# COMMAND MENU
# ============================================================


async def setup_commands(application):
    commands = [
        BotCommand("start", "Démarrer Dash"),
        BotCommand("menu", "Menu principal"),
        BotCommand("rename", "Renommer un fichier"),
        BotCommand("thumbnail", "Créer une miniature"),
        BotCommand("history", "Historique"),
        BotCommand("settings", "Paramètres"),
        BotCommand("status", "État du bot"),
        BotCommand("about", "À propos"),
        BotCommand("help", "Aide"),
        BotCommand("cancel", "Annuler"),
        BotCommand("admin", "Administration"),
    ]

    await application.bot.set_my_commands(
        commands
    )


# ============================================================
# START
# ============================================================


async def start(update, context):
    save_user(update.effective_user)

    text = (
        "⚡ <b>DASH FILEBOT</b>\n\n"
        "Bienvenue sur Dash.\n\n"
        "Envoie-moi simplement un fichier et je pourrai "
        "le renommer rapidement.\n\n"
        "✏️ Renommer\n"
        "🖼️ Miniature vidéo\n"
        "📦 Compression\n"
        "ℹ️ Informations\n\n"
        "<b>Propulsé par NEXA</b>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📁 Envoyer un fichier",
                callback_data="send_file"
            )
        ],
        [
            InlineKeyboardButton(
                "❓ Aide",
                callback_data="help"
            )
        ]
    ])

    image = (
        Path(__file__).resolve().parent
        / "dash.png"
    )

    if image.exists():

        try:
            with image.open("rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            return

        except TelegramError as error:
            print(
                "WELCOME IMAGE ERROR:",
                repr(error)
            )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ============================================================
# MENU
# ============================================================


async def menu_command(update, context):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ Renommer",
                callback_data="rename_command"
            )
        ],
        [
            InlineKeyboardButton(
                "🖼️ Miniature",
                callback_data="thumbnail_command"
            )
        ],
        [
            InlineKeyboardButton(
                "📜 Historique",
                callback_data="history"
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Paramètres",
                callback_data="settings"
            )
        ],
        [
            InlineKeyboardButton(
                "❓ Aide",
                callback_data="help"
            )
        ]
    ])

    await update.message.reply_text(
        "⚡ <b>MENU DASH</b>\n\n"
        "Choisis une fonction :",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ============================================================
# RECEIVE FILE
# ============================================================


   # ============================================================
# 3. REMPLACE LA FONCTION receive_file()
# ============================================================

async def receive_file(update, context):

    save_user(update.effective_user)

    message = update.message
    user_id = update.effective_user.id

    data = get_file_info(message)

    if not data:
        return

    # Ne bloque plus automatiquement à 20 MB.
    # Si un Bot API cloud est utilisé et que Telegram refuse
    # le téléchargement d'un gros fichier, l'erreur sera traitée
    # proprement plus bas.

    sessions[user_id] = {
        **data,
        "chat_id": update.effective_chat.id,
        "message_id": message.message_id,
        "caption": message.caption or "",
        "thumbnail_file_id": None,
    }

    stats["files"] += 1

    text = (
        "╭────────────────────────╮\n"
        "│       ⚡ <b>DASH</b>        │\n"
        "╰────────────────────────╯\n\n"
        f"📄 <b>Nom :</b> "
        f"<code>{safe_filename(data['name'])}</code>\n"
        f"💾 <b>Taille :</b> "
        f"{format_size(data['size'])}\n"
    )

    if data.get("duration") is not None:
        text += (
            f"⏱️ <b>Durée :</b> "
            f"{format_duration(data['duration'])}\n"
        )

    if data.get("width") and data.get("height"):
        text += (
            f"📐 <b>Résolution :</b> "
            f"{data['width']} × {data['height']}\n"
        )

    if data.get("mime"):
        text += (
            f"🔎 <b>Type :</b> "
            f"{data['mime']}\n"
        )

    text += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "Choisis une action :"
    )

    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=file_keyboard(data)
    )

# ============================================================
# RECEIVE PHOTO AS THUMBNAIL
# ============================================================


async def receive_photo(update, context):
    save_user(update.effective_user)

    user_id = update.effective_user.id
    photo = update.message.photo[-1]

    sessions[user_id] = {
        "thumbnail_file_id": photo.file_id,
        "thumbnail_message_id": update.message.message_id,
        "thumbnail_chat_id": update.effective_chat.id,
    }

    await update.message.reply_text(
        "🖼️ <b>Miniature enregistrée</b>\n\n"
        "Cette image sera utilisée comme miniature "
        "de la prochaine vidéo que tu m'enverras.\n\n"
        "🎬 Envoie maintenant ta vidéo.",
        parse_mode="HTML"
    )


# ============================================================
# DOWNLOAD
# ============================================================


async def download_file(
    context,
    file_id,
    filename,
    directory
):
    filename = safe_filename(filename)

    destination = os.path.join(
        directory,
        filename
    )

    telegram_file = await context.bot.get_file(
        file_id
    )

    await telegram_file.download_to_drive(
        destination
    )

    if not os.path.exists(destination):
        raise RuntimeError(
            "Le téléchargement Telegram a échoué."
        )

    return destination


# ============================================================
# SEND FILE
# ============================================================


async def send_result(
    update,
    session,
    path,
    filename,
    mode="document",
    thumbnail=None,
    caption=""
):
    chat = update.effective_chat

    caption = caption[:1024]

    if mode == "video":

        kwargs = {
            "video": open(path, "rb"),
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": True,
        }

        if thumbnail and os.path.exists(thumbnail):
            kwargs["thumbnail"] = open(
                thumbnail,
                "rb"
            )

        try:
            await chat.send_video(
                **kwargs
            )
        finally:
            kwargs["video"].close()

            if "thumbnail" in kwargs:
                kwargs["thumbnail"].close()

        return

    with open(path, "rb") as document:
        await chat.send_document(
            document=document,
            filename=filename,
            caption=caption,
            parse_mode="HTML"
        )


# ============================================================
# RENAME
# ============================================================


async def rename_command(update, context):
    if not await require_nexa(
        update,
        context
    ):
        return

    user_id = update.effective_user.id

    session = get_session(user_id)

    if not session:
        await update.message.reply_text(
            "📁 Envoie d'abord un fichier."
        )
        return

    session["waiting_name"] = True

    await update.message.reply_text(
        "✏️ <b>Nouveau nom</b>\n\n"
        "Envoie le nouveau nom du fichier.\n\n"
        "Exemple : <code>Mon_Video_2026</code>",
        parse_mode="HTML"
    )


# ============================================================
# PROCESS RENAME
# ============================================================


async def process_rename(
    update,
    context,
    new_name
):
    user_id = update.effective_user.id
    session = get_session(user_id)

    if not session:
        await update.message.reply_text(
            "⚠️ Aucun fichier actif."
        )
        return

    new_name = safe_filename(
        new_name
    )

    original_ext = extension(
        session["name"]
    )

    if not Path(new_name).suffix:
        new_name += original_ext

    mode = session.get(
        "rename_mode",
        "document"
    )

    directory = temp_dir(
        "dash_rename_"
    )

    progress = None

    try:
        progress = await update.message.reply_text(
            "⚙️ <b>DASH</b>\n\n"
            "▰▱▱▱▱▱▱▱▱▱ 10%\n"
            "🔍 Analyse...",
            parse_mode="HTML"
        )

        source = await download_file(
            context,
            session["file_id"],
            session["name"],
            directory
        )

        await safe_edit(
            progress,
            "⚙️ <b>DASH</b>\n\n"
            "▰▰▰▰▱▱▱▱▱▱ 40%\n"
            "📦 Préparation...",
            parse_mode="HTML"
        )

        output = os.path.join(
            directory,
            new_name
        )

        os.replace(
            source,
            output
        )

        thumbnail = None

        if (
            session.get("thumbnail_file_id")
            and session["kind"] == "video"
        ):
            thumbnail = os.path.join(
                directory,
                "thumbnail.jpg"
            )

            thumb_file = await context.bot.get_file(
                session["thumbnail_file_id"]
            )

            await thumb_file.download_to_drive(
                thumbnail
            )

        await safe_edit(
            progress,
            "⚙️ <b>DASH</b>\n\n"
            "▰▰▰▰▰▰▰▱▱▱ 70%\n"
            "📤 Préparation de l'envoi...",
            parse_mode="HTML"
        )

        await send_result(
            update,
            session,
            output,
            new_name,
            mode=mode,
            thumbnail=thumbnail,
            caption=(
                f"✏️ <b>{new_name}</b>\n"
                "⚡ Propulsé par NEXA"
            )
        )

        save_history(
            user_id,
            session["name"],
            new_name
        )

        stats["renames"] += 1

        await safe_edit(
            progress,
            "⚡ <b>DASH</b>\n\n"
            "▰▰▰▰▰▰▰▰▰▰ 100%\n"
            "✅ <b>Renommage terminé</b>",
            parse_mode="HTML"
        )

        clear_session(user_id)

    except Exception as error:

        stats["errors"] += 1

        await update.message.reply_text(
            "❌ <b>Une erreur est survenue.</b>\n\n"
            f"<code>{str(error)[:1000]}</code>",
            parse_mode="HTML"
        )

    finally:
        cleanup(directory)


# ============================================================
# THUMBNAIL
# ============================================================


async def thumbnail_command(update, context):
    if not await require_nexa(
        update,
        context
    ):
        return

    await update.message.reply_text(
        "🖼️ <b>Miniature DASH</b>\n\n"
        "Envoie une image maintenant.\n\n"
        "Elle sera utilisée comme miniature "
        "de ta prochaine vidéo.",
        parse_mode="HTML"
    )


# ============================================================
# ADMIN
# ============================================================


async def admin_command(update, context):
    if not is_admin(
        update.effective_user.id
    ):
        await update.message.reply_text(
            "⛔ Accès refusé."
        )
        return

    await update.message.reply_text(
        "👑 <b>DASH ADMIN</b>\n\n"
        "Bienvenue dans le panneau administrateur.",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


async def admin_stats(update, context):
    await update.callback_query.answer()

    await update.callback_query.message.reply_text(
        "📊 <b>STATISTIQUES DASH</b>\n\n"
        f"👥 Utilisateurs : "
        f"{user_count()}\n"
        f"📁 Fichiers : "
        f"{stats['files']}\n"
        f"✏️ Renommages : "
        f"{stats['renames']}\n"
        f"📢 Diffusions : "
        f"{stats['broadcasts']}\n"
        f"❌ Erreurs : "
        f"{stats['errors']}",
        parse_mode="HTML"
    )


# ============================================================
# BROADCAST
# ============================================================


async def broadcast_command(update, context):
    if not is_admin(
        update.effective_user.id
    ):
        return

    context.user_data[
        "broadcast_waiting"
    ] = True

    await update.message.reply_text(
        "📢 <b>Nouvelle annonce</b>\n\n"
        "Envoie maintenant le message, "
        "la photo, la vidéo, le document, "
        "l'audio ou le GIF à diffuser.\n\n"
        "Pour annuler : /cancel",
        parse_mode="HTML"
    )


async def broadcast_message(
    context,
    source_message
):
    users = get_users()

    sent = 0
    failed = 0

    for user_id in users:

        try:
            await source_message.copy(
                chat_id=user_id
            )

            sent += 1

        except Exception as error:

            failed += 1

            print(
                "BROADCAST ERROR",
                user_id,
                repr(error)
            )

        await asyncio.sleep(
            0.05
        )

    stats["broadcasts"] += 1

    return sent, failed


# ============================================================
# HISTORY
# ============================================================


async def history_command(update, context):
    user_id = update.effective_user.id

    connection = db()

    rows = connection.execute("""
        SELECT old_name, new_name, created_at
        FROM history
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
    """, (
        user_id,
    )).fetchall()

    connection.close()

    if not rows:
        await update.message.reply_text(
            "📜 Ton historique est vide."
        )
        return

    text = (
        "📜 <b>TON HISTORIQUE</b>\n\n"
    )

    for row in rows:
        text += (
            f"• <code>{row['old_name']}</code>\n"
            f"  ➜ <code>{row['new_name']}</code>\n\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# ============================================================
# SETTINGS / STATUS / ABOUT / HELP
# ============================================================


async def settings_command(update, context):
    await update.message.reply_text(
        "⚙️ <b>PARAMÈTRES</b>\n\n"
        "🔔 Notifications : Telegram\n"
        "🌍 Langue : Français\n"
        "🎨 Interface : Dash\n\n"
        "D'autres paramètres seront disponibles "
        "dans les prochaines versions.",
        parse_mode="HTML"
    )


async def status_command(update, context):
    ffmpeg = shutil.which(
        "ffmpeg"
    )

    await update.message.reply_text(
        "⚡ <b>DASH STATUS</b>\n\n"
        f"FFmpeg : "
        f"{'✅ Disponible' if ffmpeg else '❌ Absent'}\n"
        f"Utilisateurs : "
        f"{user_count()}\n"
        f"Sessions actives : "
        f"{len(sessions)}\n"
        f"Fichiers traités : "
        f"{stats['files']}\n"
        f"Renommages : "
        f"{stats['renames']}",
        parse_mode="HTML"
    )


async def about_command(update, context):
    await update.message.reply_text(
        "⚡ <b>DASH FILEBOT</b>\n\n"
        "Un gestionnaire de fichiers Telegram "
        "créé par <b>NEXA</b>.\n\n"
        "✏️ Renommage\n"
        "🖼️ Miniatures\n"
        "📦 Traitement de fichiers\n"
        "⚡ Interface rapide\n\n"
        "© NEXA",
        parse_mode="HTML"
    )


async def help_command(update, context):
    await update.message.reply_text(
        "❓ <b>AIDE DASH</b>\n\n"
        "/start — Accueil\n"
        "/menu — Menu\n"
        "/rename — Renommer\n"
        "/thumbnail — Miniature\n"
        "/history — Historique\n"
        "/settings — Paramètres\n"
        "/status — État du bot\n"
        "/about — À propos\n"
        "/help — Aide\n"
        "/cancel — Annuler\n"
        "/admin — Administration\n\n"
        "Tu peux également envoyer directement "
        "une vidéo ou un document.",
        parse_mode="HTML"
    )


async def cancel_command(update, context):
    user_id = update.effective_user.id

    sessions.pop(
        user_id,
        None
    )

    context.user_data.clear()

    await update.message.reply_text(
        "🗑️ <b>Opération annulée.</b>",
        parse_mode="HTML"
    )


# ============================================================
# TEXT ROUTER
# ============================================================


async def receive_text(update, context):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if context.user_data.get(
        "broadcast_waiting"
    ):

        if not is_admin(user_id):
            return

        context.user_data[
            "broadcast_waiting"
        ] = False

        sent, failed = await broadcast_message(
            context,
            update.message
        )

        await update.message.reply_text(
            "📢 <b>Diffusion terminée</b>\n\n"
            f"✅ Envoyés : {sent}\n"
            f"❌ Échecs : {failed}",
            parse_mode="HTML"
        )

        return

    session = get_session(user_id)

    if session and session.get(
        "waiting_name"
    ):

        if not await require_nexa(
            update,
            context
        ):
            return

        session["waiting_name"] = False
        session["new_name"] = text

        await update.message.reply_text(
            "📁 <b>Choisis le format de sortie</b>",
            parse_mode="HTML",
            reply_markup=rename_type_keyboard()
        )

        return


# ============================================================
# CALLBACKS
# ============================================================


async def callbacks(update, context):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "verify_nexa":

        if await is_nexa_member(
            context,
            user_id
        ) or is_admin(user_id):

            await query.edit_message_text(
                "✅ <b>Abonnement vérifié !</b>\n\n"
                "Tu peux maintenant utiliser Dash.",
                parse_mode="HTML"
            )

        else:

            await query.answer(
                "❌ Tu n'es pas encore membre de NEXA.",
                show_alert=True
            )

        return

    if data == "cancel":

        clear_session(user_id)

        await query.edit_message_text(
            "🗑️ <b>Session annulée.</b>",
            parse_mode="HTML"
        )

        return

    if data == "send_file":

        await query.message.reply_text(
            "📁 Envoie maintenant ton fichier."
        )

        return

    if data == "help":

        await query.message.reply_text(
            "❓ Utilise /help pour voir toutes "
            "les fonctions de Dash."
        )

        return

    if data == "rename_command":

        if not await require_nexa(
            update,
            context
        ):
            return

        session = get_session(user_id)

        if not session:

            await query.message.reply_text(
                "📁 Envoie d'abord le fichier "
                "que tu veux renommer."
            )

            return

        session["waiting_name"] = True

        await query.message.reply_text(
            "✏️ Envoie le nouveau nom du fichier."
        )

        return

    if data == "thumbnail_command":

        await query.message.reply_text(
            "🖼️ Envoie une image pour "
            "l'utiliser comme miniature "
            "de ta prochaine vidéo."
        )

        return

    if data == "rename":

        if not await require_nexa(
            update,
            context
        ):
            return

        session = get_session(user_id)

        if not session:

            await query.message.reply_text(
                "⚠️ Aucun fichier actif."
            )

            return

        session["waiting_name"] = True

        await query.message.reply_text(
            "✏️ <b>Quel nouveau nom veux-tu ?</b>\n\n"
            "Envoie uniquement le nouveau nom.",
            parse_mode="HTML"
        )

        return

    if data.startswith(
        "rename_type:"
    ):

        if not await require_nexa(
            update,
            context
        ):
            return

        session = get_session(user_id)

        if not session:
            return

        mode = data.split(
            ":",
            1
        )[1]

        session["rename_mode"] = mode

        new_name = session.get(
            "new_name"
        )

        if not new_name:
            await query.message.reply_text(
                "⚠️ Nom manquant."
            )
            return

        await query.message.reply_text(
            "⚙️ Traitement du fichier..."
        )

        await process_rename(
            update,
            context,
            new_name
        )

        return

    if data == "info":

        session = get_session(user_id)

        if not session:
            return

        text = (
            "ℹ️ <b>INFORMATIONS</b>\n\n"
            f"📄 Nom : "
            f"<code>{safe_filename(session['name'])}</code>\n"
            f"💾 Taille : "
            f"{format_size(session['size'])}\n"
            f"🔎 Type : "
            f"{session.get('mime') or 'Inconnu'}\n"
        )

        if session.get("duration"):
            text += (
                f"⏱️ Durée : "
                f"{format_duration(session['duration'])}\n"
            )

        if (
            session.get("width")
            and session.get("height")
        ):
            text += (
                f"📐 Résolution : "
                f"{session['width']} × "
                f"{session['height']}\n"
            )

        await query.message.reply_text(
            text,
            parse_mode="HTML"
        )

        return

    if data == "thumbnail":

        if not await require_nexa(
            update,
            context
        ):
            return

        session = get_session(user_id)

        if not session:
            return

        if session["kind"] != "video":

            await query.message.reply_text(
                "🖼️ Les miniatures sont "
                "disponibles pour les vidéos."
            )

            return

        await query.message.reply_text(
            "🖼️ <b>MINIATURE</b>\n\n"
            "Envoie une image maintenant.\n\n"
            "Elle sera appliquée à ta vidéo.",
            parse_mode="HTML"
        )

        session["waiting_thumbnail"] = True

        return

    if data == "compress":

        await query.message.reply_text(
            "📦 La compression avancée sera "
            "activée dans le module de traitement."
        )

        return

    if data == "history":

        await history_command(
            update,
            context
        )

        return

    if data == "settings":

        await settings_command(
            update,
            context
        )

        return

    if data == "admin_stats":

        if not is_admin(user_id):
            return

        await admin_stats(
            update,
            context
        )

        return

    if data == "admin_users":

        if not is_admin(user_id):
            return

        await query.message.reply_text(
            "👥 <b>UTILISATEURS</b>\n\n"
            f"Nombre total : <b>{user_count()}</b>",
            parse_mode="HTML"
        )

        return

    if data == "admin_broadcast":

        if not is_admin(user_id):
            return

        context.user_data[
            "broadcast_waiting"
        ] = True

        await query.message.reply_text(
            "📢 <b>Annonce</b>\n\n"
            "Envoie maintenant le texte, "
            "la photo, la vidéo, le document, "
            "l'audio ou le GIF à diffuser.",
            parse_mode="HTML"
        )

        return

    if data == "admin_cleanup":

        if not is_admin(user_id):
            return

        removed = 0

        root = Path(TEMP_ROOT)

        if root.exists():

            for item in root.iterdir():

                if (
                    item.is_dir()
                    and item.name.startswith("dash_")
                ):

                    shutil.rmtree(
                        item,
                        ignore_errors=True
                    )

                    removed += 1

        await query.message.reply_text(
            f"🧹 Nettoyage terminé.\n\n"
            f"Dossiers supprimés : {removed}"
        )

        return

    if data == "admin_config":

        if not is_admin(user_id):
            return

        await query.message.reply_text(
            "⚙️ <b>CONFIGURATION</b>\n\n"
            f"NEXA : {NEXA_CHANNEL}\n"
            f"Temporaire : {TEMP_ROOT}\n"
            f"Limite actuelle : "
            f"{format_size(MAX_DOWNLOAD_BYTES)}",
            parse_mode="HTML"
        )

        return


# ============================================================
# ERROR HANDLER
# ============================================================


async def error_handler(update, context):
    stats["errors"] += 1

    print(
        "DASH ERROR:",
        repr(context.error)
    )


# ============================================================
# POST INIT
# ============================================================


async def post_init(application):
    init_db()

    await setup_commands(
        application
    )

    print(
        "========================================"
    )

    print(
        "DASH FILEBOT STARTED"
    )

    print(
        "Created by NEXA"
    )

    print(
        "========================================"
    )


# ============================================================
# MAIN
# ============================================================


def main():

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "menu",
            menu_command
        )
    )

    application.add_handler(
        CommandHandler(
            "rename",
            rename_command
        )
    )

    application.add_handler(
        CommandHandler(
            "thumbnail",
            thumbnail_command
        )
    )

    application.add_handler(
        CommandHandler(
            "history",
            history_command
        )
    )

    application.add_handler(
        CommandHandler(
            "settings",
            settings_command
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command
        )
    )

    application.add_handler(
        CommandHandler(
            "about",
            about_command
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            receive_photo
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.ANIMATION,
            receive_file
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            receive_text
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    application.add_error_handler(
        error_handler
    )

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    webhook_url = os.getenv(
        "WEBHOOK_URL",
        ""
    ).strip()

    if webhook_url:

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=(
                webhook_url.rstrip("/")
                + "/telegram"
            ),
            drop_pending_updates=True
        )

    else:

        application.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()
