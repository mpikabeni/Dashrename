import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# DASH RENAMER
# Créé par NEXA
# Renommage uniquement - aucune compression/conversion/découpage
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dash")

BASE_DIR = Path(__file__).resolve().parent

TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL = os.getenv("NEXA_CHANNEL", "@Nexa_CG").strip()
CHANNEL_URL = os.getenv(
    "NEXA_CHANNEL_URL",
    "https://t.me/Nexa_CG",
).strip()

ADMINS = {
    int(v.strip())
    for v in os.getenv("ADMIN_IDS", "").split(",")
    if v.strip().isdigit()
}

# Telegram officiel par défaut.
# Sur Render avec le Local Bot API : TELEGRAM_LOCAL_API=true
LOCAL = os.getenv(
    "TELEGRAM_LOCAL_API",
    "false",
).lower() in ("true", "1", "yes")

DB_PATH = os.getenv(
    "DB_PATH",
    "/app/data/dash.sqlite3",
)

# ============================================================
# BACKBLAZE B2
# ============================================================

B2_BUCKET = os.getenv("B2_BUCKET", "").strip()
B2_ENDPOINT = os.getenv("B2_ENDPOINT", "").strip()
B2_KEY_ID = os.getenv("B2_KEY_ID", "").strip()
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "").strip()
B2_PREFIX = os.getenv("B2_PREFIX", "dash-renamer").strip("/")
B2_RETENTION = os.getenv(
    "B2_RETENTION",
    "false",
).lower() in ("true", "1", "yes")

B2_ENABLED = all(
    (
        B2_BUCKET,
        B2_ENDPOINT,
        B2_KEY_ID,
        B2_APPLICATION_KEY,
    )
)

SESSIONS = {}
WAITING = set()
BROADCAST = set()
LOCKS = {}


# ============================================================
# DATABASE
# ============================================================

def database():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            old_name TEXT NOT NULL,
            new_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_thumbnails (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    con.commit()
    return con


def record_user(uid):
    with database() as db:
        db.execute(
            "INSERT OR IGNORE INTO users VALUES (?, ?)",
            (uid, datetime.now(timezone.utc).isoformat()),
        )


def claim_message(update):
    """Atomically accepts an incoming Telegram message only once."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return False

    chat_id = chat.id
    message_id = message.message_id
    now = datetime.now(timezone.utc).isoformat()

    with database() as db:
        cur = db.execute(
            """
            INSERT OR IGNORE INTO processed_messages
            (chat_id, message_id, created_at)
            VALUES (?, ?, ?)
            """,
            (chat_id, message_id, now),
        )
        accepted = cur.rowcount == 1

        # Keep the table small.
        db.execute(
            """
            DELETE FROM processed_messages
            WHERE created_at < datetime('now', '-7 days')
            """
        )

    if not accepted:
        log.warning(
            "Duplicate update ignored: chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )
    return accepted


def save_photo_thumbnail(update, file_id):
    """Save the latest photo as the user's permanent thumbnail."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return

    now = datetime.now(timezone.utc).isoformat()
    with database() as db:
        # Keep the history/statistics row. The newest row is the active
        # thumbnail for this user. A new photo therefore replaces the
        # previous active thumbnail automatically.
        db.execute(
            """
            INSERT OR REPLACE INTO photo_thumbnails
            (chat_id, message_id, user_id, file_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat.id, message.message_id, user.id, file_id, now),
        )


def current_thumbnail(uid):
    """Return the newest permanent thumbnail file_id for a user."""
    with database() as db:
        row = db.execute(
            """
            SELECT file_id
            FROM photo_thumbnails
            WHERE user_id=?
            ORDER BY created_at DESC, message_id DESC
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
    return row[0] if row else None


async def download_current_thumbnail(context, uid, directory):
    """Download the saved Telegram photo to a temporary JPEG file."""
    file_id = current_thumbnail(uid)
    if not file_id:
        return None

    path = Path(directory) / "dash_thumbnail.jpg"
    try:
        tg_file = await context.bot.get_file(
            file_id,
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
            pool_timeout=30,
        )
        await tg_file.download_to_drive(
            custom_path=path,
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
            pool_timeout=30,
        )
        if not path.exists() or path.stat().st_size <= 0:
            return None
        # Telegram requires a thumbnail to be a small JPEG. The smallest
        # PhotoSize saved by receive_photo is normally already within the
        # Telegram thumbnail limits. Do not send it if it is unexpectedly large.
        if path.stat().st_size > 200 * 1024:
            log.warning("Saved thumbnail is too large: %s bytes", path.stat().st_size)
            return None
        return path
    except Exception:
        log.exception("Unable to download permanent thumbnail | user=%s", uid)
        return None


def thumbnail_status(uid):
    return "🖼️ Miniature permanente : ✅ enregistrée" if current_thumbnail(uid) else "🖼️ Miniature permanente : ❌ aucune"


def record_rename(uid, old_name, new_name):
    with database() as db:
        db.execute(
            """
            INSERT INTO history
            (user_id, old_name, new_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                uid,
                old_name,
                new_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


# ============================================================
# B2
# ============================================================

def b2_client():
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3},
        ),
    )


async def b2_upload_safe(path: Path, key: str) -> bool:
    """Upload to B2 without ever blocking a successful Telegram rename."""
    if not B2_ENABLED:
        return False
    try:
        await asyncio.to_thread(
            b2_client().upload_file,
            str(path),
            B2_BUCKET,
            key,
        )
        log.info("B2 upload OK: %s", key)
        return True
    except Exception:
        log.exception("B2 upload failed; rename will continue")
        return False


async def b2_delete_safe(key: str):
    if not B2_ENABLED or not key:
        return
    try:
        await asyncio.to_thread(
            b2_client().delete_object,
            Bucket=B2_BUCKET,
            Key=key,
        )
        log.info("B2 cleanup OK: %s", key)
    except Exception:
        log.exception("B2 cleanup failed; renamed file was already sent")


# ============================================================
# FILE HELPERS
# ============================================================

def safe_name(name):
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name)
    name = name.strip(" .")
    return name[:180] or "fichier"


def renamed_name(old_name, proposed):
    name = safe_name(proposed)
    suffix = Path(old_name).suffix
    if suffix and not Path(name).suffix:
        name += suffix
    return safe_name(name)


def human_size(size):
    size = float(size)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024 or unit == "To":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} To"


def file_info(message):
    for attr, default in (
        ("document", "document.bin"),
        ("video", "video.mp4"),
        ("audio", "audio.mp3"),
        ("animation", "animation.gif"),
        ("voice", "vocal.ogg"),
    ):
        item = getattr(message, attr, None)
        if item:
            return {
                "file_id": item.file_id,
                "name": getattr(item, "file_name", None) or default,
                "size": item.file_size or 0,
                "kind": attr,
                "duration": getattr(item, "duration", None),
            }

    if message.photo:
        item = message.photo[-1]
        return {
            "file_id": item.file_id,
            "name": "photo.jpg",
            "size": item.file_size or 0,
            "kind": "photo",
            "duration": None,
        }

    return None


# ============================================================
# KEYBOARDS
# ============================================================

def file_menu(uid=None):
    rows = [
        [
            InlineKeyboardButton("✏️ Renommer", callback_data="rename"),
            InlineKeyboardButton("ℹ️ Infos", callback_data="info"),
        ],
    ]
    if uid is not None:
        rows.append([
            InlineKeyboardButton("🖼️ Voir miniature", callback_data="thumb:show"),
            InlineKeyboardButton("🗑️ Supprimer miniature", callback_data="thumb:delete"),
        ])
    rows.append([InlineKeyboardButton("🗑️ Annuler", callback_data="clear")])
    return InlineKeyboardMarkup(rows)


def join_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Rejoindre NEXA", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ Vérifier", callback_data="verify")],
        ]
    )


# ============================================================
# MEMBERSHIP
# ============================================================

async def is_member(context, uid):
    if uid in ADMINS:
        return True

    try:
        member = await context.bot.get_chat_member(CHANNEL, uid)
        return (
            member.status in ("creator", "administrator", "member")
            or (
                member.status == "restricted"
                and member.is_member
            )
        )
    except TelegramError as exc:
        log.warning("Membership check failed: %s", exc)
        return False


async def ensure_member(update, context):
    user = update.effective_user
    message = update.effective_message

    if not user:
        return False

    if await is_member(context, user.id):
        return True

    if message:
        await message.reply_text(
            "🔐 Rejoins le canal NEXA avant de renommer un fichier.",
            reply_markup=join_menu(),
        )
    return False


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    record_user(user.id)

    text = (
        "⚡ <b>DASH RENAMER</b>\n"
        "Créé par NEXA\n\n"
        "Envoie un document, une vidéo, un audio ou une image.\n"
        "Je te renverrai ton fichier avec le nouveau nom.\n\n"
        "✏️ <b>Renommage uniquement</b>\n"
        "📁 Aucun réencodage"
    )

    logo = BASE_DIR / "dash.png"
    log.info("Recherche de dash.png : %s", logo)

    if logo.is_file():
        try:
            with logo.open("rb") as image:
                await update.message.reply_photo(
                    photo=image,
                    caption=text,
                    parse_mode="HTML",
                )
            log.info("dash.png envoyé avec succès")
            return
        except Exception:
            log.exception("Erreur lors de l'envoi de dash.png")

    log.error("dash.png introuvable : %s", logo)
    await update.message.reply_text(text, parse_mode="HTML")


# ============================================================
# RECEIVE FILE
# IMPORTANT : aucune limite de taille n'est appliquée par Dash.
# ============================================================

async def _store_session(update: Update, data: dict):
    uid = update.effective_user.id
    record_user(uid)
    SESSIONS[uid] = data
    WAITING.discard(uid)

    await update.message.reply_text(
        (
            "📁 <b>Fichier reçu</b>\n\n"
            f"Nom : <code>{html.escape(data['name'])}</code>\n"
            f"Taille : {human_size(data['size'])}\n"
            f"{thumbnail_status(uid)}\n\n"
            "Choisis une action :"
        ),
        parse_mode="HTML",
        reply_markup=file_menu(uid),
    )


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Documents/videos/audios/voices/animations only.
    # PHOTO is intentionally excluded.
    if not claim_message(update):
        return

    data = file_info(update.message)
    if not data:
        return
    await _store_session(update, data)


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Exactly one photo handler.
    # Every Telegram photo contains several PhotoSize objects. The smallest
    # one is kept as the thumbnail reference; the largest one is used for
    # the actual rename/download operation.
    if not update.message or not update.message.photo:
        return

    if not claim_message(update):
        return

    thumbnail = update.message.photo[0]
    item = update.message.photo[-1]

    # Persist EVERY received photo as a thumbnail record.
    save_photo_thumbnail(update, thumbnail.file_id)

    data = {
        # Largest PhotoSize = actual image to rename/send.
        "file_id": item.file_id,
        "name": "photo.jpg",
        "size": item.file_size or 0,
        "kind": "photo",
        "duration": None,

        # Smallest PhotoSize = persistent thumbnail reference.
        "thumbnail_file_id": thumbnail.file_id,
        "thumbnail_size": thumbnail.file_size or 0,
        "thumbnail_width": thumbnail.width,
        "thumbnail_height": thumbnail.height,
        "thumbnail_saved": True,
    }

    await _store_session(update, data)


# ============================================================
# MENU / RENAME
# ============================================================

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SESSIONS.get(update.effective_user.id)
    if session:
        await update.message.reply_text(
            "📁 Ton dernier fichier :",
            reply_markup=file_menu(update.effective_user.id),
        )
    else:
        await update.message.reply_text(
            "📁 Envoie un fichier pour commencer."
        )


async def rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in SESSIONS:
        await update.message.reply_text("⚠️ Envoie d'abord un fichier.")
        return

    if await ensure_member(update, context):
        SESSIONS[uid].pop("pending_name", None)
        SESSIONS[uid].pop("output_mode", None)
        WAITING.add(uid)
        await update.message.reply_text(
            "✏️ <b>Renommer le fichier</b>\n\n"
            "Envoie maintenant le nouveau nom.\n"
            "Exemple : <code>Mon Film 2026</code>",
            parse_mode="HTML",
        )


async def do_rename(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    proposed: str,
):
    uid = update.effective_user.id
    session = SESSIONS.get(uid)
    message = update.effective_message
    chat = update.effective_chat

    # A callback query has update.message == None. Always use the effective
    # message/chat so the rename button works from both messages and buttons.
    if not message or not chat:
        log.error("Rename update has no effective message/chat | user=%s", uid)
        return

    if not session:
        await message.reply_text("⚠️ Aucun fichier actif.")
        return

    if not await ensure_member(update, context):
        return

    proposed = proposed.strip()
    if not proposed:
        await message.reply_text("⚠️ Le nouveau nom ne peut pas être vide.")
        WAITING.add(uid)
        return

    lock = LOCKS.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await message.reply_text("⏳ Un renommage est déjà en cours.")
        return

    async with lock:
        old_name = session["name"]
        name = renamed_name(old_name, proposed)
        status = await message.reply_text("⏳ Téléchargement du fichier…")
        b2_key = None
        b2_uploaded = False

        try:
            with tempfile.TemporaryDirectory(prefix="dash_") as directory:
                source = Path(directory) / safe_name(old_name)
                target = Path(directory) / name

                # Large-file friendly Telegram download timeouts.
                tg_file = await context.bot.get_file(
                    session["file_id"],
                    read_timeout=3600,
                    write_timeout=3600,
                    connect_timeout=120,
                    pool_timeout=120,
                )
                await tg_file.download_to_drive(
                    custom_path=source,
                    read_timeout=3600,
                    write_timeout=3600,
                    connect_timeout=120,
                    pool_timeout=120,
                )

                if not source.exists():
                    raise RuntimeError("Le téléchargement Telegram a échoué.")

                # Rename only: no conversion, no compression.
                if source != target:
                    source.rename(target)

                if not target.exists():
                    raise RuntimeError("Le fichier renommé n'existe pas.")

                await status.edit_text(
                    "📤 <b>Envoi du fichier renommé…</b>\n"
                    f"📁 <code>{html.escape(name)}</code>",
                    parse_mode="HTML",
                )

                # Download the permanent thumbnail only when one exists.
                # It is tiny compared with the main file.
                thumbnail_path = await download_current_thumbnail(
                    context, uid, directory
                )

                common = dict(
                    filename=name,
                    caption=(
                        "✅ <b>Renommage terminé</b>\n"
                        f"📁 <code>{html.escape(name)}</code>"
                    ),
                    parse_mode="HTML",
                    read_timeout=3600,
                    write_timeout=3600,
                    connect_timeout=120,
                    pool_timeout=120,
                )

                output_mode = session.get("output_mode", "document")

                with target.open("rb") as file:
                    if output_mode == "video" and session.get("kind") == "video":
                        kwargs = dict(common)
                        kwargs.pop("filename", None)
                        if thumbnail_path:
                            with thumbnail_path.open("rb") as thumb:
                                await chat.send_video(
                                    video=file,
                                    thumbnail=thumb,
                                    **kwargs,
                                )
                        else:
                            await chat.send_video(video=file, **kwargs)
                    else:
                        kwargs = dict(common)
                        if thumbnail_path:
                            with thumbnail_path.open("rb") as thumb:
                                await chat.send_document(
                                    document=file,
                                    thumbnail=thumb,
                                    **kwargs,
                                )
                        else:
                            await chat.send_document(
                                document=file,
                                **kwargs,
                            )

                # Telegram already has the result. B2 is secondary and is
                # deliberately done AFTER delivery so a bad B2 key cannot
                # delay or break the rename.
                if B2_ENABLED:
                    b2_key = (
                        f"{B2_PREFIX}/{uid}/"
                        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}/"
                        f"{name}"
                    )
                    b2_uploaded = await b2_upload_safe(target, b2_key)

            record_rename(uid, old_name, name)
            SESSIONS[uid] = {
                **session,
                "name": name,
                "pending_name": None,
            }

            mode_label = (
                "🎬 Vidéo"
                if session.get("output_mode") == "video"
                else "📄 Document"
            )
            thumb_label = (
                "🖼️ Miniature : utilisée"
                if current_thumbnail(uid)
                else "🖼️ Miniature : aucune"
            )

            text = (
                "✨ <b>Opération terminée</b>\n\n"
                f"📁 <code>{html.escape(name)}</code>\n"
                f"📤 Format : {mode_label}\n"
                f"{thumb_label}"
            )
            if B2_ENABLED and not b2_uploaded:
                text += "\n⚠️ Copie B2 non enregistrée."

            try:
                await status.edit_text(text, parse_mode="HTML")
            except TelegramError:
                pass

        except Exception as exc:
            log.exception("Rename failed")
            try:
                await status.edit_text(
                    "❌ <b>Échec du renommage.</b>\n\n"
                    f"<code>{html.escape(str(exc)[:1000])}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        finally:
            if b2_uploaded and b2_key and not B2_RETENTION:
                await b2_delete_safe(b2_key)


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid in BROADCAST and uid in ADMINS:
        BROADCAST.discard(uid)

        with database() as db:
            users = [
                row[0]
                for row in db.execute(
                    "SELECT user_id FROM users"
                )
            ]

        sent = 0
        for target in users:
            try:
                await context.bot.copy_message(
                    chat_id=target,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id,
                )
                sent += 1
                await asyncio.sleep(0.05)
            except TelegramError:
                continue

        await update.message.reply_text(
            f"📢 Diffusion terminée : {sent}/{len(users)} destinataires."
        )
        return

    if uid in WAITING:
        WAITING.discard(uid)

        session = SESSIONS.get(uid)
        if not session:
            await update.message.reply_text("⚠️ Aucun fichier actif.")
            return

        proposed = update.message.text.strip()
        if not proposed:
            await update.message.reply_text("⚠️ Le nouveau nom ne peut pas être vide.")
            WAITING.add(uid)
            return

        session["pending_name"] = proposed
        SESSIONS[uid] = session

        # Modern, compact output selector.
        await update.message.reply_text(
            "🎯 <b>Nom enregistré</b>\n\n"
            f"📁 <code>{html.escape(proposed)}</code>\n\n"
            "📤 <b>Comment veux-tu recevoir le fichier ?</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📄 Document",
                            callback_data="output:document",
                        ),
                        InlineKeyboardButton(
                            "🎬 Vidéo",
                            callback_data="output:video",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Annuler",
                            callback_data="clear",
                        ),
                    ],
                ]
            ),
        )
        return


# ============================================================
# CALLBACKS
# ============================================================

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action = query.data

    await query.answer()

    if action == "verify":
        if await is_member(context, uid):
            await query.message.reply_text(
                "✅ Abonnement vérifié !",
                reply_markup=file_menu(uid) if uid in SESSIONS else None,
            )
        else:
            await query.message.reply_text(
                "❌ Abonnement non détecté. Vérifie que tu as rejoint NEXA.",
                reply_markup=join_menu(),
            )
        return

    if action.startswith("admin:"):
        if uid not in ADMINS:
            await query.answer("Accès refusé", show_alert=True)
            return

        admin_action = action.split(":", 1)[1]

        if admin_action == "stats":
            with database() as db:
                users = db.execute(
                    "SELECT COUNT(*) FROM users"
                ).fetchone()[0]
                renamed = db.execute(
                    "SELECT COUNT(*) FROM history"
                ).fetchone()[0]
                thumbnails = db.execute(
                    "SELECT COUNT(*) FROM photo_thumbnails"
                ).fetchone()[0]

            await query.message.reply_text(
                (
                    "📊 <b>Statistiques Dash</b>\n\n"
                    f"👤 Utilisateurs : {users}\n"
                    f"✏️ Renommages : {renamed}\n"
                    f"🖼️ Miniatures : {thumbnails}\n"
                    f"📁 Sessions : {len(SESSIONS)}\n"
                    f"☁️ B2 : {'activé' if B2_ENABLED else 'désactivé'}"
                ),
                parse_mode="HTML",
            )

        elif admin_action == "broadcast":
            BROADCAST.add(uid)
            await query.message.reply_text(
                "📢 Envoie maintenant ton message à diffuser.\n"
                "/cancel pour annuler."
            )

        elif admin_action == "cleanup":
            count = len(SESSIONS)
            SESSIONS.clear()
            WAITING.clear()
            await query.message.reply_text(
                f"🧹 {count} sessions supprimées."
            )

        return

    if action == "thumb:show":
        file_id = current_thumbnail(uid)
        if not file_id:
            await query.message.reply_text("🖼️ Aucune miniature permanente enregistrée.")
            return
        try:
            await query.message.reply_photo(
                photo=file_id,
                caption="🖼️ <b>Miniature permanente actuelle</b>",
                parse_mode="HTML",
            )
        except TelegramError:
            log.exception("Unable to show thumbnail from callback | user=%s", uid)
            await query.message.reply_text("⚠️ Impossible d'afficher la miniature.")
        return

    if action == "thumb:delete":
        with database() as db:
            db.execute("DELETE FROM photo_thumbnails WHERE user_id=?", (uid,))
        await query.message.reply_text("🗑️ Miniature permanente supprimée.")
        return

    session = SESSIONS.get(uid)

    if action == "clear":
        SESSIONS.pop(uid, None)
        WAITING.discard(uid)
        await query.message.reply_text("🗑️ Session annulée.")
        return

    if not session:
        await query.message.reply_text(
            "⚠️ Envoie un nouveau fichier."
        )
        return

    if action == "info":
        text = (
            "ℹ️ <b>Informations du fichier</b>\n\n"
            f"📁 Nom : <code>{html.escape(session['name'])}</code>\n"
            f"💾 Taille : {human_size(session['size'])}\n"
            f"📦 Type : {session['kind']}"
        )

        text += "\n" + thumbnail_status(uid)

        if session.get("duration"):
            text += f"\n⏱️ Durée : {session['duration']}s"

        await query.message.reply_text(
            text,
            parse_mode="HTML",
        )
        return

    if action == "rename":
        if await ensure_member(update, context):
            WAITING.add(uid)
            await query.message.reply_text(
                "✏️ <b>Renommer le fichier</b>\n\n"
                "Envoie maintenant le nouveau nom.",
                parse_mode="HTML",
            )
            return

    if action.startswith("output:"):
        mode = action.split(":", 1)[1]
        session = SESSIONS.get(uid)

        if not session:
            await query.message.reply_text("⚠️ Aucun fichier actif.")
            return

        if mode not in ("document", "video"):
            return

        # A video can be delivered either as a normal Telegram video
        # or as a document. Other source types stay documents unless
        # the user explicitly chooses video, in which case we explain
        # that no conversion is performed.
        if mode == "video" and session.get("kind") != "video":
            await query.message.reply_text(
                "⚠️ <b>Mode vidéo indisponible</b>\n\n"
                "Dash ne convertit pas les fichiers. "
                "Le mode 🎬 Vidéo est disponible uniquement pour une vidéo.",
                parse_mode="HTML",
            )
            return

        WAITING.discard(uid)
        proposed = session.pop("pending_name", None)
        if not proposed:
            await query.message.reply_text(
                "⚠️ Le nom à appliquer n'est plus disponible. "
                "Appuie sur « Renommer » et recommence."
            )
            return

        session["output_mode"] = mode
        SESSIONS[uid] = session

        await do_rename(
            update,
            context,
            proposed,
        )
        return


async def show_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    file_id = current_thumbnail(uid)
    if not file_id:
        await update.effective_message.reply_text(
            "🖼️ Aucune miniature permanente enregistrée."
        )
        return

    try:
        await update.effective_chat.send_photo(
            photo=file_id,
            caption="🖼️ <b>Miniature permanente actuelle</b>",
            parse_mode="HTML",
        )
    except TelegramError:
        log.exception("Unable to show thumbnail | user=%s", uid)
        await update.effective_message.reply_text(
            "⚠️ La miniature est enregistrée mais Telegram ne permet pas de l'afficher actuellement."
        )


async def delete_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with database() as db:
        db.execute("DELETE FROM photo_thumbnails WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(
        "🗑️ Miniature permanente supprimée. Tu peux en envoyer une nouvelle à tout moment."
    )


# ============================================================
# HISTORY
# ============================================================

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    with database() as db:
        rows = db.execute(
            """
            SELECT old_name, new_name
            FROM history
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (uid,),
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "📜 Aucun renommage enregistré."
        )
        return

    lines = [
        f"{i}. {html.escape(old)} → {html.escape(new)}"
        for i, (old, new) in enumerate(rows, 1)
    ]

    await update.message.reply_text(
        "📜 <b>Historique</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ============================================================
# BASIC COMMANDS
# ============================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    WAITING.discard(uid)
    BROADCAST.discard(uid)
    await update.message.reply_text("✅ Action annulée.")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in ADMINS:
        await update.message.reply_text("⛔ Accès refusé.")
        return

    await update.message.reply_text(
        "👑 <b>Administration Dash</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📊 Statistiques",
                        callback_data="admin:stats",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📢 Diffusion",
                        callback_data="admin:broadcast",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🧹 Nettoyer",
                        callback_data="admin:cleanup",
                    )
                ],
            ]
        ),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        (
            "⚡ <b>Dash Renamer</b>\n\n"
            "🟢 Service : opérationnel\n"
            "✏️ Fonction : renommage uniquement\n"
            "📏 Limite ajoutée par Dash : aucune\n"
            f"☁️ B2 : {'activé' if B2_ENABLED else 'désactivé'}\n"
            f"📁 Sessions : {len(SESSIONS)}"
        ),
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        (
            "⚡ <b>Dash Renamer</b>\n\n"
            "Envoie un fichier, appuie sur « Renommer » "
            "puis indique son nouveau nom.\n\n"
            "✏️ Renommage uniquement\n"
            "ℹ️ Informations\n"
            "📜 Historique\n\n"
            "/start /menu /rename /history /settings "
            "/status /about /help /cancel /showthumb /delthumb"
        ),
        parse_mode="HTML",
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        (
            "⚙️ <b>Paramètres Dash</b>\n\n"
            "✏️ Renommage sans réencodage.\n"
            "📏 Aucune limite de taille ajoutée par Dash.\n"
            "🗑️ Les fichiers temporaires sont supprimés après traitement.\n"
            f"☁️ B2 : {'configuré' if B2_ENABLED else 'non configuré'}."
        ),
        parse_mode="HTML",
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        (
            "⚡ <b>Dash Renamer</b>\n\n"
            "Créé par <b>NEXA</b>\n"
            f"Canal : {CHANNEL_URL}"
        ),
        parse_mode="HTML",
    )


# ============================================================
# ERRORS / COMMAND MENU
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(
        "Telegram error: %r",
        context.error,
        exc_info=context.error,
    )


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Accueil"),
            BotCommand("menu", "Menu"),
            BotCommand("rename", "Renommer"),
            BotCommand("history", "Historique"),
            BotCommand("settings", "Paramètres"),
            BotCommand("status", "Statut"),
            BotCommand("about", "À propos"),
            BotCommand("help", "Aide"),
            BotCommand("cancel", "Annuler"),
            BotCommand("showthumb", "Voir la miniature"),
            BotCommand("delthumb", "Supprimer la miniature"),
            BotCommand("admin", "Administration"),
        ]
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN est manquant.")

    database().close()

    builder = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .concurrent_updates(False)
    )

    if LOCAL:
        api_base = os.getenv(
            "TELEGRAM_API_BASE_URL",
            "http://127.0.0.1:8081/bot",
        )
        api_file_base = os.getenv(
            "TELEGRAM_API_FILE_BASE_URL",
            "http://127.0.0.1:8081/file/bot",
        )

        log.warning("TELEGRAM_LOCAL_API=true")
        log.warning("Telegram API : %s", api_base)

        builder = (
            builder
            .base_url(api_base)
            .base_file_url(api_file_base)
            .local_mode(True)
        )
    else:
        log.info(
            "Telegram API officielle activée : "
            "https://api.telegram.org"
        )

    app = builder.build()
    log.info(
        "Dash ready | Local API=%s | B2=%s | "
        "Photo handler=single | Persistent message dedup=ON",
        LOCAL,
        B2_ENABLED,
    )

    commands = [
        ("start", start),
        ("menu", menu),
        ("rename", rename),
        ("history", history),
        ("settings", settings),
        ("status", status),
        ("about", about),
        ("help", help_command),
        ("cancel", cancel),
        ("showthumb", show_thumbnail),
        ("delthumb", delete_thumbnail),
        ("admin", admin),
    ]

    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))

    app.add_handler(CallbackQueryHandler(callback))

    # Non-photo files. PHOTO is handled by exactly one dedicated handler.
    app.add_handler(
        MessageHandler(
            (
                filters.Document.ALL
                | filters.VIDEO
                | filters.AUDIO
                | filters.VOICE
                | filters.ANIMATION
            ),
            receive_file,
        ),
        group=0,
    )

    # Photos are handled exactly once and also stored as a thumbnail reference.
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            receive_photo,
        ),
        group=0,
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    app.add_error_handler(error_handler)

    url = (
        os.getenv("WEBHOOK_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
    )

    if url:
        path = os.getenv(
            "WEBHOOK_PATH",
            "telegram",
        ).strip("/")

        webhook_url = f"{url.rstrip('/')}/{path}"

        log.info("Webhook : %s", webhook_url)

        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", "10000")),
            url_path=path,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Démarrage en polling.")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES
        )


if __name__ == "__main__":
    main()