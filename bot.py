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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('dash')
TOKEN = os.environ['BOT_TOKEN']
CHANNEL = os.getenv('NEXA_CHANNEL', '@Nexa_CG')
CHANNEL_URL = os.getenv('NEXA_CHANNEL_URL', 'https://t.me/Nexa_CG')
ADMINS = {int(v) for v in os.getenv('ADMIN_IDS', '').split(',') if v.strip().isdigit()}
LOCAL = os.getenv('TELEGRAM_LOCAL_API', 'false').lower() in ('true', '1', 'yes')
MAX_SIZE = int(os.getenv('MAX_DOWNLOAD_BYTES', '0'))
DB_PATH = os.getenv('DB_PATH', '/app/data/dash.sqlite3')
B2_BUCKET = os.getenv('B2_BUCKET', '')
B2_ENDPOINT = os.getenv('B2_ENDPOINT', '')
B2_KEY_ID = os.getenv('B2_KEY_ID', '')
B2_APPLICATION_KEY = os.getenv('B2_APPLICATION_KEY', '')
B2_ENABLED = all((B2_BUCKET, B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY))
B2_PREFIX = os.getenv('B2_PREFIX', 'dash-renamer').strip('/')
B2_RETENTION = os.getenv('B2_RETENTION', 'false').lower() in ('true', '1', 'yes')
SESSIONS = {}
WAITING = set()
BROADCAST = set()
LOCKS = {}


def database():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, old_name TEXT NOT NULL, new_name TEXT NOT NULL, created_at TEXT NOT NULL)')
    con.commit()
    return con


def record_user(uid):
    with database() as db:
        db.execute('INSERT OR IGNORE INTO users VALUES (?, ?)', (uid, datetime.now(timezone.utc).isoformat()))


def record_rename(uid, old, new):
    with database() as db:
        db.execute('INSERT INTO history (user_id, old_name, new_name, created_at) VALUES (?, ?, ?, ?)', (uid, old, new, datetime.now(timezone.utc).isoformat()))


def b2_client():
    return boto3.client('s3', endpoint_url=B2_ENDPOINT, aws_access_key_id=B2_KEY_ID,
                        aws_secret_access_key=B2_APPLICATION_KEY,
                        config=Config(signature_version='s3v4', retries={'max_attempts': 3}))


def safe_name(name):
    name = str(name).replace('\\', '/').split('/')[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '_', name).strip(' .')
    return name[:180] or 'fichier'


def renamed_name(old, proposed):
    name = safe_name(proposed)
    suffix = Path(old).suffix
    if suffix and not Path(name).suffix:
        name += suffix
    return safe_name(name)


def file_info(message):
    for attr, default in [('document', 'document.bin'), ('video', 'video.mp4'), ('audio', 'audio.mp3'),
                          ('animation', 'animation.gif'), ('voice', 'vocal.ogg')]:
        item = getattr(message, attr, None)
        if item:
            return {'file_id': item.file_id, 'name': getattr(item, 'file_name', None) or default,
                    'size': item.file_size or 0, 'kind': attr, 'duration': getattr(item, 'duration', None)}
    if message.photo:
        item = message.photo[-1]
        return {'file_id': item.file_id, 'name': 'photo.jpg', 'size': item.file_size or 0, 'kind': 'photo'}
    return None


def human_size(size):
    for unit in ('o', 'Ko', 'Mo', 'Go'):
        if size < 1024 or unit == 'Go':
            return f'{size:.1f} {unit}'
        size /= 1024


def file_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✏️ Renommer', callback_data='rename'), InlineKeyboardButton('ℹ️ Infos', callback_data='info')],
        [InlineKeyboardButton('🗑️ Annuler', callback_data='clear')],
    ])


def join_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📢 Rejoindre NEXA', url=CHANNEL_URL)],
        [InlineKeyboardButton('✅ Vérifier', callback_data='verify')],
    ])


async def is_member(context, uid):
    if uid in ADMINS:
        return True
    try:
        member = await context.bot.get_chat_member(CHANNEL, uid)
        return member.status in ('creator', 'administrator', 'member') or (member.status == 'restricted' and member.is_member)
    except TelegramError as exc:
        log.warning('Membership check failed: %s', exc)
        return False


async def ensure_member(update, context):
    if await is_member(context, update.effective_user.id):
        return True
    await update.effective_message.reply_text('🔐 Rejoins le canal NEXA avant de renommer un fichier.', reply_markup=join_menu())
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    record_user(update.effective_user.id)
    text = ('⚡ <b>DASH RENAMER</b>\nCréé par NEXA\n\n'
            'Envoie un document, une vidéo, un audio ou une image. '
            'Je te renverrai ton fichier avec le nouveau nom.\n\n'
            '✏️ Renommage uniquement • 📁 Aucun réencodage')
    logo = Path(__file__).with_name('dash.png')
    if logo.exists():
        try:
            with logo.open('rb') as f:
                await update.message.reply_photo(f, caption=text, parse_mode='HTML')
            return
        except TelegramError:
            log.exception('Welcome image failed')
    await update.message.reply_text(text, parse_mode='HTML')


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = file_info(update.message)
    if not data:
        return
    uid = update.effective_user.id
    record_user(uid)
    if MAX_SIZE > 0 and data['size'] > MAX_SIZE:
        await update.message.reply_text(f'⚠️ Taille maximale configurée : {human_size(MAX_SIZE)}')
        return
    SESSIONS[uid] = data
    WAITING.discard(uid)
    await update.message.reply_text(
        f"📁 <b>Fichier reçu</b>\n\nNom : <code>{html.escape(data['name'])}</code>\n"
        f"Taille : {human_size(data['size'])}\n\nChoisis une action :",
        parse_mode='HTML', reply_markup=file_menu())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SESSIONS.get(update.effective_user.id)
    if session:
        await update.message.reply_text('📁 Ton dernier fichier :', reply_markup=file_menu())
    else:
        await update.message.reply_text('📁 Envoie un fichier pour commencer.')


async def rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in SESSIONS:
        await update.message.reply_text("⚠️ Envoie d'abord un fichier.")
    elif await ensure_member(update, context):
        WAITING.add(uid)
        await update.message.reply_text('✏️ Envoie le nouveau nom du fichier.\nExemple : Mon Film 2026')


async def do_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, proposed: str):
    uid = update.effective_user.id
    session = SESSIONS.get(uid)
    if not session:
        await update.message.reply_text('⚠️ Aucun fichier actif.')
        return
    if not await ensure_member(update, context):
        return
    lock = LOCKS.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text('⏳ Un renommage est déjà en cours.')
        return
    async with lock:
        name = renamed_name(session['name'], proposed)
        status = await update.message.reply_text('⏳ Téléchargement et renommage en cours…')
        b2_key = None
        try:
            with tempfile.TemporaryDirectory(prefix='dash_') as directory:
                source = Path(directory) / safe_name(session['name'])
                target = Path(directory) / name
                tg_file = await context.bot.get_file(session['file_id'])
                await tg_file.download_to_drive(custom_path=source)
                if source != target:
                    source.rename(target)
                if B2_ENABLED:
                    b2_key = f"{B2_PREFIX}/{uid}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}/{name}"
                    await asyncio.to_thread(b2_client().upload_file, str(target), B2_BUCKET, b2_key)
                await status.edit_text('📤 Envoi du fichier renommé…')
                # Sending as a document preserves the exact filename for every input type.
                with target.open('rb') as f:
                    await update.message.reply_document(document=f, filename=name,
                                                        caption=f'✅ Renommé par Dash Renamer\n📁 {name}',
                                                        read_timeout=3600, write_timeout=3600)
            record_rename(uid, session['name'], name)
            SESSIONS[uid] = {**session, 'name': name}
            await status.edit_text('✅ Renommage terminé !')
        except Exception as exc:
            log.exception('Rename failed')
            await status.edit_text('❌ Échec du renommage. Vérifie les limites Telegram et les journaux Render.')
        finally:
            if b2_key and not B2_RETENTION:
                try:
                    await asyncio.to_thread(b2_client().delete_object, Bucket=B2_BUCKET, Key=b2_key)
                except Exception:
                    log.exception('B2 cleanup failed')


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in BROADCAST and uid in ADMINS:
        BROADCAST.discard(uid)
        with database() as db:
            users = [row[0] for row in db.execute('SELECT user_id FROM users')]
        sent = 0
        for target in users:
            try:
                await context.bot.copy_message(chat_id=target, from_chat_id=update.effective_chat.id,
                                               message_id=update.message.message_id)
                sent += 1
                await asyncio.sleep(0.05)
            except TelegramError:
                continue
        await update.message.reply_text(f'📢 Diffusion terminée : {sent}/{len(users)} destinataires.')
    elif uid in WAITING:
        WAITING.discard(uid)
        await do_rename(update, context, update.message.text.strip())


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action = query.data
    await query.answer()
    if action == 'verify':
        if await is_member(context, uid):
            await query.message.reply_text('✅ Abonnement vérifié !', reply_markup=file_menu() if uid in SESSIONS else None)
        else:
            await query.message.reply_text('❌ Abonnement non détecté. Vérifie que tu as rejoint NEXA.', reply_markup=join_menu())
        return
    if action.startswith('admin:'):
        if uid not in ADMINS:
            await query.answer('Accès refusé', show_alert=True)
            return
        action = action.split(':', 1)[1]
        if action == 'stats':
            with database() as db:
                users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
                renamed = db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
            await query.message.reply_text(f'📊 Utilisateurs : {users}\n✏️ Renommages : {renamed}\n📁 Sessions : {len(SESSIONS)}\n☁️ B2 : {"activé" if B2_ENABLED else "désactivé"}')
        elif action == 'broadcast':
            BROADCAST.add(uid)
            await query.message.reply_text('📢 Envoie maintenant ton message à diffuser. /cancel pour annuler.')
        elif action == 'cleanup':
            count = len(SESSIONS)
            SESSIONS.clear()
            WAITING.clear()
            await query.message.reply_text(f'🧹 {count} sessions supprimées.')
        return
    session = SESSIONS.get(uid)
    if action == 'clear':
        SESSIONS.pop(uid, None)
        WAITING.discard(uid)
        await query.message.reply_text('🗑️ Session annulée.')
    elif not session:
        await query.message.reply_text('⚠️ Envoie un nouveau fichier.')
    elif action == 'info':
        await query.message.reply_text(f"ℹ️ Nom : {session['name']}\nTaille : {human_size(session['size'])}\nType : {session['kind']}")
    elif action == 'rename':
        if await ensure_member(update, context):
            WAITING.add(uid)
            await query.message.reply_text('✏️ Envoie le nouveau nom du fichier.')


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with database() as db:
        rows = db.execute('SELECT old_name, new_name FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10',
                          (update.effective_user.id,)).fetchall()
    if not rows:
        await update.message.reply_text('📜 Aucun renommage enregistré.')
    else:
        lines = [f'{i}. {html.escape(old)} → {html.escape(new)}' for i, (old, new) in enumerate(rows, 1)]
        await update.message.reply_text('📜 <b>Historique</b>\n\n' + '\n'.join(lines), parse_mode='HTML')


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    WAITING.discard(uid)
    BROADCAST.discard(uid)
    await update.message.reply_text('✅ Action annulée.')


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text('⛔ Accès refusé.')
        return
    await update.message.reply_text('👑 Administration Dash', reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton('📊 Statistiques', callback_data='admin:stats')],
        [InlineKeyboardButton('📢 Diffusion', callback_data='admin:broadcast')],
        [InlineKeyboardButton('🧹 Nettoyer', callback_data='admin:cleanup')],
    ]))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f'⚡ Dash Renamer opérationnel\n☁️ B2 : {"activé" if B2_ENABLED else "désactivé"}\n📁 Sessions : {len(SESSIONS)}')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('⚡ Dash Renamer\n\nEnvoie un fichier, appuie sur Renommer et saisis le nouveau nom.\n\n/start /menu /rename /history /settings /status /about /help /cancel')


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('⚙️ Dash Renamer\nRenommage sans réencodage.\nLes fichiers sont supprimés du disque après traitement.\n' + ('☁️ B2 configuré.' if B2_ENABLED else '☁️ B2 non configuré.'))


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f'⚡ Dash Renamer\nCréé par NEXA\nCanal : {CHANNEL_URL}')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error('Telegram error: %r', context.error, exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand('start', 'Accueil'), BotCommand('menu', 'Menu'),
                                   BotCommand('rename', 'Renommer'), BotCommand('history', 'Historique'),
                                   BotCommand('settings', 'Paramètres'), BotCommand('status', 'Statut'),
                                   BotCommand('about', 'À propos'), BotCommand('help', 'Aide'),
                                   BotCommand('cancel', 'Annuler'), BotCommand('admin', 'Administration')])


def main():
    database().close()
    builder = Application.builder().token(TOKEN).post_init(post_init).concurrent_updates(False)
    if LOCAL:
        builder = (builder.base_url(os.getenv('TELEGRAM_API_BASE_URL', 'http://127.0.0.1:8081/bot'))
                   .base_file_url(os.getenv('TELEGRAM_API_FILE_BASE_URL', 'http://127.0.0.1:8081/file/bot'))
                   .local_mode(True))
    app = builder.build()
    for command, handler in [('start', start), ('menu', menu), ('rename', rename), ('history', history),
                             ('settings', settings), ('status', status), ('about', about),
                             ('help', help_command), ('cancel', cancel), ('admin', admin)]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO |
                                   filters.VOICE | filters.ANIMATION, receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)
    url = os.getenv('WEBHOOK_URL') or os.getenv('RENDER_EXTERNAL_URL')
    if url:
        path = os.getenv('WEBHOOK_PATH', 'telegram').strip('/')
        app.run_webhook(listen='0.0.0.0', port=int(os.getenv('PORT', '10000')), url_path=path,
                        webhook_url=f'{url.rstrip("/")}/{path}', allowed_updates=Update.ALL_TYPES)
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
