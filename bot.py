"""
🚀 Production-Grade YouTube Video & Audio Downloader Telegram Bot
===================================================================
Developer / Owner Contact: @Devsahatanas
Other Bot: @BomssssssssBot
Features:
  - Force Channel Join Verification (@sahatanas, @sahatanass)
  - Video (360p, 480p, 720p, 1080p Premium) & Audio (MP3) extraction
  - Live progress update hook (Downloaded % & MBs)
  - Referral System (100 invites = Auto Lifetime Free Premium)
  - Prominent display & button for Other Bot (@BomssssssssBot)
  - Admin Commands (/add_premium, /remove_premium, /stats, /broadcast, /post)
  - Integrated Flask ping server for 24/7 uptime on Render/Railway
"""

import os
import re
import gc
import time
import logging
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask
import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------- #
# Environment & Bot Configuration
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
ADMIN_CODE = os.environ.get("ADMIN_CODE", "1169")

BOT_DEV = "@Devsahatanas"
CHANNEL_1 = "@sahatanas"
CHANNEL_2 = "@sahatanass"
CHANNEL_1_URL = "https://t.me/sahatanas"
CHANNEL_2_URL = "https://t.me/sahatanass"
OTHER_BOT_USERNAME = "@BomssssssssBot"
OTHER_BOT_URL = "https://t.me/BomssssssssBot"
SUPPORT_URL = "https://t.me/Devsahatanas"

DB_PATH = os.environ.get("DB_PATH", "/tmp/bot_data.db")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
PORT = int(os.environ.get("PORT", 10000))

MAX_DAILY_DOWNLOADS_FREE = 2
MAX_FILE_SIZE_MB = 80  # Telegram upload safe limit for free tier servers
MAX_DURATION_SECONDS = 40 * 60  # 40 minutes safety limit
MAX_CONCURRENT_DOWNLOADS = 1

YOUTUBE_URL_REGEX = re.compile(
    r"(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be)/\S+", re.IGNORECASE
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("yt_downloader_bot")

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
active_status_text = {}

# --------------------------------------------------------------------------- #
# Flask Keep-Alive Server (For Render / Web Service Pings)
# --------------------------------------------------------------------------- #

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running perfectly 24/7!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# --------------------------------------------------------------------------- #
# Database Layer
# --------------------------------------------------------------------------- #

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_premium INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER DEFAULT 0,
            referral_count INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT,
            downloaded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

def ensure_user(user_id: int, username: str, referred_by: int = 0):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            """
            INSERT INTO users (user_id, username, is_admin, is_premium, referred_by, referral_count, joined_at)
            VALUES (?, ?, ?, 0, ?, 0, ?)
            """,
            (
                user_id,
                username,
                1 if user_id == ADMIN_ID else 0,
                referred_by,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        
        # Referrer tracking
        if referred_by and referred_by != user_id:
            cur.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (referred_by,))
            conn.commit()
            
            # Check 100 referrals auto premium
            cur.execute("SELECT referral_count, is_premium FROM users WHERE user_id = ?", (referred_by,))
            row = cur.fetchone()
            if row and row["referral_count"] >= 100 and row["is_premium"] == 0:
                cur.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (referred_by,))
                conn.commit()

    conn.close()

def is_user_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row["is_admin"] == 1)

def is_user_premium(user_id: int) -> bool:
    if is_user_admin(user_id):
        return True
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT is_premium FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row["is_premium"] == 1)

def set_premium_status(user_id: int, status: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_premium = ? WHERE user_id = ?", (status, user_id))
    conn.commit()
    conn.close()

def get_user_data(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_all_user_ids():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]

def get_downloads_last_24h(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cur.execute(
        "SELECT COUNT(*) AS c FROM downloads WHERE user_id = ? AND downloaded_at >= ?",
        (user_id, since),
    )
    row = cur.fetchone()
    conn.close()
    return row["c"] if row else 0

def record_download(user_id: int, url: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO downloads (user_id, url, downloaded_at) VALUES (?, ?, ?)",
        (user_id, url, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

def can_user_download(user_id: int) -> bool:
    if is_user_premium(user_id):
        return True
    return get_downloads_last_24h(user_id) < MAX_DAILY_DOWNLOADS_FREE

# --------------------------------------------------------------------------- #
# Force Channel Join Verification Helper
# --------------------------------------------------------------------------- #

async def check_force_join(bot, user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    for ch in [CHANNEL_1, CHANNEL_2]:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status not in ["creator", "administrator", "member"]:
                return False
        except Exception as e:
            logger.warning("Error checking channel %s membership for user %s: %s", ch, user_id, e)
            return False
    return True

def force_join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📢 Channel 1", url=CHANNEL_1_URL),
                InlineKeyboardButton("📢 Channel 2", url=CHANNEL_2_URL),
            ],
            [
                InlineKeyboardButton(f"🤖 Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL),
            ],
            [
                InlineKeyboardButton("💬 Support", url=SUPPORT_URL),
                InlineKeyboardButton("✅ Verify Join", callback_data="verify_join"),
            ],
        ]
    )

def main_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"🤖 Try Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL),
            ],
            [
                InlineKeyboardButton("💬 Support", url=SUPPORT_URL),
                InlineKeyboardButton("📢 Channel", url=CHANNEL_1_URL),
            ]
        ]
    )

# --------------------------------------------------------------------------- #
# yt-dlp Configuration & Progress Tracker
# --------------------------------------------------------------------------- #

YTDLP_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
            "skip": ["hls", "dash"],
        }
    },
    "retries": 5,
    "fragment_retries": 5,
}

def format_bytes(bytes_val):
    if not bytes_val:
        return "0 MB"
    mb = bytes_val / (1024 * 1024)
    return f"{mb:.2f} MB"

def _extract_info_sync(url: str) -> dict:
    opts = {**YTDLP_BASE_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def _download_sync(url: str, output_template: str, format_spec: str, is_audio: bool, loop, status_msg, chat_id, bot) -> str:
    last_update_time = [0.0]

    def progress_hook(d):
        if d['status'] == 'downloading':
            now = time.time()
            if now - last_update_time[0] >= 3.0:
                last_update_time[0] = now
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                percent = d.get('_percent_str', '0%').strip()
                
                down_str = format_bytes(downloaded)
                tot_str = format_bytes(total)
                
                text = (
                    f"📥 <b>Downloading...</b>\n\n"
                    f"📊 <b>Progress:</b> <code>{percent}</code>\n"
                    f"💾 <b>Size:</b> <code>{down_str} / {tot_str}</code>"
                )
                asyncio.run_coroutine_threadsafe(
                    set_status(status_msg, chat_id, text), loop
                )

    opts = {
        **YTDLP_BASE_OPTS,
        "format": format_spec,
        "outtmpl": output_template,
        "restrictfilenames": True,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "progress_hooks": [progress_hook],
    }

    if is_audio:
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        if is_audio:
            base = os.path.splitext(filename)[0]
            filename = f"{base}.mp3"

        if os.path.exists(filename):
            return filename

        base = os.path.splitext(filename)[0]
        for ext in ("mp4", "mkv", "webm", "mp3"):
            candidate = f"{base}.{ext}"
            if os.path.exists(candidate):
                return candidate
        return filename

async def extract_info(url: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_info_sync, url)

async def download_media(url: str, output_template: str, format_spec: str, is_audio: bool, status_msg, chat_id, bot) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _download_sync, url, output_template, format_spec, is_audio, loop, status_msg, chat_id, bot
    )

# --------------------------------------------------------------------------- #
# UI Helpers & Keyboards
# --------------------------------------------------------------------------- #

def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Live Status", callback_data="live_status"),
                InlineKeyboardButton(f"🤖 Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL),
            ],
            [
                InlineKeyboardButton("💬 Support", url=SUPPORT_URL),
            ]
        ]
    )

def quality_selection_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎵 MP3 Audio", callback_data=f"dl|mp3"),
                InlineKeyboardButton("📺 360p", callback_data=f"dl|360"),
            ],
            [
                InlineKeyboardButton("📺 480p", callback_data=f"dl|480"),
                InlineKeyboardButton("🎬 720p HD", callback_data=f"dl|720"),
            ],
            [
                InlineKeyboardButton("⭐ 1080p FHD (Premium)", callback_data=f"dl|1080"),
            ],
            [
                InlineKeyboardButton(f"🤖 Check Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL),
            ]
        ]
    )

async def set_status(message, chat_id: int, text: str, keyboard=None):
    try:
        await message.edit_text(
            text, reply_markup=keyboard or status_keyboard(), parse_mode=ParseMode.HTML
        )
        active_status_text[(chat_id, message.message_id)] = text
    except Exception as exc:
        logger.debug("Status edit warning: %s", exc)

# --------------------------------------------------------------------------- #
# User Command Handlers
# --------------------------------------------------------------------------- #

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot = context.bot

    # Parse referral argument
    referred_by = 0
    if context.args and context.args[0].startswith("ref_"):
        try:
            referred_by = int(context.args[0].replace("ref_", ""))
        except ValueError:
            referred_by = 0

    ensure_user(user.id, user.username or user.first_name, referred_by=referred_by)

    # Force Channel Join Check
    is_joined = await check_force_join(bot, user.id)
    if not is_joined:
        await update.message.reply_text(
            "⚠️ <b>Access Restricted!</b>\n\n"
            "Bot use korte hole apnake obosshoi amader official channel gulote join korte habe:\n\n"
            f"1️⃣ {CHANNEL_1}\n"
            f"2️⃣ {CHANNEL_2}\n\n"
            f"Amader onno bot try korun: 🤖 {OTHER_BOT_USERNAME}\n\n"
            "Nicher button a click kore join korun, tarpor <b>'Verify Join'</b> a click করুন!",
            parse_mode=ParseMode.HTML,
            reply_markup=force_join_keyboard(),
        )
        return

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
    user_data = get_user_data(user.id)
    ref_count = user_data["referral_count"] if user_data else 0

    text = (
        f"👋 <b>Welcome to YT Downloader Bot!</b>\n\n"
        f"Send me any YouTube link and select your preferred quality!\n\n"
        f"📊 <b>Bot Rules:</b>\n"
        f"• Free Users: <b>{MAX_DAILY_DOWNLOADS_FREE} downloads / 24 hrs</b> (up to 720p)\n"
        f"• Premium Users: <b>Unlimited downloads</b> & <b>1080p Access</b>\n"
        f"• Max File Size Cap: <b>{MAX_FILE_SIZE_MB}MB</b>\n\n"
        f"🎁 <b>Free Lifetime Premium Offer:</b>\n"
        f"Invite <b>100 friends</b> to use the bot and get lifetime free premium automatically!\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>\n"
        f"👥 <b>Your Referrals:</b> <code>{ref_count}/100</code>\n\n"
        f"🤖 <b>Check out our Other Bot:</b> {OTHER_BOT_USERNAME}\n\n"
        f"🛠 <i>Developed by {BOT_DEV}</i>"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_inline_keyboard(),
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
    user_data = get_user_data(user.id)
    ref_count = user_data["referral_count"] if user_data else 0

    if is_user_premium(user.id):
        text = (
            "👑 <b>You have Unlimited (Premium / Admin) Access!</b>\n\n"
            f"🤖 Check our other bot: {OTHER_BOT_USERNAME}"
        )
    else:
        used = get_downloads_last_24h(user.id)
        remaining = max(0, MAX_DAILY_DOWNLOADS_FREE - used)
        text = (
            f"📊 <b>Your Quota & Account Details:</b>\n\n"
            f"• Daily Downloads Used: <b>{used}/{MAX_DAILY_DOWNLOADS_FREE}</b>\n"
            f"• Downloads Remaining: <b>{remaining}</b>\n"
            f"• Referrals Count: <b>{ref_count}/100</b>\n\n"
            f"🎁 <i>Invite 100 friends to unlock Lifetime Premium!</i>\n"
            f"🔗 <b>Referral Link:</b>\n<code>{ref_link}</code>\n\n"
            f"🤖 Try our other bot: {OTHER_BOT_USERNAME}"
        )

    await update.message.reply_text(
        text, 
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"🤖 Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL)]]
        )
    )

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/redeem &lt;code&gt;</code>", parse_mode=ParseMode.HTML
        )
        return

    submitted_code = context.args[0].strip()
    if submitted_code == ADMIN_CODE:
        set_premium_status(user.id, 1)
        await update.message.reply_text(
            "🎉 <b>Code Redeemed!</b> You now have <b>Unlimited Premium Access</b>! 👑",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("❌ Invalid code. Contact support for help.")

# --------------------------------------------------------------------------- #
# Admin Command Handlers
# --------------------------------------------------------------------------- #

async def add_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/add_premium &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(context.args[0])
        set_premium_status(target_id, 1)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> is now a <b>Premium Member</b>!", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")

async def remove_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/remove_premium &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(context.args[0])
        set_premium_status(target_id, 0)
        await update.message.reply_text(f"✅ Removed Premium from user <code>{target_id}</code>.", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_admin(update.effective_user.id):
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS total FROM users")
    total_users = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) AS prem FROM users WHERE is_premium = 1")
    prem_users = cur.fetchone()["prem"]

    cur.execute("SELECT COUNT(*) AS dl FROM downloads")
    total_dls = cur.fetchone()["dl"]
    conn.close()

    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👤 Total Users: <b>{total_users}</b>\n"
        f"⭐ Premium Users: <b>{prem_users}</b>\n"
        f"📥 Total Downloads: <b>{total_dls}</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/broadcast &lt;message text&gt;</code>", parse_mode=ParseMode.HTML)
        return

    msg_text = " ".join(context.args)
    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    await update.message.reply_text(f"⏳ Broadcasting to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=msg_text, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Broadcast finished!\nSent: <b>{sent}</b> | Failed: <b>{failed}</b>", parse_mode=ParseMode.HTML)

async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_admin(update.effective_user.id):
        return

    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text("❌ Reply to any message or media post with <code>/post</code> to broadcast it.")
        return

    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    await update.message.reply_text(f"⏳ Forwarding post to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            await replied.copy(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Media post broadcast finished!\nSent: <b>{sent}</b> | Failed: <b>{failed}</b>", parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# Callback Query Handler
# --------------------------------------------------------------------------- #

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    data = query.data

    if data == "verify_join":
        joined = await check_force_join(context.bot, user.id)
        if joined:
            await query.answer("✅ Verification successful! APni akhon bot bebohar korte parben.", show_alert=True)
            await query.message.delete()
        else:
            await query.answer("❌ APni ekhono duti channel-e join korenni! Join kore abar try করুন.", show_alert=True)

    elif data == "live_status":
        current = active_status_text.get((chat_id, message_id), "⏳ Kono active task cholche na.")
        plain = re.sub(r"<[^>]+>", "", current)
        await query.answer(text=plain, show_alert=True)

    elif data.startswith("dl|"):
        quality = data.split("|")[1]
        url = context.user_data.get("pending_url")

        if not url:
            await query.answer("❌ Download session expired. Abar youtube link pathan.", show_alert=True)
            return

        if quality == "1080" and not is_user_premium(user.id):
            await query.answer("⭐ 1080p shudhu Premium user-der jonno! Upgrade korun ba 100 friend invite করুন.", show_alert=True)
            return

        await query.answer()
        await start_download_process(query.message, user, url, quality, context)

# --------------------------------------------------------------------------- #
# YouTube Link Message Handler & Download Process
# --------------------------------------------------------------------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    text = (message.text or "").strip()

    if not YOUTUBE_URL_REGEX.search(text):
        await message.reply_text(
            "🤔 Eti valid YouTube link noy.\n"
            "Doya kore sothik link pathan ba /start type করুন."
        )
        return

    # Check force channel join
    is_joined = await check_force_join(context.bot, user.id)
    if not is_joined:
        await message.reply_text(
            "⚠️ <b>Access Restricted!</b>\n"
            "Download korar age amader channel gulo-te join korte habe.",
            parse_mode=ParseMode.HTML,
            reply_markup=force_join_keyboard(),
        )
        return

    ensure_user(user.id, user.username or user.first_name)

    if not can_user_download(user.id):
        await message.reply_text(
            "🚫 <b>Daily Limit Reached</b>\n\n"
            f"Apni apnar daily <b>{MAX_DAILY_DOWNLOADS_FREE} free downloads</b> sesh korechhen.\n"
            "⏰ Download limit 24h por reset hobe.\n\n"
            f"💡 Lifetime Premium-er jonno apnar referral link diye 100 friend-ke invite করুন!\n"
            f"🤖 Amader onno bot-o try korte parben: {OTHER_BOT_USERNAME}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"🤖 Try Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL)]]
            )
        )
        return

    url_match = YOUTUBE_URL_REGEX.search(text)
    url = url_match.group(0)

    context.user_data["pending_url"] = url

    await message.reply_text(
        "🎬 <b>YouTube Link Detected!</b>\n\n"
        "Doya kore apnar pochhonder quality / format select করুন:",
        parse_mode=ParseMode.HTML,
        reply_markup=quality_selection_keyboard(url),
    )

async def start_download_process(status_msg, user, url: str, quality: str, context: ContextTypes.DEFAULT_TYPE):
    await set_status(status_msg, status_msg.chat_id, "⏳ <b>Processing video details...</b>")

    output_path = None
    try:
        async with download_semaphore:
            try:
                info = await asyncio.wait_for(extract_info(url), timeout=45)
            except asyncio.TimeoutError:
                await set_status(status_msg, status_msg.chat_id, "⏱️ Timed out fetching video info.")
                return
            except Exception as exc:
                await set_status(status_msg, status_msg.chat_id, "❌ Video info extract kora jayni.")
                return

            if info.get("is_live"):
                await set_status(status_msg, status_msg.chat_id, "🔴 Live streams support kore na.")
                return

            duration = info.get("duration") or 0
            if duration > MAX_DURATION_SECONDS:
                await set_status(status_msg, status_msg.chat_id, f"📏 Video size max {MAX_DURATION_SECONDS // 60} min-er besi.")
                return

            # Format selection
            is_audio = False
            if quality == "mp3":
                is_audio = True
                format_spec = "bestaudio/best"
            elif quality == "360":
                format_spec = "bestvideo[height<=360]+bestaudio/best[height<=360]/best"
            elif quality == "480":
                format_spec = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
            elif quality == "720":
                format_spec = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
            elif quality == "1080":
                format_spec = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            else:
                format_spec = "best"

            safe_name = f"{user.id}_{int(time.time())}"
            output_template = os.path.join(DOWNLOAD_DIR, f"{safe_name}.%(ext)s")

            await set_status(status_msg, status_msg.chat_id, f"📥 <b>Download shuru hocche ({quality})...</b>")

            try:
                output_path = await asyncio.wait_for(
                    download_media(url, output_template, format_spec, is_audio, status_msg, status_msg.chat_id, context.bot),
                    timeout=600,
                )
            except asyncio.TimeoutError:
                await set_status(status_msg, status_msg.chat_id, "⏱️ Download timed out.")
                return
            except Exception as exc:
                logger.error("Download error: %s", exc)
                await set_status(status_msg, status_msg.chat_id, "❌ Download failed.")
                return

            if not output_path or not os.path.exists(output_path):
                await set_status(status_msg, status_msg.chat_id, "❌ File creation failed.")
                return

            actual_size = os.path.getsize(output_path)
            if actual_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                await set_status(status_msg, status_msg.chat_id, f"📦 File limit exceed korechhe ({MAX_FILE_SIZE_MB}MB).")
                return

            await set_status(status_msg, status_msg.chat_id, "📤 <b>Telegram-e upload hocche...</b>")
            title = info.get("title", "Downloaded Content")

            caption_text = (
                f"🎬 <b>{title}</b> ({quality}p)\n\n"
                f"🛠 via Bot by {BOT_DEV}\n"
                f"🤖 Try our other bot: {OTHER_BOT_USERNAME}"
            )
            if is_audio:
                caption_text = (
                    f"🎵 <b>{title}</b>\n\n"
                    f"🛠 via Bot by {BOT_DEV}\n"
                    f"🤖 Try our other bot: {OTHER_BOT_USERNAME}"
                )

            with open(output_path, "rb") as media_file:
                if is_audio:
                    await context.bot.send_audio(
                        chat_id=status_msg.chat_id,
                        audio=media_file,
                        title=title,
                        caption=caption_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton(f"🤖 Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL)]]
                        )
                    )
                else:
                    await context.bot.send_video(
                        chat_id=status_msg.chat_id,
                        video=media_file,
                        caption=caption_text,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton(f"🤖 Other Bot ({OTHER_BOT_USERNAME})", url=OTHER_BOT_URL)]]
                        )
                    )

            record_download(user.id, url)
            await set_status(status_msg, status_msg.chat_id, "✅ <b>Upload complete!</b>")

    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        await set_status(status_msg, status_msg.chat_id, "⚠️ An unexpected error occurred.")
    finally:
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        active_status_text.pop((status_msg.chat_id, status_msg.message_id), None)
        gc.collect()

# --------------------------------------------------------------------------- #
# Entry Point
# --------------------------------------------------------------------------- #

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing!")

    init_db()

    # Flask background keep-alive server
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    # User Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("redeem", redeem_command))

    # Admin Commands
    application.add_handler(CommandHandler("add_premium", add_premium_command))
    application.add_handler(CommandHandler("remove_premium", remove_premium_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("post", post_command))

    # Handlers
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started successfully by %s...", BOT_DEV)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
