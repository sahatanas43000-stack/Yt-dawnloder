"""
Telegram YouTube Downloader Bot
================================
Dev: Anas | Contact: @Devsahatanas

Features:
  - Multiple quality: 360p, 480p, 720p, 1080p (Premium)
  - MP3 audio extraction
  - Live download progress (MB + %)
  - Force channel join (2 channels)
  - Referral system (100 referrals = lifetime premium)
  - Daily quota (2/day free users)
  - Premium membership with expiry
  - Admin broadcast (text + media)
  - /add_premium, /remove_premium, /stats, /broadcast, /post
  - Bug fixes: status_msg guard, single URL match, clean memory
  - RAM-safe: Semaphore + 80MB cap + gc.collect()
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
    ChatMember,
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

# =========================================================================== #
#  CONFIGURATION  —  edit these or set as env vars
# =========================================================================== #

BOT_TOKEN     = os.environ.get("BOT_TOKEN")
ADMIN_ID      = int(os.environ.get("ADMIN_ID", "0"))   # your numeric Telegram ID
BOT_DEV       = "Anas"
SUPPORT_URL   = "https://t.me/Devsahatanas"

CHANNEL_1     = "@sahatanas"
CHANNEL_2     = "@sahatanass"
CHANNEL_1_URL = "https://t.me/sahatanas"
CHANNEL_2_URL = "https://t.me/sahatanass"
OTHER_BOT_URL = "https://t.me/BomssssssssBot"

DB_PATH       = os.environ.get("DB_PATH",      "/tmp/bot_data.db")
DOWNLOAD_DIR  = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
PORT          = int(os.environ.get("PORT", "10000"))

MAX_DAILY_DOWNLOADS   = 2
MAX_FILE_SIZE_MB      = 80        # Render free-tier safe cap
MAX_DURATION_SECONDS  = 30 * 60  # 30 min
MAX_CONCURRENT_DOWNLOADS = 1
REFERRALS_FOR_PREMIUM = 100       # invite 100 → lifetime premium

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ytbot")

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
active_status_text: dict[tuple, str] = {}

# =========================================================================== #
#  yt-dlp FORMAT STRINGS
# =========================================================================== #

FORMATS = {
    "360":   "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]/best",
    "480":   "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best",
    "720":   "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
    "1080":  "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
    "audio": "bestaudio[ext=m4a]/bestaudio",
}

YTDLP_BASE_OPTS = {
    "quiet":           True,
    "no_warnings":     True,
    "noplaylist":      True,
    "socket_timeout":  30,
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
    "retries":          5,
    "fragment_retries": 5,
    "ignoreerrors":     False,
}

YOUTUBE_URL_REGEX = re.compile(
    r"(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be)/\S+", re.IGNORECASE
)

# =========================================================================== #
#  DATABASE
# =========================================================================== #

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur  = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            is_premium  INTEGER NOT NULL DEFAULT 0,
            premium_until TEXT,
            referred_by INTEGER,
            joined_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL,
            joined_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            url           TEXT,
            quality       TEXT,
            downloaded_at TEXT NOT NULL
        );
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_PATH)


# ----------- user helpers --------------------------------------------------

def ensure_user(user_id: int, username: str, referred_by: int | None = None):
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            """INSERT INTO users
               (user_id, username, is_premium, premium_until, referred_by, joined_at)
               VALUES (?, ?, 0, NULL, ?, ?)""",
            (user_id, username, referred_by,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        # credit referrer
        if referred_by and referred_by != user_id:
            cur.execute(
                """INSERT INTO referrals (referrer_id, referred_id, joined_at)
                   VALUES (?, ?, ?)""",
                (referred_by, user_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            _maybe_grant_referral_premium(cur, conn, referred_by)

    conn.close()


def _maybe_grant_referral_premium(cur, conn, referrer_id: int):
    cur.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?",
        (referrer_id,),
    )
    row = cur.fetchone()
    if row and row["c"] >= REFERRALS_FOR_PREMIUM:
        cur.execute(
            "UPDATE users SET is_premium = 1, premium_until = NULL WHERE user_id = ?",
            (referrer_id,),
        )
        conn.commit()
        logger.info("User %s earned lifetime premium via referrals.", referrer_id)


def get_user(user_id: int) -> sqlite3.Row | None:
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row  = cur.fetchone()
    conn.close()
    return row


def is_premium(user_id: int) -> bool:
    """True for bot admin OR users with active premium."""
    if user_id == ADMIN_ID:
        return True
    row = get_user(user_id)
    if not row or not row["is_premium"]:
        return False
    # check expiry
    if row["premium_until"] is None:
        return True   # lifetime
    expiry = datetime.fromisoformat(row["premium_until"])
    return datetime.now(timezone.utc) < expiry


def set_premium(user_id: int, until: datetime | None):
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
        (until.isoformat() if until else None, user_id),
    )
    conn.commit()
    conn.close()


def remove_premium(user_id: int):
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE users SET is_premium = 0, premium_until = NULL WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_all_user_ids() -> list[int]:
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    ids  = [r["user_id"] for r in cur.fetchall()]
    conn.close()
    return ids


def get_referral_count(user_id: int) -> int:
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row["c"] if row else 0


# ----------- download quota ------------------------------------------------

def get_downloads_last_24h(user_id: int) -> int:
    conn  = db_connect()
    cur   = conn.cursor()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cur.execute(
        "SELECT COUNT(*) AS c FROM downloads WHERE user_id = ? AND downloaded_at >= ?",
        (user_id, since),
    )
    row = cur.fetchone()
    conn.close()
    return row["c"] if row else 0


def record_download(user_id: int, url: str, quality: str):
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO downloads (user_id, url, quality, downloaded_at) VALUES (?, ?, ?, ?)",
        (user_id, url, quality, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def can_user_download(user_id: int) -> bool:
    if is_premium(user_id):
        return True
    return get_downloads_last_24h(user_id) < MAX_DAILY_DOWNLOADS


def get_total_stats() -> dict:
    conn = db_connect()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_premium = 1")
    premium_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM downloads")
    dl_total = cur.fetchone()["c"]
    conn.close()
    return {"total": total, "premium": premium_count, "downloads": dl_total}


# =========================================================================== #
#  CHANNEL JOIN CHECK
# =========================================================================== #

async def check_channel_membership(bot, user_id: int) -> bool:
    """Returns True if user has joined both channels."""
    for channel in (CHANNEL_1, CHANNEL_2):
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in (
                ChatMember.LEFT,
                ChatMember.BANNED,
                "kicked",
            ):
                return False
        except Exception:
            return False
    return True


def join_channels_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Channel 1", url=CHANNEL_1_URL),
            InlineKeyboardButton("📢 Channel 2", url=CHANNEL_2_URL),
        ],
        [InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="check_join")],
    ])


# =========================================================================== #
#  UI HELPERS
# =========================================================================== #

def quality_keyboard(is_prem: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📱 360p",  callback_data="q_360"),
            InlineKeyboardButton("📺 480p",  callback_data="q_480"),
        ],
        [
            InlineKeyboardButton("🖥 720p HD",  callback_data="q_720"),
            InlineKeyboardButton(
                "🔥 1080p FHD ⭐" if is_prem else "🔒 1080p FHD (Premium)",
                callback_data="q_1080" if is_prem else "premium_required",
            ),
        ],
        [InlineKeyboardButton("🎵 MP3 Audio", callback_data="q_audio")],
        [InlineKeyboardButton("❌ Cancel",     callback_data="cancel_download")],
    ]
    return InlineKeyboardMarkup(rows)


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Live Status", callback_data="live_status"),
        InlineKeyboardButton("💬 Support",      url=SUPPORT_URL),
    ]])


def limit_reached_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Get Premium via Referral", callback_data="referral_info")],
        [InlineKeyboardButton("💬 Contact Support",          url=SUPPORT_URL)],
    ])


async def set_status(message, chat_id: int, text: str, keyboard=None):
    try:
        await message.edit_text(
            text,
            reply_markup=keyboard or status_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        active_status_text[(chat_id, message.message_id)] = text
    except Exception as exc:
        logger.warning("set_status failed: %s", exc)


# =========================================================================== #
#  yt-dlp HELPERS  (run in thread pool so event loop never blocks)
# =========================================================================== #

def _extract_info_sync(url: str) -> dict:
    opts = {**YTDLP_BASE_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_sync(
    url: str,
    output_template: str,
    fmt: str,
    progress_callback,   # callable(downloaded_mb, total_mb, pct)
    is_audio: bool = False,
) -> str:

    def _hook(d):
        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes", 0) or 0
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                pct  = downloaded / total * 100
                dl_mb = downloaded / 1_048_576
                tot_mb = total / 1_048_576
                progress_callback(dl_mb, tot_mb, pct)

    opts = {
        **YTDLP_BASE_OPTS,
        "format":              fmt,
        "outtmpl":             output_template,
        "restrictfilenames":   True,
        "max_filesize":        MAX_FILE_SIZE_MB * 1_048_576,
        "concurrent_fragment_downloads": 1,
        "progress_hooks":      [_hook],
    }

    if is_audio:
        opts["postprocessors"] = [{
            "key":           "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{
            "key":            "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info     = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if os.path.exists(filename):
            return filename
        base = os.path.splitext(filename)[0]
        for ext in ("mp4", "mp3", "mkv", "webm", "m4a"):
            cand = f"{base}.{ext}"
            if os.path.exists(cand):
                return cand
        return filename


async def extract_info(url: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_info_sync, url)


async def download_video(
    url: str,
    output_template: str,
    fmt: str,
    progress_callback,
    is_audio: bool = False,
) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _download_sync, url, output_template, fmt, progress_callback, is_audio
    )


# =========================================================================== #
#  CORE DOWNLOAD FLOW
# =========================================================================== #

async def do_download(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    quality: str,
    user_id: int,
    chat_id: int,
    reply_to_message_id: int,
):
    """Shared download engine called from callback or message handler."""
    is_audio = quality == "audio"
    fmt      = FORMATS[quality]
    prem     = is_premium(user_id)

    # quota check
    if not can_user_download(user_id):
        ref_count = get_referral_count(user_id)
        needed    = REFERRALS_FOR_PREMIUM - ref_count
        text = (
            "🚫 <b>Daily Limit Reached</b>\n\n"
            f"You've used your <b>{MAX_DAILY_DOWNLOADS} free downloads</b> for today.\n"
            "⏰ Quota resets 24 hours after your first download.\n\n"
            f"👥 <b>Referral Progress:</b> {ref_count}/{REFERRALS_FOR_PREMIUM} "
            f"({needed} more needed for lifetime premium)\n\n"
            "Share your referral link: /referral"
        )
        await context.bot.send_message(
            chat_id, text,
            parse_mode=ParseMode.HTML,
            reply_markup=limit_reached_keyboard(),
            reply_to_message_id=reply_to_message_id,
        )
        return

    # send initial status message
    status_msg = await context.bot.send_message(
        chat_id,
        "⏳ <b>Processing your request...</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=status_keyboard(),
        reply_to_message_id=reply_to_message_id,
    )
    active_status_text[(chat_id, status_msg.message_id)] = "⏳ Processing..."

    output_path = None
    last_update_time = [0.0]   # mutable container for closure

    def progress_cb(dl_mb: float, tot_mb: float, pct: float):
        now = time.time()
        if now - last_update_time[0] < 3:   # throttle to 1 update / 3 s
            return
        last_update_time[0] = now
        bar_filled = int(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        text = (
            f"📥 <b>Downloading {'Audio' if is_audio else quality + 'p'}...</b>\n\n"
            f"<code>[{bar}] {pct:.1f}%</code>\n"
            f"📦 {dl_mb:.1f} MB / {tot_mb:.1f} MB"
        )
        asyncio.run_coroutine_threadsafe(
            set_status(status_msg, chat_id, text),
            asyncio.get_event_loop(),
        )

    try:
        async with download_semaphore:
            # ---------- validate ----------
            await set_status(status_msg, chat_id, "🔍 <b>Fetching video info...</b>")
            try:
                info = await asyncio.wait_for(extract_info(url), timeout=45)
            except asyncio.TimeoutError:
                await set_status(status_msg, chat_id,
                                 "⏱️ <b>Timed out fetching video info.</b> Try again.")
                return
            except yt_dlp.utils.DownloadError as exc:
                logger.info("Info extraction failed: %s", exc)
                await set_status(status_msg, chat_id,
                                 "❌ <b>Cannot access this video.</b>\n"
                                 "It may be private, age-restricted, or unavailable.")
                return

            if info.get("is_live"):
                await set_status(status_msg, chat_id,
                                 "🔴 <b>Live streams are not supported.</b>")
                return

            duration = info.get("duration") or 0
            if duration and duration > MAX_DURATION_SECONDS:
                await set_status(status_msg, chat_id,
                                 f"📏 <b>Video too long.</b>\n"
                                 f"Max: {MAX_DURATION_SECONDS // 60} minutes.")
                return

            approx_size = info.get("filesize") or info.get("filesize_approx")
            if approx_size and approx_size > MAX_FILE_SIZE_MB * 1_048_576:
                await set_status(status_msg, chat_id,
                                 f"📦 <b>File too large.</b>\n"
                                 f"Exceeds {MAX_FILE_SIZE_MB} MB limit.")
                return

            # ---------- download ----------
            await set_status(status_msg, chat_id,
                             f"📥 <b>Starting download ({'Audio' if is_audio else quality + 'p'})...</b>")

            safe_name       = f"{user_id}_{int(time.time())}"
            output_template = os.path.join(DOWNLOAD_DIR, f"{safe_name}.%(ext)s")

            try:
                output_path = await asyncio.wait_for(
                    download_video(url, output_template, fmt, progress_cb, is_audio),
                    timeout=600,
                )
            except asyncio.TimeoutError:
                await set_status(status_msg, chat_id,
                                 "⏱️ <b>Download timed out.</b> Please try again later.")
                return
            except yt_dlp.utils.DownloadError as exc:
                logger.info("Download failed: %s", exc)
                await set_status(status_msg, chat_id,
                                 "❌ <b>Download failed.</b>\n"
                                 "The video might be restricted or temporarily unavailable.")
                return

            if not output_path or not os.path.exists(output_path):
                await set_status(status_msg, chat_id,
                                 "❌ <b>File not created.</b> Something went wrong.")
                return

            actual_size = os.path.getsize(output_path)
            if actual_size > MAX_FILE_SIZE_MB * 1_048_576:
                await set_status(status_msg, chat_id,
                                 f"📦 <b>File too large to send ({actual_size/1_048_576:.1f} MB).</b>\n"
                                 f"Limit is {MAX_FILE_SIZE_MB} MB.")
                return

            # ---------- upload ----------
            await set_status(status_msg, chat_id, "📤 <b>Uploading to Telegram...</b>")
            title   = info.get("title", "video")
            caption = (
                f"🎬 <b>{title}</b>\n"
                f"📊 Quality: <b>{'MP3 Audio' if is_audio else quality + 'p'}</b>\n\n"
                f"🤖 @{(await context.bot.get_me()).username} | Dev: {BOT_DEV}"
            )

            with open(output_path, "rb") as f:
                if is_audio:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60,
                    )
                else:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60,
                    )

            record_download(user_id, url, quality)

            # ---------- done ----------
            remaining = (
                "∞ Unlimited (Premium ⭐)" if prem
                else str(max(0, MAX_DAILY_DOWNLOADS - get_downloads_last_24h(user_id)))
            )
            await set_status(
                status_msg, chat_id,
                f"✅ <b>Done!</b>\n"
                f"📊 Downloads remaining today: <b>{remaining}</b>\n\n"
                f"👥 Invite friends & earn premium: /referral",
            )

    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        try:
            await set_status(status_msg, chat_id,
                             "⚠️ <b>Unexpected error.</b> Please try again later.")
        except Exception:
            pass

    finally:
        try:
            if output_path and os.path.exists(output_path):
                os.remove(output_path)
                logger.info("Cleaned up: %s", output_path)
        except Exception as exc:
            logger.warning("Cleanup failed: %s", exc)

        if status_msg:
            active_status_text.pop((chat_id, status_msg.message_id), None)
        gc.collect()


# =========================================================================== #
#  COMMAND HANDLERS
# =========================================================================== #

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # referral: /start ref_<user_id>
    referred_by = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referred_by = int(arg[4:])
            except ValueError:
                pass

    ensure_user(user.id, user.username or user.first_name, referred_by)

    prem = is_premium(user.id)
    star = "⭐ Premium" if prem else "🆓 Free"

    text = (
        f"👋 <b>Welcome to YT Downloader Bot!</b>\n\n"
        f"🏷 Your plan: <b>{star}</b>\n\n"
        "Send any <b>YouTube link</b> and choose your quality:\n"
        "📱 360p | 📺 480p | 🖥 720p | 🔥 1080p (Premium) | 🎵 MP3\n\n"
        "📊 <b>Free limits:</b>\n"
        f"• {MAX_DAILY_DOWNLOADS} downloads / 24 hours\n"
        f"• Max duration: {MAX_DURATION_SECONDS // 60} minutes\n"
        f"• Max file size: {MAX_FILE_SIZE_MB} MB\n\n"
        "👥 Invite friends & earn <b>Lifetime Premium</b>: /referral\n"
        "📈 Check your stats: /status\n\n"
        f"🛠 Dev: {BOT_DEV} | {SUPPORT_URL}"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 Channel 1", url=CHANNEL_1_URL),
                InlineKeyboardButton("📢 Channel 2", url=CHANNEL_2_URL),
            ],
            [InlineKeyboardButton("🤖 Try Other Bot", url=OTHER_BOT_URL)],
            [InlineKeyboardButton("💬 Support",       url=SUPPORT_URL)],
        ]),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>How to use:</b>\n\n"
        "1. Join both channels (required)\n"
        "2. Send a YouTube link\n"
        "3. Pick your quality\n"
        "4. Wait for the download ⚡\n\n"
        "<b>Commands:</b>\n"
        "/start   — Welcome & info\n"
        "/status  — Your quota & plan\n"
        "/referral — Get your invite link\n\n"
        f"💬 Support: {SUPPORT_URL}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    prem      = is_premium(user.id)
    used      = get_downloads_last_24h(user.id)
    remaining = MAX_DAILY_DOWNLOADS - used if not prem else "∞"
    ref_count = get_referral_count(user.id)
    needed    = max(0, REFERRALS_FOR_PREMIUM - ref_count)

    row = get_user(user.id)
    if prem and row and row["premium_until"]:
        expiry_str = row["premium_until"][:10]
        plan = f"⭐ Premium (expires {expiry_str})"
    elif prem:
        plan = "⭐ Premium (Lifetime)"
    else:
        plan = "🆓 Free"

    text = (
        f"📊 <b>Your Status</b>\n\n"
        f"👤 Plan: <b>{plan}</b>\n"
        f"📥 Used today: <b>{used}/{MAX_DAILY_DOWNLOADS if not prem else '∞'}</b>\n"
        f"⏳ Remaining: <b>{remaining}</b>\n\n"
        f"👥 Referrals: <b>{ref_count}/{REFERRALS_FOR_PREMIUM}</b>\n"
        f"{'🎉 You have lifetime premium!' if prem else f'Need {needed} more to unlock lifetime premium!'}\n\n"
        "Get your invite link: /referral"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    bot_info  = await context.bot.get_me()
    ref_link  = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
    ref_count = get_referral_count(user.id)
    needed    = max(0, REFERRALS_FOR_PREMIUM - ref_count)

    text = (
        "👥 <b>Referral Program</b>\n\n"
        f"🔗 Your invite link:\n<code>{ref_link}</code>\n\n"
        f"📊 Progress: <b>{ref_count}/{REFERRALS_FOR_PREMIUM}</b> referrals\n"
        f"{'🎉 You already have lifetime premium!' if needed == 0 else f'Invite {needed} more friends to get Lifetime Premium!'}\n\n"
        "✅ Both conditions:\n"
        f"• Your friend must start the bot via your link\n"
        f"• They must join both channels ({CHANNEL_1} & {CHANNEL_2})\n\n"
        "🎁 Reward: <b>Lifetime Premium</b> (1080p + unlimited downloads)"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Share Invite Link", switch_inline_query=ref_link)],
        ]),
    )


# =========================================================================== #
#  ADMIN COMMANDS
# =========================================================================== #

def admin_only(func):
    """Decorator: reject non-admins."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Admin only.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def add_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
      /add_premium <user_id> lifetime
      /add_premium <user_id> 1month
      /add_premium <user_id> 3month
      /add_premium <user_id> 30          ← days
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /add_premium <user_id> <lifetime|1month|3month|Ndays>"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    period = args[1].lower()
    if period == "lifetime":
        until = None
    elif period == "1month":
        until = datetime.now(timezone.utc) + timedelta(days=30)
    elif period == "3month":
        until = datetime.now(timezone.utc) + timedelta(days=90)
    else:
        try:
            days  = int(period)
            until = datetime.now(timezone.utc) + timedelta(days=days)
        except ValueError:
            await update.message.reply_text("❌ Unknown period. Use: lifetime / 1month / 3month / <days>")
            return

    set_premium(target_id, until)
    until_str = until.strftime("%Y-%m-%d") if until else "Lifetime"
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> granted Premium until <b>{until_str}</b>.",
        parse_mode=ParseMode.HTML,
    )
    # notify user
    try:
        await context.bot.send_message(
            target_id,
            f"🎉 <b>Congratulations!</b>\nYou've been granted <b>Premium access</b> until <b>{until_str}</b>!\n"
            "Enjoy 1080p FHD + unlimited downloads. ⭐",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


@admin_only
async def remove_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove_premium <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    remove_premium(target_id)
    await update.message.reply_text(
        f"✅ Removed premium from <code>{target_id}</code>.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_total_stats()
    await update.message.reply_text(
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👤 Total users:   <b>{s['total']}</b>\n"
        f"⭐ Premium users: <b>{s['premium']}</b>\n"
        f"📥 Total downloads: <b>{s['downloads']}</b>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message text>")
        return
    text     = " ".join(context.args)
    user_ids = get_all_user_ids()
    sent = failed = 0
    progress_msg = await update.message.reply_text(
        f"📢 Broadcasting to {len(user_ids)} users..."
    )
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)   # stay under Telegram rate limits
    await progress_msg.edit_text(
        f"✅ Broadcast done.\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


@admin_only
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to a media message with /post to forward it to all users."""
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a media message with /post.")
        return
    src      = update.message.reply_to_message
    user_ids = get_all_user_ids()
    sent = failed = 0
    progress_msg = await update.message.reply_text(
        f"📢 Forwarding media to {len(user_ids)} users..."
    )
    for uid in user_ids:
        try:
            await src.forward(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await progress_msg.edit_text(
        f"✅ Post sent.\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


# =========================================================================== #
#  MESSAGE HANDLER  (YouTube link → quality picker)
# =========================================================================== #

# Temp store for pending downloads: user_id -> {url, info}
pending_downloads: dict[int, dict] = {}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user    = update.effective_user
    text    = (message.text or "").strip()

    url_match = YOUTUBE_URL_REGEX.search(text)
    if not url_match:
        await message.reply_text(
            "🤔 That doesn't look like a YouTube link.\n"
            "Send a youtube.com or youtu.be URL, or type /help."
        )
        return

    url = url_match.group(0)
    ensure_user(user.id, user.username or user.first_name)

    # ---- channel join check ----
    joined = await check_channel_membership(context.bot, user.id)
    if not joined:
        await message.reply_text(
            "🔒 <b>Join Required</b>\n\n"
            "Please join <b>both channels</b> below to use this bot, then tap <b>I've Joined</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=join_channels_keyboard(),
        )
        # store url so we can resume after join check
        pending_downloads[user.id] = {"url": url, "reply_id": message.message_id}
        return

    # ---- show quality picker ----
    prem = is_premium(user.id)
    pending_downloads[user.id] = {"url": url, "reply_id": message.message_id}

    # quick info fetch for title
    try:
        info = await asyncio.wait_for(extract_info(url), timeout=30)
        title = info.get("title", "YouTube Video")[:60]
        duration = info.get("duration") or 0
        dur_str  = f"{duration//60}:{duration%60:02d}" if duration else "?"
    except Exception:
        title   = "YouTube Video"
        dur_str = "?"
        info    = {}

    await message.reply_text(
        f"🎬 <b>{title}</b>\n"
        f"⏱ Duration: <b>{dur_str}</b>\n\n"
        "Choose your download quality:",
        parse_mode=ParseMode.HTML,
        reply_markup=quality_keyboard(prem),
    )


# =========================================================================== #
#  CALLBACK QUERY HANDLER
# =========================================================================== #

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user    = query.from_user
    data    = query.data
    chat_id = query.message.chat_id
    msg_id  = query.message.message_id

    # ---- live status popup ----
    if data == "live_status":
        current = active_status_text.get((chat_id, msg_id), "⏳ No active task.")
        plain   = re.sub(r"<[^>]+>", "", current)
        await query.answer(text=plain[:200], show_alert=True)
        return

    # ---- channel join re-check ----
    if data == "check_join":
        joined = await check_channel_membership(context.bot, user.id)
        if not joined:
            await query.answer(
                "❌ You haven't joined both channels yet!", show_alert=True
            )
            return
        await query.answer("✅ Great! You're all set.", show_alert=True)
        # resume pending download if any
        pending = pending_downloads.get(user.id)
        if pending:
            prem = is_premium(user.id)
            await query.message.edit_text(
                "✅ Joined! Now choose your quality:",
                reply_markup=quality_keyboard(prem),
            )
        return

    # ---- premium required ----
    if data == "premium_required":
        await query.answer(
            "🔒 1080p is for Premium users.\n"
            f"Invite {REFERRALS_FOR_PREMIUM} friends or contact admin for Premium.",
            show_alert=True,
        )
        return

    # ---- referral info ----
    if data == "referral_info":
        await query.answer("Use /referral to get your invite link!", show_alert=True)
        return

    # ---- cancel ----
    if data == "cancel_download":
        pending_downloads.pop(user.id, None)
        await query.message.edit_text("❌ Download cancelled.")
        return

    # ---- quality selected ----
    if data.startswith("q_"):
        quality = data[2:]   # "360" / "480" / "720" / "1080" / "audio"

        pending = pending_downloads.pop(user.id, None)
        if not pending:
            await query.answer("⚠️ No pending download. Send a link first.", show_alert=True)
            return

        url      = pending["url"]
        reply_id = pending.get("reply_id")

        # 1080p: premium only
        if quality == "1080" and not is_premium(user.id):
            await query.answer("🔒 1080p is Premium only.", show_alert=True)
            return

        # update picker to "Starting..."
        try:
            await query.message.edit_text(
                f"⚡ Starting {'audio' if quality == 'audio' else quality + 'p'} download...",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        await query.answer()

        asyncio.create_task(
            do_download(
                update_or_query=query,
                context=context,
                url=url,
                quality=quality,
                user_id=user.id,
                chat_id=chat_id,
                reply_to_message_id=reply_id,
            )
        )
        return

    await query.answer()


# =========================================================================== #
#  GLOBAL ERROR HANDLER
# =========================================================================== #

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error: %s", update, context.error, exc_info=True)


# =========================================================================== #
#  FLASK KEEP-ALIVE  (for Render free tier)
# =========================================================================== #

flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "YT Bot is alive ✅", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)


# =========================================================================== #
#  ENTRYPOINT
# =========================================================================== #

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set. Add it to your environment variables."
        )
    if ADMIN_ID == 0:
        logger.warning("ADMIN_ID is 0 — admin commands will not work properly.")

    init_db()

    # start Flask in background thread (keeps Render service alive)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask keep-alive started on port %s", PORT)

    app = Application.builder().token(BOT_TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start",    start_command))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("status",   status_command))
    app.add_handler(CommandHandler("referral", referral_command))

    # admin commands
    app.add_handler(CommandHandler("add_premium",    add_premium_command))
    app.add_handler(CommandHandler("remove_premium", remove_premium_command))
    app.add_handler(CommandHandler("stats",          stats_command))
    app.add_handler(CommandHandler("broadcast",      broadcast_command))
    app.add_handler(CommandHandler("post",           post_command))

    # callbacks & messages
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot starting — Dev: %s", BOT_DEV)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
