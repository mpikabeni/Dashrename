import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
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
# DASH RENAMER
# Créé par NEXA
#
# Bot centré sur le renommage de fichiers :
# - Menu Telegram (/)
# - Détection document / vidéo / audio / image
# - Boutons Renommer / Annuler
# - Choix de sortie : Document ou Vidéo
# - Barre de progression graphique
# - Miniature automatique pour les vidéos
# - Extension conservée automatiquement
# - Vérification du canal NEXA avant le renommage
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "@Nexa_CG")
NEXA_URL = "https://t.me/Nexa_CG"

# Limite volontairement configurable.
MAX_DOWNLOAD_BYTES = int(
    os.getenv("MAX_DOWNLOAD_BYTES", str(20 * 1024 * 1024))
)

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN est manquant.")

# Session en mémoire.
# Une session = dernier fichier envoyé par l'utilisateur.
sessions: dict[int, dict] = {}

# Utilisateurs qui sont en train de donner un nouveau nom.
waiting_name: set[int] = set()

# Utilisateurs qui doivent choisir le type de sortie.
waiting_output: set[int] = set()

# Historique léger en mémoire.
history: dict[int, list[dict]] = {}


# ============================================================
# OUTILS
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.2f} GB"


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.replace("/", "_").replace("\\", "_")
    name = name.strip(" .")

    if not name:
        name = "fichier"

    return name[:180]


def extension(name: str) -> str:
    return Path(name).suffix


def file_kind_label(kind: str) -> str:
    return {
        "document": "📄 Document",
        "video": "🎬 Vidéo",
        "audio": "🎵 Audio",
        "photo": "🖼️ Image",
        "animation": "🎞️ Animation",
        "voice": "🎙️ Vocal",
    }.get(kind, "📁 Fichier")


def progress_bar(percent: int, length: int = 10) -> str:
    percent = max(0, min(100, percent))
    filled = round(length * percent / 100)
    return "▰" * filled + "▱" * (length - filled)


def session_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Renommer", callback_data="rename"),
            InlineKeyboardButton("🗑️ Annuler", callback_data="cancel"),
        ]
    ])


def output_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Document", callback_data="output:document"),
            InlineKeyboardButton("🎬 Vidéo", callback_data="output:video"),
        ],
        [
            InlineKeyboardButton("↩️ Annuler", callback_data="cancel"),
        ],
    ])


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁 Envoyer un fichier", callback_data="send_file"),
        ],
        [
            InlineKeyboardButton("📜 Historique", callback_data="history"),
            InlineKeyboardButton("❓ Aide", callback_data="help"),
        ],
        [
            InlineKeyboardButton("📢 NEXA", url=NEXA_URL),
        ],
    ])


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Rejoindre NEXA", url=NEXA_URL)],
        [InlineKeyboardButton("✅ Vérifier mon abonnement", callback_data="verify:nexa")],
    ])


def detect_file(message) -> Optional[dict]:
    if message.document:
        f = message.document
        return {
            "kind": "document",
            "file_id": f.file_id,
            "name": f.file_name or "document",
            "size": f.file_size or 0,
            "mime": f.mime_type or "application/octet-stream",
        }

    if message.video:
        f = message.video
        return {
            "kind": "video",
            "file_id": f.file_id,
            "name": f.file_name or "video.mp4",
            "size": f.file_size or 0,
            "mime": f.mime_type or "video",
            "width": f.width,
            "height": f.height,
            "duration": f.duration,
        }

    if message.audio:
        f = message.audio
        return {
            "kind": "audio",
            "file_id": f.file_id,
            "name": f.file_name or "audio.mp3",
            "size": f.file_size or 0,
            "mime": f.mime_type or "audio",
            "duration": f.duration,
        }

    if message.photo:
        f = message.photo[-1]
        return {
            "kind": "photo",
            "file_id": f.file_id,
            "name": "photo.jpg",
            "size": f.file_size or 0,
            "mime": "image/jpeg",
            "width": f.width,
            "height": f.height,
        }

    if message.animation:
        f = message.animation
        return {
            "kind": "animation",
            "file_id": f.file_id,
            "name": f.file_name or "animation.gif",
            "size": f.file_size or 0,
            "mime": "animation",
            "width": f.width,
            "height": f.height,
            "duration": f.duration,
        }

    if message.voice:
        f = message.voice
        return {
            "kind": "voice",
            "file_id": f.file_id,
            "name": "voice.ogg",
            "size": f.file_size or 0,
            "mime": "audio/ogg",
            "duration": f.duration,
        }

    return None


# ============================================================
# NEXA
# ============================================================

async def is_nexa_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if is_admin(user_id):
        return True

    try:
        member = await context.bot.get_chat_member(NEXA_CHANNEL, user_id)
        return member.status in {"creator", "administrator", "member"}
    except TelegramError as exc:
        print("NEXA CHECK:", repr(exc))
        return False


async def require_nexa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id

    if await is_nexa_member(context, user_id):
        return True

    text = (
        "🔐 <b>Accès au renommage</b>\n\n"
        "Pour utiliser le renamer de Dash, rejoins d'abord le canal "
        "<b>NEXA</b>, puis appuie sur vérifier.\n\n"
        "⚡ NEXA — Propulsé par NEXA."
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=subscription_keyboard(),
        )
    else:
        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=subscription_keyboard(),
        )

    return False


# ============================================================
# COMMANDES
# ============================================================

async def set_commands(app: Application) -> None:
    commands = [
        BotCommand("start", "Ouvrir Dash"),
        BotCommand("menu", "Afficher le menu"),
        BotCommand("rename", "Renommer le dernier fichier"),
        BotCommand("history", "Voir l'historique"),
        BotCommand("help", "Aide"),
        BotCommand("cancel", "Annuler l'opération"),
    ]

    await app.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    text = (
        "╭────────────────────────────╮\n"
        "│        ⚡ <b>DASH RENAMER</b>        │\n"
        "╰────────────────────────────╯\n\n"
        f"Salut <b>{user.first_name}</b> 👋\n\n"
        "Envoie-moi simplement ton fichier.\n"
        "Je vais détecter automatiquement son type et te proposer de le renommer.\n\n"
        "✏️ Renommage propre\n"
        "🎬 Vidéos avec miniature\n"
        "📄 Sortie en document\n"
        "📊 Informations du fichier\n"
        "📜 Petit historique\n\n"
        "<i>Simple, propre et rapide.</i>"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "☰ <b>Menu Dash</b>\n\nChoisis une action :",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Aide Dash Renamer</b>\n\n"
        "1️⃣ Envoie une vidéo ou un fichier.\n"
        "2️⃣ Appuie sur <b>✏️ Renommer</b>.\n"
        "3️⃣ Écris le nouveau nom.\n"
        "4️⃣ Choisis <b>📄 Document</b> ou <b>🎬 Vidéo</b>.\n"
        "5️⃣ Dash prépare et renvoie ton fichier.\n\n"
        "L'extension d'origine est conservée automatiquement si tu ne la modifies pas."
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_name.discard(user_id)
    waiting_output.discard(user_id)

    await update.message.reply_text(
        "🗑️ <b>Opération annulée.</b>\n\nTu peux envoyer un autre fichier.",
        parse_mode="HTML",
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    items = history.get(user_id, [])

    if not items:
        await update.message.reply_text(
            "📜 <b>Historique vide.</b>\n\nAucun fichier renommé pour le moment.",
            parse_mode="HTML",
        )
        return

    lines = ["📜 <b>Derniers fichiers renommés</b>\n"]

    for item in items[-10:][::-1]:
        lines.append(
            f"• <code>{item['old']}</code>\n"
            f"  ↳ <b>{item['new']}</b>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not sessions.get(user_id):
        await update.message.reply_text(
            "📁 Envoie d'abord un fichier à renommer."
        )
        return

    if not await require_nexa(update, context):
        return

    waiting_name.add(user_id)
    waiting_output.discard(user_id)

    session = sessions[user_id]

    await update.message.reply_text(
        "✏️ <b>Nouveau nom</b>\n\n"
        f"Ancien nom : <code>{session['name']}</code>\n\n"
        "Écris maintenant le nouveau nom du fichier.\n"
        "Tu peux écrire le nom sans extension.",
        parse_mode="HTML",
    )


# ============================================================
# FICHIER REÇU
# ============================================================

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = detect_file(update.message)

    if not data:
        return

    user_id = update.effective_user.id

    if data["size"] > MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            "⚠️ <b>Fichier trop volumineux</b>\n\n"
            f"Limite actuelle : <b>{format_size(MAX_DOWNLOAD_BYTES)}</b>\n"
            "Cette limite peut être augmentée avec une autre architecture Telegram.",
            parse_mode="HTML",
        )
        return

    data["chat_id"] = update.effective_chat.id
    data["message_id"] = update.message.message_id
    data["caption"] = update.message.caption or ""

    sessions[user_id] = data
    waiting_name.discard(user_id)
    waiting_output.discard(user_id)

    text = (
        "╭────────────────────────────╮\n"
        "│       📁 <b>FICHIER REÇU</b>       │\n"
        "╰────────────────────────────╯\n\n"
        f"{file_kind_label(data['kind'])}\n\n"
        f"📄 <b>Nom :</b> <code>{data['name']}</code>\n"
        f"💾 <b>Taille :</b> {format_size(data['size'])}\n"
        f"🔎 <b>Type :</b> <code>{data['mime']}</code>\n"
    )

    if data.get("width") and data.get("height"):
        text += f"📐 <b>Taille :</b> {data['width']} × {data['height']}\n"

    if data.get("duration"):
        text += f"⏱️ <b>Durée :</b> {data['duration']} s\n"

    text += "\n<b>Que veux-tu faire ?</b>"

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=session_keyboard(),
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    if data == "cancel":
        waiting_name.discard(user_id)
        waiting_output.discard(user_id)

        await query.message.reply_text(
            "🗑️ <b>Annulé.</b>\n\nEnvoie un autre fichier quand tu veux.",
            parse_mode="HTML",
        )
        return

    if data == "rename":
        if not sessions.get(user_id):
            await query.message.reply_text(
                "⚠️ Aucun fichier actif. Envoie d'abord un fichier."
            )
            return

        if not await require_nexa(update, context):
            return

        waiting_name.add(user_id)
        waiting_output.discard(user_id)

        session = sessions[user_id]

        await query.message.reply_text(
            "╭────────────────────────────╮\n"
            "│       ✏️ <b>RENOMMER</b>       │\n"
            "╰────────────────────────────╯\n\n"
            f"Nom actuel : <code>{session['name']}</code>\n\n"
            "➡️ Envoie maintenant le nouveau nom.",
            parse_mode="HTML",
        )
        return

    if data == "verify:nexa":
        if await is_nexa_member(context, user_id):
            await query.message.reply_text(
                "✅ <b>Abonnement vérifié.</b>\n\n"
                "Tu peux maintenant utiliser le renommage.",
                parse_mode="HTML",
            )
        else:
            await query.message.reply_text(
                "❌ Je ne vois pas encore ton abonnement.\n\n"
                "Rejoins le canal NEXA puis réessaie.",
                parse_mode="HTML",
                reply_markup=subscription_keyboard(),
            )
        return

    if data == "output:document":
        if user_id not in waiting_output:
            await query.message.reply_text(
                "⚠️ Aucun renommage en attente."
            )
            return

        waiting_output.discard(user_id)
        session = sessions.get(user_id)

        if not session or "pending_name" not in session:
            await query.message.reply_text(
                "⚠️ Session de renommage expirée."
            )
            return

        await do_rename(
            update,
            context,
            user_id,
            output_type="document",
        )
        return

    if data == "output:video":
        if user_id not in waiting_output:
            await query.message.reply_text(
                "⚠️ Aucun renommage en attente."
            )
            return

        waiting_output.discard(user_id)
        session = sessions.get(user_id)

        if not session or "pending_name" not in session:
            await query.message.reply_text(
                "⚠️ Session de renommage expirée."
            )
            return

        await do_rename(
            update,
            context,
            user_id,
            output_type="video",
        )
        return

    if data == "send_file":
        await query.message.reply_text(
            "📁 Envoie maintenant ton document ou ta vidéo."
        )
        return

    if data == "help":
        await help_command(update, context)
        return

    if data == "history":
        await history_command(update, context)
        return


# ============================================================
# NOM ENVOYÉ PAR L'UTILISATEUR
# ============================================================

async def receive_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in waiting_name:
        return

    if not await require_nexa(update, context):
        return

    session = sessions.get(user_id)

    if not session:
        waiting_name.discard(user_id)
        await update.message.reply_text(
            "⚠️ Ta session a expiré. Envoie de nouveau le fichier."
        )
        return

    new_name = safe_filename(update.message.text or "")

    if not new_name:
        await update.message.reply_text(
            "❌ Nom invalide. Envoie un autre nom."
        )
        return

    old_ext = extension(session["name"])

    # Si l'utilisateur n'a pas écrit d'extension,
    # on conserve automatiquement celle du fichier original.
    if not Path(new_name).suffix and old_ext:
        new_name += old_ext

    session["pending_name"] = new_name

    waiting_name.discard(user_id)
    waiting_output.add(user_id)

    await update.message.reply_text(
        "╭────────────────────────────╮\n"
        "│     📦 <b>FORMAT DE SORTIE</b>     │\n"
        "╰────────────────────────────╯\n\n"
        f"📄 Nouveau nom : <code>{new_name}</code>\n\n"
        "Choisis comment Dash doit te renvoyer le fichier :",
        parse_mode="HTML",
        reply_markup=output_keyboard(),
    )


# ============================================================
# TÉLÉCHARGEMENT
# ============================================================

async def download_file(
    context: ContextTypes.DEFAULT_TYPE,
    session: dict,
    directory: str,
) -> str:

    source_name = safe_filename(session["name"])
    source = os.path.join(directory, source_name)

    telegram_file = await context.bot.get_file(session["file_id"])
    await telegram_file.download_to_drive(source)

    if not os.path.exists(source):
        raise RuntimeError("Téléchargement Telegram échoué.")

    return source


# ============================================================
# MINIATURE VIDÉO
# ============================================================

def make_video_thumbnail(video_path: str, directory: str) -> Optional[str]:
    """
    Génère une miniature JPEG avec FFmpeg si disponible.
    On n'empêche jamais le renommage si FFmpeg est absent.
    """

    if not shutil.which("ffmpeg"):
        return None

    thumbnail = os.path.join(directory, "dash_thumbnail.jpg")

    command = [
        "ffmpeg",
        "-y",
        "-ss", "00:00:01",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", "scale=640:-2",
        thumbnail,
    ]

    result = os.system(
        " ".join(
            f'"{str(x).replace(chr(34), "")}"'
            for x in command
        )
    )

    if result != 0 or not os.path.exists(thumbnail):
        return None

    return thumbnail


# ============================================================
# RENOMMAGE
# ============================================================

async def do_rename(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    output_type: str,
):
    session = sessions.get(user_id)

    if not session:
        await update.effective_message.reply_text(
            "⚠️ Aucun fichier actif."
        )
        return

    new_name = safe_filename(session.get("pending_name", ""))

    if not new_name:
        await update.effective_message.reply_text(
            "⚠️ Nouveau nom introuvable."
        )
        return

    directory = tempfile.mkdtemp(prefix="dash_rename_")
    progress = None

    try:
        progress = await update.effective_message.reply_text(
            "╭────────────────────────────╮\n"
            "│      ⚡ <b>DASH RENAMER</b>      │\n"
            "╰────────────────────────────╯\n\n"
            f"{progress_bar(10)} <b>10%</b>\n"
            "🔍 Préparation du fichier...",
            parse_mode="HTML",
        )

        source = await download_file(context, session, directory)

        await progress.edit_text(
            "╭────────────────────────────╮\n"
            "│      ⚡ <b>DASH RENAMER</b>      │\n"
            "╰────────────────────────────╯\n\n"
            f"{progress_bar(35)} <b>35%</b>\n"
            "⬇️ Fichier téléchargé...",
            parse_mode="HTML",
        )

        output_path = os.path.join(directory, new_name)

        # Le renommage ne modifie pas les données du fichier.
        os.replace(source, output_path)

        thumbnail = None

        if output_type == "video":
            # Si la source n'est pas une vidéo, on renvoie en document.
            if session["kind"] == "video":
                thumbnail = make_video_thumbnail(
                    output_path,
                    directory,
                )
            else:
                output_type = "document"

        await progress.edit_text(
            "╭────────────────────────────╮\n"
            "│      ⚡ <b>DASH RENAMER</b>      │\n"
            "╰────────────────────────────╯\n\n"
            f"{progress_bar(70)} <b>70%</b>\n"
            "⚙️ Préparation de l'envoi...",
            parse_mode="HTML",
        )

        caption = (
            f"✏️ <b>{new_name}</b>\n"
            "⚡ Renommé avec Dash\n"
            "© NEXA"
        )

        with open(output_path, "rb") as file_obj:

            if output_type == "video" and session["kind"] == "video":
                kwargs = {
                    "video": file_obj,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "supports_streaming": True,
                }

                if thumbnail and os.path.exists(thumbnail):
                    with open(thumbnail, "rb") as thumb:
                        kwargs["thumbnail"] = thumb
                        await update.effective_chat.send_video(**kwargs)
                else:
                    await update.effective_chat.send_video(**kwargs)

            else:
                await update.effective_chat.send_document(
                    document=file_obj,
                    filename=new_name,
                    caption=caption,
                    parse_mode="HTML",
                )

        await progress.edit_text(
            "╭────────────────────────────╮\n"
            "│      ⚡ <b>DASH RENAMER</b>      │\n"
            "╰────────────────────────────╯\n\n"
            f"{progress_bar(100)} <b>100%</b>\n"
            "✅ <b>Renommage terminé !</b>\n\n"
            f"📄 <code>{new_name}</code>",
            parse_mode="HTML",
        )

        history.setdefault(user_id, []).append({
            "old": session["name"],
            "new": new_name,
        })

        # Maximum 20 entrées par utilisateur.
        history[user_id] = history[user_id][-20:]

        # Nettoyage de l'état de travail.
        session.pop("pending_name", None)

    except Exception as exc:
        print("DASH RENAME ERROR:", repr(exc))

        error_text = (
            "❌ <b>Le renommage a échoué.</b>\n\n"
            "Le fichier original n'a pas été modifié.\n\n"
            f"<code>{str(exc)[:700]}</code>"
        )

        if progress:
            try:
                await progress.edit_text(
                    error_text,
                    parse_mode="HTML",
                )
            except TelegramError:
                await update.effective_message.reply_text(
                    error_text,
                    parse_mode="HTML",
                )
        else:
            await update.effective_message.reply_text(
                error_text,
                parse_mode="HTML",
            )

    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ============================================================
# APPLICATION
# ============================================================

async def post_init(application: Application):
    await set_commands(application)
    print("⚡ Dash Renamer démarré.")
    print("☰ Menu Telegram configuré.")
    print(f"📢 Canal NEXA : {NEXA_CHANNEL}")


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rename", rename_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(
        CallbackQueryHandler(callbacks)
    )

    # Le texte est traité avant le filtre fichier.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_new_name,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.PHOTO
            | filters.ANIMATION
            | filters.VOICE,
            receive_file,
        )
    )

    # Render Web Service -> webhook.
    port = int(os.getenv("PORT", "10000"))
    public_url = (
        os.getenv("WEBHOOK_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
    )

    if not public_url:
        raise RuntimeError(
            "WEBHOOK_URL ou RENDER_EXTERNAL_URL est requis."
        )

    public_url = public_url.rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", "telegram").strip("/")
    webhook_url = f"{public_url}/{webhook_path}"

    print("🌐 Port:", port)
    print("🔗 Webhook:", webhook_url)

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
