"""
Telegram YouTube Downloader Bot
================================
Dev: Anas

Production-grade YouTube downloader bot built for deployment on Railway.app's
free tier (512MB RAM). Uses python-telegram-bot v20+ (async) and yt-dlp.

Key design choices for Railway free tier survival:
    - Single-download concurrency (asyncio.Semaphore) to avoid RAM spikes.
    - Hard-capped resolution (720p) and file size to avoid disk/RAM blowups.
    - Downloaded files are ALWAYS removed in a `finally` block, even on error.
    - Explicit `gc.collect()` after every upload cycle.
    - SQLite (file-based, zero extra services) for quota + admin tracking.
"""

import os
import re
import gc
import time
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone

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
# Configuration
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CODE = os.environ.get("ADMIN_CODE", "1169")
BOT_DEV = "Anas"
SUPPORT_URL = os.environ.get("SUPPORT_URL", "https://t.me/DevAnas")

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")

MAX_DAILY_DOWNLOADS = 2                 # standard users
MAX_FILE_SIZE_MB = 50                   # Telegram Bot API hard cap for uploads
MAX_DURATION_SECONDS = 30 * 60          # 30 minutes safety cap
MAX_RESOLUTION_FORMAT = (
    "bestvideo[height<=720]+bestaudio/best[height<=720]/best[height<=720]"
)
MAX_CONCURRENT_DOWNLOADS = 1            # keep RAM usage predictable on free tier

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

# in-memory map of currently displayed status text, used by the
# "Live Status" inline button so it can answer with a fresh popup.
# key: (chat_id, message_id) -> str
active_status_text = {}


# --------------------------------------------------------------------------- #
# Database layer
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


def ensure_user(user_id: int, username: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users (user_id, username, is_admin, joined_at) VALUES (?, ?, 0, ?)",
            (user_id, username, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    conn.close()


def is_user_admin(user_id: int) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row["is_admin"] == 1)


def grant_admin(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


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
    if is_user_admin(user_id):
        return True
    return get_downloads_last_24h(user_id) < MAX_DAILY_DOWNLOADS


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #

def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Live Status", callback_data="live_status"),
                InlineKeyboardButton("💬 Support", url=SUPPORT_URL),
            ]
        ]
    )


def limit_reached_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 How to Get Unlimited Access", callback_data="how_to_redeem")],
            [InlineKeyboardButton("💬 Contact Support", url=SUPPORT_URL)],
        ]
    )


async def set_status(message, chat_id: int, text: str, keyboard=None):
    """Edit the status message and remember the text for the Live Status button."""
    try:
        await message.edit_text(
            text, reply_markup=keyboard or status_keyboard(), parse_mode=ParseMode.HTML
        )
        active_status_text[(chat_id, message.message_id)] = text
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to edit status message: %s", exc)


# --------------------------------------------------------------------------- #
# yt-dlp helpers (run in a background thread so we never block the event loop)
# --------------------------------------------------------------------------- #

def _extract_info_sync(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_sync(url: str, output_template: str) -> str:
    opts = {
        "format": MAX_RESOLUTION_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 1,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


async def extract_info(url: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_info_sync, url)


async def download_video(url: str, output_template: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_sync, url, output_template)


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    text = (
        "👋 <b>Welcome to YT Downloader Bot!</b>\n\n"
        "Send me any YouTube link and I'll fetch the video for you "
        "(capped at <b>720p</b> to keep things fast and stable).\n\n"
        "📊 <b>Rules:</b>\n"
        f"• Free users: <b>{MAX_DAILY_DOWNLOADS} downloads / 24 hours</b>\n"
        f"• Max video length: <b>{MAX_DURATION_SECONDS // 60} minutes</b>\n"
        f"• Max file size: <b>{MAX_FILE_SIZE_MB}MB</b>\n"
        "• Live streams / private videos are not supported\n\n"
        "🔑 Have an access code? Use:\n"
        "<code>/redeem &lt;code&gt;</code>\n\n"
        "Just paste a YouTube link below to get started! 🎬\n\n"
        f"🛠 <i>Bot developed by {BOT_DEV}</i>"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("💬 Support", url=SUPPORT_URL)]]
        ),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if is_user_admin(user.id):
        text = "👑 <b>You have unlimited (Admin/VIP) access.</b>"
    else:
        used = get_downloads_last_24h(user.id)
        remaining = max(0, MAX_DAILY_DOWNLOADS - used)
        text = (
            f"📊 <b>Your Quota</b>\n"
            f"Used today: <b>{used}/{MAX_DAILY_DOWNLOADS}</b>\n"
            f"Remaining: <b>{remaining}</b>\n\n"
            "Want unlimited downloads? Use <code>/redeem &lt;code&gt;</code>"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
        if is_user_admin(user.id):
            await update.message.reply_text("✅ You already have unlimited access!")
            return
        grant_admin(user.id)
        await update.message.reply_text(
            "🎉 <b>Access Granted!</b>\n"
            "You now have <b>unlimited</b> downloads. Enjoy! 👑",
            parse_mode=ParseMode.HTML,
        )
        logger.info("User %s redeemed admin access.", user.id)
    else:
        await update.message.reply_text("❌ Invalid code. Please check and try again.")


# --------------------------------------------------------------------------- #
# Callback query (inline button) handler
# --------------------------------------------------------------------------- #

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    if query.data == "live_status":
        current = active_status_text.get(
            (chat_id, message_id), "⏳ No active task right now."
        )
        # strip simple HTML tags for the alert popup (alerts render plain text only)
        plain = re.sub(r"<[^>]+>", "", current)
        await query.answer(text=plain, show_alert=True)

    elif query.data == "how_to_redeem":
        await query.answer(
            text="Use the command: /redeem <code>  — ask an admin for your access code.",
            show_alert=True,
        )
    else:
        await query.answer()


# --------------------------------------------------------------------------- #
# Core: handle a YouTube link sent as plain text
# --------------------------------------------------------------------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    text = (message.text or "").strip()

    if not YOUTUBE_URL_REGEX.search(text):
        await message.reply_text(
            "🤔 That doesn't look like a YouTube link.\n"
            "Send a valid link (youtube.com or youtu.be), or use /start for help."
        )
        return

    ensure_user(user.id, user.username or user.first_name)

    # --- Quota check --------------------------------------------------- #
    if not can_user_download(user.id):
        await message.reply_text(
            "🚫 <b>Daily Limit Reached</b>\n\n"
            f"You've used your <b>{MAX_DAILY_DOWNLOADS} free downloads</b> for today.\n"
            "⏰ Your quota resets 24 hours after each download.\n\n"
            "Want unlimited access? Enter your access code with "
            "<code>/redeem &lt;code&gt;</code> 🔑",
            parse_mode=ParseMode.HTML,
            reply_markup=limit_reached_keyboard(),
        )
        return

    url_match = YOUTUBE_URL_REGEX.search(text)
    url = url_match.group(0)

    status_msg = await message.reply_text(
        "⏳ <b>Processing...</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=status_keyboard(),
    )
    active_status_text[(status_msg.chat_id, status_msg.message_id)] = "⏳ Processing..."

    output_path = None
    try:
        async with download_semaphore:
            # ---- Validate video before downloading ---- #
            try:
                info = await asyncio.wait_for(extract_info(url), timeout=30)
            except asyncio.TimeoutError:
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "⏱️ <b>Timed out</b> while fetching video info. Please try again.",
                )
                return
            except yt_dlp.utils.DownloadError as exc:
                logger.info("Info extraction failed: %s", exc)
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "❌ <b>Couldn't access this video.</b>\n"
                    "It may be private, age-restricted, or unavailable.",
                )
                return

            if info.get("is_live"):
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "🔴 <b>Live streams are not supported.</b>",
                )
                return

            duration = info.get("duration") or 0
            if duration and duration > MAX_DURATION_SECONDS:
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "📏 <b>Video too long.</b>\n"
                    f"Max allowed duration is {MAX_DURATION_SECONDS // 60} minutes.",
                )
                return

            approx_size = info.get("filesize") or info.get("filesize_approx")
            if approx_size and approx_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "📦 <b>File too large.</b>\n"
                    f"This video exceeds the {MAX_FILE_SIZE_MB}MB upload limit at 720p.",
                )
                return

            # ---- Download stage ---- #
            await set_status(
                status_msg, status_msg.chat_id, "📥 <b>Downloading (720p)...</b>"
            )

            safe_name = f"{user.id}_{int(time.time())}"
            output_template = os.path.join(DOWNLOAD_DIR, f"{safe_name}.%(ext)s")

            try:
                output_path = await asyncio.wait_for(
                    download_video(url, output_template), timeout=600
                )
            except asyncio.TimeoutError:
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "⏱️ <b>Download timed out.</b> Please try again later.",
                )
                return
            except yt_dlp.utils.DownloadError as exc:
                logger.info("Download failed: %s", exc)
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "❌ <b>Download failed.</b>\n"
                    "The video might be restricted or temporarily unavailable.",
                )
                return

            if not output_path or not os.path.exists(output_path):
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "❌ <b>Something went wrong.</b> File was not created.",
                )
                return

            # Hard safety check on actual file size post-download.
            actual_size = os.path.getsize(output_path)
            if actual_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                await set_status(
                    status_msg,
                    status_msg.chat_id,
                    "📦 <b>File too large to send.</b>\n"
                    f"Exceeds the {MAX_FILE_SIZE_MB}MB limit.",
                )
                return

            # ---- Upload stage ---- #
            await set_status(status_msg, status_msg.chat_id, "📤 <b>Uploading...</b>")

            title = info.get("title", "video")
            with open(output_path, "rb") as video_file:
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    video=video_file,
                    caption=f"🎬 <b>{title}</b>\n\n🛠 via YT Downloader Bot by {BOT_DEV}",
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                )

            record_download(user.id, url)

            remaining = (
                "Unlimited (Admin)"
                if is_user_admin(user.id)
                else str(max(0, MAX_DAILY_DOWNLOADS - get_downloads_last_24h(user.id)))
            )
            await set_status(
                status_msg,
                status_msg.chat_id,
                f"✅ <b>Done!</b> Remaining downloads today: <b>{remaining}</b>",
            )

    except Exception as exc:  # noqa: BLE001 - final safety net
        logger.exception("Unexpected error handling %s: %s", url, exc)
        try:
            await set_status(
                status_msg,
                status_msg.chat_id,
                "⚠️ <b>Unexpected error occurred.</b> Please try again later.",
            )
        except Exception:  # noqa: BLE001
            pass

    finally:
        # ---- Cleanup stage: ALWAYS runs, even on failure ---- #
        try:
            if output_path and os.path.exists(output_path):
                os.remove(output_path)
                logger.info("Removed temp file: %s", output_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to remove temp file %s: %s", output_path, exc)

        active_status_text.pop((status_msg.chat_id, status_msg.message_id), None)
        gc.collect()


# --------------------------------------------------------------------------- #
# Global error handler
# --------------------------------------------------------------------------- #

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error: %s", update, context.error)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Set it in Railway's Variables tab before deploying."
        )

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("redeem", redeem_command))
    application.add_handler(CommandHandler("admin", redeem_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_error_handler(error_handler)

    logger.info("Bot starting (dev: %s)...", BOT_DEV)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
