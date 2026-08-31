# ============================================================
# pip install: python-telegram-bot[job-queue] yt-dlp flask gunicorn psutil
# FFmpeg (Ubuntu): sudo apt update && sudo apt install -y ffmpeg
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

DB_FILE                          = "user_data.db"
MAX_FILE_SIZE_MB                 = 80
RAM_LIMIT_MB                     = 400
REFERRAL_THRESHOLD_FOR_UNLIMITED = 100
TELEGRAM_UPLOAD_LIMIT_MB         = 49
DOWNLOAD_LOCK                    = asyncio.Semaphore(2)

# ── YouTube URL validation — FIX: &list= সহ সব URL ধরবে ──
_YT_RE = re.compile(
    r'(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)'
    r'(/watch\?[^\s]*|/shorts/[^\s]*|/[^\s]*)?'
)

def is_youtube_url(text: str) -> bool:
    return bool(_YT_RE.search(text))

def clean_youtube_url(url: str) -> str:
    """Playlist ও extra parameter বাদ দিয়ে শুধু video URL রাখো"""
    # video id বের করো
    match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    # youtu.be short link
    match = re.search(r'youtu\.be/([a-zA-Z0-9_-]{11})', url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    # shorts
    match = re.search(r'/shorts/([a-zA-Z0-9_-]{11})', url)
    if match:
        return f"https://www.youtube.com/shorts/{match.group(1)}"
    return url

# ============================================================
# DATABASE
# ============================================================
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with _conn() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
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

def is_user_banned(user_id: int) -> bool:
    with _conn() as db:
        row = db.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0])

def is_user_premium(user_id: int) -> tuple[bool, str]:
    if user_id == ADMIN_ID:
        return True, "Admin"
    with _conn() as db:
        row = db.execute(
            "SELECT referral_count, premium_until FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return False, "Free User"
    ref_count, prem_until = row
    if ref_count >= REFERRAL_THRESHOLD_FOR_UNLIMITED or prem_until == "UNLIMITED":
        return True, "Unlimited Access"
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

def set_premium_db(user_id: int, duration_str: str):
    duration_str = duration_str.lower().strip()
    with _conn() as db:
        db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        if "unlimited" in duration_str:
            db.execute("UPDATE users SET premium_until='UNLIMITED' WHERE user_id=?", (user_id,))
            return "Lifetime Unlimited"
        else:
            days_match = re.search(r'\d+', duration_str)
            days = int(days_match.group()) if days_match else 1
            exp = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (exp, user_id))
            return f"{days} Days (Expires: {exp})"

def get_all_users():
    with _conn() as db:
        rows = db.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
    return [r[0] for r in rows]

# ============================================================
# KEYBOARDS
# ============================================================
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ Contact Admin", url="https://t.me/Devsahatanas")],
        [InlineKeyboardButton("⚡ Any Video Downloader Bot", url=OTHER_BOT_URL)],
    ])

def quality_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 360p", callback_data="q_360"),
            InlineKeyboardButton("📺 480p", callback_data="q_480"),
        ],
        [
            InlineKeyboardButton("🎬 720p HD", callback_data="q_720"),
            InlineKeyboardButton("🖥️ 1080p FHD", callback_data="q_1080"),
        ],
        [
            InlineKeyboardButton("🎵 MP3 Audio", callback_data="q_mp3"),
        ],
    ])

# ============================================================
# yt-dlp helpers
# ============================================================
def _ffmpeg_dir() -> str | None:
    p = shutil.which("ffmpeg")
    return os.path.dirname(p) if p else None

def get_base_ydl_opts(download_dir: str = "downloads", quality: str = "best") -> dict:
    if quality == "mp3":
        # MP3: শুধু audio
        fmt = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif quality in ("360", "480", "720", "1080"):
        # ✅ FINAL FIX: একদম সহজ — শুধু height filter, বাকি সব "best" fallback
        fmt = f"best[height<={quality}]/best"
        postprocessors = []
    else:
        fmt = "best"
        postprocessors = []

    opts: dict = {
        "outtmpl": os.path.join(download_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": fmt,
        "merge_output_format": "mp4",

        # ✅ mweb client — সবচেয়ে সহজ, format সব দেয়, cookies লাগে না
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "ios", "web"],
            }
        },

        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },

        "sleep_interval": 1,
        "max_sleep_interval": 3,
        "throttledratelimit": 100000,
        "retries": 5,
        "fragment_retries": 5,
        "skip_unavailable_fragments": True,
    }

    if postprocessors:
        opts["postprocessors"] = postprocessors

    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"

    fd = _ffmpeg_dir()
    if fd:
        opts["ffmpeg_location"] = fd

    return opts

def find_downloaded_file(ydl, info: dict, download_dir: str):
    filename = ydl.prepare_filename(info)
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
    raise FileNotFoundError("Downloaded file not found")

# ============================================================
# COMMANDS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name)

    if is_user_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    me = await context.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user.id}"
    today_dl, ref_count = get_user_stats(user.id)
    is_prem, status_str = is_user_premium(user.id)

    badge       = "👑 Admin" if user.id == ADMIN_ID else ("⭐ Premium" if is_prem else "🆓 Free")
    status_line = "♾️ Unlimited Access" if is_prem else f"📥 {today_dl}/2 downloads used"

    filled_blocks = min(10, ref_count // 10)
    progress_bar  = "█" * filled_blocks + "░" * (10 - filled_blocks)

    msg_text = (
        f"🎬 *YouTube Downloader Bot*\n"
        f"───────────────────\n\n"
        f"👋 *Welcome, {user.first_name}!*\n\n"
        f"💳 *Account:* `{badge}`\n"
        f"📊 *Status:* `{status_line}`\n"
        f"⏰ *Today's Downloads:* `{today_dl}/{'∞' if is_prem else '2'}`\n\n"
        f"───────────────────\n"
        f"👥 *Referral Progress*\n"
        f"`[{progress_bar}]` `{ref_count}/100`\n"
        f"🎯 *100 জন আনলে Lifetime Unlimited!*\n\n"
        f"🔗 *তোমার Referral Link:*\n`{ref_link}`\n\n"
        f"───────────────────\n"
        f"📩 *যে কোনো YouTube link পাঠাও!*\n"
        f"⬇️ *360p • 480p • 720p • 1080p • MP3*\n\n"
        f"📌 */help — সব commands দেখো*"
    )

    await update.message.reply_text(
        msg_text,
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ এই কমান্ডটি শুধু Admin ব্যবহার করতে পারবে।")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ **ব্যবহার পদ্ধতি:**\n`/premium <user_id> <1days|2days|30days|unlimited>`",
            parse_mode="Markdown"
        )
        return

    try:
        target_id = int(context.args[0])
        duration  = context.args[1]
    except ValueError:
        await update.message.reply_text("❌ সঠিক User ID লিখুন।")
        return

    res = set_premium_db(target_id, duration)
    await update.message.reply_text(
        f"✅ User `{target_id}` updated!\nStatus: `{res}`", parse_mode="Markdown"
    )

async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ এই কমান্ডটি শুধু Admin ব্যবহার করতে পারবে।")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ ব্যবহার পদ্ধতি: `/post আপনার মেসেজটি লিখুন`", parse_mode="Markdown"
        )
        return

    broadcast_text = " ".join(context.args)
    users      = get_all_users()
    status_msg = await update.message.reply_text(
        f"⏳ {len(users)} জন ইউজারের কাছে ব্রডকাস্ট পাঠানো হচ্ছে..."
    )

    sent = failed = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=broadcast_text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"📢 *পোস্ট ব্রডকাস্ট সম্পন্ন হয়েছে!*\n\n✅ সফল: `{sent}`\n❌ ব্যর্থ: `{failed}`",
        parse_mode="Markdown",
    )

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

    is_prem, _ = is_user_premium(user.id)
    today_dl, _ = get_user_stats(user.id)

    if not is_prem and today_dl >= 2:
        await update.message.reply_text(
            "🚫 *Daily Limit Reached!*\n\nFree users: দিনে *2টি* download পেতে পারবেন।",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )
        return

    # FIX: URL clean করে সেভ করো — &list= বাদ যাবে
    clean_url = clean_youtube_url(text)
    context.user_data["yt_url"] = clean_url

    await update.message.reply_text(
        "✅ *লিঙ্ক পাওয়া গেছে!*\n\n🎯 কোন quality তে ডাউনলোড করবেন?",
        parse_mode="Markdown",
        reply_markup=quality_kb(),
    )

# ============================================================
# QUALITY CALLBACK
# ============================================================
async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user    = query.from_user
    quality = query.data.replace("q_", "")
    text    = context.user_data.get("yt_url")

    if not text:
        await query.edit_message_text("❌ URL আর নেই, নতুন লিঙ্ক পাঠান।")
        return

    is_prem, _ = is_user_premium(user.id)
    today_dl, _ = get_user_stats(user.id)

    if not is_prem and today_dl >= 2:
        await query.edit_message_text(
            "🚫 *Daily Limit Reached!*\n\nFree users: দিনে *2টি* download পাবেন।",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )
        return

    quality_label = "🎵 MP3" if quality == "mp3" else f"🎬 {quality}p"
    status_msg = await query.edit_message_text(
        f"⏳ *{quality_label} ডাউনলোড হচ্ছে...*\nঅপেক্ষা করুন।",
        parse_mode="Markdown",
    )

    file_path   = None
    user_dl_dir = None

    try:
        loop        = asyncio.get_running_loop()
        user_dl_dir = os.path.join("downloads", str(user.id))
        os.makedirs(user_dl_dir, exist_ok=True)

        ydl_opts = get_base_ydl_opts(download_dir=user_dl_dir, quality=quality)

        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=True)
                return find_downloaded_file(ydl, info, user_dl_dir), info.get("title", "Video")

        async with DOWNLOAD_LOCK:
            file_path, title = await loop.run_in_executor(None, download)

        size_mb = os.path.getsize(file_path) / 1_048_576
        if size_mb > TELEGRAM_UPLOAD_LIMIT_MB:
            await status_msg.edit_text(
                f"❌ File too large: `{size_mb:.1f}MB` (Telegram limit 50MB)\n"
                f"ছোট quality বেছে নিন।",
                parse_mode="Markdown",
            )
            return

        await status_msg.edit_text("📤 *Telegram এ পাঠানো হচ্ছে...*", parse_mode="Markdown")
        caption = f"🎥 {title}\n\n📊 Quality: {quality_label}\nDownloaded via YouTube Bot"

        with open(file_path, "rb") as f:
            if quality == "mp3":
                await context.bot.send_audio(
                    chat_id=user.id,
                    audio=f,
                    caption=caption,
                    reply_markup=main_kb(),
                )
            else:
                await context.bot.send_video(
                    chat_id=user.id,
                    video=f,
                    caption=caption,
                    reply_markup=main_kb(),
                    supports_streaming=True,
                )

        log_download(user.id, title, quality.upper())

        if not is_prem:
            increment_user_quota(user.id)

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.exception("Download failed for user %s", user.id)
        err_str = str(e).lower()

        if "private" in err_str or "sign in" in err_str:
            reason = "ভিডিওটি Private — Login ছাড়া ডাউনলোড হবে না।"
        elif "copyright" in err_str or "removed" in err_str:
            reason = "ভিডিওটি Copyright বা Remove হয়ে গেছে।"
        elif "age" in err_str:
            reason = "Age-restricted ভিডিও — ডাউনলোড সম্ভব না।"
        elif "unavailable" in err_str or "not available" in err_str:
            reason = "ভিডিওটি এই সার্ভারে পাওয়া যাচ্ছে না।"
        elif "throttl" in err_str or "429" in err_str:
            reason = "YouTube rate limit করেছে — কিছুক্ষণ পর চেষ্টা করুন।"
        elif "format" in err_str:
            reason = "এই quality তে ভিডিও নেই — অন্য quality বেছে নিন।"
        else:
            reason = "অন্য quality বা অন্য link দিয়ে চেষ্টা করুন।"

        try:
            await status_msg.edit_text(
                f"❌ *Download Failed!*\n\n⚠️ {reason}",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    finally:
        try:
            if file_path and os.path.isfile(file_path):
                os.remove(file_path)
            if user_dl_dir and os.path.isdir(user_dl_dir):
                for name in os.listdir(user_dl_dir):
                    p = os.path.join(user_dl_dir, name)
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
        except Exception:
            pass
        gc.collect()

# ============================================================
# MAIN
# ============================================================
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("CRITICAL: Set BOT_TOKEN environment variable!")
        return

    os.makedirs("downloads", exist_ok=True)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern="^q_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started successfully!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )

if __name__ == "__main__":
    main()
