# ============================================================
# pip install: python-telegram-bot[job-queue] yt-dlp flask gunicorn psutil
# FFmpeg (Ubuntu): sudo apt update && sudo apt install -y ffmpeg
# FFmpeg check: ffmpeg -version
# Run: python bot.py
# ============================================================

import os
import gc
import re
import shutil
import logging
import sqlite3
import asyncio
import psutil
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── env ────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "123456789"))

CHANNEL_1     = "@sahatanas"
CHANNEL_2     = "@sahatanass"
CHANNEL_1_URL = "https://t.me/sahatanas"
CHANNEL_2_URL = "https://t.me/sahatanass"
OTHER_BOT_URL = "https://t.me/BomssssssssBot"

DB_FILE                       = "user_data.db"
MAX_FILE_SIZE_MB               = 45  # Render free tier-এ মেমোরি ক্র্যাশ ঠেকাতে 45MB লিমিট
RAM_LIMIT_MB                   = 400
REFERRAL_THRESHOLD_FOR_UNLIMITED = 100
TELEGRAM_UPLOAD_LIMIT_MB       = 45
DOWNLOAD_LOCK                  = asyncio.Semaphore(1)   # Render 512MB RAM এর জন্য ১টি concurrent download

# ── Markdown escape ────────────────────────────────────────
_MD_SPECIAL = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')

def md_escape(text: str) -> str:
    """Escape MarkdownV2 special chars — used only in captions."""
    return _MD_SPECIAL.sub(r'\\\1', str(text))

# ── YouTube URL validation ─────────────────────────────────
_YT_RE = re.compile(
    r'(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)'
    r'(/[^\s]*)?'
)

def is_youtube_url(text: str) -> bool:
    return bool(_YT_RE.search(text))

# ============================================================
# DATABASE
# ============================================================
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE, timeout=20)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with _conn() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                referral_count INTEGER DEFAULT 0,
                premium_until  TEXT,
                is_banned      INTEGER DEFAULT 0,
                join_date      TEXT    DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS quota (
                user_id       INTEGER,
                download_date TEXT,
                count         INTEGER,
                PRIMARY KEY (user_id, download_date)
            );
            CREATE TABLE IF NOT EXISTS download_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER,
                title         TEXT,
                quality       TEXT,
                download_time TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS referral_log (
                referrer_id INTEGER,
                referee_id  INTEGER,
                PRIMARY KEY (referrer_id, referee_id)
            );
        """)

init_db()

# ── DB helpers ─────────────────────────────────────────────
def register_user(user_id: int, username: str = None, first_name: str = None):
    with _conn() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
            (user_id, username, first_name),
        )
        db.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (username, first_name, user_id),
        )

def add_referral(referrer_id: int, referee_id: int) -> bool:
    with _conn() as db:
        try:
            db.execute(
                "INSERT INTO referral_log (referrer_id, referee_id) VALUES (?,?)",
                (referrer_id, referee_id),
            )
            db.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?",
                (referrer_id,),
            )
            return True
        except sqlite3.IntegrityError:
            return False

def is_user_banned(user_id: int) -> bool:
    with _conn() as db:
        row = db.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0])

def ban_user(user_id: int):
    with _conn() as db:
        db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        db.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))

def unban_user(user_id: int):
    with _conn() as db:
        db.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))

def is_user_premium(user_id: int) -> tuple[bool, str]:
    if user_id == ADMIN_ID:
        return True, "Admin (Unlimited)"
    with _conn() as db:
        row = db.execute(
            "SELECT referral_count, premium_until FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return False, "Free User"
    ref_count, prem_until = row
    if ref_count >= REFERRAL_THRESHOLD_FOR_UNLIMITED:
        return True, "Unlimited (100+ Referrals)"
    if prem_until:
        try:
            exp = datetime.strptime(prem_until, "%Y-%m-%d %H:%M:%S")
            if datetime.now() < exp:
                days_left = (exp - datetime.now()).days + 1
                return True, f"Premium ({days_left} Days Left)"
        except ValueError:
            pass
    return False, "Free User"

def get_user_stats(user_id: int):
    today = str(datetime.now().date())
    with _conn() as db:
        ref = db.execute(
            "SELECT referral_count FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        q = db.execute(
            "SELECT count FROM quota WHERE user_id=? AND download_date=?", (user_id, today)
        ).fetchone()
    return (q[0] if q else 0), (ref[0] if ref else 0)

def get_total_downloads(user_id: int) -> int:
    with _conn() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM download_log WHERE user_id=?", (user_id,)
        ).fetchone()
    return row[0] if row else 0

def increment_user_quota(user_id: int):
    today = str(datetime.now().date())
    with _conn() as db:
        db.execute(
            """INSERT INTO quota (user_id, download_date, count) VALUES (?,?,1)
               ON CONFLICT(user_id, download_date) DO UPDATE SET count=count+1""",
            (user_id, today),
        )

def log_download(user_id: int, title: str, quality: str):
    with _conn() as db:
        db.execute(
            "INSERT INTO download_log (user_id, title, quality) VALUES (?,?,?)",
            (user_id, title, quality),
        )

def get_all_users():
    with _conn() as db:
        rows = db.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
    return [r[0] for r in rows]

def get_top_referrers(limit=10):
    with _conn() as db:
        rows = db.execute(
            """SELECT user_id, first_name, username, referral_count
               FROM users WHERE referral_count > 0
               ORDER BY referral_count DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return rows

def get_premium_expiring_soon():
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as db:
        rows = db.execute(
            """SELECT user_id FROM users
               WHERE premium_until IS NOT NULL
               AND premium_until > ? AND premium_until <= ?""",
            (now, tomorrow),
        ).fetchall()
    return [r[0] for r in rows]

def set_premium(user_id: int, days: int):
    exp = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as db:
        db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (exp, user_id))
    return exp

def remove_premium_db(user_id: int):
    with _conn() as db:
        db.execute("UPDATE users SET premium_until=NULL WHERE user_id=?", (user_id,))

# ============================================================
# RAM / DISK helpers
# ============================================================
def get_ram_usage_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / 1_048_576

def is_ram_safe() -> bool:
    return get_ram_usage_mb() < RAM_LIMIT_MB

def cleanup_old_files():
    """Delete files in download directory — safe to call anytime."""
    dl_folder = "downloads"
    if not os.path.exists(dl_folder):
        return
    cutoff = datetime.now().timestamp() - 1800  # 30 minute cutoff
    for root, _, files in os.walk(dl_folder):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
            except Exception:
                pass
    gc.collect()

# ============================================================
# KEYBOARDS
# ============================================================
async def check_force_join(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    for ch in [CHANNEL_1, CHANNEL_2]:
        try:
            m = await context.bot.get_chat_member(chat_id=ch, user_id=user_id)
            if m.status in ("left", "kicked"):
                return False
        except Exception as e:
            logger.warning("Force-join check skipped (%s): %s", ch, e)
            return True  # বট মেম্বারশিপ চ্যানেল চেক করতে না পারলে আটকে রাখবে না
    return True

def force_join_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel 1", url=CHANNEL_1_URL)],
        [InlineKeyboardButton("📢 Join Channel 2", url=CHANNEL_2_URL)],
        [InlineKeyboardButton("🚀 Try Other Bot",  url=OTHER_BOT_URL)],
    ])

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ Contact Admin",           url="https://t.me/Devsahatanas")],
        [InlineKeyboardButton("⚡ Any Video Downloader Bot", url=OTHER_BOT_URL)],
    ])

# ============================================================
# yt-dlp helpers
# ============================================================
def _ffmpeg_dir() -> str | None:
    p = shutil.which("ffmpeg")
    return os.path.dirname(p) if p else None

def get_base_ydl_opts(
    progress_hook=None,
    download_dir: str = "downloads",
    is_premium_user: bool = False,
) -> dict:
    opts: dict = {
        "outtmpl":           os.path.join(download_dir, "%(id)s.%(ext)s"),
        "quiet":             True,
        "no_warnings":       True,
        "noplaylist":        True,
        "retries":           3,
        "fragment_retries":  3,
        "continuedl":        True,
        "overwrites":        False,
        "merge_output_format": "mp4",
        "format_sort":       ["res", "fps", "vcodec:h264", "acodec:aac", "br"],
        "max_filesize":      f"{MAX_FILE_SIZE_MB}M",  # Render free tier-এ স্টোরেজ সেভ করতে ৪৫MB সীমানা
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "ios"],
            }
        },
        "http_headers": {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    fd = _ffmpeg_dir()
    if fd:
        opts["ffmpeg_location"] = fd
    return opts

def get_format_string(quality: str) -> str:
    if quality == "dl_audio":
        return "bestaudio/best"
    h = {"dl_360": 360, "dl_480": 480, "dl_720": 720, "dl_1080": 1080}.get(quality, 360)
    return (
        f"bestvideo[height<={h}][filesize<={MAX_FILE_SIZE_MB}M]+bestaudio/"
        f"best[height<={h}][filesize<={MAX_FILE_SIZE_MB}M]/"
        "best"
    )

def find_downloaded_file(ydl, info: dict, download_dir: str, is_audio: bool):
    filename = ydl.prepare_filename(info)
    if is_audio:
        mp3 = os.path.splitext(filename)[0] + ".mp3"
        if os.path.exists(mp3):
            return mp3
    if os.path.exists(filename):
        return filename
    base = os.path.splitext(filename)[0]
    for ext in (".mp4", ".mkv", ".webm", ".m4v", ".mp3"):
        c = base + ext
        if os.path.exists(c):
            return c
    files = [
        os.path.join(download_dir, f)
        for f in os.listdir(download_dir)
        if os.path.isfile(os.path.join(download_dir, f))
    ]
    if files:
        return max(files, key=os.path.getmtime)
    raise FileNotFoundError("Downloaded file not found after extraction")

# ============================================================
# COMMANDS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name)

    if is_user_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                is_new = add_referral(referrer_id, user.id)
                if is_new:
                    try:
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=(
                                f"🎉 *নতুন Referral!*\n\n"
                                f"👤 `{user.first_name}` তোমার link দিয়ে join করেছে!\n"
                                f"✅ তোমার referral count বাড়ছে।"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
        except ValueError:
            pass

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"👤 *নতুন User!*\n"
                f"Name: `{user.first_name}`\n"
                f"ID: `{user.id}`\n"
                f"Username: @{user.username or 'N/A'}"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    me = await context.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user.id}"
    today_dl, ref_count = get_user_stats(user.id)
    is_prem, status_str = is_user_premium(user.id)

    badge      = "👑 Admin"   if user.id == ADMIN_ID else ("⭐ Premium" if is_prem else "🆓 Free")
    status_line = "♾️ Unlimited Access" if user.id == ADMIN_ID else (f"✅ {status_str}" if is_prem else f"📥 Today: {today_dl}/2 downloads used")

    filled       = min(int(ref_count / REFERRAL_THRESHOLD_FOR_UNLIMITED * 10), 10)
    progress_bar = "🟩" * filled + "⬜" * (10 - filled)
    remaining    = max(REFERRAL_THRESHOLD_FOR_UNLIMITED - ref_count, 0)

    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 *YouTube Downloader Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 *Welcome, {user.first_name}!*\n\n"
        f"┌ 🏷️ *Account:* `{badge}`\n"
        f"├ 📊 *Status:* `{status_line}`\n"
        f"└ 🕐 *Today's Downloads:* `{today_dl}/{'∞' if is_prem else '2'}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 *Referral Progress*\n"
        f"{progress_bar} `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
        f"🎯 _{remaining} জন আনলে Lifetime Unlimited!_\n\n"
        f"🔗 *তোমার Referral Link:*\n`{ref_link}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📩 *যেকোনো YouTube link পাঠাও!*\n"
        f"⬇️ 360p • 480p • 720p • 1080p • MP3\n\n"
        f"📌 /help — সব commands দেখো",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_banned(update.effective_user.id):
        return
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━\n"
        "📖 *Bot Commands*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "👤 *User Commands:*\n"
        "▸ /start — Bot শুরু করো\n"
        "▸ /help — এই help দেখো\n"
        "▸ /profile — তোমার account card\n"
        "▸ /mystats — তোমার download stats\n"
        "▸ /leaderboard — Top referrers\n\n"
        "🎬 *Download:*\n"
        "▸ YouTube link পাঠাও\n"
        "▸ Quality select করো\n"
        "▸ Free: দিনে 2টা download\n"
        "▸ Premium/Unlimited: No limit\n\n"
        "⭐ *Premium পেতে:*\n"
        "▸ 100 জন referral করো\n"
        "▸ অথবা Admin কে contact করো\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ Admin: @Devsahatanas",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        return
    today_dl, ref_count = get_user_stats(user.id)
    is_prem, status_str = is_user_premium(user.id)
    total_dl = get_total_downloads(user.id)
    badge = "👑 Admin" if user.id == ADMIN_ID else ("⭐ Premium" if is_prem else "🆓 Free")
    filled = min(int(ref_count / REFERRAL_THRESHOLD_FOR_UNLIMITED * 10), 10)
    pb = "🟩" * filled + "⬜" * (10 - filled)
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *My Profile*\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"┌ 🆔 *ID:* `{user.id}`\n"
        f"├ 👤 *Name:* `{user.first_name}`\n"
        f"├ 🏷️ *Badge:* `{badge}`\n"
        f"├ 📊 *Status:* `{status_str}`\n"
        f"├ 📥 *Today:* `{today_dl}/{'∞' if is_prem else '2'}`\n"
        f"└ 📦 *Total Downloads:* `{total_dl}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 *Referral Progress*\n"
        f"{pb} `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
        f"━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )

async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        return
    today_dl, ref_count = get_user_stats(user.id)
    total_dl = get_total_downloads(user.id)
    is_prem, status_str = is_user_premium(user.id)
    with _conn() as db:
        rows = db.execute(
            "SELECT title, quality FROM download_log WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (user.id,),
        ).fetchall()
    if rows:
        recent = "\n".join(
            f"  {i}. `{(t[:25]+'...' if len(t)>25 else t)}` — {q}"
            for i, (t, q) in enumerate(rows, 1)
        )
    else:
        recent = "  _কোনো download নেই_"
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *My Stats*\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"┌ 📥 *আজকের Download:* `{today_dl}/{'∞' if is_prem else '2'}`\n"
        f"├ 📦 *Total Downloads:* `{total_dl}`\n"
        f"├ 👥 *Referrals:* `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
        f"└ ⭐ *Status:* `{status_str}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 *সাম্প্রতিক Downloads:*\n{recent}\n"
        f"━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_banned(update.effective_user.id):
        return
    top = get_top_referrers(10)
    if not top:
        await update.message.reply_text("📊 এখনো কোনো referral নেই!")
        return
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = "\n".join(
        f"{medals[i]} *{fname or uname or uid}* — `{cnt}` referrals"
        for i, (uid, fname, uname, cnt) in enumerate(top)
    )
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━\n🏆 *Top Referrers*\n━━━━━━━━━━━━━━━━━━━\n\n{lines}\n\n━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )

# ── admin ──────────────────────────────────────────────────
async def add_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        tid, days = int(context.args[0]), int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/add_premium <user_id> <days>`", parse_mode="Markdown")
        return
    exp = set_premium(tid, days)
    await update.message.reply_text(
        f"✅ User `{tid}` — *{days} days* Premium!\n📅 Expires: `{exp}`",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            tid,
            f"🎉 *Congratulations!*\n\nAdmin তোমাকে *{days} দিনের Premium* দিয়েছে!\n✨ Unlimited download করো!",
            parse_mode="Markdown",
        )
    except Exception:
        pass

async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        tid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/remove_premium <user_id>`", parse_mode="Markdown")
        return
    remove_premium_db(tid)
    await update.message.reply_text(f"🗑️ `{tid}` এর Premium সরানো হয়েছে!", parse_mode="Markdown")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        tid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/ban <user_id>`", parse_mode="Markdown")
        return
    ban_user(tid)
    await update.message.reply_text(f"🚫 User `{tid}` banned!", parse_mode="Markdown")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        tid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/unban <user_id>`", parse_mode="Markdown")
        return
    unban_user(tid)
    await update.message.reply_text(f"✅ User `{tid}` unbanned!", parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    ram_mb = get_ram_usage_mb()
    with _conn() as db:
        total   = db.execute("SELECT COUNT(*) FROM users WHERE is_banned=0").fetchone()[0]
        banned  = db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        premium = db.execute("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until > datetime('now')").fetchone()[0]
        today_dl= db.execute("SELECT COUNT(*) FROM download_log WHERE date(download_time)=date('now')").fetchone()[0]
        all_dl  = db.execute("SELECT COUNT(*) FROM download_log").fetchone()[0]
    ram_bar = "🟥" * min(int(ram_mb/50), 10) + "⬜" * max(10 - int(ram_mb/50), 0)
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━\n📊 *Bot Statistics*\n━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 *Users:*\n┌ Total: `{total}`\n├ Premium: `{premium}`\n└ Banned: `{banned}`\n\n"
        f"📥 *Downloads:*\n┌ আজকে: `{today_dl}`\n└ সর্বমোট: `{all_dl}`\n\n"
        f"💾 *Server RAM:*\n{ram_bar} `{ram_mb:.1f}MB / 500MB`\n━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Usage: `/broadcast <message>`")
        return
    text = " ".join(context.args)
    users = get_all_users()
    sent = failed = 0
    msg = await update.message.reply_text(f"⏳ Broadcasting to {len(users)} users...")
    for uid in users:
        try:
            await context.bot.send_message(uid, text, parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await msg.edit_text(
        f"📢 *Broadcast Done!*\n\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`",
        parse_mode="Markdown",
    )

# ── job ────────────────────────────────────────────────────
async def check_premium_expiry(context: ContextTypes.DEFAULT_TYPE):
    for uid in get_premium_expiring_soon():
        try:
            await context.bot.send_message(
                uid,
                "⚠️ *Premium Expiry Warning!*\n\n"
                "তোমার Premium আগামী *24 ঘণ্টার মধ্যে* শেষ হয়ে যাবে!\n\n"
                "🔗 Renew করতে: @Devsahatanas",
                parse_mode="Markdown",
            )
        except Exception:
            pass

# ============================================================
# MESSAGE HANDLER
# ============================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name)

    if is_user_banned(user.id):
        await update.message.reply_text("🚫 You are banned.")
        return

    text = update.message.text.strip()

    if not is_youtube_url(text):
        await update.message.reply_text("⚠️ Please send a valid YouTube link.")
        return

    if not is_ram_safe():
        await update.message.reply_text("⚠️ *Server busy!* একটু পরে try করো।", parse_mode="Markdown")
        return

    if not await check_force_join(user.id, context):
        await update.message.reply_text(
            "⚠️ *Access Denied!*\n\nDownload করতে দুটো channel join করো।",
            parse_mode="Markdown",
            reply_markup=force_join_kb(),
        )
        return

    is_prem, status_str = is_user_premium(user.id)
    today_dl, ref_count = get_user_stats(user.id)

    if not is_prem and today_dl >= 2:
        me = await context.bot.get_me()
        ref_link = f"https://t.me/{me.username}?start={user.id}"
        await update.message.reply_text(
            f"🚫 *Daily Limit Reached!*\n\nFree users: দিনে *2টা* download।\n\n"
            f"👥 Referral: `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
            f"আরো `{max(REFERRAL_THRESHOLD_FOR_UNLIMITED-ref_count,0)}` জন আনো → Lifetime Unlimited!\n\n"
            f"🔗 তোমার link:\n`{ref_link}`",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )
        return

    context.user_data["yt_url"] = text

    kb = [
        [InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data="dl_audio")],
        [InlineKeyboardButton("📱 360p (Low Data)",  callback_data="dl_360")],
        [InlineKeyboardButton("🎥 480p (Medium)",    callback_data="dl_480")],
        [InlineKeyboardButton("🎬 720p HD",          callback_data="dl_720")],
        [InlineKeyboardButton("⭐ 1080p Full HD", callback_data="dl_1080")]
        if is_prem else
        [InlineKeyboardButton("🔒 1080p [Premium Only]", callback_data="dl_locked")],
        [InlineKeyboardButton("🤖 Check Other Bot", url=OTHER_BOT_URL)],
    ]
    await update.message.reply_text(
        f"🎬 *Select Quality:*\n*Status:* `{status_str}`\n💾 RAM: `{get_ram_usage_mb():.0f}MB/500MB`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

# ============================================================
# CALLBACK HANDLER
# ============================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id

    if query.data == "dl_locked":
        await query.message.reply_text("🔒 *1080p is Premium Only!*", parse_mode="Markdown")
        return

    url = context.user_data.get("yt_url")
    if not url:
        await query.edit_message_text("❌ Session expired. Link আবার পাঠাও।")
        return

    if not is_ram_safe():
        await query.message.reply_text("⚠️ *Server busy!* একটু পরে try করো।", parse_mode="Markdown")
        return

    status_msg = await query.message.reply_text("⏳ *Queue (অপেক্ষা করো)...*", parse_mode="Markdown")

    file_path        = None
    user_dl_dir      = None
    is_prem, _       = is_user_premium(user_id)
    quality_label    = query.data.replace("dl_", "").upper()
    is_audio         = query.data == "dl_audio"

    async with DOWNLOAD_LOCK:  # Render ফ্রি সার্ভারের জন্য concurrent download লিমিট ১ করা
        try:
            await status_msg.edit_text("⏳ *Initializing Download...*", parse_mode="Markdown")
            loop     = asyncio.get_running_loop()
            last_upd = [0.0]

            def progress_hook(d: dict):
                if d["status"] != "downloading":
                    return
                now = loop.time()
                if now - last_upd[0] < 3.0: # Render ফ্রি সার্ভারে বারবার মেসেজ এডিট এড়িয়ে চলার জন্য interval
                    return
                last_upd[0] = now
                dl   = d.get("downloaded_bytes", 0) / 1_048_576
                tot  = (d.get("total_bytes") or d.get("total_bytes_estimate") or 0) / 1_048_576
                pct  = d.get("_percent_str", "?%").strip()
                ram  = get_ram_usage_mb()
                try:
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(
                            f"⏬ *Downloading...*\n\n"
                            f"📊 Progress: `{pct}`\n"
                            f"📁 Size: `{dl:.1f}MB / {tot:.1f}MB`\n"
                            f"💾 RAM: `{ram:.0f}MB/500MB`",
                            parse_mode="Markdown",
                        ),
                        loop,
                    )
                except Exception:
                    pass

            user_dl_dir = os.path.join("downloads", str(user_id))
            os.makedirs(user_dl_dir, exist_ok=True)

            ydl_opts = get_base_ydl_opts(
                progress_hook=progress_hook,
                download_dir=user_dl_dir,
                is_premium_user=is_prem,
            )
            ydl_opts["format"] = get_format_string(query.data)

            if is_audio:
                if _ffmpeg_dir():
                    ydl_opts["postprocessors"] = [{
                        "key":             "FFmpegExtractAudio",
                        "preferredcodec":  "mp3",
                        "preferredquality":"192",
                    }]
                else:
                    ydl_opts["format"] = "bestaudio/best"

            def download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return find_downloaded_file(ydl, info, user_dl_dir, is_audio), info.get("title", "Media")

            file_path, title = await loop.run_in_executor(None, download)

            size_mb = os.path.getsize(file_path) / 1_048_576
            if size_mb > TELEGRAM_UPLOAD_LIMIT_MB:
                await status_msg.edit_text(
                    f"❌ File too large: `{size_mb:.1f}MB`\n"
                    f"Render & Telegram limit: {TELEGRAM_UPLOAD_LIMIT_MB}MB\n"
                    "360p বা 480p দিয়ে আবার চেষ্টা করো।"
                )
                return

            await status_msg.edit_text("📤 *Uploading to Telegram...*", parse_mode="Markdown")

            caption = f"{'🎵' if is_audio else '🎥'} {title}\n\nDownloaded via YouTube Bot"

            with open(file_path, "rb") as f:
                if is_audio:
                    await context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=f,
                        title=title,
                        caption=caption,
                        reply_markup=main_kb(),
                    )
                else:
                    await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=f,
                        caption=caption,
                        reply_markup=main_kb(),
                        supports_streaming=True,
                    )

            log_download(user_id, title, quality_label)

            if not is_prem:
                increment_user_quota(user_id)

            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception:
            logger.exception("Download failed for user %s", user_id)
            raw = str(__import__("sys").exc_info()[1])

            if "Requested format is not available" in raw:
                msg = "এই quality-তে video পাওয়া যাচ্ছে না। অন্য quality try করো।"
            elif "ffmpeg" in raw.lower() or "postprocessor" in raw.lower():
                msg = "Server-এ FFmpeg পাওয়া যাচ্ছে না। MP3 ছাড়া অন্য format try করো।"
            elif "Video unavailable" in raw or "Private video" in raw:
                msg = "Video unavailable বা private।"
            elif "age" in raw.lower() and ("restricted" in raw.lower() or "confirm" in raw.lower()):
                msg = "Age-restricted video — download করা যাচ্ছে না।"
            elif "Sign in to confirm" in raw:
                msg = "YouTube verification চাইছে। কিছুক্ষণ পরে আবার চেষ্টা করো।"
            elif "429" in raw:
                msg = "YouTube rate limit। ১ মিনিট পরে try করো।"
            elif "too large" in raw.lower() or "max_filesize" in raw.lower():
                msg = f"File {MAX_FILE_SIZE_MB}MB-এর বেশি। কম quality ব্যবহার করো।"
            elif "FileNotFoundError" in raw:
                msg = "Download হয়েছে কিন্তু file খুঁজে পাচ্ছি না। আবার try করো।"
            else:
                msg = f"`{raw[:200]}`"

            try:
                await status_msg.edit_text(f"❌ *Download Failed!*\n\n{msg}", parse_mode="Markdown")
            except Exception:
                pass

        finally:
            # 🚀 RENDER CRITICAL FIX: ডাউনলোড সম্পূর্ণ হওয়া মাত্রই লোকাল ফাইল ও ফোল্ডার সম্পূর্ণ মুছে ফেলা
            try:
                if user_dl_dir and os.path.isdir(user_dl_dir):
                    shutil.rmtree(user_dl_dir, ignore_errors=True)
            except Exception:
                logger.exception("Cleanup failed")
            gc.collect()

# ============================================================
# MAIN
# ============================================================
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("CRITICAL: Set BOT_TOKEN environment variable!")
        return

    os.makedirs("downloads", exist_ok=True)
    cleanup_old_files()

    ffmpeg_ok = bool(_ffmpeg_dir())
    logger.info("FFmpeg available: %s", ffmpeg_ok)
    if not ffmpeg_ok:
        logger.warning("FFmpeg NOT found — MP3 and 720p/1080p merge will fallback")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("help",           help_command))
    app.add_handler(CommandHandler("profile",        profile))
    app.add_handler(CommandHandler("mystats",        mystats))
    app.add_handler(CommandHandler("leaderboard",    leaderboard))
    app.add_handler(CommandHandler("add_premium",    add_premium))
    app.add_handler(CommandHandler("remove_premium", remove_premium))
    app.add_handler(CommandHandler("ban",            ban_cmd))
    app.add_handler(CommandHandler("unban",          unban_cmd))
    app.add_handler(CommandHandler("stats",          stats))
    app.add_handler(CommandHandler("broadcast",      broadcast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.job_queue.run_repeating(check_premium_expiry, interval=21600, first=60)

    logger.info("Bot started successfully!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )

if __name__ == "__main__":
    main()
