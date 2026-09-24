# bot.py

import os
import re
import shutil
import asyncio
import tempfile
from pathlib import Path

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

from config import (
    BOT_TOKEN,
    NEXA_CHANNEL,
    NEXA_URL,
    ADMIN_IDS,
    PORT,
    WEBHOOK_URL,
    WEBHOOK_PATH,
    TEMP_ROOT,
    MAX_PROCESS_SECONDS,
    TELEGRAM_API_BASE_URL,
    TELEGRAM_API_FILE_BASE_URL,
    TELEGRAM_LOCAL_API,
)

from database import (
    init_db,
    save_user,
    get_users,
    get_history,
    add_rename,
    user_count,
    get_statistics,
    save_broadcast,
)

from services.renamer import (
    safe_filename,
)

from services.broadcast import (
    broadcast_message,
)


# ============================================================
# DASH FILEBOT
# CREATED BY NEXA
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

def is_admin(user_id):
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

    hours, rest = divmod(
        seconds,
        3600
    )

    minutes, seconds = divmod(
        rest,
        60
    )

    if hours:
        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return (
        f"{minutes:02d}:"
        f"{seconds:02d}"
    )


def temp_dir():
    return tempfile.mkdtemp(
        prefix="dash_",
        dir=TEMP_ROOT
    )


def cleanup(directory):
    if directory:
        shutil.rmtree(
            directory,
            ignore_errors=True
        )


async def safe_edit(
    message,
    text,
    **kwargs
):
    try:
        await message.edit_text(
            text,
            **kwargs
        )
    except TelegramError:
        pass


# ============================================================
# MEMBERSHIP
# ============================================================

async def is_nexa_member(
    context,
    user_id
):

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
            "NEXA CHECK ERROR:",
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


async def require_nexa(
    update,
    context
):

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
        "Rejoins notre canal puis appuie sur "
        "<b>Vérifier</b>."
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
# FILE INFORMATION
# ============================================================

def get_file_info(message):

    if message.document:

        f = message.document

        return {
            "kind": "document",
            "file_id": f.file_id,
            "name": f.file_name or "document",
            "size": f.file_size or 0,
            "mime": f.mime_type or "",
            "width": None,
            "height": None,
            "duration": None,
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
            "width": None,
            "height": None,
            "duration": f.duration,
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

    return None


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
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "📦 Compresser",
            callback_data="compress"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "🗑️ Annuler",
            callback_data="cancel"
        )
    ])

    return InlineKeyboardMarkup(
        rows
    )


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
                "📢 Diffusion",
                callback_data="admin_broadcast"
            )
        ],
        [
            InlineKeyboardButton(
                "🧹 Nettoyage",
                callback_data="admin_cleanup"
            )
        ]
    ])


# ============================================================
# COMMANDS
# ============================================================

async def setup_commands(
    application
):

    commands = [
        BotCommand(
            "start",
            "Démarrer Dash"
        ),
        BotCommand(
            "menu",
            "Menu principal"
        ),
        BotCommand(
            "rename",
            "Renommer un fichier"
        ),
        BotCommand(
            "thumbnail",
            "Miniature vidéo"
        ),
        BotCommand(
            "history",
            "Historique"
        ),
        BotCommand(
            "settings",
            "Paramètres"
        ),
        BotCommand(
            "status",
            "État du bot"
        ),
        BotCommand(
            "about",
            "À propos"
        ),
        BotCommand(
            "help",
            "Aide"
        ),
        BotCommand(
            "cancel",
            "Annuler"
        ),
        BotCommand(
            "admin",
            "Administration"
        ),
    ]

    await application.bot.set_my_commands(
        commands
    )


# ============================================================
# START
# ============================================================

async def start(
    update,
    context
):

    save_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.first_name,
        update.effective_user.last_name,
        getattr(
            update.effective_user,
            "language_code",
            "fr"
        )
    )

    text = (
        "⚡ <b>DASH FILEBOT</b>\n\n"
        "Bienvenue sur Dash.\n\n"
        "Envoie directement ton fichier.\n\n"
        "✏️ Renommer\n"
        "🖼️ Miniature vidéo\n"
        "📦 Traitement\n"
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
                "📋 Menu",
                callback_data="menu"
            ),
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

async def menu_command(
    update,
    context
):

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

async def receive_file(
    update,
    context
):

    save_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.first_name,
        update.effective_user.last_name,
        getattr(
            update.effective_user,
            "language_code",
            "fr"
        )
    )

    data = get_file_info(
        update.message
    )

    if not data:
        return

    user_id = update.effective_user.id

    previous = sessions.get(
        user_id,
        {}
    )

    thumbnail_file_id = previous.get(
        "thumbnail_file_id"
    )

    sessions[user_id] = {
        **data,
        "chat_id": update.effective_chat.id,
        "message_id": update.message.message_id,
        "caption": update.message.caption or "",
        "thumbnail_file_id": thumbnail_file_id,
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

    if (
        data.get("width")
        and data.get("height")
    ):

        text += (
            f"📐 <b>Résolution :</b> "
            f"{data['width']} × "
            f"{data['height']}\n"
        )

    if data.get("mime"):

        text += (
            f"🔎 <b>Type :</b> "
            f"{data['mime']}\n"
        )

    if thumbnail_file_id:

        text += (
            "\n🖼️ <b>Miniature personnalisée :</b> ✅\n"
        )

    text += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "Choisis une action :"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=file_keyboard(data)
    )


# ============================================================
# PHOTO = THUMBNAIL
# ============================================================

async def receive_photo(
    update,
    context
):

    user_id = update.effective_user.id

    photo = update.message.photo[-1]

    session = sessions.get(
        user_id,
        {}
    )

    session["thumbnail_file_id"] = (
        photo.file_id
    )

    session["waiting_thumbnail"] = False

    sessions[user_id] = session

    await update.message.reply_text(
        "🖼️ <b>Miniature enregistrée !</b>\n\n"
        "Envoie maintenant la vidéo à laquelle "
        "tu veux appliquer cette miniature.",
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

    destination = os.path.join(
        directory,
        safe_filename(filename)
    )

    telegram_file = await context.bot.get_file(
        file_id
    )

    await telegram_file.download_to_drive(
        destination
    )

    if not os.path.exists(
        destination
    ):

        raise RuntimeError(
            "Le téléchargement du fichier a échoué."
        )

    return destination


# ============================================================
# SEND RESULT
# ============================================================

async def send_result(
    update,
    session,
    path,
    filename,
    mode="document",
    thumbnail=None
):

    chat = update.effective_chat

    if mode == "video":

        video_file = open(
            path,
            "rb"
        )

        thumbnail_file = None

        try:

            kwargs = {
                "video": video_file,
                "caption": (
                    f"✏️ <b>{filename}</b>\n"
                    "⚡ Propulsé par NEXA"
                ),
                "parse_mode": "HTML",
                "supports_streaming": True,
            }

            if (
                thumbnail
                and os.path.exists(thumbnail)
            ):

                thumbnail_file = open(
                    thumbnail,
                    "rb"
                )

                kwargs["thumbnail"] = (
                    thumbnail_file
                )

            await chat.send_video(
                **kwargs
            )

        finally:

            video_file.close()

            if thumbnail_file:
                thumbnail_file.close()

        return

    with open(
        path,
        "rb"
    ) as document:

        await chat.send_document(
            document=document,
            filename=filename,
            caption=(
                f"✏️ <b>{filename}</b>\n"
                "⚡ Propulsé par NEXA"
            ),
            parse_mode="HTML"
        )


# ============================================================
# RENAME
# ============================================================

async def rename_command(
    update,
    context
):

    if not await require_nexa(
        update,
        context
    ):
        return

    session = sessions.get(
        update.effective_user.id
    )

    if not session:

        await update.message.reply_text(
            "📁 Envoie d'abord le fichier."
        )

        return

    session["waiting_name"] = True

    await update.message.reply_text(
        "✏️ <b>Nouveau nom</b>\n\n"
        "Envoie le nouveau nom du fichier.",
        parse_mode="HTML"
    )


async def process_rename(
    update,
    context,
    new_name
):

    user_id = update.effective_user.id

    session = sessions.get(
        user_id
    )

    if not session:

        await update.message.reply_text(
            "⚠️ Aucun fichier actif."
        )

        return

    new_name = safe_filename(
        new_name
    )

    original_extension = Path(
        session["name"]
    ).suffix

    if not Path(
        new_name
    ).suffix:

        new_name += original_extension

    mode = session.get(
        "rename_mode",
        "document"
    )

    directory = temp_dir()

    progress = None

    try:

        progress = await update.message.reply_text(
            "⚙️ <b>DASH</b>\n\n"
            "▰▱▱▱▱▱▱▱▱▱ 10%\n"
            "🔍 Préparation...",
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
            "📦 Renommage...",
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
            mode == "video"
            and session.get(
                "thumbnail_file_id"
            )
        ):

            thumbnail = os.path.join(
                directory,
                "thumbnail.jpg"
            )

            thumbnail_file = (
                await context.bot.get_file(
                    session[
                        "thumbnail_file_id"
                    ]
                )
            )

            await thumbnail_file.download_to_drive(
                thumbnail
            )

        await safe_edit(
            progress,
            "⚙️ <b>DASH</b>\n\n"
            "▰▰▰▰▰▰▰▱▱▱ 70%\n"
            "📤 Envoi...",
            parse_mode="HTML"
        )

        await send_result(
            update,
            session,
            output,
            new_name,
            mode=mode,
            thumbnail=thumbnail
        )

        add_rename(
            user_id,
            session["name"],
            new_name,
            session["kind"]
        )

        stats["renames"] += 1

        await safe_edit(
            progress,
            "⚡ <b>DASH</b>\n\n"
            "▰▰▰▰▰▰▰▰▰▰ 100%\n"
            "✅ <b>Terminé !</b>",
            parse_mode="HTML"
        )

        sessions.pop(
            user_id,
            None
        )

    except Exception as error:

        stats["errors"] += 1

        print(
            "RENAME ERROR:",
            repr(error)
        )

        await update.message.reply_text(
            "❌ <b>Impossible de traiter ce fichier.</b>\n\n"
            "Si le fichier est très volumineux, vérifie que "
            "le <b>Local Bot API Server</b> est bien activé.\n\n"
            f"<code>{str(error)[:800]}</code>",
            parse_mode="HTML"
        )

    finally:

        cleanup(
            directory
        )


# ============================================================
# HISTORY
# ============================================================

async def history_command(
    update,
    context
):

    rows = get_history(
        update.effective_user.id,
        10
    )

    if not rows:

        await update.message.reply_text(
            "📜 Ton historique est vide."
        )

        return

    text = (
        "📜 <b>HISTORIQUE</b>\n\n"
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
# THUMBNAIL
# ============================================================

async def thumbnail_command(
    update,
    context
):

    if not await require_nexa(
        update,
        context
    ):
        return

    await update.message.reply_text(
        "🖼️ <b>Miniature</b>\n\n"
        "Envoie une image.\n"
        "Elle sera utilisée pour la prochaine vidéo.",
        parse_mode="HTML"
    )


# ============================================================
# STATUS
# ============================================================

async def status_command(
    update,
    context
):

    local = (
        "✅ ACTIVÉ"
        if TELEGRAM_LOCAL_API
        else "❌ DÉSACTIVÉ"
    )

    await update.message.reply_text(
        "⚡ <b>DASH STATUS</b>\n\n"
        f"Local Bot API : {local}\n"
        f"Utilisateurs : {user_count()}\n"
        f"Fichiers : {stats['files']}\n"
        f"Renommages : {stats['renames']}\n"
        f"Erreurs : {stats['errors']}",
        parse_mode="HTML"
    )


# ============================================================
# SETTINGS
# ============================================================

async def settings_command(
    update,
    context
):

    await update.message.reply_text(
        "⚙️ <b>PARAMÈTRES DASH</b>\n\n"
        "🌍 Langue : Français\n"
        "🔔 Notifications : Telegram\n"
        "⚡ Mode fichiers volumineux : "
        f"{'Activé' if TELEGRAM_LOCAL_API else 'Désactivé'}",
        parse_mode="HTML"
    )


# ============================================================
# ABOUT
# ============================================================

async def about_command(
    update,
    context
):

    await update.message.reply_text(
        "⚡ <b>DASH FILEBOT</b>\n\n"
        "Gestionnaire de fichiers Telegram "
        "créé par <b>NEXA</b>.\n\n"
        "✏️ Renommage\n"
        "🖼️ Miniatures\n"
        "📦 Traitement\n"
        "⚡ Gros fichiers avec Local Bot API\n\n"
        "© NEXA",
        parse_mode="HTML"
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update,
    context
):

    await update.message.reply_text(
        "❓ <b>AIDE DASH</b>\n\n"
        "/start — Accueil\n"
        "/menu — Menu\n"
        "/rename — Renommer\n"
        "/thumbnail — Miniature\n"
        "/history — Historique\n"
        "/settings — Paramètres\n"
        "/status — État\n"
        "/about — À propos\n"
        "/help — Aide\n"
        "/cancel — Annuler\n"
        "/admin — Administration\n\n"
        "Tu peux aussi envoyer directement "
        "une vidéo ou un document.",
        parse_mode="HTML"
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_command(
    update,
    context
):

    sessions.pop(
        update.effective_user.id,
        None
    )

    context.user_data.clear()

    await update.message.reply_text(
        "🗑️ <b>Opération annulée.</b>",
        parse_mode="HTML"
    )


# ============================================================
# ADMIN
# ============================================================

async def admin_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "⛔ Accès refusé."
        )

        return

    await update.message.reply_text(
        "👑 <b>DASH ADMIN</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


# ============================================================
# BROADCAST
# ============================================================

async def receive_broadcast(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not context.user_data.get(
        "broadcast_waiting"
    ):
        return

    context.user_data[
        "broadcast_waiting"
    ] = False

    result = await broadcast_message(
        context.bot,
        update.effective_user.id,
        update.message
    )

    stats["broadcasts"] += 1

    await update.message.reply_text(
        "📢 <b>DIFFUSION TERMINÉE</b>\n\n"
        f"👥 Total : {result['total']}\n"
        f"✅ Envoyés : {result['sent']}\n"
        f"❌ Échecs : {result['failed']}",
        parse_mode="HTML"
    )


# ============================================================
# TEXT ROUTER
# ============================================================

async def receive_text(
    update,
    context
):

    user_id = update.effective_user.id

    if context.user_data.get(
        "broadcast_waiting"
    ):

        await receive_broadcast(
            update,
            context
        )

        return

    session = sessions.get(
        user_id
    )

    if not session:
        return

    if session.get(
        "waiting_name"
    ):

        if not await require_nexa(
            update,
            context
        ):
            return

        session["waiting_name"] = False
        session["new_name"] = (
            update.message.text.strip()
        )

        await update.message.reply_text(
            "📁 <b>Choisis le format :</b>",
            parse_mode="HTML",
            reply_markup=rename_type_keyboard()
        )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "verify_nexa":

        if (
            is_admin(user_id)
            or await is_nexa_member(
                context,
                user_id
            )
        ):

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

    if data == "send_file":

        await query.message.reply_text(
            "📁 <b>Envoie maintenant ton fichier.</b>",
            parse_mode="HTML"
        )

        return

    if data == "menu":

        await query.message.reply_text(
            "⚡ <b>MENU DASH</b>\n\n"
            "/rename\n"
            "/thumbnail\n"
            "/history\n"
            "/settings\n"
            "/help",
            parse_mode="HTML"
        )

        return

    if data == "help":

        await query.message.reply_text(
            "❓ Utilise /help pour voir toutes "
            "les commandes."
        )

        return

    if data == "cancel":

        sessions.pop(
            user_id,
            None
        )

        await query.edit_message_text(
            "🗑️ <b>Session annulée.</b>",
            parse_mode="HTML"
        )

        return

    if data == "rename_command":

        await rename_command(
            update,
            context
        )

        return

    if data == "thumbnail_command":

        await thumbnail_command(
            update,
            context
        )

        return

    if data == "rename":

        if not await require_nexa(
            update,
            context
        ):
            return

        session = sessions.get(
            user_id
        )

        if not session:
            return

        session["waiting_name"] = True

        await query.message.reply_text(
            "✏️ Envoie le nouveau nom."
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

        session = sessions.get(
            user_id
        )

        if not session:
            return

        session["rename_mode"] = data.split(
            ":",
            1
        )[1]

        new_name = session.get(
            "new_name"
        )

        if not new_name:
            await query.message.reply_text(
                "⚠️ Nom manquant."
            )
            return

        await query.message.reply_text(
            "⚙️ Traitement..."
        )

        await process_rename(
            update,
            context,
            new_name
        )

        return

    if data == "info":

        session = sessions.get(
            user_id
        )

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

        session = sessions.get(
            user_id
        )

        if not session:
            return

        if session["kind"] != "video":

            await query.message.reply_text(
                "🖼️ Cette fonction est réservée "
                "aux vidéos."
            )

            return

        session["waiting_thumbnail"] = True

        await query.message.reply_text(
            "🖼️ <b>Envoie l'image de miniature.</b>",
            parse_mode="HTML"
        )

        return

    if data == "compress":

        await query.message.reply_text(
            "📦 La compression sera traitée "
            "dans le module FFmpeg."
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

        information = get_statistics()

        await query.message.reply_text(
            "📊 <b>STATISTIQUES</b>\n\n"
            f"👥 Utilisateurs : "
            f"{information['users']}\n"
            f"✏️ Renommages : "
            f"{information['renames']}\n"
            f"📢 Diffusions : "
            f"{information['broadcasts']}",
            parse_mode="HTML"
        )

        return

    if data == "admin_users":

        if not is_admin(user_id):
            return

        await query.message.reply_text(
            f"👥 Utilisateurs enregistrés : "
            f"<b>{user_count()}</b>",
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
            "📢 <b>Nouvelle annonce</b>\n\n"
            "Envoie maintenant le texte, "
            "la photo, la vidéo, le document, "
            "l'audio ou le GIF.",
            parse_mode="HTML"
        )

        return

    if data == "admin_cleanup":

        if not is_admin(user_id):
            return

        removed = 0

        root = Path(
            TEMP_ROOT
        )

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


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    stats["errors"] += 1

    print(
        "DASH ERROR:",
        repr(context.error)
    )


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application
):

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
        "LOCAL BOT API:",
        TELEGRAM_LOCAL_API
    )

    print(
        "========================================"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    init_db()

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
    )

    # ========================================================
    # LOCAL BOT API
    # ========================================================

    if TELEGRAM_LOCAL_API:

        builder = builder.base_url(
            TELEGRAM_API_BASE_URL
        )

        builder = builder.base_file_url(
            TELEGRAM_API_FILE_BASE_URL
        )

        # PTB doit savoir qu'il communique avec
        # un serveur Bot API local.
        builder = builder.local_mode(
            True
        )

    application = (
        builder
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
            filters.TEXT & ~filters.COMMAND,
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

    # ========================================================
    # WEBHOOK
    # ========================================================

    if WEBHOOK_URL:

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=(
                WEBHOOK_URL.rstrip("/")
                + "/"
                + WEBHOOK_PATH
            ),
            drop_pending_updates=True
        )

    else:

        application.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()
