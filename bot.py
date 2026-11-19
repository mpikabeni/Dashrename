import os
import time
import shutil
import asyncio
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from aiohttp import web, ClientSession
from PIL import Image
from pyrogram import Client, filters, idle, StopPropagation
from pyrogram.enums import ChatAction, ChatMemberStatus
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import (
    Message, ForceReply, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton,
)

# ---------- Config ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # chaîne de connexion Neon
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").replace(" ", "").split(",") if x]
MAX_SIZE_MB = int(os.environ.get("MAX_SIZE_MB", "1900"))  # baisse selon ton disque
FORCE_CHANNEL = os.environ.get("FORCE_CHANNEL", "Nexa_CG").lstrip("@")  # vide = désactivé
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
LOGO_ID = None       # file_id du logo après le premier envoi (évite de ré-uploader)
member_cache = {}    # user_id -> timestamp d'expiration du cache d'abonnement
START = time.time()

app = Client(
    "dash_rename",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=32,
    max_concurrent_transmissions=4,
)

pending = {}   # user_id -> {"msg", "mode", "thumb", "caption"}
busy = set()   # utilisateurs avec un renommage en cours
os.makedirs("downloads", exist_ok=True)

is_admin = filters.user(ADMINS)
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


# ---------- Base de données (Neon / Postgres) ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id        BIGINT PRIMARY KEY,
    joined    DOUBLE PRECISION NOT NULL,
    last_seen DOUBLE PRECISION NOT NULL,
    renamed   INTEGER NOT NULL DEFAULT 0,
    bytes     BIGINT  NOT NULL DEFAULT 0,
    banned    BOOLEAN NOT NULL DEFAULT FALSE,
    thumb     TEXT,
    caption   TEXT,
    prefix    TEXT
);
"""

pool = None


def clean_dsn(url: str) -> str:
    """asyncpg ne gère pas channel_binding (présent dans certaines chaînes Neon)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def init_db():
    global pool
    pool = await asyncpg.create_pool(
        clean_dsn(DATABASE_URL),
        min_size=1,
        max_size=5,
        statement_cache_size=0,               # compatible avec le pooler de Neon
        max_inactive_connection_lifetime=240,  # recycle avant la mise en veille (5 min)
        command_timeout=60,
    )
    await pool.execute(SCHEMA)


async def _run(method: str, *args):
    """Exécute une requête, avec une nouvelle tentative si Neon vient de se réveiller."""
    for attempt in range(2):
        try:
            return await getattr(pool, method)(*args)
        except (
            asyncpg.exceptions.ConnectionDoesNotExistError,
            asyncpg.exceptions.InterfaceError,
            ConnectionResetError,
            OSError,
        ):
            if attempt:
                raise
            await asyncio.sleep(1)


async def execute(q, *a):
    return await _run("execute", q, *a)


async def fetch(q, *a):
    return await _run("fetch", q, *a)


async def fetchrow(q, *a):
    return await _run("fetchrow", q, *a)


async def fetchval(q, *a):
    return await _run("fetchval", q, *a)


async def get_user(uid: int) -> dict:
    row = await fetchrow("SELECT * FROM users WHERE id=$1", uid)
    return dict(row) if row else {}


# ---------- Utilitaires ----------
def human(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


RUN = ["🚀", "⚡", "🔥", "✨"]
DOTS = ["", ".", "..", "..."]


def fancy_bar(pct: float, size: int = 12) -> str:
    """Barre 🟩 avec une tête qui clignote (🟨/🟧) pour l'effet animé."""
    filled = int(size * pct / 100)
    if filled >= size:
        return "🟩" * size
    head = "🟨" if int(time.time()) % 2 else "🟧"
    return "🟩" * filled + head + "⬜" * (size - filled - 1)


async def animate(msg: Message, frames: list, delay: float = 0.7):
    """Joue une suite d'images en éditant le même message."""
    for f in frames:
        try:
            await msg.edit_text(f)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            pass
        await asyncio.sleep(delay)


async def loading(m: Message, label: str) -> Message:
    msg = await m.reply_text(f"⏳ {label}")
    await animate(msg, [f"⌛ {label}.", f"⏳ {label}..", f"⌛ {label}..."], 0.45)
    return msg


async def react(m: Message, emoji: str, big: bool = False):
    """Réaction animée sur le message de l'utilisateur (sans jamais planter)."""
    try:
        await m.react(emoji, big=big)
    except TypeError:
        try:
            await m.react(emoji)
        except Exception:
            pass
    except Exception:
        pass


def fmt_time(s: float) -> str:
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}j {h}h {m}m {s}s" if d else f"{h}h {m}m {s}s"


def folder_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def clean_downloads(max_age: int = 0):
    """Supprime les dossiers temporaires. max_age=0 : tout ; sinon seulement les plus vieux."""
    n, freed = 0, 0
    for d in os.listdir("downloads"):
        p = os.path.join("downloads", d)
        if max_age and time.time() - os.path.getmtime(p) < max_age:
            continue
        freed += folder_size(p) if os.path.isdir(p) else os.path.getsize(p)
        shutil.rmtree(p, ignore_errors=True)
        n += 1
    return n, freed


async def progress(current, total, msg: Message, start: float, label: str, state: dict):
    now = time.time()
    if now - state.get("last", 0) < 2.0 and current != total:
        return  # limite les edits (anti FloodWait)
    state["last"] = now
    state["i"] = state.get("i", 0) + 1
    i = state["i"]
    pct = current * 100 / total
    speed = current / max(now - start, 0.1)
    eta = int((total - current) / speed) if speed else 0
    text = (
        f"{RUN[i % 4]} **{label}**{DOTS[i % 4]}\n\n"
        f"{fancy_bar(pct)} `{pct:.1f}%`\n"
        f"📦 {human(current)} / {human(total)}\n"
        f"⚡ {human(speed)}/s   ⏳ {eta}s"
    )
    try:
        await msg.edit_text(text)
    except Exception:
        pass
    action = state.get("action")  # indicateur « envoie une vidéo… » en haut du chat
    if action:
        try:
            await msg._client.send_chat_action(msg.chat.id, action)
        except Exception:
            pass


# ---------- Garde : enregistrement + bannissement ----------
@app.on_message(filters.private, group=-1)
async def guard(c: Client, m: Message):
    if not m.from_user:
        return
    uid = m.from_user.id
    now = time.time()
    banned = await fetchval(
        """
        INSERT INTO users (id, joined, last_seen) VALUES ($1, $2, $2)
        ON CONFLICT (id) DO UPDATE SET last_seen = $2
        RETURNING banned
        """,
        uid, now,
    )
    if banned and uid not in ADMINS:
        await m.reply_text("🚫 Tu es banni de ce bot.")
        raise StopPropagation
    if not await is_member(c, uid):
        await m.reply_text(JOIN_TEXT, reply_markup=join_keyboard())
        raise StopPropagation


# ---------- Commandes utilisateur ----------
HELP = (
    "⚡ **Dash Rename Bot**\n\n"
    "1️⃣ Envoie-moi un fichier, une vidéo ou un audio\n"
    "2️⃣ Choisis les options avec les boutons\n"
    "3️⃣ Envoie le nouveau nom (ou « Garder le nom »)\n\n"
    "🖼 /setthumb · /delthumb · /thumb — miniature (ou envoie juste une photo)\n"
    "💬 /setcaption `texte` — légende perso (`{filename}` et `{size}` dispo)\n"
    "🗑 /delcaption — supprimer la légende\n"
    "🏷 /setprefix `[Dash] ` — préfixe ajouté à chaque nom\n"
    "🗑 /delprefix — supprimer le préfixe\n"
    "📊 /me — tes statistiques\n"
    "❌ /cancel — annuler l'opération en attente"
)


JOIN_TEXT = (
    "🔒 **Accès réservé**\n\n"
    "Pour utiliser **Dash Rename Bot**, rejoins d'abord notre canal **Nexa** 👇\n"
    "Puis appuie sur « ✅ J'ai rejoint »."
)


def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Rejoindre Nexa", url=f"https://t.me/{FORCE_CHANNEL}")],
        [InlineKeyboardButton("✅ J'ai rejoint", callback_data="checksub")],
    ])


async def is_member(c: Client, uid: int) -> bool:
    if not FORCE_CHANNEL or uid in ADMINS:
        return True
    if member_cache.get(uid, 0) > time.time():
        return True
    try:
        cm = await c.get_chat_member(f"@{FORCE_CHANNEL}", uid)
        ok = cm.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    except UserNotParticipant:
        ok = False
    except Exception as e:
        # Bot pas admin du canal, canal introuvable, etc. : on ne bloque personne
        print(f"[abonnement] vérification impossible : {e}")
        return True
    if ok:
        member_cache[uid] = time.time() + 300  # cache 5 min
    return ok


async def send_welcome(c: Client, chat_id: int):
    """Message de démarrage : animation de chargement, puis logo + aide + bouton vers le canal."""
    global LOGO_ID
    try:
        intro = await c.send_message(chat_id, "⚡ **Dash Rename Bot**\n\n`▱▱▱▱▱▱▱▱▱▱  0%`")
        frames = []
        for n, pct in [(2, 20), (4, 45), (7, 70), (9, 90)]:
            frames.append(f"⚡ **Dash Rename Bot**\n\n`{'▰' * n}{'▱' * (10 - n)}  {pct}%`\nChargement…")
        frames.append("✅ **Dash Rename Bot** prêt !\n\n`▰▰▰▰▰▰▰▰▰▰  100%`")
        await animate(intro, frames, 0.7)
        await intro.delete()
    except Exception:
        pass
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Canal Nexa", url=f"https://t.me/{FORCE_CHANNEL}")]])
    if LOGO_ID or os.path.exists(LOGO_PATH):
        try:
            sent = await c.send_photo(chat_id, LOGO_ID or LOGO_PATH, caption=HELP, reply_markup=kb)
            LOGO_ID = sent.photo.file_id
            return
        except Exception as e:
            print(f"[logo] envoi impossible : {e}")
    await c.send_message(chat_id, HELP, reply_markup=kb)


@app.on_message(filters.command(["start", "help"]) & filters.private)
async def start(c: Client, m: Message):
    await send_welcome(c, m.chat.id)


@app.on_callback_query(filters.regex(r"^checksub$"))
async def checksub_cb(c: Client, q):
    if await is_member(c, q.from_user.id):
        await q.answer("✅ Merci, bienvenue !")
        try:
            await q.message.delete()
        except Exception:
            pass
        await send_welcome(c, q.message.chat.id)
    else:
        await q.answer("❌ Tu n'as pas encore rejoint le canal.", show_alert=True)


@app.on_message(filters.command("setthumb") & filters.private)
async def setthumb(_, m: Message):
    r = m.reply_to_message
    if not (r and r.photo):
        return await m.reply_text("📸 Réponds à une photo avec /setthumb")
    await execute("UPDATE users SET thumb=$1 WHERE id=$2", r.photo.file_id, m.from_user.id)
    await m.reply_text("✅ Miniature enregistrée !")


@app.on_message(filters.photo & filters.private)
async def photo_auto(_, m: Message):
    await execute("UPDATE users SET thumb=$1 WHERE id=$2", m.photo.file_id, m.from_user.id)
    await m.reply_text("✅ Miniature enregistrée !")


@app.on_message(filters.command("delthumb") & filters.private)
async def delthumb(_, m: Message):
    await execute("UPDATE users SET thumb=NULL WHERE id=$1", m.from_user.id)
    await m.reply_text("🗑 Miniature supprimée.")


@app.on_message(filters.command("thumb") & filters.private)
async def showthumb(_, m: Message):
    u = await get_user(m.from_user.id)
    if u.get("thumb"):
        await m.reply_photo(u["thumb"], caption="🖼 Ta miniature actuelle")
    else:
        await m.reply_text("Aucune miniature définie.")


@app.on_message(filters.command("setcaption") & filters.private)
async def setcaption(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage : `/setcaption Mon texte {filename}`")
    await execute("UPDATE users SET caption=$1 WHERE id=$2", m.text.split(None, 1)[1], m.from_user.id)
    await m.reply_text("✅ Légende enregistrée !")


@app.on_message(filters.command("delcaption") & filters.private)
async def delcaption(_, m: Message):
    await execute("UPDATE users SET caption=NULL WHERE id=$1", m.from_user.id)
    await m.reply_text("🗑 Légende supprimée.")


@app.on_message(filters.command("setprefix") & filters.private)
async def setprefix(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage : `/setprefix [Dash] `")
    prefix = m.text.split(None, 1)[1].replace("/", "_")
    await execute("UPDATE users SET prefix=$1 WHERE id=$2", prefix, m.from_user.id)
    await m.reply_text(f"✅ Préfixe : `{prefix}`")


@app.on_message(filters.command("delprefix") & filters.private)
async def delprefix(_, m: Message):
    await execute("UPDATE users SET prefix=NULL WHERE id=$1", m.from_user.id)
    await m.reply_text("🗑 Préfixe supprimé.")


@app.on_message(filters.command("me") & filters.private)
async def me(_, m: Message):
    u = await get_user(m.from_user.id)
    await m.reply_text(
        "📊 **Tes statistiques**\n\n"
        f"📁 Fichiers renommés : `{u.get('renamed', 0)}`\n"
        f"📦 Volume traité : `{human(u.get('bytes', 0))}`\n"
        f"🖼 Miniature : {'✅' if u.get('thumb') else '❌'}\n"
        f"💬 Légende : {'✅' if u.get('caption') else '❌'}\n"
        f"🏷 Préfixe : `{u.get('prefix') or '—'}`"
    )


@app.on_message(filters.command("cancel") & filters.private)
async def cancel(_, m: Message):
    if pending.pop(m.from_user.id, None):
        await m.reply_text("❌ Opération annulée.")
    else:
        await m.reply_text("Rien à annuler.")


# ---------- Commandes admin ----------
@app.on_message(filters.command("admin") & filters.private & is_admin)
async def admin_help(_, m: Message):
    await m.reply_text(
        "🛠 **Commandes admin**\n\n"
        "📈 /stats — statistiques du bot\n"
        "🧹 /clean — vider les fichiers temporaires du disque\n"
        "🗄 /purge `jours` — supprimer de la base les utilisateurs inactifs\n"
        "🚫 /ban `id` · ✅ /unban `id`\n"
        "📢 /broadcast — réponds à un message pour le diffuser"
    )


@app.on_message(filters.command("stats") & filters.private & is_admin)
async def stats(_, m: Message):
    msg = await loading(m, "Analyse en cours")
    row = await fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE banned) AS banned,
               COALESCE(sum(renamed), 0) AS r,
               COALESCE(sum(bytes), 0) AS b
        FROM users
        """
    )
    d = shutil.disk_usage("/")
    temp = folder_size("downloads")
    await msg.edit_text(
        "📈 **Statistiques du bot**\n\n"
        f"👥 Utilisateurs : `{row['total']}` (🚫 {row['banned']})\n"
        f"📁 Fichiers renommés : `{row['r']}`\n"
        f"📦 Volume total : `{human(row['b'])}`\n"
        f"⚙️ En cours : `{len(busy)}`\n"
        f"🗂 Fichiers temporaires : `{human(temp)}`\n"
        f"💾 Disque libre : `{human(d.free)}` / `{human(d.total)}`\n"
        f"⏱ Uptime : `{fmt_time(time.time() - START)}`"
    )


@app.on_message(filters.command("clean") & filters.private & is_admin)
async def clean(_, m: Message):
    # Si des tâches tournent, on ne supprime que les dossiers vieux de plus de 3 h
    msg = await loading(m, "Nettoyage")
    n, freed = clean_downloads(max_age=10800 if busy else 0)
    note = "\n__(tâches en cours : seuls les vieux dossiers ont été supprimés)__" if busy else ""
    await msg.edit_text(f"🧹 `{n}` dossier(s) supprimé(s) · `{human(freed)}` libérés.{note}")


@app.on_message(filters.command("purge") & filters.private & is_admin)
async def purge(_, m: Message):
    if len(m.command) < 2 or not m.command[1].isdigit() or int(m.command[1]) < 1:
        return await m.reply_text("Usage : `/purge 30` (utilisateurs inactifs depuis 30 jours)")
    days = int(m.command[1])
    cutoff = time.time() - days * 86400
    count = await fetchval(
        "SELECT count(*) FROM users WHERE id <> ALL($1::bigint[]) AND last_seen < $2",
        ADMINS, cutoff,
    )
    if not count:
        return await m.reply_text("✅ Aucun utilisateur inactif à supprimer.")
    await m.reply_text(
        f"⚠️ `{count}` utilisateur(s) inactif(s) depuis plus de `{days}` jour(s) seront supprimés "
        "de la base (miniature, légende, préfixe, stats).",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirmer", callback_data=f"purge_yes:{days}"),
            InlineKeyboardButton("❌ Annuler", callback_data="purge_no"),
        ]]),
    )


@app.on_callback_query(filters.regex(r"^purge_") & is_admin)
async def purge_cb(_, q):
    if q.data == "purge_no":
        await q.message.edit_text("❌ Purge annulée.")
        return await q.answer()
    days = int(q.data.split(":")[1])
    cutoff = time.time() - days * 86400
    status = await execute(
        "DELETE FROM users WHERE id <> ALL($1::bigint[]) AND last_seen < $2",
        ADMINS, cutoff,
    )
    deleted = status.split()[-1]  # "DELETE 12"
    await q.message.edit_text(f"🗄 `{deleted}` utilisateur(s) supprimé(s) de la base.")
    await q.answer("Terminé")


@app.on_message(filters.command("ban") & filters.private & is_admin)
async def ban(_, m: Message):
    if len(m.command) < 2 or not m.command[1].isdigit():
        return await m.reply_text("Usage : `/ban user_id`")
    now = time.time()
    await execute(
        """
        INSERT INTO users (id, joined, last_seen, banned) VALUES ($1, $2, $2, TRUE)
        ON CONFLICT (id) DO UPDATE SET banned = TRUE
        """,
        int(m.command[1]), now,
    )
    await m.reply_text(f"🚫 `{m.command[1]}` banni.")


@app.on_message(filters.command("unban") & filters.private & is_admin)
async def unban(_, m: Message):
    if len(m.command) < 2 or not m.command[1].isdigit():
        return await m.reply_text("Usage : `/unban user_id`")
    await execute("UPDATE users SET banned=FALSE WHERE id=$1", int(m.command[1]))
    await m.reply_text(f"✅ `{m.command[1]}` débanni.")


@app.on_message(filters.command("broadcast") & filters.private & is_admin)
async def broadcast(c: Client, m: Message):
    r = m.reply_to_message
    if not r:
        return await m.reply_text("Réponds au message à diffuser avec /broadcast")
    status = await m.reply_text("📢 Diffusion en cours…")
    ok = fail = 0
    rows = await fetch("SELECT id FROM users WHERE NOT banned")
    total = len(rows)
    for n, row in enumerate(rows, 1):
        try:
            await r.copy(row["id"])
            ok += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
        if n % 20 == 0 and n != total:
            try:
                await status.edit_text(f"📢 Diffusion\n\n{fancy_bar(n * 100 / total)} `{n}/{total}`")
            except Exception:
                pass
    await status.edit_text(f"📢 Terminé : ✅ {ok} · ❌ {fail}")


# ---------- Réception d'un fichier + boutons ----------
def keyboard(st: dict) -> InlineKeyboardMarkup:
    def lab(k, t):
        return ("✅ " if st["mode"] == k else "") + t

    onoff = lambda v: "ON" if v else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(lab("document", "📄 Fichier"), callback_data="mode:document"),
            InlineKeyboardButton(lab("video", "🎬 Vidéo"), callback_data="mode:video"),
            InlineKeyboardButton(lab("audio", "🎵 Audio"), callback_data="mode:audio"),
        ],
        [
            InlineKeyboardButton(f"🖼 Miniature : {onoff(st['thumb'])}", callback_data="thumb"),
            InlineKeyboardButton(f"💬 Légende : {onoff(st['caption'])}", callback_data="cap"),
        ],
        [
            InlineKeyboardButton("⚡ Garder le nom", callback_data="keep"),
            InlineKeyboardButton("❌ Annuler", callback_data="cancel"),
        ],
    ])


def prompt_text(media) -> str:
    return (
        f"📝 Nom actuel : `{media.file_name}`\n\n"
        "Choisis les options ci-dessous, puis envoie le **nouveau nom** "
        "(avec ou sans extension) — ou appuie sur « Garder le nom »."
    )


@app.on_message((filters.document | filters.video | filters.audio) & filters.private)
async def got_file(_, m: Message):
    uid = m.from_user.id
    media = m.document or m.video or m.audio
    size = media.file_size or 0
    if size > MAX_SIZE_MB * 1024 * 1024:
        return await m.reply_text(f"⚠️ Fichier trop gros (max {MAX_SIZE_MB} Mo).")
    if shutil.disk_usage("downloads").free < size + 200 * 1024 * 1024:
        return await m.reply_text("⚠️ Pas assez d'espace disque pour l'instant, réessaie plus tard.")
    if uid in busy:
        return await m.reply_text("⏳ Un renommage est déjà en cours, patiente.")

    u = await get_user(uid)
    st = {
        "msg": m,
        "mode": "video" if m.video else "audio" if m.audio else "document",
        "thumb": bool(u.get("thumb")),
        "caption": True,
    }
    pending[uid] = st
    await react(m, "⚡")
    try:
        await m._client.send_chat_action(m.chat.id, ChatAction.TYPING)
    except Exception:
        pass
    await m.reply_text(prompt_text(media), reply_markup=keyboard(st))


@app.on_callback_query(filters.regex(r"^(mode:|thumb$|cap$|keep$|cancel$)"))
async def options_cb(c: Client, q):
    uid = q.from_user.id
    st = pending.get(uid)
    if not st:
        return await q.answer("Session expirée, renvoie le fichier.", show_alert=True)

    d = q.data
    if d.startswith("mode:"):
        st["mode"] = d.split(":")[1]
    elif d == "thumb":
        u = await get_user(uid)
        if not u.get("thumb"):
            return await q.answer("Aucune miniature définie : envoie une photo d'abord.", show_alert=True)
        st["thumb"] = not st["thumb"]
    elif d == "cap":
        st["caption"] = not st["caption"]
    elif d == "cancel":
        pending.pop(uid, None)
        await q.message.edit_text("❌ Opération annulée.")
        return await q.answer()
    elif d == "keep":
        if uid in busy:
            return await q.answer("Un renommage est déjà en cours.", show_alert=True)
        pending.pop(uid, None)
        await q.answer("C'est parti ⚡")
        status = await q.message.edit_text("🚀 Démarrage…")
        return asyncio.create_task(process(c, uid, q.message.chat.id, st, None, status))

    try:
        await q.message.edit_reply_markup(keyboard(st))
    except Exception:
        pass
    await q.answer()


@app.on_message(filters.text & filters.private & ~filters.regex(r"^/"))
async def got_name(c: Client, m: Message):
    uid = m.from_user.id
    st = pending.pop(uid, None)
    if not st:
        return await m.reply_text("Envoie d'abord un fichier 📁")
    status = await m.reply_text("🚀 Démarrage…")
    await process(c, uid, m.chat.id, st, m.text, status)


# ---------- Traitement ----------
async def process(c: Client, uid: int, chat_id: int, st: dict, typed_name, status: Message):
    src = st["msg"]
    u = await get_user(uid)
    media = src.document or src.video or src.audio
    old_name = media.file_name or ("video.mp4" if src.video else "audio.mp3" if src.audio else "file")

    new_name = (typed_name or old_name).strip().replace("/", "_")
    if typed_name and "." not in new_name and "." in old_name:
        new_name += "." + old_name.rsplit(".", 1)[1]
    new_name = (u.get("prefix") or "") + new_name

    work = f"downloads/{uid}_{int(time.time())}"
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, new_name)
    busy.add(uid)
    t0 = time.time()

    try:
        state = {"action": ChatAction.TYPING}
        start = time.time()
        await c.download_media(
            src, file_name=path,
            progress=progress, progress_args=(status, start, "Téléchargement", state),
        )

        thumb_path = None
        if st["thumb"] and u.get("thumb"):
            thumb_path = await c.download_media(u["thumb"], file_name=f"{work}/thumb.jpg")
            img = Image.open(thumb_path).convert("RGB")
            img.thumbnail((320, 320))
            img.save(thumb_path, "JPEG", quality=85)

        size = os.path.getsize(path)
        if st["caption"]:
            caption = (u.get("caption") or "{filename}").replace("{filename}", new_name).replace("{size}", human(size))
        else:
            caption = ""

        mode = st["mode"]
        state = {"action": {"video": ChatAction.UPLOAD_VIDEO, "audio": ChatAction.UPLOAD_AUDIO}.get(
            mode, ChatAction.UPLOAD_DOCUMENT)}
        start = time.time()
        args = dict(
            chat_id=chat_id, thumb=thumb_path, caption=caption,
            progress=progress, progress_args=(status, start, "Envoi", state),
        )
        mode = st["mode"]
        if mode == "video":
            v = src.video
            await c.send_video(
                video=path, file_name=new_name,
                duration=v.duration if v else 0,
                width=v.width if v else 0,
                height=v.height if v else 0,
                supports_streaming=True, **args,
            )
        elif mode == "audio":
            a = src.audio
            await c.send_audio(audio=path, file_name=new_name, duration=a.duration if a else 0, **args)
        else:
            await c.send_document(document=path, file_name=new_name, force_document=True, **args)

        await execute("UPDATE users SET renamed = renamed + 1, bytes = bytes + $1 WHERE id=$2", size, uid)
        await animate(status, ["✅ **Terminé**", "🎉 **Terminé !**", "🎊 **Terminé !** ⚡"], 0.6)
        await status.edit_text(
            f"🎉 **Terminé !** ⚡\n\n📁 `{new_name}`\n📦 {human(size)}\n⏱ {fmt_time(time.time() - t0)}"
        )
        await react(src, "🎉", big=True)
    except Exception as e:
        try:
            await status.edit_text(f"❌ Erreur : `{e}`")
        except Exception:
            pass
    finally:
        busy.discard(uid)
        shutil.rmtree(work, ignore_errors=True)


# ---------- Mini serveur web (Render) + tâches de fond ----------
async def start_web():
    async def ok(_):
        return web.json_response({"status": "ok", "uptime": int(time.time() - START)})

    web_app = web.Application()
    web_app.router.add_get("/", ok)
    web_app.router.add_get("/health", ok)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()


async def keep_alive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    async with ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                await s.get(url + "/health")
            except Exception:
                pass


async def auto_clean():
    """Nettoyage automatique toutes les heures des dossiers temporaires oubliés (> 3 h)."""
    while True:
        await asyncio.sleep(3600)
        try:
            clean_downloads(max_age=10800)
        except Exception:
            pass


async def main():
    await start_web()
    await init_db()
    await app.start()
    await app.set_bot_commands([
        BotCommand("start", "Démarrer"),
        BotCommand("setthumb", "Définir la miniature"),
        BotCommand("delthumb", "Supprimer la miniature"),
        BotCommand("thumb", "Voir la miniature"),
        BotCommand("setcaption", "Définir la légende"),
        BotCommand("setprefix", "Définir un préfixe"),
        BotCommand("me", "Mes statistiques"),
        BotCommand("cancel", "Annuler"),
    ])
    asyncio.create_task(keep_alive())
    asyncio.create_task(auto_clean())
    print("Dash Rename Bot démarré ⚡")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
