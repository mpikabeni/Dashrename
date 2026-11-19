import os
import re
import time
import shutil
import asyncio
import datetime
import platform
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from aiohttp import web, ClientSession
from PIL import Image
from pyrogram import Client, filters, idle, StopPropagation
from pyrogram.enums import ChatAction, ChatMemberStatus
from pyrogram.errors import (
    FloodWait, UserNotParticipant, UserIsBlocked, InputUserDeactivated, PeerIdInvalid,
)
from pyrogram.types import (
    Message, BotCommand, BotCommandScopeChat, InlineKeyboardMarkup, InlineKeyboardButton,
)

try:  # boucle d'événements bien plus rapide (Linux)
    import uvloop
    uvloop.install()
except ImportError:
    pass

# ---------- Config ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # chaîne de connexion Neon
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").replace(" ", "").split(",") if x]
MAX_SIZE_MB = int(os.environ.get("MAX_SIZE_MB", "1900"))      # baisse selon ton disque
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "4"))        # renommages simultanés (global)
MAX_PER_USER = int(os.environ.get("MAX_PER_USER", "3"))        # renommages simultanés par utilisateur
DL_WORKERS = int(os.environ.get("DL_WORKERS", "4"))           # morceaux téléchargés en parallèle par fichier
MAX_PENDING = int(os.environ.get("MAX_PENDING", "15"))         # fichiers en attente de nom par utilisateur
FORCE_CHANNEL = os.environ.get("FORCE_CHANNEL", "Nexa_CG").lstrip("@")  # vide = désactivé
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(BASE_DIR, "logo.png")
START = time.time()

app = Client(
    "dash_rename",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=64,
    max_concurrent_transmissions=int(os.environ.get("MAX_TRANSMISSIONS", "16")),
    sleep_threshold=60,   # attend automatiquement les petits FloodWait
)

# ---------- État en mémoire ----------
pending = {}       # (user_id, id du message d'invite) -> {"msg", "mode", "thumb", "caption", "t"}
active = {}        # user_id -> nombre de renommages en cours
usems = {}         # user_id -> limite de renommages simultanés par utilisateur
thumb_locks = {}   # user_id -> verrou pour la miniature
ucache = {}        # user_id -> {"exp": ts, "row": dict}  (cache DB 60 s)
member_cache = {}  # user_id -> expiration du cache d'abonnement
thumb_ids = {}     # user_id -> file_id de la miniature déjà sur disque
ann = {}           # admin_id -> annonce en préparation
stat = {"waiting": 0}
FLAGS = {"maintenance": False}
SEM = None         # limite de renommages simultanés (créé dans main)
LOGO_ID = None     # file_id du logo après le premier envoi

os.makedirs("downloads", exist_ok=True)
os.makedirs("thumbs", exist_ok=True)

is_admin = filters.user(ADMINS)
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
RUN = ["🚀", "⚡", "🔥", "✨"]
DOTS = ["", ".", "..", "..."]
LINE = "━━━━━━━━━━━━━━━━"
DAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


# =====================================================================
#  Base de données (Neon / Postgres)
# =====================================================================
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
ALTER TABLE users ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS users_last_seen_idx ON users (last_seen);
CREATE TABLE IF NOT EXISTS daily_stats (
    day       DATE PRIMARY KEY,
    files     INTEGER NOT NULL DEFAULT 0,
    bytes     BIGINT  NOT NULL DEFAULT 0,
    new_users INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS announcements (
    id      SERIAL PRIMARY KEY,
    created DOUBLE PRECISION NOT NULL,
    target  TEXT NOT NULL,
    total   INTEGER NOT NULL,
    ok      INTEGER NOT NULL,
    blocked INTEGER NOT NULL,
    failed  INTEGER NOT NULL,
    secs    DOUBLE PRECISION NOT NULL
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
        max_size=8,
        statement_cache_size=0,                # compatible avec le pooler de Neon
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
    """Profil utilisateur, avec un cache de 60 s pour éviter des allers-retours vers la base."""
    e = ucache.get(uid)
    if e and e["exp"] > time.time():
        return e["row"]
    row = await fetchrow("SELECT * FROM users WHERE id=$1", uid)
    d = dict(row) if row else {}
    if d:
        ucache[uid] = {"exp": time.time() + 60, "row": d}
    return d


async def set_field(uid: int, field: str, value):
    assert field in ("thumb", "caption", "prefix")
    await execute(f"UPDATE users SET {field}=$1 WHERE id=$2", value, uid)
    ucache.pop(uid, None)


async def bump_daily(files: int = 0, size: int = 0, new_users: int = 0):
    await execute(
        """
        INSERT INTO daily_stats (day, files, bytes, new_users) VALUES (CURRENT_DATE, $1, $2, $3)
        ON CONFLICT (day) DO UPDATE SET
            files = daily_stats.files + EXCLUDED.files,
            bytes = daily_stats.bytes + EXCLUDED.bytes,
            new_users = daily_stats.new_users + EXCLUDED.new_users
        """,
        files, size, new_users,
    )


# =====================================================================
#  Utilitaires & design
# =====================================================================
def n_active() -> int:
    return sum(active.values())


def user_keys(uid: int) -> list:
    return [k for k in pending if k[0] == uid]


def human(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(s: float) -> str:
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}j {h}h {m}m {s}s" if d else f"{h}h {m}m {s}s"


def fmt_eta(s: int) -> str:
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def safe(s) -> str:
    """Nettoie un texte pour qu'il ne casse pas le Markdown."""
    out = "".join(ch for ch in str(s or "") if ch not in "`*_~|[]()<>\\").strip()
    return out or "—"


def code(s) -> str:
    """Pour afficher un texte tel quel dans un bloc `code` (garde crochets, espaces…)."""
    return str(s or "").replace("`", "'")


def card(title: str, lines: list, footer: str = None) -> str:
    out = f"**{title}**\n{LINE}\n" + "\n".join(lines)
    if footer:
        out += f"\n{LINE}\n{footer}"
    return out


def table(rows: list, kw: int = 11, vw: int = 26) -> str:
    """Tableau à 2 colonnes (largeur fixe, donc il ne « saute » pas) dans un bloc de code."""
    def cut(t, w):
        t = code(t).replace("\n", " ")
        return t if len(t) <= w else t[:w - 1] + "…"
    top = "┌" + "─" * (kw + 2) + "┬" + "─" * (vw + 2) + "┐"
    sep = "├" + "─" * (kw + 2) + "┼" + "─" * (vw + 2) + "┤"
    end = "└" + "─" * (kw + 2) + "┴" + "─" * (vw + 2) + "┘"
    body = f"\n{sep}\n".join(f"│ {cut(a, kw):<{kw}} │ {cut(b, vw):<{vw}} │" for a, b in rows)
    return f"```\n{top}\n{body}\n{end}\n```"


def tbar(pct: float, size: int = 10) -> str:
    n = max(0, min(size, int(size * pct / 100)))
    return "█" * n + "░" * (size - n) + f" {pct:5.1f}%"


def fancy_bar(pct: float, size: int = 12) -> str:
    """Barre 🟩 avec une tête qui clignote (🟨/🟧) pour l'effet animé."""
    filled = int(size * pct / 100)
    if filled >= size:
        return "🟩" * size
    head = "🟨" if int(time.time()) % 2 else "🟧"
    return "🟩" * filled + head + "⬜" * (size - filled - 1)


def pbar(pct: float, size: int = 10) -> str:
    n = max(0, min(size, round(size * pct / 100)))
    return "▰" * n + "▱" * (size - n)


def spark(values: list) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    mx = max(values) if values else 0
    if not mx:
        return "▁" * len(values)
    return "".join(blocks[min(7, int(v / mx * 7))] if v else "▁" for v in values)


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
    await animate(msg, [f"⌛ {label}.", f"⏳ {label}..", f"⌛ {label}..."], 0.4)
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


_bg_tasks = set()


def _bg(coro):
    """Lance une tâche de fond (sans bloquer le transfert) et la garde en vie."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


async def push_progress(current, total, msg: Message, start: float, label: str, state: dict):
    try:
        if state.get("done"):
            return
        now = time.time()
        state["i"] = state.get("i", 0) + 1
        i = state["i"]
        pct = current * 100 / max(total, 1)
        speed = current / max(now - start, 0.1)
        eta = int((total - current) / speed) if speed else 0
        rows = []
        if state.get("name"):
            rows.append(("Fichier", state["name"]))
        rows += [
            ("Étape", label),
            ("Progression", tbar(pct)),
            ("Taille", f"{human(current)} / {human(total)}"),
            ("Vitesse", f"{human(speed)}/s"),
            ("Restant", fmt_eta(eta)),
        ]
        text = f"{RUN[i % 4]} **EN COURS**{DOTS[i % 4]}\n{table(rows)}"
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
    finally:
        state["busy"] = False


async def progress(current, total, msg: Message, start: float, label: str, state: dict):
    """Appelé à chaque morceau : ne fait que lancer la mise à jour en tâche de fond,
    donc le transfert n'attend jamais Telegram pour afficher la progression."""
    now = time.time()
    if state.get("busy") or (now - state.get("last", 0) < 2.5 and current != total):
        return
    state["last"] = now
    state["busy"] = True
    state["task"] = _bg(push_progress(current, total, msg, start, label, state))


async def settle(state: dict):
    """Attend la dernière mise à jour en cours avant d'afficher le message final."""
    state["done"] = True
    t = state.get("task")
    if t:
        await asyncio.gather(t, return_exceptions=True)


# ---------- Infos système ----------
def read_mem():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                info[k] = int(v.split()[0]) * 1024
    except Exception:
        return None
    return info


def proc_rss() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


# =====================================================================
#  Textes (stats, serveur, etc.) — utilisés par les commandes ET les boutons admin
# =====================================================================
async def stats_text() -> str:
    now = time.time()
    r = await fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE banned)  AS banned,
               count(*) FILTER (WHERE blocked) AS blocked,
               count(*) FILTER (WHERE last_seen > $1) AS a24,
               count(*) FILTER (WHERE last_seen > $2) AS a7,
               count(*) FILTER (WHERE last_seen > $3) AS a30,
               count(*) FILTER (WHERE joined > $1) AS n24,
               count(*) FILTER (WHERE joined > $2) AS n7,
               COALESCE(sum(renamed), 0) AS files,
               COALESCE(sum(bytes), 0)   AS bytes
        FROM users
        """,
        now - 86400, now - 7 * 86400, now - 30 * 86400,
    )
    files, vol = r["files"], r["bytes"]
    avg = vol / files if files else 0
    return card("📈 STATISTIQUES", [
        f"👥 Utilisateurs : `{r['total']}`",
        f"🟢 Actifs : `{r['a24']}` (24 h) · `{r['a7']}` (7 j) · `{r['a30']}` (30 j)",
        f"✨ Nouveaux : `{r['n24']}` (24 h) · `{r['n7']}` (7 j)",
        f"🚫 Bannis : `{r['banned']}` · 🔕 Bloqués : `{r['blocked']}`",
        "",
        f"📁 Fichiers renommés : `{files}`",
        f"📦 Volume traité : `{human(vol)}`",
        f"📏 Taille moyenne : `{human(avg)}`",
        "",
        f"⚙️ En cours : `{n_active()}` · ⏳ En attente : `{stat['waiting']}`",
    ], footer=f"⏱ Uptime : `{fmt_time(time.time() - START)}`")


async def server_text() -> str:
    d = shutil.disk_usage("/")
    used_pct = d.used * 100 / d.total
    lines = [f"💾 Disque : `{human(d.used)}` / `{human(d.total)}`\n      {pbar(used_pct)} `{used_pct:.0f}%`"]
    mem = read_mem()
    if mem and "MemTotal" in mem:
        total = mem["MemTotal"]
        used = total - mem.get("MemAvailable", mem.get("MemFree", 0))
        pct = used * 100 / total
        lines.append(f"🧠 RAM : `{human(used)}` / `{human(total)}`\n      {pbar(pct)} `{pct:.0f}%`")
    lines.append(f"🤖 Mémoire du bot : `{human(proc_rss())}`")
    try:
        l1, l5, l15 = os.getloadavg()
        lines.append(f"🧮 Charge CPU : `{l1:.2f}` · `{l5:.2f}` · `{l15:.2f}`")
    except Exception:
        pass
    lines.append(f"🗂 Fichiers temporaires : `{human(folder_size('downloads'))}`")
    t = time.time()
    try:
        await fetchval("SELECT 1")
        lines.append(f"🗄 Base de données : `{(time.time() - t) * 1000:.0f} ms`")
    except Exception as e:
        lines.append(f"🗄 Base de données : ❌ `{safe(e)[:60]}`")
    lines.append(f"🐍 Python `{platform.python_version()}` · parallèle max `{MAX_PARALLEL}`")
    return card("🖥 SERVEUR", lines, footer=f"⏱ Uptime : `{fmt_time(time.time() - START)}`")


async def daily_text() -> str:
    rows = await fetch(
        """
        SELECT d::date AS day, COALESCE(s.files, 0) AS files,
               COALESCE(s.bytes, 0) AS bytes, COALESCE(s.new_users, 0) AS nu
        FROM generate_series(CURRENT_DATE - 6, CURRENT_DATE, '1 day'::interval) AS d
        LEFT JOIN daily_stats s ON s.day = d::date
        ORDER BY d
        """
    )
    files = [r["files"] for r in rows]
    mx = max(files) if files else 0
    lines = []
    for r in rows:
        n = int(8 * r["files"] / mx) if mx else 0
        bar = "█" * n + "░" * (8 - n)
        lines.append(
            f"`{DAYS[r['day'].weekday()]} {r['day'].day:02d}` {bar} `{r['files']}` · {human(r['bytes'])} · +{r['nu']}👤"
        )
    lines += ["", f"📊 Tendance : `{spark(files)}`",
              f"📁 Total 7 j : `{sum(files)}` fichiers · `{human(sum(r['bytes'] for r in rows))}`"]
    return card("📅 7 DERNIERS JOURS", lines)


async def top_text() -> str:
    rows = await fetch(
        "SELECT id, name, username, renamed, bytes FROM users WHERE renamed > 0 ORDER BY bytes DESC LIMIT 10"
    )
    if not rows:
        return card("🏆 TOP UTILISATEURS", ["Aucune activité pour l'instant."])
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = []
    for i, r in enumerate(rows):
        who = safe(r["name"]) + (f" (@{safe(r['username'])})" if r["username"] else "")
        lines.append(f"{medals[i]} **{who}**\n      `{r['id']}` · {r['renamed']} fichiers · {human(r['bytes'])}")
    return card("🏆 TOP UTILISATEURS", lines)


async def banlist_text() -> str:
    rows = await fetch("SELECT id, name, username FROM users WHERE banned ORDER BY last_seen DESC LIMIT 30")
    if not rows:
        return card("🚫 UTILISATEURS BANNIS", ["Personne n'est banni 🎉"])
    lines = [f"• `{r['id']}` {safe(r['name'])}" + (f" (@{safe(r['username'])})" if r["username"] else "") for r in rows]
    return card("🚫 UTILISATEURS BANNIS", lines, footer="`/unban id` pour débannir")


async def history_text() -> str:
    rows = await fetch("SELECT * FROM announcements ORDER BY id DESC LIMIT 5")
    if not rows:
        return card("📜 HISTORIQUE DES ANNONCES", ["Aucune annonce envoyée."])
    lines = []
    for r in rows:
        when = datetime.datetime.fromtimestamp(r["created"]).strftime("%d/%m %H:%M")
        lines.append(
            f"📢 `{when}` · {'actifs' if r['target'] == 'active' else 'tous'}\n"
            f"      ✅ {r['ok']}/{r['total']} · 🔕 {r['blocked']} · ❌ {r['failed']} · ⏱ {r['secs']:.0f}s"
        )
    return card("📜 HISTORIQUE DES ANNONCES", lines)


async def user_text(uid: int) -> str:
    r = await fetchrow("SELECT * FROM users WHERE id=$1", uid)
    if not r:
        return card("👤 UTILISATEUR", [f"`{uid}` introuvable."])
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M")
    return card("👤 UTILISATEUR", [
        f"🆔 `{r['id']}`",
        f"📛 {safe(r['name'])}" + (f" (@{safe(r['username'])})" if r["username"] else ""),
        f"📅 Inscrit : `{fmt(r['joined'])}`",
        f"🕒 Dernière activité : `{fmt(r['last_seen'])}`",
        f"📁 Fichiers : `{r['renamed']}` · 📦 `{human(r['bytes'])}`",
        f"🖼 Miniature : {'✅' if r['thumb'] else '❌'} · 💬 Légende : {'✅' if r['caption'] else '❌'}"
        f" · 🏷 Préfixe : `{code(r['prefix']) if r['prefix'] else '—'}`",
        f"🚫 Banni : {'oui' if r['banned'] else 'non'} · 🔕 Bloqué : {'oui' if r['blocked'] else 'non'}",
    ])


def me_text(u: dict) -> str:
    return card("📊 MES STATISTIQUES", [
        f"📁 Fichiers renommés : `{u.get('renamed', 0)}`",
        f"📦 Volume traité : `{human(u.get('bytes', 0))}`",
        "",
        f"🖼 Miniature : {'✅ active' if u.get('thumb') else '❌ aucune'}",
        f"💬 Légende : {'✅ ' + code(u['caption'])[:40] if u.get('caption') else '❌ aucune'}",
        f"🏷 Préfixe : `{code(u['prefix']) if u.get('prefix') else '—'}`",
    ], footer="by Nexa · @Nexa_CG")


HELP = card("⚡ DASH RENAME BOT", [
    "**Comment ça marche**",
    "1️⃣ Envoie un fichier, une vidéo ou un audio",
    "2️⃣ Choisis les options avec les boutons",
    "3️⃣ Envoie le nouveau nom (ou « Garder le nom »)",
    "",
    "**Commandes**",
    "🖼 /setthumb · /delthumb · /thumb — miniature (ou envoie une photo)",
    "💬 /setcaption `texte` · /delcaption — légende (`{filename}` `{size}`)",
    "🏷 /setprefix `[Dash] ` · /delprefix — préfixe des noms",
    "⚙️ /settings — mon panneau · 📊 /me — mes stats",
    "📚 /go — lance tous les fichiers en attente (noms d'origine)",
    "🔢 /rename `modèle` — renomme TOUS les fichiers en attente d'un coup",
    "      ex : `/rename Naruto S01E{n:02}` → Naruto S01E01, S01E02…",
    "      `{n}` numéro · `{n:03}` numéro sur 3 chiffres · `{name}` nom d'origine",
    "📝 Ou envoie **un nom par ligne** : le 1er nom va au 1er fichier, etc.",
    "🏓 /ping · ❌ /cancel",
], footer="by Nexa · @Nexa_CG")

ANN_HELP = card("📢 ANNONCES", [
    "**1) En répondant à un message** (texte, photo, vidéo, GIF, fichier…)",
    "`/announce` → copie exacte du message",
    "",
    "**2) Directement dans ta commande** (image + légende possible)",
    "Envoie une photo/vidéo avec cette légende :",
    "`/announce active Voir l'offre|https://exemple.com`",
    "`Ton texte ici (Markdown : **gras**, __italique__)`",
    "",
    "**Options de la 1re ligne**",
    "• `all` (défaut) ou `active` (actifs sur 7 jours)",
    "• `Texte du bouton|https://lien` pour ajouter un bouton",
    "",
    "Un aperçu s'affiche avant l'envoi (test, épingler, confirmer).",
], footer="Alias : /broadcast")

def admin_text() -> str:
    return card("🛠 PANNEAU ADMIN", [
        f"🔧 Maintenance : **{'ON' if FLAGS['maintenance'] else 'OFF'}**",
        f"⚙️ En cours : `{n_active()}` · ⏳ En attente : `{stat['waiting']}`",
        f"⏱ Uptime : `{fmt_time(time.time() - START)}`",
        "",
        "Choisis une action 👇",
    ], footer="Dash Rename Bot · Nexa")


ADMIN_HELP = card("📖 COMMANDES ADMIN", [
    "**Panneau & statistiques**",
    "/admin — panneau · /stats · /server · /daily · /top",
    "",
    "**Utilisateurs** (id ou @pseudo)",
    "/user `id` — fiche d'un utilisateur",
    "/ban `id` · /unban `id` · /banlist",
    "/purge `30` — supprime les inactifs depuis 30 jours",
    "",
    "**Annonces**",
    "/announce — aide et envoi · /history — historique",
    "",
    "**Serveur**",
    "/maintenance `on` ou `off` · /clean",
], footer="Alias : /sys /week /broadcast")


# =====================================================================
#  Claviers
# =====================================================================
def channel_row():
    return [InlineKeyboardButton("📢 Canal Nexa", url=f"https://t.me/{FORCE_CHANNEL}")] if FORCE_CHANNEL else []


def menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🖼 Miniature", callback_data="menu:thumb"),
         InlineKeyboardButton("💬 Légende", callback_data="menu:caption")],
        [InlineKeyboardButton("🏷 Préfixe", callback_data="menu:prefix"),
         InlineKeyboardButton("📊 Mes stats", callback_data="menu:stats")],
        [InlineKeyboardButton("❓ Aide", callback_data="menu:help")] + channel_row(),
    ]
    return InlineKeyboardMarkup(rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    maint = "🔧 Maintenance : ON" if FLAGS["maintenance"] else "🔧 Maintenance : OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Stats", callback_data="adm:stats"),
         InlineKeyboardButton("🖥 Serveur", callback_data="adm:server")],
        [InlineKeyboardButton("📅 7 jours", callback_data="adm:daily"),
         InlineKeyboardButton("🏆 Top", callback_data="adm:top")],
        [InlineKeyboardButton("📢 Annonces", callback_data="adm:ann"),
         InlineKeyboardButton("📜 Historique", callback_data="adm:history")],
        [InlineKeyboardButton("🚫 Bannis", callback_data="adm:banlist"),
         InlineKeyboardButton("🧹 Nettoyer", callback_data="adm:clean")],
        [InlineKeyboardButton("📖 Commandes", callback_data="adm:help")],
        [InlineKeyboardButton(maint, callback_data="adm:maint")],
    ])


BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="adm:home")]])


def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        channel_row()[:1] or [InlineKeyboardButton("📢 Canal", url="https://t.me/")],
        [InlineKeyboardButton("✅ J'ai rejoint", callback_data="checksub")],
    ])


# =====================================================================
#  Abonnement obligatoire
# =====================================================================
JOIN_TEXT = card("🔒 ACCÈS RÉSERVÉ", [
    "Pour utiliser **Dash Rename Bot**, rejoins d'abord notre canal **Nexa** 👇",
    "Puis appuie sur « ✅ J'ai rejoint ».",
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


# =====================================================================
#  Garde : enregistrement, bannissement, maintenance, abonnement
# =====================================================================
UPSERT = """
INSERT INTO users (id, joined, last_seen, name, username) VALUES ($1, $2, $2, $3, $4)
ON CONFLICT (id) DO UPDATE SET last_seen = $2, name = $3, username = $4, blocked = FALSE
RETURNING *, (xmax = 0) AS inserted
"""


@app.on_message(filters.private, group=-1)
async def guard(c: Client, m: Message):
    if not m.from_user:
        return
    uid = m.from_user.id
    if FLAGS["maintenance"] and uid not in ADMINS:
        await m.reply_text(card("🛠 MAINTENANCE", ["Le bot revient très vite !", "Merci de ta patience 🙏"]))
        raise StopPropagation
    now = time.time()
    e = ucache.get(uid)
    if not e or e["exp"] < now:
        name = safe(" ".join(filter(None, [m.from_user.first_name, m.from_user.last_name])))
        row = dict(await fetchrow(UPSERT, uid, now, name, m.from_user.username))
        if row.pop("inserted"):
            await bump_daily(new_users=1)
        e = {"exp": now + 60, "row": row}
        ucache[uid] = e
    if e["row"].get("banned") and uid not in ADMINS:
        await m.reply_text(card("🚫 ACCÈS REFUSÉ", ["Tu es banni de ce bot."]))
        raise StopPropagation
    if not await is_member(c, uid):
        await m.reply_text(JOIN_TEXT, reply_markup=join_keyboard())
        raise StopPropagation


async def cb_allowed(c: Client, q) -> bool:
    """Même contrôle que la garde, pour les boutons."""
    uid = q.from_user.id
    if uid in ADMINS:
        return True
    if FLAGS["maintenance"]:
        await q.answer("🛠 Maintenance en cours, réessaie bientôt.", show_alert=True)
        return False
    if (await get_user(uid)).get("banned"):
        await q.answer("🚫 Accès refusé.", show_alert=True)
        return False
    if not await is_member(c, uid):
        await q.answer("🔒 Rejoins d'abord le canal Nexa.", show_alert=True)
        return False
    return True


# =====================================================================
#  ADMIN — panneau, stats, outils
# =====================================================================
@app.on_message(filters.command("admin") & filters.private & is_admin)
async def admin_panel(_, m: Message):
    await m.reply_text(admin_text(), reply_markup=admin_keyboard())


@app.on_message(filters.command("adminhelp") & filters.private & is_admin)
async def admin_help(_, m: Message):
    await m.reply_text(ADMIN_HELP)


@app.on_callback_query(filters.regex(r"^adm:") & is_admin)
async def adm_cb(c: Client, q):
    act = q.data.split(":")[1]
    await q.answer()
    kb = BACK_KB
    if act == "home":
        text, kb = admin_text(), admin_keyboard()
    elif act == "stats":
        text = await stats_text()
    elif act == "server":
        text = await server_text()
    elif act == "daily":
        text = await daily_text()
    elif act == "top":
        text = await top_text()
    elif act == "history":
        text = await history_text()
    elif act == "banlist":
        text = await banlist_text()
    elif act == "ann":
        text = ANN_HELP
    elif act == "help":
        text = ADMIN_HELP
    elif act == "clean":
        n, freed = clean_downloads(max_age=10800 if n_active() else 0)
        text = card("🧹 NETTOYAGE", [f"🗂 Dossiers supprimés : `{n}`", f"💾 Libéré : `{human(freed)}`"])
    elif act == "maint":
        FLAGS["maintenance"] = not FLAGS["maintenance"]
        text, kb = admin_text(), admin_keyboard()
    else:
        return
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@app.on_message(filters.command("stats") & filters.private & is_admin)
async def stats(_, m: Message):
    msg = await loading(m, "Analyse en cours")
    await msg.edit_text(await stats_text())


@app.on_message(filters.command(["server", "sys"]) & filters.private & is_admin)
async def server(_, m: Message):
    msg = await loading(m, "Lecture du serveur")
    await msg.edit_text(await server_text())


@app.on_message(filters.command(["daily", "week"]) & filters.private & is_admin)
async def daily(_, m: Message):
    await m.reply_text(await daily_text())


@app.on_message(filters.command("top") & filters.private & is_admin)
async def top(_, m: Message):
    await m.reply_text(await top_text())


@app.on_message(filters.command("history") & filters.private & is_admin)
async def history(_, m: Message):
    await m.reply_text(await history_text())


@app.on_message(filters.command("banlist") & filters.private & is_admin)
async def banlist(_, m: Message):
    await m.reply_text(await banlist_text())


async def resolve_uid(m: Message):
    """Accepte un id numérique ou un @pseudo déjà connu du bot."""
    if len(m.command) < 2:
        return None
    arg = m.command[1].lstrip("@")
    if arg.isdigit():
        return int(arg)
    return await fetchval("SELECT id FROM users WHERE lower(username)=lower($1)", arg)


@app.on_message(filters.command("user") & filters.private & is_admin)
async def user_info(_, m: Message):
    uid = await resolve_uid(m)
    if not uid:
        return await m.reply_text("Usage : `/user id` ou `/user @pseudo` (utilisateur connu du bot)")
    await m.reply_text(await user_text(uid))


@app.on_message(filters.command("maintenance") & filters.private & is_admin)
async def maintenance(_, m: Message):
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        state = "ON 🔧" if FLAGS["maintenance"] else "OFF ✅"
        return await m.reply_text(f"Maintenance : **{state}**\nUsage : `/maintenance on` ou `/maintenance off`")
    FLAGS["maintenance"] = arg == "on"
    await m.reply_text("🔧 Maintenance **activée** : seuls les admins peuvent utiliser le bot."
                       if FLAGS["maintenance"] else "✅ Maintenance **désactivée**.")


@app.on_message(filters.command("clean") & filters.private & is_admin)
async def clean(_, m: Message):
    msg = await loading(m, "Nettoyage")
    n, freed = clean_downloads(max_age=10800 if n_active() else 0)
    note = "\n__(tâches en cours : seuls les vieux dossiers ont été supprimés)__" if n_active() else ""
    await msg.edit_text(f"🧹 `{n}` dossier(s) supprimé(s) · `{human(freed)}` libérés.{note}")


@app.on_message(filters.command("purge") & filters.private & is_admin)
async def purge(_, m: Message):
    if len(m.command) < 2 or not m.command[1].isdigit() or int(m.command[1]) < 1:
        return await m.reply_text("Usage : `/purge 30` (utilisateurs inactifs depuis 30 jours)")
    days = int(m.command[1])
    cutoff = time.time() - days * 86400
    count = await fetchval(
        "SELECT count(*) FROM users WHERE id <> ALL($1::bigint[]) AND last_seen < $2", ADMINS, cutoff,
    )
    if not count:
        return await m.reply_text("✅ Aucun utilisateur inactif à supprimer.")
    await m.reply_text(
        card("🗄 PURGE", [
            f"⚠️ `{count}` utilisateur(s) inactif(s) depuis plus de `{days}` jour(s)",
            "seront supprimés (miniature, légende, préfixe, stats).",
        ]),
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
        "DELETE FROM users WHERE id <> ALL($1::bigint[]) AND last_seen < $2", ADMINS, cutoff,
    )
    ucache.clear()
    await q.message.edit_text(f"🗄 `{status.split()[-1]}` utilisateur(s) supprimé(s) de la base.")
    await q.answer("Terminé")


@app.on_message(filters.command("ban") & filters.private & is_admin)
async def ban(_, m: Message):
    uid = await resolve_uid(m)
    if not uid:
        return await m.reply_text("Usage : `/ban id` ou `/ban @pseudo` (utilisateur connu du bot)")
    if uid in ADMINS:
        return await m.reply_text("⚠️ Impossible de bannir un admin.")
    await execute(
        """
        INSERT INTO users (id, joined, last_seen, banned) VALUES ($1, $2, $2, TRUE)
        ON CONFLICT (id) DO UPDATE SET banned = TRUE
        """,
        uid, time.time(),
    )
    ucache.pop(uid, None)
    await m.reply_text(f"🚫 `{uid}` banni.")


@app.on_message(filters.command("unban") & filters.private & is_admin)
async def unban(_, m: Message):
    uid = await resolve_uid(m)
    if not uid:
        return await m.reply_text("Usage : `/unban id` ou `/unban @pseudo` (utilisateur connu du bot)")
    await execute("UPDATE users SET banned=FALSE WHERE id=$1", uid)
    ucache.pop(uid, None)
    await m.reply_text(f"✅ `{uid}` débanni.")


ADMIN_ONLY_CMDS = ["admin", "adminhelp", "stats", "server", "sys", "daily", "week", "top", "history",
                   "banlist", "user", "maintenance", "clean", "purge", "ban", "unban", "announce", "broadcast"]


@app.on_message(filters.command(ADMIN_ONLY_CMDS) & filters.private & ~is_admin)
async def not_admin(_, m: Message):
    await m.reply_text("⛔ Cette commande est réservée aux admins.")


# ---------- Annonces (texte, images, vidéos…) ----------
def parse_announce(m: Message):
    raw = m.text or m.caption or ""
    md = getattr(raw, "markdown", str(raw))
    first, _, body = md.partition("\n")
    toks = first.split()[1:]
    target = "all"
    if toks and toks[0].lower() in ("all", "tous", "active", "actifs"):
        target = "active" if toks[0].lower() in ("active", "actifs") else "all"
        toks = toks[1:]
    label = url = None
    rest = " ".join(toks)
    if "|" in rest:
        lab, link = (x.strip() for x in rest.split("|", 1))
        if lab and link.startswith(("http://", "https://", "tg://")):
            label, url = lab, link
    return target, label, url, body.strip()


def link_markup(a: dict):
    if a["url"]:
        return InlineKeyboardMarkup([[InlineKeyboardButton(a["label"], url=a["url"])]])
    return None


def preview_keyboard(a: dict) -> InlineKeyboardMarkup:
    rows = []
    if a["url"]:
        rows.append([InlineKeyboardButton(a["label"], url=a["url"])])
    rows += [
        [InlineKeyboardButton(f"✅ Envoyer à {len(a['ids'])} personne(s)", callback_data="ann:send")],
        [InlineKeyboardButton(f"📌 Épingler : {'ON' if a['pin'] else 'OFF'}", callback_data="ann:pin"),
         InlineKeyboardButton("🧪 Test (moi)", callback_data="ann:test")],
        [InlineKeyboardButton("❌ Annuler", callback_data="ann:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


async def target_ids(target: str) -> list:
    if target == "active":
        rows = await fetch(
            "SELECT id FROM users WHERE NOT banned AND NOT blocked AND last_seen > $1", time.time() - 7 * 86400
        )
    else:
        rows = await fetch("SELECT id FROM users WHERE NOT banned AND NOT blocked")
    return [r["id"] for r in rows]


async def deliver(c: Client, chat_id: int, a: dict, markup):
    src = a["src"]
    if a["body"] is not None:  # message construit dans la commande elle-même
        if src.media:
            return await src.copy(chat_id, caption=a["body"], reply_markup=markup)
        return await c.send_message(chat_id, a["body"], reply_markup=markup)
    return await src.copy(chat_id, reply_markup=markup)  # copie exacte du message cité


@app.on_message(filters.command(["announce", "broadcast"]) & filters.private & is_admin)
async def announce(c: Client, m: Message):
    target, label, url, body = parse_announce(m)
    if m.reply_to_message:
        src, body_val = m.reply_to_message, None
    elif m.media or body:
        src, body_val = m, body
    else:
        return await m.reply_text(ANN_HELP)
    ids = await target_ids(target)
    if not ids:
        return await m.reply_text("⚠️ Aucun destinataire.")
    a = {"src": src, "body": body_val, "label": label, "url": url,
         "target": target, "ids": ids, "pin": False}
    ann[m.from_user.id] = a
    await m.reply_text(card("📢 APERÇU DE L'ANNONCE", [
        f"🎯 Cible : **{'actifs (7 j)' if target == 'active' else 'tous les utilisateurs'}**",
        f"👥 Destinataires : `{len(ids)}`",
        "👇 Voici le message tel qu'il sera reçu",
    ]))
    try:
        await deliver(c, m.chat.id, a, preview_keyboard(a))
    except Exception as e:
        ann.pop(m.from_user.id, None)
        await m.reply_text(f"❌ Impossible de préparer l'aperçu : `{safe(e)[:100]}`")


async def send_one(c: Client, rid: int, a: dict, markup) -> str:
    for _ in range(3):
        try:
            sent = await deliver(c, rid, a, markup)
            if a["pin"] and sent:
                try:
                    await c.pin_chat_message(rid, sent.id, disable_notification=True)
                except Exception:
                    pass
            return "ok"
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
            return "blocked"
        except Exception:
            return "fail"
    return "fail"


async def run_broadcast(c: Client, a: dict, status: Message):
    ids, markup = a["ids"], link_markup(a)
    total, ok, fail = len(ids), 0, 0
    blocked = []
    sem = asyncio.Semaphore(25)

    async def one(rid):
        nonlocal ok, fail
        async with sem:
            r = await send_one(c, rid, a, markup)
        if r == "ok":
            ok += 1
        elif r == "blocked":
            blocked.append(rid)
        else:
            fail += 1

    t0 = time.time()
    for i in range(0, total, 25):  # ~25 messages par seconde (limite Telegram ≈ 30/s)
        tick = time.time()
        await asyncio.gather(*(one(r) for r in ids[i:i + 25]))
        done = min(i + 25, total)
        if (i // 25) % 2 == 0 and done != total:
            try:
                await status.edit_text(card("📢 DIFFUSION EN COURS", [
                    fancy_bar(done * 100 / total),
                    f"📊 `{done}/{total}` · ✅ {ok} · 🔕 {len(blocked)} · ❌ {fail}",
                ]))
            except Exception:
                pass
        left = 1.0 - (time.time() - tick)
        if left > 0:
            await asyncio.sleep(left)

    secs = time.time() - t0
    if blocked:
        await execute("UPDATE users SET blocked=TRUE WHERE id = ANY($1::bigint[])", blocked)
        for b in blocked:
            ucache.pop(b, None)
    await execute(
        "INSERT INTO announcements (created, target, total, ok, blocked, failed, secs) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        time.time(), a["target"], total, ok, len(blocked), fail, secs,
    )
    await status.edit_text(card("🎉 ANNONCE TERMINÉE", [
        f"✅ Reçus : `{ok}` / `{total}`",
        f"🔕 Ont bloqué le bot : `{len(blocked)}`",
        f"❌ Échecs : `{fail}`",
        f"⏱ Durée : `{secs:.0f}s` · ⚡ `{total / max(secs, 0.1):.0f}` msg/s",
    ]))


@app.on_callback_query(filters.regex(r"^ann:") & is_admin)
async def ann_cb(c: Client, q):
    uid = q.from_user.id
    act = q.data.split(":")[1]
    a = ann.get(uid)
    if not a:
        return await q.answer("Annonce expirée, relance /announce.", show_alert=True)
    if act == "cancel":
        ann.pop(uid, None)
        try:
            await q.message.edit_reply_markup(None)
        except Exception:
            pass
        await q.message.reply_text("❌ Annonce annulée.")
        return await q.answer()
    if act == "pin":
        a["pin"] = not a["pin"]
        try:
            await q.message.edit_reply_markup(preview_keyboard(a))
        except Exception:
            pass
        return await q.answer(f"📌 Épinglage : {'ON' if a['pin'] else 'OFF'}")
    if act == "test":
        try:
            await deliver(c, q.message.chat.id, a, link_markup(a))
            return await q.answer("🧪 Test envoyé")
        except Exception as e:
            return await q.answer(f"Erreur : {safe(e)[:100]}", show_alert=True)
    if act == "send":
        ann.pop(uid, None)
        try:
            await q.message.edit_reply_markup(None)
        except Exception:
            pass
        await q.answer("🚀 Diffusion lancée")
        status = await q.message.reply_text("📢 Diffusion en cours…")
        asyncio.create_task(run_broadcast(c, a, status))


# =====================================================================
#  UTILISATEURS — accueil, réglages, commandes
# =====================================================================
async def send_welcome(c: Client, chat_id: int, intro: bool = True):
    """Animation de chargement, puis logo + présentation + menu."""
    global LOGO_ID
    if intro:
        try:
            msg = await c.send_message(chat_id, "⚡ **Dash Rename Bot**\n\n`▱▱▱▱▱▱▱▱▱▱  0%`")
            await animate(msg, [
                "⚡ **Dash Rename Bot**\n\n`▰▰▰▱▱▱▱▱▱▱  30%`",
                "⚡ **Dash Rename Bot**\n\n`▰▰▰▰▰▰▰▱▱▱  70%`",
                "✅ **Dash Rename Bot** prêt !\n\n`▰▰▰▰▰▰▰▰▰▰  100%`",
            ], 0.5)
            await msg.delete()
        except Exception:
            pass
    caption = card("⚡ DASH RENAME BOT", [
        "Renomme tes fichiers et vidéos, change leur miniature —",
        "jusqu'à **2 Go**, à vitesse éclair 🚀",
        "",
        "📤 **Envoie-moi un fichier pour commencer**",
    ], footer="by Nexa · @Nexa_CG")
    kb = menu_keyboard()
    if LOGO_ID or os.path.exists(LOGO_PATH):
        try:
            sent = await c.send_photo(chat_id, LOGO_ID or LOGO_PATH, caption=caption, reply_markup=kb)
            LOGO_ID = sent.photo.file_id
            return
        except Exception as e:
            print(f"[logo] envoi impossible : {e}")
    await c.send_message(chat_id, caption, reply_markup=kb)


@app.on_message(filters.command("start") & filters.private)
async def start(c: Client, m: Message):
    await send_welcome(c, m.chat.id)


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, m: Message):
    await m.reply_text(HELP)


@app.on_message(filters.command("settings") & filters.private)
async def settings(_, m: Message):
    u = await get_user(m.from_user.id)
    await m.reply_text(me_text(u), reply_markup=menu_keyboard())


@app.on_message(filters.command("ping") & filters.private)
async def ping(_, m: Message):
    t = time.time()
    msg = await m.reply_text("🏓 Pong…")
    await msg.edit_text(card("🏓 PING", [f"⚡ Réponse : `{(time.time() - t) * 1000:.0f} ms`"], footer="Dash Rename Bot"))


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


def del_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Supprimer", callback_data=f"menu:{action}")]])


@app.on_callback_query(filters.regex(r"^menu:"))
async def menu_cb(c: Client, q):
    if not await cb_allowed(c, q):
        return
    act = q.data.split(":")[1]
    uid, chat = q.from_user.id, q.message.chat.id
    u = await get_user(uid)
    if act in ("delthumb", "delcaption", "delprefix"):
        await set_field(uid, act[3:], None)
        return await q.answer("🗑 Supprimé", show_alert=False)
    await q.answer()
    if act == "stats":
        await c.send_message(chat, me_text(u))
    elif act == "help":
        await c.send_message(chat, HELP)
    elif act == "thumb":
        if u.get("thumb"):
            await c.send_photo(chat, u["thumb"], caption=card("🖼 MA MINIATURE", [
                "Elle est ajoutée à tes fichiers.", "Envoie une autre photo pour la remplacer."]),
                reply_markup=del_kb("delthumb"))
        else:
            await c.send_message(chat, card("🖼 MA MINIATURE", [
                "Aucune miniature définie.", "📸 **Envoie-moi une photo** pour l'enregistrer."]))
    elif act == "caption":
        if u.get("caption"):
            await c.send_message(chat, card("💬 MA LÉGENDE", [f"`{code(u['caption'])}`", "", "Change-la avec `/setcaption texte`"]),
                                 reply_markup=del_kb("delcaption"))
        else:
            await c.send_message(chat, card("💬 MA LÉGENDE", [
                "Aucune légende définie.", "Exemple : `/setcaption {filename} — {size}`"]))
    elif act == "prefix":
        if u.get("prefix"):
            await c.send_message(chat, card("🏷 MON PRÉFIXE", [f"`{code(u['prefix'])}`", "", "Change-le avec `/setprefix texte`"]),
                                 reply_markup=del_kb("delprefix"))
        else:
            await c.send_message(chat, card("🏷 MON PRÉFIXE", [
                "Aucun préfixe défini.", "Exemple : `/setprefix [Dash] `"]))


@app.on_message(filters.command("setthumb") & filters.private)
async def setthumb(_, m: Message):
    r = m.reply_to_message
    if not (r and r.photo):
        return await m.reply_text("📸 Réponds à une photo avec /setthumb")
    await set_field(m.from_user.id, "thumb", r.photo.file_id)
    await m.reply_text("✅ Miniature enregistrée !")


@app.on_message(filters.command("delthumb") & filters.private)
async def delthumb(_, m: Message):
    await set_field(m.from_user.id, "thumb", None)
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
    await set_field(m.from_user.id, "caption", m.text.split(None, 1)[1])
    await m.reply_text("✅ Légende enregistrée !")


@app.on_message(filters.command("delcaption") & filters.private)
async def delcaption(_, m: Message):
    await set_field(m.from_user.id, "caption", None)
    await m.reply_text("🗑 Légende supprimée.")


@app.on_message(filters.command("setprefix") & filters.private)
async def setprefix(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage : `/setprefix [Dash] `")
    prefix = m.text.split(None, 1)[1].replace("/", "_")
    await set_field(m.from_user.id, "prefix", prefix)
    await m.reply_text(f"✅ Préfixe : `{code(prefix)}`")


@app.on_message(filters.command("delprefix") & filters.private)
async def delprefix(_, m: Message):
    await set_field(m.from_user.id, "prefix", None)
    await m.reply_text("🗑 Préfixe supprimé.")


@app.on_message(filters.command("me") & filters.private)
async def me(_, m: Message):
    await m.reply_text(me_text(await get_user(m.from_user.id)))


@app.on_message(filters.command("cancel") & filters.private)
async def cancel(_, m: Message):
    keys = user_keys(m.from_user.id)
    for k in keys:
        pending.pop(k, None)
    if keys:
        await m.reply_text(f"❌ {len(keys)} fichier(s) en attente annulé(s).")
    else:
        await m.reply_text("Rien à annuler.")


@app.on_message(filters.command(["go", "keepall"]) & filters.private)
async def go_all(c: Client, m: Message):
    """Lance tous les fichiers en attente avec leur nom d'origine (+ préfixe éventuel)."""
    uid = m.from_user.id
    keys = sorted(user_keys(uid), key=lambda k: k[1])
    if not keys:
        return await m.reply_text("Aucun fichier en attente. Envoie d'abord des fichiers 📁")
    await m.reply_text(f"⚡ **{len(keys)}** fichier(s) lancé(s) !")
    for k in keys:
        st = pending.pop(k)
        try:
            await c.delete_messages(m.chat.id, k[1])
        except Exception:
            pass
        status = await m.reply_text("🚀 Démarrage…")
        asyncio.create_task(process(c, uid, m.chat.id, st, None, status))


# ---------- Renommage groupé ----------
def old_name_of(src: Message) -> str:
    media = src.document or src.video or src.audio
    return media.file_name or ("video.mp4" if src.video else "audio.mp3" if src.audio else "file")


def split_ext(name: str):
    if "." in name:
        base, ext = name.rsplit(".", 1)
        if base and ext and len(ext) <= 6:
            return base, ext
    return name, ""


def apply_pattern(pattern: str, n: int, old_name: str) -> str:
    """{n} / {n:03} = numéro, {name} = nom d'origine sans extension. L'extension d'origine est conservée."""
    base, ext = split_ext(old_name)
    out = re.sub(r"\{n(?::(\d+))?\}", lambda m: str(n).zfill(int(m.group(1) or 0)), pattern)
    out = out.replace("{name}", base).replace("/", "_").strip()
    out = out or base
    if ext and not out.lower().endswith("." + ext.lower()):
        out += "." + ext
    return out


async def launch_one(c: Client, m: Message, uid: int, key: tuple, name):
    st = pending.pop(key, None)
    if not st:
        return
    try:
        await c.delete_messages(m.chat.id, key[1])
    except Exception:
        pass
    status = await m.reply_text("🚀 Démarrage…")
    asyncio.create_task(process(c, uid, m.chat.id, st, name, status))
    await asyncio.sleep(0.5)  # évite le flood quand il y a beaucoup de fichiers


@app.on_message(filters.command(["rename", "batch"]) & filters.private)
async def rename_all(c: Client, m: Message):
    """Renomme tous les fichiers en attente avec un modèle : /rename Série S01E{n:02}"""
    uid = m.from_user.id
    keys = sorted(user_keys(uid), key=lambda k: k[1])  # ordre d'envoi
    pattern = m.text.split(None, 1)[1].strip() if len(m.command) > 1 else ""
    if not keys:
        return await m.reply_text("Aucun fichier en attente. Envoie d'abord des fichiers 📁")
    if not pattern:
        return await m.reply_text(card("🔢 RENOMMAGE GROUPÉ", [
            f"📚 `{len(keys)}` fichier(s) en attente",
            "",
            "`/rename Naruto S01E{n:02}`",
            "→ Naruto S01E01, Naruto S01E02…",
            "",
            "`{n}` numéro · `{n:03}` sur 3 chiffres",
            "`{name}` nom d'origine (sans extension)",
            "L'extension d'origine est gardée automatiquement.",
            "",
            "💡 Ou envoie **un nom par ligne** (1re ligne = 1er fichier).",
        ]))
    if len(keys) > 1 and "{n" not in pattern and "{name}" not in pattern:
        pattern += " {n:02}"  # sans numéro, tous les fichiers auraient le même nom
    await m.reply_text(f"⚡ **{len(keys)}** fichier(s) lancé(s) !")
    for i, k in enumerate(keys, 1):
        await launch_one(c, m, uid, k, apply_pattern(pattern, i, old_name_of(pending[k]["msg"])))


# =====================================================================
#  Réception d'un fichier + boutons d'options
# =====================================================================
@app.on_message(filters.photo & filters.private)
async def photo_auto(_, m: Message):
    uid = m.from_user.id
    if uid in ADMINS and m.caption:  # photo légendée d'un admin = source d'annonce potentielle
        return await m.reply_text("📢 Image reçue. Réponds-lui avec /announce pour la diffuser.", quote=True)
    await set_field(uid, "thumb", m.photo.file_id)
    await m.reply_text(card("🖼 MINIATURE", ["✅ Enregistrée !", "Elle sera ajoutée à tes prochains fichiers."]))


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


def prompt_text(media, waiting: int = 1) -> str:
    lines = [
        f"📁 Actuel : `{safe(media.file_name)}`",
        f"📦 Taille : `{human(media.file_size or 0)}`",
        "",
        "1️⃣ Choisis les options ci-dessous",
        "2️⃣ **Réponds à ce message** avec le nouveau nom",
        "   (avec ou sans extension) ou appuie sur « ⚡ Garder le nom »",
    ]
    if waiting > 1:
        lines += ["", f"📚 `{waiting}` fichiers en attente",
                  "• /go = tout lancer avec les noms d'origine",
                  "• /rename `modèle` = tout renommer d'un coup (ex : `/rename Film {n:02}`)"]
    return card("📝 NOUVEAU NOM", lines)


@app.on_message((filters.document | filters.video | filters.audio) & filters.private)
async def got_file(_, m: Message):
    uid = m.from_user.id
    media = m.document or m.video or m.audio
    size = media.file_size or 0
    if size > MAX_SIZE_MB * 1024 * 1024:
        return await m.reply_text(card("⚠️ FICHIER TROP GROS", [f"Maximum : `{MAX_SIZE_MB} Mo`"]))
    if shutil.disk_usage("downloads").free < size + 200 * 1024 * 1024:
        return await m.reply_text("⚠️ Pas assez d'espace disque pour l'instant, réessaie plus tard.")
    if len(user_keys(uid)) >= MAX_PENDING:
        return await m.reply_text(card("⚠️ TROP DE FICHIERS EN ATTENTE", [
            f"Maximum : `{MAX_PENDING}`", "Nomme-les, ou /go pour tout lancer, ou /cancel."]))

    u = await get_user(uid)
    st = {
        "msg": m,
        "mode": "video" if m.video else "audio" if m.audio else "document",
        "thumb": bool(u.get("thumb")),
        "caption": True,
        "t": time.time(),
    }
    await react(m, "⚡")
    try:
        await m._client.send_chat_action(m.chat.id, ChatAction.TYPING)
    except Exception:
        pass
    prompt = await m.reply_text(prompt_text(media, len(user_keys(uid)) + 1), reply_markup=keyboard(st))
    pending[(uid, prompt.id)] = st


@app.on_callback_query(filters.regex(r"^(mode:|thumb$|cap$|keep$|cancel$)"))
async def options_cb(c: Client, q):
    if not await cb_allowed(c, q):
        return
    uid = q.from_user.id
    key = (uid, q.message.id)
    st = pending.get(key)
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
        pending.pop(key, None)
        await q.message.edit_text("❌ Opération annulée.")
        return await q.answer()
    elif d == "keep":
        pending.pop(key, None)
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
    r = m.reply_to_message
    keys = user_keys(uid)
    lines = [l.strip() for l in (m.text or "").splitlines() if l.strip()]
    if len(lines) > 1 and keys:  # un nom par ligne → appliqué aux fichiers dans l'ordre d'envoi
        keys = sorted(keys, key=lambda k: k[1])[:len(lines)]
        await m.reply_text(f"⚡ **{len(keys)}** fichier(s) lancé(s) !")
        for k, line in zip(keys, lines):
            await launch_one(c, m, uid, k, line)
        return
    if r and (uid, r.id) in pending:
        key = (uid, r.id)               # le nom répond à l'invite d'un fichier précis
    elif keys:
        key = max(keys, key=lambda k: k[1])  # sinon : le fichier le plus récent
    else:
        return await m.reply_text("Envoie d'abord un fichier 📁")
    st = pending.pop(key)
    try:
        await c.delete_messages(m.chat.id, key[1])  # retire l'invite et ses boutons
    except Exception:
        pass
    status = await m.reply_text("🚀 Démarrage…")
    asyncio.create_task(process(c, uid, m.chat.id, st, m.text, status))


# =====================================================================
#  Traitement (téléchargement → envoi)
# =====================================================================
def prep_thumb(path: str):
    img = Image.open(path).convert("RGB")
    img.thumbnail((320, 320))
    img.save(path, "JPEG", quality=85)


async def get_thumb(c: Client, uid: int, file_id: str):
    """Miniature de l'utilisateur, gardée sur disque pour ne pas la re-télécharger à chaque fois."""
    path = f"thumbs/{uid}.jpg"
    async with thumb_locks.setdefault(uid, asyncio.Lock()):  # évite les accès simultanés
        if thumb_ids.get(uid) == file_id and os.path.exists(path):
            return path
        got = await c.download_media(file_id, file_name=path)
        await asyncio.to_thread(prep_thumb, got)
        thumb_ids[uid] = file_id
        return got


CHUNK = 1024 * 1024   # taille d'un morceau Telegram
SEG = 8               # morceaux par segment (8 Mo)


async def fast_download(c: Client, src: Message, media, path: str, pargs: tuple):
    """Télécharge plusieurs segments du fichier en même temps (bien plus rapide que
    le téléchargement normal, qui lit les morceaux un par un). Repli automatique sur
    le téléchargement classique en cas de problème."""
    size = getattr(media, "file_size", 0) or 0
    if size < 16 * CHUNK or DL_WORKERS < 2:
        return await c.download_media(src, file_name=path, progress=progress, progress_args=pargs)
    try:
        total_chunks = -(-size // CHUNK)
        queue = asyncio.Queue()
        for first in range(0, total_chunks, SEG):
            queue.put_nowait((first, min(SEG, total_chunks - first)))
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        done = 0
        try:
            async def run_seg(first, n):
                nonlocal done
                pos, got = first * CHUNK, 0
                async for chunk in c.stream_media(src, offset=first, limit=n):
                    os.pwrite(fd, chunk, pos)
                    pos += len(chunk)
                    got += 1
                    done += len(chunk)
                    await progress(done, size, *pargs)
                if got != n:
                    raise IOError("segment incomplet")

            async def worker():
                while True:
                    try:
                        first, n = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await run_seg(first, n)

            first, n = queue.get_nowait()
            await run_seg(first, n)  # 1er segment seul : ouvre proprement la connexion au serveur
            tasks = [asyncio.create_task(worker()) for _ in range(min(DL_WORKERS, queue.qsize()))]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        finally:
            os.close(fd)
        if done != size:
            raise IOError("taille inattendue")
        return path
    except Exception as e:
        print(f"[fast_download] repli sur le téléchargement classique : {e!r}")
        try:
            os.remove(path)
        except OSError:
            pass
        return await c.download_media(src, file_name=path, progress=progress, progress_args=pargs)


async def process(c: Client, uid: int, chat_id: int, st: dict, typed_name, status: Message):
    src = st["msg"]
    u = await get_user(uid)
    media = src.document or src.video or src.audio
    old_name = media.file_name or ("video.mp4" if src.video else "audio.mp3" if src.audio else "file")

    new_name = (typed_name or old_name).strip().replace("/", "_")
    if typed_name and "." not in new_name and "." in old_name:
        new_name += "." + old_name.rsplit(".", 1)[1]
    new_name = (u.get("prefix") or "") + new_name

    t0 = time.time()
    usem = usems.setdefault(uid, asyncio.Semaphore(MAX_PER_USER))
    waiting = usem.locked() or SEM.locked()
    if waiting:  # file d'attente : limite par utilisateur puis limite globale
        stat["waiting"] += 1
        try:
            await status.edit_text("⏳ **FILE D'ATTENTE**\n" + table([
                ("Fichier", safe(new_name)),
                ("Statut", "En attente"),
                ("En cours", f"{n_active()} fichier(s)"),
                ("Démarrage", "Automatique"),
            ]))
        except Exception:
            pass
    await usem.acquire()
    try:
        await SEM.acquire()
    except BaseException:
        usem.release()
        raise
    finally:
        if waiting:
            stat["waiting"] -= 1
    active[uid] = active.get(uid, 0) + 1

    work = f"downloads/{uid}_{time.time_ns()}"
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, new_name)

    try:
        state = {"action": ChatAction.TYPING, "name": safe(new_name)[:40]}
        start = time.time()
        dl = fast_download(c, src, media, path, (status, start, "Téléchargement", state))
        thumb_path = None
        if st["thumb"] and u.get("thumb"):
            # fichier + miniature téléchargés en parallèle
            _, thumb_path = await asyncio.gather(dl, get_thumb(c, uid, u["thumb"]))
        else:
            await dl

        size = os.path.getsize(path)
        if st["caption"]:
            caption = (u.get("caption") or "{filename}").replace("{filename}", new_name).replace("{size}", human(size))
        else:
            caption = ""

        mode = st["mode"]
        state = {
            "action": {"video": ChatAction.UPLOAD_VIDEO, "audio": ChatAction.UPLOAD_AUDIO}.get(
                mode, ChatAction.UPLOAD_DOCUMENT),
            "name": safe(new_name)[:40],
        }
        start = time.time()
        args = dict(
            chat_id=chat_id, thumb=thumb_path, caption=caption,
            progress=progress, progress_args=(status, start, "Envoi", state),
        )
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

        await settle(state)
        ucache.pop(uid, None)
        _bg(execute("UPDATE users SET renamed = renamed + 1, bytes = bytes + $1 WHERE id=$2", size, uid))
        _bg(bump_daily(files=1, size=size))
        await status.edit_text("🎉 **TERMINÉ !**\n" + table([
            ("Fichier", safe(new_name)),
            ("Statut", "Terminé"),
            ("Taille", human(size)),
            ("Durée", fmt_time(time.time() - t0)),
        ]) + "\n⚡ Dash Rename Bot")
        _bg(react(src, "🎉", big=True))
    except Exception as e:
        try:
            await status.edit_text(card("❌ ERREUR", [f"`{safe(e)[:200]}`", "", "Réessaie ou contacte l'admin."]))
        except Exception:
            pass
    finally:
        active[uid] = active.get(uid, 1) - 1
        if active[uid] <= 0:
            active.pop(uid, None)
        SEM.release()
        usem.release()
        shutil.rmtree(work, ignore_errors=True)


# =====================================================================
#  Mini serveur web (Render) + tâches de fond
# =====================================================================
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
    """Toutes les heures : dossiers temporaires oubliés (> 3 h) et caches expirés."""
    while True:
        await asyncio.sleep(3600)
        try:
            clean_downloads(max_age=10800)
            now = time.time()
            for k in [k for k, v in ucache.items() if v["exp"] < now]:
                ucache.pop(k, None)
            for k in [k for k, v in member_cache.items() if v < now]:
                member_cache.pop(k, None)
            for k in [k for k, v in pending.items() if now - v.get("t", now) > 7200]:
                pending.pop(k, None)
        except Exception:
            pass


USER_COMMANDS = [
    BotCommand("start", "Démarrer"),
    BotCommand("settings", "Mon panneau"),
    BotCommand("setthumb", "Définir la miniature"),
    BotCommand("delthumb", "Supprimer la miniature"),
    BotCommand("setcaption", "Définir la légende"),
    BotCommand("setprefix", "Définir un préfixe"),
    BotCommand("me", "Mes statistiques"),
    BotCommand("ping", "Tester la vitesse"),
    BotCommand("go", "Lancer tous les fichiers en attente"),
    BotCommand("rename", "Renommer tous les fichiers en attente"),
    BotCommand("cancel", "Annuler"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("admin", "Panneau admin"),
    BotCommand("adminhelp", "Liste des commandes admin"),
    BotCommand("stats", "Statistiques"),
    BotCommand("server", "État du serveur"),
    BotCommand("daily", "7 derniers jours"),
    BotCommand("top", "Top utilisateurs"),
    BotCommand("announce", "Envoyer une annonce"),
    BotCommand("history", "Historique des annonces"),
    BotCommand("user", "Infos d'un utilisateur"),
    BotCommand("ban", "Bannir"),
    BotCommand("unban", "Débannir"),
    BotCommand("banlist", "Liste des bannis"),
    BotCommand("maintenance", "Mode maintenance"),
    BotCommand("clean", "Nettoyer le disque"),
    BotCommand("purge", "Purger les inactifs"),
]


async def main():
    global SEM
    SEM = asyncio.Semaphore(MAX_PARALLEL)
    await start_web()
    await init_db()
    await app.start()
    await app.set_bot_commands(USER_COMMANDS)
    for aid in ADMINS:  # menu de commandes étendu pour les admins
        try:
            await app.set_bot_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            pass
    asyncio.create_task(keep_alive())
    asyncio.create_task(auto_clean())
    print("Dash Rename Bot démarré ⚡")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
