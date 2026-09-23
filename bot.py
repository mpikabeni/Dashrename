import os
import re
import json
import shutil
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# DASH FILEBOT
# Created by NEXA
#
# Main features:
# - Automatic file detection
# - Rename
# - Video/audio/image conversion with FFmpeg
# - Compression
# - Caption editing
# - Thumbnail tools
# - File information
# - Video cutting
# - Audio extraction
# - Admin panel
# - Automatic temporary-file cleanup
#
# Required environment variables:
# BOT_TOKEN=your_telegram_bot_token
# ADMIN_IDS=123456789,987654321
#
# FFmpeg and ffprobe must be installed on the server.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

MAX_DOWNLOAD_BYTES = int(
    os.getenv("MAX_DOWNLOAD_BYTES", str(20 * 1024 * 1024))
)
MAX_PROCESS_SECONDS = int(os.getenv("MAX_PROCESS_SECONDS", "1800"))
TEMP_ROOT = os.getenv("TEMP_ROOT", tempfile.gettempdir())
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "@Nexa_CG")
CLEANUP_TZ = os.getenv("CLEANUP_TZ", "Africa/Brazzaville")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN est manquant dans les variables d'environnement.")

# Telegram Bot API cloud limitations can vary by operation/account.
# Keep the default download limit conservative. A local Bot API server
# can be used later if you need much larger files.

user_sessions: dict[int, dict] = {}
rename_waiting: set[int] = set()
caption_waiting: set[int] = set()
cut_waiting: set[int] = set()
admin_broadcast_waiting: set[int] = set()

stats = {
    "files": 0,
    "conversions": 0,
    "renames": 0,
    "compressions": 0,
    "errors": 0,
}

ALLOWED_VIDEO = {"mp4", "mkv", "webm", "avi", "mov", "flv", "ts"}
ALLOWED_AUDIO = {"mp3", "wav", "ogg", "m4a", "flac", "aac"}
ALLOWED_IMAGE = {"jpg", "jpeg", "png", "webp", "gif"}

# ============================================================
# BASIC HELPERS
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_nexa_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Check real Telegram membership in the NEXA channel."""
    try:
        member = await context.bot.get_chat_member(NEXA_CHANNEL, user_id)
        return member.status in {"creator", "administrator", "member"}
    except TelegramError as exc:
        print("NEXA MEMBERSHIP CHECK ERROR:", repr(exc))
        return False


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Rejoindre NEXA", url="https://t.me/Nexa_CG")],
        [InlineKeyboardButton("✅ J'ai rejoint — Vérifier", callback_data="verify:nexa")],
    ])


async def require_nexa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if is_admin(user_id) or await is_nexa_member(context, user_id):
        return True
    text = (
        "🔐 <b>Fonction réservée aux membres de NEXA</b>\n\n"
        "Pour utiliser <b>Renommer</b> ou <b>Compresser</b>, rejoins notre canal puis vérifie ton abonnement.\n\n"
        "⚡ <b>NEXA</b> — les outils de demain, aujourd'hui."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=subscription_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=subscription_keyboard())
    return False


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.2f} GB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def safe_filename(name: str, fallback: str = "file") -> str:
    name = Path(name).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.replace("/", "_").replace("\\", "_")
    name = name.strip(" .")
    if not name:
        name = fallback
    return name[:180]


def extension_of(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".")


def stem_of(name: str) -> str:
    return Path(name).stem


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def has_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def new_temp_dir(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=TEMP_ROOT)


def cleanup_dir(path: Optional[str]) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


def cleanup_dash_temp() -> int:
    root = Path(TEMP_ROOT)
    removed = 0
    if not root.exists():
        return 0
    prefixes = ("dash_",)
    for item in root.iterdir():
        if item.is_dir() and item.name.startswith(prefixes):
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
    return removed


async def midnight_cleanup_loop() -> None:
    """Clean abandoned Dash temporary folders every midnight in Brazzaville."""
    try:
        tz = ZoneInfo(CLEANUP_TZ)
    except Exception:
        tz = ZoneInfo("UTC")
    while True:
        now = datetime.now(tz)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep(max(1, (tomorrow - now).total_seconds()))
        removed = cleanup_dash_temp()
        print(f"🧹 DASH MIDNIGHT CLEANUP: {removed} dossier(s) supprimé(s).")


def session_for(user_id: int) -> Optional[dict]:
    return user_sessions.get(user_id)


# ============================================================
# FILE DETECTION
# ============================================================

def message_file_info(message) -> Optional[dict]:
    if message.document:
        return {
            "kind": "document",
            "file_id": message.document.file_id,
            "name": message.document.file_name or "document",
            "size": message.document.file_size or 0,
            "mime": message.document.mime_type or "",
        }

    if message.video:
        return {
            "kind": "video",
            "file_id": message.video.file_id,
            "name": message.video.file_name or "video.mp4",
            "size": message.video.file_size or 0,
            "mime": "video",
            "width": message.video.width,
            "height": message.video.height,
            "duration": message.video.duration,
        }

    if message.audio:
        return {
            "kind": "audio",
            "file_id": message.audio.file_id,
            "name": message.audio.file_name or "audio.mp3",
            "size": message.audio.file_size or 0,
            "mime": "audio",
            "duration": message.audio.duration,
        }

    if message.photo:
        photo = message.photo[-1]
        return {
            "kind": "photo",
            "file_id": photo.file_id,
            "name": "photo.jpg",
            "size": photo.file_size or 0,
            "mime": "image/jpeg",
            "width": photo.width,
            "height": photo.height,
        }

    if message.voice:
        return {
            "kind": "voice",
            "file_id": message.voice.file_id,
            "name": "voice.ogg",
            "size": message.voice.file_size or 0,
            "mime": "audio/ogg",
            "duration": message.voice.duration,
        }

    if message.animation:
        return {
            "kind": "animation",
            "file_id": message.animation.file_id,
            "name": message.animation.file_name or "animation.gif",
            "size": message.animation.file_size or 0,
            "mime": "animation",
            "duration": message.animation.duration,
        }

    return None


def display_kind(kind: str) -> str:
    return {
        "video": "🎬 Vidéo",
        "audio": "🎵 Audio",
        "photo": "🖼️ Image",
        "document": "📄 Document",
        "voice": "🎙️ Vocal",
        "animation": "🎞️ Animation",
    }.get(kind, "📁 Fichier")


# ============================================================
# KEYBOARDS
# ============================================================

def main_file_keyboard(data: dict) -> InlineKeyboardMarkup:
    kind = data["kind"]
    rows = [
        [
            InlineKeyboardButton("✏️ Renommer", callback_data="rename"),
            InlineKeyboardButton("ℹ️ Infos", callback_data="info"),
        ]
    ]

    if kind == "video":
        rows += [
            [
                InlineKeyboardButton("🔄 Convertir", callback_data="convert"),
                InlineKeyboardButton("📦 Compresser", callback_data="compress"),
            ],
            [
                InlineKeyboardButton("🖼️ Vignette", callback_data="thumbnail"),
                InlineKeyboardButton("📝 Caption", callback_data="caption"),
            ],
            [
                InlineKeyboardButton("✂️ Découper", callback_data="cut"),
                InlineKeyboardButton("🎵 Extraire audio", callback_data="extract_audio"),
            ],
        ]
    elif kind in {"audio", "voice"}:
        rows += [
            [
                InlineKeyboardButton("🔄 Convertir", callback_data="convert"),
                InlineKeyboardButton("📦 Compresser", callback_data="compress"),
            ],
            [
                InlineKeyboardButton("📝 Caption", callback_data="caption"),
            ],
        ]
    elif kind == "photo":
        rows += [
            [
                InlineKeyboardButton("🔄 Convertir", callback_data="convert"),
                InlineKeyboardButton("📦 Compresser", callback_data="compress"),
            ],
            [
                InlineKeyboardButton("📝 Caption", callback_data="caption"),
            ],
        ]
    else:
        rows += [
            [
                InlineKeyboardButton("🔄 Convertir", callback_data="convert"),
                InlineKeyboardButton("📦 Compresser", callback_data="compress"),
            ],
            [
                InlineKeyboardButton("📝 Caption", callback_data="caption"),
            ],
        ]

    rows.append([
        InlineKeyboardButton("🗑️ Effacer la session", callback_data="clear")
    ])
    return InlineKeyboardMarkup(rows)


def convert_keyboard(kind: str) -> InlineKeyboardMarkup:
    if kind == "video":
        rows = [
            [
                InlineKeyboardButton("MP4", callback_data="conv:mp4"),
                InlineKeyboardButton("MKV", callback_data="conv:mkv"),
            ],
            [
                InlineKeyboardButton("WEBM", callback_data="conv:webm"),
                InlineKeyboardButton("AVI", callback_data="conv:avi"),
            ],
            [
                InlineKeyboardButton("MP3", callback_data="conv:mp3"),
                InlineKeyboardButton("GIF", callback_data="conv:gif"),
            ],
        ]
    elif kind in {"audio", "voice"}:
        rows = [
            [
                InlineKeyboardButton("MP3", callback_data="conv:mp3"),
                InlineKeyboardButton("WAV", callback_data="conv:wav"),
            ],
            [
                InlineKeyboardButton("OGG", callback_data="conv:ogg"),
                InlineKeyboardButton("M4A", callback_data="conv:m4a"),
            ],
        ]
    elif kind == "photo":
        rows = [
            [
                InlineKeyboardButton("JPG", callback_data="conv:jpg"),
                InlineKeyboardButton("PNG", callback_data="conv:png"),
            ],
            [
                InlineKeyboardButton("WEBP", callback_data="conv:webp"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton("PDF", callback_data="conv:pdf"),
                InlineKeyboardButton("TXT", callback_data="conv:txt"),
            ]
        ]

    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def thumbnail_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Extraire une vignette", callback_data="thumb:extract"),
        ],
        [
            InlineKeyboardButton("📤 Envoyer une image", callback_data="thumb:upload"),
        ],
        [
            InlineKeyboardButton("🚫 Sans vignette", callback_data="thumb:none"),
        ],
        [
            InlineKeyboardButton("⬅️ Retour", callback_data="back"),
        ],
    ])


def compression_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Légère", callback_data="compress:light"),
            InlineKeyboardButton("🟡 Moyenne", callback_data="compress:medium"),
        ],
        [
            InlineKeyboardButton("🔴 Forte", callback_data="compress:strong"),
        ],
        [
            InlineKeyboardButton("⬅️ Retour", callback_data="back"),
        ],
    ])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Statistiques", callback_data="admin:stats"),
            InlineKeyboardButton("⚙️ Paramètres", callback_data="admin:settings"),
        ],
        [
            InlineKeyboardButton("📢 Diffusion", callback_data="admin:broadcast"),
            InlineKeyboardButton("🧹 Nettoyage", callback_data="admin:cleanup"),
        ],
        [
            InlineKeyboardButton("👥 Admins", callback_data="admin:admins"),
        ],
    ])


# ============================================================
# START / HELP / ADMIN
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚡ <b>Bienvenue sur Dash FileBot</b>\n"
        "<i>Un outil de gestion de fichiers créé par NEXA</i>\n\n"
        "Envoie simplement une vidéo, un audio, une image ou un document. "
        "Dash détecte automatiquement ton fichier et te propose les actions disponibles.\n\n"
        "✏️ Renommer\n"
        "🔄 Convertir\n"
        "📦 Compresser\n"
        "🖼️ Gérer la vignette\n"
        "📝 Modifier le caption\n"
        "✂️ Découper une vidéo\n"
        "🎵 Extraire l'audio\n"
        "ℹ️ Voir les informations\n\n"
        "<b>Simple • Rapide • Puissant</b>\n\n"
        "Aucune commande n'est nécessaire pour les opérations normales. 📁"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁 Envoyer un fichier", callback_data="send"),
            InlineKeyboardButton("❓ Aide", callback_data="help"),
        ]
    ])

    # Place dash.png dans le même dossier que bot.py sur GitHub/Render.
    # Si l'image est absente, Dash envoie quand même le message de bienvenue.
    image_path = Path(__file__).resolve().parent / "dash.png"
    if image_path.exists():
        try:
            with image_path.open("rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            return
        except TelegramError as exc:
            print("DASH WELCOME PHOTO ERROR:", repr(exc))

    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Dash FileBot — Aide</b>\n\n"
        "📁 Envoie un fichier : Dash affiche automatiquement son menu.\n\n"
        "<b>Commandes :</b>\n"
        "/start — accueil\n"
        "/help — aide\n"
        "/settings — paramètres\n"
        "/status — état du bot\n"
        "/admin — panneau administrateur\n\n"
        "<b>Fonctions :</b>\n"
        "✏️ Renommage\n"
        "🔄 Conversion\n"
        "📦 Compression\n"
        "📝 Caption\n"
        "🖼️ Vignette\n"
        "✂️ Découpage vidéo\n"
        "🎵 Extraction audio\n"
        "ℹ️ Informations"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ <b>Paramètres</b>\n\n"
        "Les réglages personnalisés seront ajoutés progressivement.",
        parse_mode="HTML"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ffmpeg = "✅" if has_ffmpeg() else "❌"
    ffprobe = "✅" if has_ffprobe() else "❌"
    await update.message.reply_text(
        "⚡ <b>Dash Status</b>\n\n"
        f"FFmpeg : {ffmpeg}\n"
        f"FFprobe : {ffprobe}\n"
        f"Sessions actives : {len(user_sessions)}\n"
        f"Fichiers traités : {stats['files']}\n"
        f"Conversions : {stats['conversions']}\n"
        f"Renommages : {stats['renames']}\n"
        f"Compressons : {stats['compressions']}\n"
        f"Erreurs : {stats['errors']}",
        parse_mode="HTML"
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Accès administrateur refusé.")
        return

    await update.message.reply_text(
        "👑 <b>Dash Admin Panel</b>\n\n"
        "Choisis une fonction :",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# RECEIVE FILE
# ============================================================

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = message_file_info(update.message)
    if not data:
        return

    user_id = update.effective_user.id
    size = data["size"]

    if size > MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"⚠️ Fichier trop volumineux pour cette configuration.\n"
            f"Limite actuelle : {format_size(MAX_DOWNLOAD_BYTES)}\n\n"
            "On pourra passer à un serveur Bot API local pour traiter "
            "de très gros fichiers."
        )
        return

    # A session stores the source Telegram message instead of keeping
    # the binary file in RAM.
    session = {
        **data,
        "chat_id": update.effective_chat.id,
        "message_id": update.message.message_id,
        "caption": update.message.caption or "",
    }
    user_sessions[user_id] = session
    stats["files"] += 1

    lines = [
        "╭────────────────────────────╮",
        "│      ⚡ <b>DASH FILEBOT</b>      │",
        "╰────────────────────────────╯",
        "",
        f"{display_kind(data['kind'])}",
        "",
        f"📄 <b>Nom :</b> <code>{data['name']}</code>",
        f"💾 <b>Taille :</b> {format_size(size)}",
    ]

    if data.get("duration") is not None:
        lines.append(f"⏱️ <b>Durée :</b> {format_duration(data['duration'])}")

    if data.get("width") and data.get("height"):
        lines.append(
            f"📐 <b>Résolution :</b> {data['width']} × {data['height']}"
        )

    if data.get("mime"):
        lines.append(f"🔎 <b>Type :</b> {data['mime']}")

    lines += ["", "Choisis une action :"]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_file_keyboard(data),
    )


# ============================================================
# TELEGRAM DOWNLOAD
# ============================================================

async def download_session_file(context, session: dict, directory: str) -> str:
    source_name = safe_filename(session["name"])
    source = os.path.join(directory, source_name)

    tg_file = await context.bot.get_file(session["file_id"])
    await tg_file.download_to_drive(source)

    if not os.path.exists(source):
        raise RuntimeError("Téléchargement Telegram échoué.")

    return source


# ============================================================
# FFPROBE
# ============================================================

async def ffprobe_json(path: str) -> dict:
    if not has_ffprobe():
        return {}

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(
        process.communicate(), timeout=60
    )

    if process.returncode != 0:
        return {}

    try:
        return json.loads(stdout.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}


# ============================================================
# RENAME
# ============================================================

async def perform_rename(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         user_id: int, new_name: str):
    session = session_for(user_id)
    if not session:
        await update.message.reply_text("⚠️ Aucun fichier actif.")
        return

    new_name = safe_filename(new_name)
    old_ext = Path(session["name"]).suffix

    if not Path(new_name).suffix and old_ext:
        new_name += old_ext

    directory = new_temp_dir("dash_rename_")

    try:
        progress = await update.message.reply_text(
            "✏️ <b>DASH RENOMMAGE</b>\n\n"
            "▰▱▱▱▱▱▱▱▱▱ <b>10%</b>\n"
            "🔍 Analyse du fichier...",
            parse_mode="HTML",
        )
        source = await download_session_file(context, session, directory)
        await progress.edit_text(
            "✏️ <b>DASH RENOMMAGE</b>\n\n"
            "▰▰▰▰▱▱▱▱▱▱ <b>40%</b>\n"
            "⚙️ Préparation du nouveau nom...",
            parse_mode="HTML",
        )
        output = os.path.join(directory, safe_filename(new_name))
        os.replace(source, output)
        await progress.edit_text(
            "✏️ <b>DASH RENOMMAGE</b>\n\n"
            "▰▰▰▰▰▰▰▱▱▱ <b>70%</b>\n"
            "📦 Création du fichier...",
            parse_mode="HTML",
        )

        await update.message.reply_text("📤 Renvoi du fichier renommé...")

        with open(output, "rb") as f:
            await send_processed_file(
                update,
                f,
                session,
                filename=safe_filename(new_name),
                caption=f"✏️ <b>{safe_filename(new_name)}</b>",
            )

        stats["renames"] += 1
        try:
            await progress.edit_text(
                "✏️ <b>DASH RENOMMAGE</b>\n\n"
                "▰▰▰▰▰▰▰▰▰▰ <b>100%</b>\n"
                "✅ Renommage terminé !",
                parse_mode="HTML",
            )
        except TelegramError:
            pass

    except Exception as exc:
        stats["errors"] += 1
        await update.message.reply_text(
            f"❌ Renommage impossible.\n\n<code>{str(exc)[:800]}</code>",
            parse_mode="HTML",
        )
    finally:
        cleanup_dir(directory)


# ============================================================
# CAPTION
# ============================================================

async def resend_with_caption(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict,
    caption: Optional[str],
    force_document: bool = False,
    thumbnail_path: Optional[str] = None,
):
    directory = new_temp_dir("dash_caption_")
    try:
        source = await download_session_file(context, session, directory)
        with open(source, "rb") as f:
            await send_processed_file(
                update,
                f,
                session,
                filename=Path(source).name,
                caption=caption,
                force_document=force_document,
                thumbnail_path=thumbnail_path,
            )
    finally:
        cleanup_dir(directory)


# ============================================================
# SEND PROCESSED FILE
# ============================================================

async def send_processed_file(
    update: Update,
    file_obj,
    session: dict,
    filename: str,
    caption: Optional[str] = None,
    force_document: bool = False,
    thumbnail_path: Optional[str] = None,
):
    kind = session["kind"]
    caption = caption if caption is not None else ""

    if kind == "video" and not force_document:
        kwargs = {
            "video": file_obj,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "supports_streaming": True,
        }
        if thumbnail_path and os.path.exists(thumbnail_path):
            with open(thumbnail_path, "rb") as thumb:
                kwargs["thumbnail"] = thumb
                await update.effective_chat.send_video(**kwargs)
        else:
            await update.effective_chat.send_video(**kwargs)

    elif kind == "audio" and not force_document:
        await update.effective_chat.send_audio(
            audio=file_obj,
            caption=caption[:1024],
            parse_mode="HTML",
            title=Path(filename).stem[:64],
        )

    elif kind == "photo" and not force_document:
        await update.effective_chat.send_photo(
            photo=file_obj,
            caption=caption[:1024],
            parse_mode="HTML",
        )

    else:
        await update.effective_chat.send_document(
            document=file_obj,
            filename=filename,
            caption=caption[:1024],
            parse_mode="HTML",
        )


# ============================================================
# FFMPEG COMMANDS
# ============================================================

def conversion_command(source: str, output: str, target: str) -> list[str]:
    target = target.lower()

    if target == "mp4":
        return [
            "ffmpeg", "-y", "-i", source,
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-movflags", "+faststart", output
        ]

    if target == "mkv":
        return [
            "ffmpeg", "-y", "-i", source,
            "-map", "0", "-c", "copy", output
        ]

    if target == "webm":
        return [
            "ffmpeg", "-y", "-i", source,
            "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
            "-c:a", "libopus", output
        ]

    if target == "avi":
        return [
            "ffmpeg", "-y", "-i", source,
            "-c:v", "mpeg4", "-q:v", "4",
            "-c:a", "libmp3lame", output
        ]

    if target == "mp3":
        return [
            "ffmpeg", "-y", "-i", source,
            "-vn", "-c:a", "libmp3lame", "-q:a", "2", output
        ]

    if target == "wav":
        return [
            "ffmpeg", "-y", "-i", source,
            "-vn", output
        ]

    if target == "ogg":
        return [
            "ffmpeg", "-y", "-i", source,
            "-vn", "-c:a", "libopus", output
        ]

    if target == "m4a":
        return [
            "ffmpeg", "-y", "-i", source,
            "-vn", "-c:a", "aac", "-b:a", "192k", output
        ]

    if target == "gif":
        return [
            "ffmpeg", "-y", "-i", source,
            "-vf", "fps=12,scale=640:-1:flags=lanczos",
            "-an", output
        ]

    if target == "jpg":
        return [
            "ffmpeg", "-y", "-i", source,
            "-frames:v", "1", output
        ]

    if target == "png":
        return [
            "ffmpeg", "-y", "-i", source,
            "-frames:v", "1", output
        ]

    if target == "webp":
        return [
            "ffmpeg", "-y", "-i", source,
            "-frames:v", "1", output
        ]

    # Document conversions are intentionally not faked.
    # PDF/TXT require a dedicated document conversion layer.
    raise ValueError(f"Conversion {target.upper()} non disponible pour ce type.")


async def run_ffmpeg(command: list[str], timeout: int = MAX_PROCESS_SECONDS):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Le traitement a dépassé la durée maximale.")

    if process.returncode != 0:
        error = stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(error[-1500:])

    return stdout, stderr


# ============================================================
# PROGRESS
# ============================================================

async def conversion_with_progress(
    command: list[str],
    duration: Optional[float],
    progress_message,
):
    # Add -progress pipe:1 and -nostats to get machine-readable progress.
    if command[0] != "ffmpeg":
        raise RuntimeError("Commande FFmpeg invalide.")

    base = command[1:]
    command2 = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        *base
    ]

    process = await asyncio.create_subprocess_exec(
        *command2,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    last_percent = -1
    last_update = 0.0

    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break

            text = line.decode("utf-8", errors="ignore").strip()

            if text.startswith("out_time_ms=") and duration:
                try:
                    micros = int(text.split("=", 1)[1])
                    current = micros / 1_000_000
                    percent = min(99, int((current / duration) * 100))
                except ValueError:
                    continue

                now = asyncio.get_running_loop().time()
                if percent != last_percent and now - last_update >= 2:
                    last_percent = percent
                    last_update = now
                    try:
                        await progress_message.edit_text(
                            f"⚙️ <b>Traitement en cours</b>\n\n"
                            f"Progression : <b>{percent}%</b>\n"
                            f"{progress_bar(percent)}",
                            parse_mode="HTML",
                        )
                    except TelegramError:
                        pass

        stderr = await process.stderr.read()
        returncode = await process.wait()

        if returncode != 0:
            raise RuntimeError(
                stderr.decode("utf-8", errors="ignore")[-1500:]
            )

    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise


def progress_bar(percent: int) -> str:
    filled = max(0, min(10, percent // 10))
    return "█" * filled + "░" * (10 - filled)


# ============================================================
# CONVERSION
# ============================================================

async def convert_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    target: str,
):
    session = session_for(user_id)
    if not session:
        await update.effective_chat.send_message("⚠️ Aucun fichier actif.")
        return

    if not has_ffmpeg():
        await update.effective_chat.send_message(
            "❌ FFmpeg n'est pas installé sur le serveur."
        )
        return

    directory = new_temp_dir("dash_convert_")
    progress_message = None

    try:
        progress_message = await update.effective_chat.send_message(
            f"⏳ <b>Préparation de la conversion {target.upper()}...</b>",
            parse_mode="HTML",
        )

        source = await download_session_file(context, session, directory)

        stem = safe_filename(stem_of(session["name"]))
        output = os.path.join(directory, f"{stem}.{target}")

        command = conversion_command(source, output, target)

        duration = session.get("duration")
        if not duration and has_ffprobe():
            probe = await ffprobe_json(source)
            try:
                duration = float(probe.get("format", {}).get("duration", 0))
            except (TypeError, ValueError):
                duration = None

        await conversion_with_progress(
            command,
            duration,
            progress_message,
        )

        if not os.path.exists(output):
            raise RuntimeError("FFmpeg n'a pas créé le fichier.")

        await progress_message.edit_text(
            "✅ <b>Conversion terminée.</b>\n📤 Envoi du résultat...",
            parse_mode="HTML",
        )

        result_session = dict(session)
        result_session["kind"] = (
            "video" if target in {"mp4", "mkv", "webm", "avi", "gif"}
            else "audio" if target in {"mp3", "wav", "ogg", "m4a"}
            else "photo"
        )

        with open(output, "rb") as f:
            await send_processed_file(
                update,
                f,
                result_session,
                filename=os.path.basename(output),
                caption=f"⚡ <b>Dash</b> — conversion {target.upper()} terminée",
            )

        stats["conversions"] += 1

        try:
            await progress_message.delete()
        except TelegramError:
            pass

    except Exception as exc:
        stats["errors"] += 1
        if progress_message:
            try:
                await progress_message.edit_text(
                    f"❌ <b>Conversion impossible</b>\n\n"
                    f"<code>{str(exc)[:1200]}</code>",
                    parse_mode="HTML",
                )
            except TelegramError:
                await update.effective_chat.send_message(
                    f"❌ Conversion impossible.\n<code>{str(exc)[:1200]}</code>",
                    parse_mode="HTML",
                )
        else:
            await update.effective_chat.send_message(
                f"❌ Conversion impossible.\n<code>{str(exc)[:1200]}</code>",
                parse_mode="HTML",
            )
    finally:
        cleanup_dir(directory)


# ============================================================
# COMPRESSION
# ============================================================

def compression_command(source: str, output: str, kind: str, level: str):
    if kind == "video":
        crf = {
            "light": "26",
            "medium": "30",
            "strong": "34",
        }[level]

        return [
            "ffmpeg", "-y", "-i", source,
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", crf,
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output
        ]

    if kind == "audio":
        bitrate = {
            "light": "160k",
            "medium": "128k",
            "strong": "96k",
        }[level]

        return [
            "ffmpeg", "-y", "-i", source,
            "-vn", "-c:a", "libmp3lame",
            "-b:a", bitrate,
            output
        ]

    if kind == "photo":
        quality = {
            "light": "5",
            "medium": "10",
            "strong": "20",
        }[level]

        return [
            "ffmpeg", "-y", "-i", source,
            "-q:v", quality,
            output
        ]

    raise ValueError("Compression non disponible pour ce type.")


async def compress_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    level: str,
):
    session = session_for(user_id)
    if not session:
        await update.effective_chat.send_message("⚠️ Aucun fichier actif.")
        return

    if not has_ffmpeg():
        await update.effective_chat.send_message(
            "❌ FFmpeg n'est pas installé sur le serveur."
        )
        return

    directory = new_temp_dir("dash_compress_")
    progress_message = None

    try:
        progress_message = await update.effective_chat.send_message(
            f"📦 <b>Compression {level}</b>...\n\nPréparation...",
            parse_mode="HTML",
        )

        source = await download_session_file(context, session, directory)
        kind = session["kind"]

        if kind == "video":
            ext = ".mp4"
        elif kind in {"audio", "voice"}:
            ext = ".mp3"
        elif kind == "photo":
            ext = ".jpg"
        else:
            raise ValueError(
                "La compression de ce type de document n'est pas encore disponible."
            )

        output = os.path.join(
            directory,
            f"{safe_filename(stem_of(session['name']))}_compressed{ext}"
        )

        command = compression_command(
            source, output,
            "audio" if kind in {"audio", "voice"} else kind,
            level,
        )

        await progress_message.edit_text(
            "📦 <b>DASH COMPRESSION</b>\n\n"
            "▰▰▰▰▰▱▱▱▱▱ <b>50%</b>\n"
            "⚡ Compression en cours...",
            parse_mode="HTML",
        )

        await run_ffmpeg(command)

        await progress_message.edit_text(
            "📦 <b>DASH COMPRESSION</b>\n\n"
            "▰▰▰▰▰▰▰▰▰▱ <b>90%</b>\n"
            "📤 Préparation du résultat...",
            parse_mode="HTML",
        )

        if not os.path.exists(output):
            raise RuntimeError("Le fichier compressé n'a pas été créé.")

        original_size = os.path.getsize(source)
        result_size = os.path.getsize(output)

        result_session = dict(session)
        result_session["kind"] = kind

        with open(output, "rb") as f:
            await send_processed_file(
                update,
                f,
                result_session,
                filename=os.path.basename(output),
                caption=(
                    "📦 <b>Compression terminée</b>\n"
                    f"Avant : {format_size(original_size)}\n"
                    f"Après : {format_size(result_size)}"
                ),
            )

        stats["compressions"] += 1

        await progress_message.delete()

    except Exception as exc:
        stats["errors"] += 1
        if progress_message:
            try:
                await progress_message.edit_text(
                    f"❌ <b>Compression impossible</b>\n\n"
                    f"<code>{str(exc)[:1200]}</code>",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
    finally:
        cleanup_dir(directory)


# ============================================================
# THUMBNAIL
# ============================================================

async def extract_thumbnail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    session = session_for(user_id)
    if not session or session["kind"] != "video":
        await update.effective_chat.send_message(
            "⚠️ La vignette est disponible pour les vidéos."
        )
        return

    if not has_ffmpeg():
        await update.effective_chat.send_message(
            "❌ FFmpeg n'est pas installé."
        )
        return

    directory = new_temp_dir("dash_thumb_")

    try:
        source = await download_session_file(context, session, directory)
        thumb = os.path.join(directory, "thumbnail.jpg")

        command = [
            "ffmpeg", "-y", "-ss", "00:00:01",
            "-i", source,
            "-frames:v", "1",
            "-q:v", "2",
            thumb
        ]
        await run_ffmpeg(command)

        with open(thumb, "rb") as f:
            await update.effective_chat.send_photo(
                photo=f,
                caption="🖼️ Vignette extraite par Dash."
            )

    except Exception as exc:
        stats["errors"] += 1
        await update.effective_chat.send_message(
            f"❌ Extraction impossible.\n<code>{str(exc)[:800]}</code>",
            parse_mode="HTML",
        )
    finally:
        cleanup_dir(directory)


# ============================================================
# CUT VIDEO
# ============================================================

async def cut_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    start: str,
    end: str,
):
    session = session_for(user_id)
    if not session or session["kind"] != "video":
        await update.message.reply_text(
            "⚠️ Le découpage est disponible uniquement pour une vidéo."
        )
        return

    if not has_ffmpeg():
        await update.message.reply_text("❌ FFmpeg n'est pas installé.")
        return

    directory = new_temp_dir("dash_cut_")

    try:
        source = await download_session_file(context, session, directory)
        output = os.path.join(
            directory,
            f"{safe_filename(stem_of(session['name']))}_cut.mp4"
        )

        command = [
            "ffmpeg", "-y",
            "-ss", start,
            "-i", source,
            "-to", end,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            output
        ]

        await update.message.reply_text(
            "✂️ Découpage en cours..."
        )

        await run_ffmpeg(command)

        with open(output, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption="✂️ <b>Vidéo découpée par Dash</b>",
                parse_mode="HTML",
            )

    except Exception as exc:
        stats["errors"] += 1
        await update.message.reply_text(
            f"❌ Découpage impossible.\n<code>{str(exc)[:1000]}</code>",
            parse_mode="HTML",
        )
    finally:
        cleanup_dir(directory)


# ============================================================
# EXTRACT AUDIO
# ============================================================

async def extract_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    session = session_for(user_id)
    if not session or session["kind"] != "video":
        await update.effective_chat.send_message(
            "⚠️ Cette fonction nécessite une vidéo."
        )
        return

    await convert_session(update, context, user_id, "mp3")


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # General navigation
    if data == "send":
        await query.edit_message_text(
            "📁 Envoie maintenant ton fichier."
        )
        return

    if data == "help":
        await query.edit_message_text(
            "❓ Envoie simplement un fichier à Dash. "
            "Le bot détecte automatiquement son type et affiche "
            "les fonctions disponibles.\n\n"
            "Tu peux aussi utiliser /help.",
            parse_mode="HTML",
        )
        return

    if data == "back":
        session = session_for(user_id)
        if not session:
            await query.edit_message_text(
                "⚠️ Aucun fichier actif."
            )
            return

        await query.edit_message_text(
            "⚡ <b>Actions disponibles</b>",
            parse_mode="HTML",
            reply_markup=main_file_keyboard(session),
        )
        return

    if data == "verify:nexa":
        if await is_nexa_member(context, user_id):
            await query.edit_message_text(
                "✅ <b>Abonnement vérifié !</b>\n\n"
                "Tu peux maintenant utiliser <b>Renommer</b> et <b>Compresser</b>.\n\n"
                "📁 Renvoie ton fichier ou ouvre son menu.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                "❌ <b>Abonnement non détecté.</b>\n\n"
                "Rejoins @Nexa_CG puis appuie de nouveau sur <b>Vérifier</b>.",
                parse_mode="HTML",
                reply_markup=subscription_keyboard(),
            )
        return

    # File actions
    session = session_for(user_id)
    if not session and not data.startswith("admin:"):
        await query.edit_message_text(
            "⚠️ Cette session de fichier a expiré ou a été supprimée."
        )
        return

    if data == "clear":
        user_sessions.pop(user_id, None)
        rename_waiting.discard(user_id)
        caption_waiting.discard(user_id)
        cut_waiting.discard(user_id)
        await query.edit_message_text(
            "🗑️ Session du fichier supprimée."
        )
        return

    if data == "rename":
        if not await require_nexa(update, context):
            return
        rename_waiting.add(user_id)
        await query.edit_message_text(
            "✏️ <b>Renommer le fichier</b>\n\n"
            "Envoie maintenant le nouveau nom.\n\n"
            "Exemple : <code>Mon Film 2026</code>",
            parse_mode="HTML",
        )
        return

    if data == "info":
        await show_info(update, context, session)
        return

    if data == "convert":
        await query.edit_message_text(
            "🔄 <b>Choisis le format :</b>",
            parse_mode="HTML",
            reply_markup=convert_keyboard(session["kind"]),
        )
        return

    if data.startswith("conv:"):
        target = data.split(":", 1)[1]
        await query.edit_message_text(
            f"⏳ Conversion <b>{target.upper()}</b> en préparation...",
            parse_mode="HTML",
        )
        await convert_session(update, context, user_id, target)
        return

    if data == "compress":
        if not await require_nexa(update, context):
            return
        await query.edit_message_text(
            "📦 <b>Niveau de compression :</b>",
            parse_mode="HTML",
            reply_markup=compression_keyboard(),
        )
        return

    if data.startswith("compress:"):
        if not await require_nexa(update, context):
            return
        level = data.split(":", 1)[1]
        await query.edit_message_text(
            f"📦 Compression <b>{level}</b> en préparation...",
            parse_mode="HTML",
        )
        await compress_session(update, context, user_id, level)
        return

    if data == "thumbnail":
        await query.edit_message_text(
            "🖼️ <b>Gestion de la vignette</b>\n\n"
            "Choisis une action :",
            parse_mode="HTML",
            reply_markup=thumbnail_keyboard(),
        )
        return

    if data == "thumb:extract":
        await query.edit_message_text("🖼️ Extraction de la vignette...")
        await extract_thumbnail(update, context, user_id)
        return

    if data == "thumb:upload":
        context.user_data["waiting_thumbnail"] = True
        await query.edit_message_text(
            "📤 Envoie maintenant une image.\n\n"
            "Elle sera utilisée comme vignette lors du renvoi de la vidéo."
        )
        return

    if data == "thumb:none":
        # Telegram generates its own preview for send_video. A bot cannot
        # guarantee removal of Telegram's generated preview. We resend
        # as a document to avoid a video preview.
        await query.edit_message_text(
            "🚫 <b>Mode sans vignette</b>\n\n"
            "Dash va renvoyer la vidéo comme document afin d'éviter "
            "la vignette de prévisualisation.",
            parse_mode="HTML",
        )
        await resend_video_without_preview(update, context, user_id)
        return

    if data == "caption":
        caption_waiting.add(user_id)
        await query.edit_message_text(
            "📝 <b>Caption</b>\n\n"
            "Envoie le nouveau texte.\n\n"
            "Pour supprimer le caption, envoie : <code>/none</code>",
            parse_mode="HTML",
        )
        return

    if data == "cut":
        cut_waiting.add(user_id)
        await query.edit_message_text(
            "✂️ <b>Découper une vidéo</b>\n\n"
            "Envoie les horaires au format :\n"
            "<code>00:00:10 00:01:00</code>\n\n"
            "Cela signifie : de 10 secondes à 1 minute.",
            parse_mode="HTML",
        )
        return

    if data == "extract_audio":
        await query.edit_message_text(
            "🎵 Extraction audio en cours..."
        )
        await extract_audio(update, context, user_id)
        return

    # Admin actions
    if data.startswith("admin:"):
        await admin_callbacks(update, context, data)
        return


# ============================================================
# INFO
# ============================================================

async def show_info(update, context, session):
    user_id = update.effective_user.id
    directory = new_temp_dir("dash_info_")

    try:
        source = await download_session_file(context, session, directory)
        probe = await ffprobe_json(source)

        lines = [
            "ℹ️ <b>Informations du fichier</b>",
            "",
            f"📄 Nom : <code>{session['name']}</code>",
            f"💾 Taille : {format_size(session['size'])}",
            f"🔎 Type : {session['kind']}",
        ]

        fmt = probe.get("format", {})
        if fmt.get("format_name"):
            lines.append(f"📦 Format : {fmt['format_name']}")

        if fmt.get("duration"):
            try:
                lines.append(
                    f"⏱️ Durée : {format_duration(float(fmt['duration']))}"
                )
            except (TypeError, ValueError):
                pass

        streams = probe.get("streams", [])
        for stream in streams:
            if stream.get("codec_type") == "video":
                if stream.get("codec_name"):
                    lines.append(f"🎥 Codec vidéo : {stream['codec_name']}")
                if stream.get("width") and stream.get("height"):
                    lines.append(
                        f"📐 Résolution : {stream['width']} × {stream['height']}"
                    )
                break

        for stream in streams:
            if stream.get("codec_type") == "audio":
                if stream.get("codec_name"):
                    lines.append(f"🎵 Codec audio : {stream['codec_name']}")
                break

        await update.effective_chat.send_message(
            "\n".join(lines),
            parse_mode="HTML",
        )

    except Exception as exc:
        await update.effective_chat.send_message(
            f"❌ Impossible de lire les informations.\n"
            f"<code>{str(exc)[:800]}</code>",
            parse_mode="HTML",
        )
    finally:
        cleanup_dir(directory)


# ============================================================
# CAPTION / RENAME / CUT TEXT HANDLERS
# ============================================================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_id in rename_waiting:
        rename_waiting.discard(user_id)
        await perform_rename(update, context, user_id, text)
        return

    if user_id in caption_waiting:
        caption_waiting.discard(user_id)
        caption = "" if text.lower() == "/none" else text

        session = session_for(user_id)
        if not session:
            await update.message.reply_text("⚠️ Aucun fichier actif.")
            return

        await update.message.reply_text(
            "📝 Préparation du nouveau caption..."
        )
        try:
            await resend_with_caption(
                update, context, session, caption
            )
        except Exception as exc:
            stats["errors"] += 1
            await update.message.reply_text(
                f"❌ Impossible de modifier le caption.\n"
                f"<code>{str(exc)[:800]}</code>",
                parse_mode="HTML",
            )
        return

    if user_id in cut_waiting:
        cut_waiting.discard(user_id)

        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text(
                "❌ Format invalide.\nExemple : "
                "<code>00:00:10 00:01:00</code>",
                parse_mode="HTML",
            )
            return

        await cut_video(
            update,
            context,
            user_id,
            parts[0],
            parts[1],
        )
        return


# ============================================================
# THUMBNAIL IMAGE HANDLER
# ============================================================

async def receive_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_thumbnail"):
        return

    context.user_data["waiting_thumbnail"] = False

    user_id = update.effective_user.id
    session = session_for(user_id)

    if not session or session["kind"] != "video":
        await update.message.reply_text(
            "⚠️ Aucune vidéo active."
        )
        return

    if not update.message.photo:
        await update.message.reply_text(
            "⚠️ Envoie une image comme photo."
        )
        return

    directory = new_temp_dir("dash_custom_thumb_")

    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        thumb = os.path.join(directory, "thumbnail.jpg")
        await tg_file.download_to_drive(thumb)

        source = await download_session_file(context, session, directory)
        output = os.path.join(
            directory,
            safe_filename(session["name"])
        )

        # Re-encode/remux so the thumbnail can be attached during upload.
        # We do not write the thumbnail into the video stream.
        # Telegram receives it as the upload thumbnail.
        with open(output, "rb") if False else open(source, "rb") as video_file:
            await update.message.reply_text(
                "🖼️ Vignette enregistrée.\n"
                "Pour l'appliquer, utilise le renvoi vidéo ci-dessous."
            )
            await send_processed_file(
                update,
                video_file,
                session,
                filename=Path(source).name,
                caption=session.get("caption", ""),
                thumbnail_path=thumb,
            )

    except Exception as exc:
        stats["errors"] += 1
        await update.message.reply_text(
            f"❌ Impossible d'appliquer la vignette.\n"
            f"<code>{str(exc)[:800]}</code>",
            parse_mode="HTML",
        )
    finally:
        cleanup_dir(directory)


# ============================================================
# SEND WITHOUT VIDEO PREVIEW
# ============================================================

async def resend_video_without_preview(update, context, user_id):
    session = session_for(user_id)
    if not session:
        return

    directory = new_temp_dir("dash_nopreview_")
    try:
        source = await download_session_file(context, session, directory)
        with open(source, "rb") as f:
            await update.effective_chat.send_document(
                document=f,
                filename=Path(source).name,
                caption=session.get("caption", ""),
                parse_mode="HTML",
            )
    except Exception as exc:
        stats["errors"] += 1
        await update.effective_chat.send_message(
            f"❌ Impossible de renvoyer le fichier.\n"
            f"<code>{str(exc)[:800]}</code>",
            parse_mode="HTML",
        )
    finally:
        cleanup_dir(directory)


# ============================================================
# ADMIN
# ============================================================

async def admin_callbacks(update, context, data):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer("Accès refusé.", show_alert=True)
        return

    action = data.split(":", 1)[1]

    if action == "stats":
        await query.edit_message_text(
            "📊 <b>Statistiques Dash</b>\n\n"
            f"📁 Fichiers : {stats['files']}\n"
            f"🔄 Conversions : {stats['conversions']}\n"
            f"✏️ Renommages : {stats['renames']}\n"
            f"📦 Compressons : {stats['compressions']}\n"
            f"❌ Erreurs : {stats['errors']}\n"
            f"👥 Sessions actives : {len(user_sessions)}",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
        return

    if action == "settings":
        await query.edit_message_text(
            "⚙️ <b>Paramètres administrateur</b>\n\n"
            f"Limite de téléchargement : {format_size(MAX_DOWNLOAD_BYTES)}\n"
            f"Timeout traitement : {MAX_PROCESS_SECONDS}s\n"
            f"Admins configurés : {len(ADMIN_IDS)}",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
        return

    if action == "cleanup":
        # Remove expired in-memory sessions. Binary files are already
        # deleted after every processing operation.
        count = len(user_sessions)
        user_sessions.clear()
        rename_waiting.clear()
        caption_waiting.clear()
        cut_waiting.clear()

        await query.edit_message_text(
            f"🧹 Nettoyage terminé.\n\n"
            f"{count} session(s) supprimée(s).",
            reply_markup=admin_keyboard(),
        )
        return

    if action == "admins":
        admins = "\n".join(f"• <code>{x}</code>" for x in ADMIN_IDS)
        await query.edit_message_text(
            "👥 <b>Administrateurs</b>\n\n"
            f"{admins or 'Aucun admin configuré.'}",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
        return

    if action == "broadcast":
        admin_broadcast_waiting.add(user_id)
        await query.edit_message_text(
            "📢 <b>Diffusion</b>\n\n"
            "Envoie le message à diffuser.\n\n"
            "⚠️ La version actuelle ne conserve pas une base "
            "persistante des utilisateurs. Pour activer une vraie "
            "diffusion, ajoute PostgreSQL/SQLite et un système "
            "d'inscription des utilisateurs.",
            parse_mode="HTML",
        )
        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    stats["errors"] += 1
    print("DASH ERROR:", repr(context.error))


# ============================================================
# MAIN
# ============================================================

def main():
    if not has_ffmpeg():
        print("WARNING: FFmpeg n'est pas installé.")
    if not has_ffprobe():
        print("WARNING: ffprobe n'est pas installé.")

    async def post_init(app: Application):
        app.create_task(midnight_cleanup_loop())

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(False)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("admin", admin_command))

    application.add_handler(CallbackQueryHandler(callbacks))

    # Thumbnail upload must run before generic photo handling.
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            receive_thumbnail,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        ),
        group=1,
    )

    # Automatic file detection.
    application.add_handler(
        MessageHandler(
            filters.Document.ALL |
            filters.VIDEO |
            filters.AUDIO |
            filters.PHOTO |
            filters.VOICE |
            filters.ANIMATION,
            receive_file,
        ),
        group=2,
    )

    application.add_error_handler(error_handler)

    # ========================================================
    # RENDER WEB SERVICE / WEBHOOK
    # ========================================================
    port = int(os.getenv("PORT", "10000"))
    public_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")

    if not public_url:
        raise RuntimeError("WEBHOOK_URL ou RENDER_EXTERNAL_URL est requis pour le webhook.")

    public_url = public_url.rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", "telegram").strip("/")
    webhook_url = f"{public_url}/{webhook_path}"

    print("⚡ Dash FileBot démarré en mode WEBHOOK.")
    print(f"🌐 Port: {port}")
    print(f"🔗 Webhook: {webhook_url}")

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
