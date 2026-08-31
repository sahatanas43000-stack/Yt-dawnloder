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

DB_FILE                       = "user_data.db"
MAX_FILE_SIZE_MB               = 80
RAM_LIMIT_MB                   = 400
REFERRAL_THRESHOLD_FOR_UNLIMITED = 100
TELEGRAM_UPLOAD_LIMIT_MB       = 49
DOWNLOAD_LOCK                  = asyncio.Semaphore(2)

# ── Markdown escape ────────────────────────────────────────
_MD_SPECIAL = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')

def md_escape(text: str) -> str:
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
    c = sqlite3.connect(DB_FILE, timeout=10)
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
    if ref_count >= REFERRAL_THRESHOLD_FOR_UNLIMITED or prem_until == "UNLIMITED":
        return True, "Unlimited Premium"
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
    dl_folder = "downloads"
    if not os.path.exists(dl_folder):
        return
    cutoff = datetime.now().timestamp() - 3600
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
# KEYBOARDS WITH AI BUTTON
# ============================================================
def force_join_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel 1", url=CHANNEL_1_URL)],
        [InlineKeyboardButton("📢 Join Channel 2", url=CHANNEL_2_URL)],
        [InlineKeyboardButton("🤖 AI Assistant", callback_data="ai_feature")],
    ])

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 AI Assistant", callback_data="ai_feature")],
        [InlineKeyboardButton("🛠️ Contact Admin", url="https://t.me/Devsahatanas")],
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
        "outtmpl": os.path.join(download_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if not is_premium_user:
        opts["max_filesize"] = f"{MAX_FILE_SIZE_MB}M"
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    fd = _ffmpeg_dir()
    if fd:
        opts["ffmpeg_location"] = fd
    return opts

def find_downloaded_file(ydl, info: dict, download_dir: str, is_audio: bool):
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

    me = await context.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user.id}"
    today_dl, ref_count = get_user_stats(user.id)
    is_prem, status_str = is_user_premium(user.id)

    badge      = "👑 Admin"   if user.id == ADMIN_ID else ("⭐ Premium" if is_prem else "🆓 Free")
    status_line = "♾️ Unlimited Access" if user.id == ADMIN_ID else (f"✅ {status_str}" if is_prem else f"📥 Today: {today_dl}/2 downloads used")

    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 *YouTube Downloader Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 *Welcome, {user.first_name}!*\n\n"
        f"┌ 🏷️ *Account:* `{badge}`\n"
        f"├ 📊 *Status:* `{status_line}`\n"
        f"└ 🕐 *Today's Downloads:* `{today_dl}/{'∞' if is_prem else '2'}`\n\n"
        f"🔗 *তোমার Referral Link:*\n`{ref_link}`\n\n"
        f"📩 *যেকোনো YouTube link পাঠাও!*",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )

# ── NEW /premium COMMAND ────────────────────────────────────
async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ এই কমান্ডটি শুধু Admin ব্যবহার করতে পারবে।")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ **ব্যবহার পদ্ধতি:**\n"
            "`/premium <user_id> <1days|2days|30days|unlimited>`\n\n"
            "**উদাহরণ:**\n"
            "`/premium 123456789 1days`\n"
            "`/premium 123456789 30days`\n"
            "`/premium 123456789 unlimited`",
            parse_mode="Markdown"
        )
        return

    try:
        target_id = int(context.args[0])
        duration = context.args[1]
    except ValueError:
        await update.message.reply_text("❌ অনুগ্রহ করে সঠিক User ID প্রদান করুন।")
        return

    res = set_premium_db(target_id, duration)
    await update.message.reply_text(f"✅ User `{target_id}` updated to Premium!\nDetails: `{res}`", parse_mode="Markdown")

    try:
        await context.bot.send_message(
            target_id,
            f"🎉 **অভিনন্দন!**\n\nআপনার অ্যাকাউন্টে **Premium Access** একটিভ করা হয়েছে।\nমেয়াদ/স্ট্যাটাস: `{res}`",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ এই কমান্ডটি শুধু Admin ব্যবহার করতে পারবে।")
        return
    
    if not context.args:
        await update.message.reply_text("⚠️ ব্যবহার পদ্ধতি: `/post আপনার মেসেজটি লিখুন`", parse_mode="Markdown")
        return

    broadcast_text = " ".join(context.args)
    users = get_all_users()
    status_msg = await update.message.reply_text(f"⏳ {len(users)} জন ইউজারের কাছে ব্রডকাস্ট পাঠানো হচ্ছে...")

    sent = 0
    failed = 0

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=broadcast_text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1

    await status_msg.edit_text(
        f"📢 *পোস্ট ব্রডকাস্ট সম্পন্ন হয়েছে!*\n\n✅ সফল: `{sent}`\n❌ ব্যর্থ: `{failed}`",
        parse_mode="Markdown",
    )

# ── AI BUTTON HANDLER ──────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "ai_feature":
        await query.message.reply_text(
            "🤖 **AI Assistant Feature**\n\n"
            "আমাদের AI বট সার্ভিসটি ব্যবহার করতে যোগাযোগের চেষ্টা করুন বা পরবর্তীতে আপডেট পান।",
            parse_mode="Markdown"
        )

# ============================================================
# MESSAGE HANDLER & DIRECT DOWNLOADER
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

    is_prem, status_str = is_user_premium(user.id)
    today_dl, ref_count = get_user_stats(user.id)

    if not is_prem and today_dl >= 2:
        await update.message.reply_text(
            "🚫 *Daily Limit Reached!*\n\nFree users: দিনে *2টা* download।",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )
        return

    status_msg = await update.message.reply_text("⏳ *ডাউনলোড প্রসেস শুরু হচ্ছে...*", parse_mode="Markdown")

    file_path        = None
    user_dl_dir      = None

    try:
        loop = asyncio.get_running_loop()

        user_dl_dir = os.path.join("downloads", str(user.id))
        os.makedirs(user_dl_dir, exist_ok=True)

        ydl_opts = get_base_ydl_opts(
            download_dir=user_dl_dir,
            is_premium_user=is_prem,
        )

        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=True)
                return find_downloaded_file(ydl, info, user_dl_dir, is_audio=False), info.get("title", "Video")

        async with DOWNLOAD_LOCK:
            file_path, title = await loop.run_in_executor(None, download)

        size_mb = os.path.getsize(file_path) / 1_048_576
        if size_mb > TELEGRAM_UPLOAD_LIMIT_MB:
            await status_msg.edit_text(f"❌ File too large: `{size_mb:.1f}MB`")
            return

        await status_msg.edit_text("📤 *Uploading to Telegram...*", parse_mode="Markdown")

        caption = f"🎥 {title}\n\nDownloaded via YouTube Bot"

        with open(file_path, "rb") as f:
            await context.bot.send_video(
                chat_id=user.id,
                video=f,
                caption=caption,
                reply_markup=main_kb(),
                supports_streaming=True,
            )

        log_download(user.id, title, "DIRECT_BEST")

        if not is_prem:
            increment_user_quota(user.id)

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.exception("Download failed for user %s", user.id)
        try:
            await status_msg.edit_text(f"❌ *Download Failed!*", parse_mode="Markdown")
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
    cleanup_old_files()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    # Register Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))  # NEW /premium COMMAND
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started successfully!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )

if __name__ == "__main__":
    main()
