import os
import gc
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
import yt_dlp

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))

CHANNEL_1 = "@sahatanas"
CHANNEL_2 = "@sahatanass"
CHANNEL_1_URL = "https://t.me/sahatanas"
CHANNEL_2_URL = "https://t.me/sahatanass"
OTHER_BOT_URL = "https://t.me/BomssssssssBot"

DB_FILE = "user_data.db"
MAX_FILE_SIZE_MB = 100
REFERRAL_THRESHOLD_FOR_UNLIMITED = 100

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, 
                    referral_count INTEGER DEFAULT 0,
                    premium_until TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS quota (
                    user_id INTEGER, 
                    download_date TEXT, 
                    count INTEGER, 
                    PRIMARY KEY (user_id, download_date)
                )''')
    conn.commit()
    conn.close()

init_db()

def register_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, referral_count) VALUES (?, 0)", (user_id,))
    conn.commit()
    conn.close()

def add_referral(referrer_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?", (referrer_id,))
    conn.commit()
    conn.close()

def is_user_premium(user_id: int) -> tuple[bool, str]:
    if user_id == ADMIN_ID:
        return True, "Admin (Unlimited)"
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT referral_count, premium_until FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return False, "Free User"

    ref_count, prem_until = row[0], row[1]

    if ref_count >= REFERRAL_THRESHOLD_FOR_UNLIMITED:
        return True, "Unlimited (100+ Referrals)"

    if prem_until:
        try:
            exp_date = datetime.strptime(prem_until, "%Y-%m-%d %H:%M:%S")
            if datetime.now() < exp_date:
                days_left = (exp_date - datetime.now()).days + 1
                return True, f"Premium ({days_left} Days Left)"
        except ValueError:
            pass

    return False, "Free User"

def get_user_stats(user_id: int):
    today = str(datetime.now().date())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("SELECT referral_count FROM users WHERE user_id=?", (user_id,))
    u_row = c.fetchone()
    ref_count = u_row[0] if u_row else 0

    c.execute("SELECT count FROM quota WHERE user_id=? AND download_date=?", (user_id, today))
    q_row = c.fetchone()
    today_dl = q_row[0] if q_row else 0
    
    conn.close()
    return today_dl, ref_count

def increment_user_quota(user_id: int):
    today = str(datetime.now().date())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""INSERT INTO quota (user_id, download_date, count) 
                 VALUES (?, ?, 1) 
                 ON CONFLICT(user_id, download_date) 
                 DO UPDATE SET count = count + 1""", (user_id, today))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

async def check_force_join(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    for channel in [CHANNEL_1, CHANNEL_2]:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception as e:
            logger.warning(f"Force join check warning: {e}")
            continue
    return True

def get_force_join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel 1", url=CHANNEL_1_URL)],
        [InlineKeyboardButton("📢 Join Channel 2", url=CHANNEL_2_URL)],
        [InlineKeyboardButton("🚀 Try Other Bot", url=OTHER_BOT_URL)]
    ])

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ Contact Admin", url="https://t.me/Devsahatanas")],
        [InlineKeyboardButton("⚡ Any Video Downloader Bot", url=OTHER_BOT_URL)]
    ])

# ✅ সুন্দর নতুন /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id)

    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                add_referral(referrer_id)
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            f"🎉 *নতুন Referral!*\n\n"
                            f"👤 `{user.first_name}` তোমার link দিয়ে join করেছে!\n"
                            f"✅ তোমার referral count বাড়ছে।"
                        ),
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
        except ValueError:
            pass

    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user.id}"
    today_dl, ref_count = get_user_stats(user.id)
    is_prem, status_str = is_user_premium(user.id)

    # Status badge
    if user.id == ADMIN_ID:
        badge = "👑 Admin"
        status_line = "♾️ Unlimited Access"
    elif is_prem:
        badge = "⭐ Premium"
        status_line = f"✅ {status_str}"
    else:
        badge = "🆓 Free"
        status_line = f"📥 Today: {today_dl}/2 downloads used"

    # Referral progress bar
    filled = int((ref_count / REFERRAL_THRESHOLD_FOR_UNLIMITED) * 10)
    empty = 10 - filled
    progress_bar = "🟩" * filled + "⬜" * empty

    msg = (
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
        f"🎯 _{REFERRAL_THRESHOLD_FOR_UNLIMITED - ref_count} জন আনলে Lifetime Unlimited!_\n\n"
        f"🔗 *তোমার Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📩 *যেকোনো YouTube link পাঠাও!*\n"
        f"⬇️ 360p • 480p • 720p • 1080p • MP3"
    )

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def add_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
        exp_time = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, referral_count) VALUES (?, 0)", (target_id,))
        c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (exp_time, target_id))
        conn.commit()
        conn.close()

        await update.message.reply_text(f"✅ User `{target_id}` set to Premium for {days} days!", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/add_premium <user_id> <days>`", parse_mode="Markdown")

async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_id = int(context.args[0])
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET premium_until=NULL WHERE user_id=?", (target_id,))
        conn.commit()
        conn.close()

        await update.message.reply_text(f"🗑️ Premium removed for `{target_id}`!", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/remove_premium <user_id>`", parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total_u = len(get_all_users())
    await update.message.reply_text(f"📊 **Bot Stats:**\n\n👥 Total Users: `{total_u}`", parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Usage: `/broadcast <message>`")
        return
    
    text = " ".join(context.args)
    users = get_all_users()
    sent, failed = 0, 0
    msg = await update.message.reply_text(f"⏳ Broadcasting to {len(users)} users...")
    
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            
    await msg.edit_text(f"📢 **Broadcast Finished!**\n\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    register_user(user_id)
    text = update.message.text.strip()

    if not ("youtube.com" in text or "youtu.be" in text):
        await update.message.reply_text("⚠️ Please send a valid YouTube link.")
        return

    joined = await check_force_join(user_id, context)
    if not joined:
        await update.message.reply_text(
            "⚠️ **Access Denied!**\n\nYou must join both channels to download videos.",
            parse_mode="Markdown",
            reply_markup=get_force_join_keyboard()
        )
        return

    is_prem, status_str = is_user_premium(user_id)
    today_dl, ref_count = get_user_stats(user_id)

    if not is_prem and today_dl >= 2:
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        await update.message.reply_text(
            f"🚫 **Daily Limit Reached!**\n\nFree users get **2 downloads per 24 hours**.\n\n"
            f"👥 **Referral Status:** `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
            f"Invite {REFERRAL_THRESHOLD_FOR_UNLIMITED - ref_count} more users to unlock **Lifetime Unlimited** access!\n\n"
            f"🔗 Your Link:\n`{ref_link}`",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown"
        )
        return

    context.user_data['yt_url'] = text

    keyboard = [
        [InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data="dl_audio")],
        [InlineKeyboardButton("📱 360p (Low Data)", callback_data="dl_360")],
        [InlineKeyboardButton("🎥 480p (Medium)", callback_data="dl_480")],
        [InlineKeyboardButton("🎬 720p HD", callback_data="dl_720")]
    ]

    if is_prem:
        keyboard.append([InlineKeyboardButton("⭐ 1080p Full HD", callback_data="dl_1080")])
    else:
        keyboard.append([InlineKeyboardButton("🔒 1080p [Premium Only]", callback_data="dl_locked")])

    keyboard.append([InlineKeyboardButton("🤖 Check Other Bot", url=OTHER_BOT_URL)])

    await update.message.reply_text(
        f"🎬 **Select Quality:**\n*Status:* `{status_str}`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "dl_locked":
        await query.message.reply_text("🔒 **1080p is reserved for Premium Members!**", parse_mode="Markdown")
        return

    url = context.user_data.get('yt_url')
    if not url:
        await query.edit_message_text("❌ Session expired. Send the link again.")
        return

    status_msg = await query.message.reply_text("⏳ **Initializing Download...**", parse_mode="Markdown")

    file_path = None
    try:
        last_update = [0]

        def progress_hook(d):
            if d['status'] == 'downloading':
                curr_time = asyncio.get_event_loop().time()
                if curr_time - last_update[0] > 2.5:
                    last_update[0] = curr_time
                    downloaded = d.get('downloaded_bytes', 0) / (1024 * 1024)
                    total = d.get('total_bytes', 0) or d.get('total_bytes_estimate', 0)
                    total_mb = total / (1024 * 1024) if total else 0

                    is_prem, _ = is_user_premium(user_id)
                    if total_mb > MAX_FILE_SIZE_MB and not is_prem:
                        raise Exception(f"File size exceeds free limit of {MAX_FILE_SIZE_MB}MB!")

                    percent = d.get('_percent_str', '0%').strip()
                    msg_text = f"⏬ **Downloading...**\n\n📊 *Progress:* `{percent}`\n📁 *Downloaded:* `{downloaded:.1f}MB / {total_mb:.1f}MB`"
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(msg_text, parse_mode="Markdown"),
                        asyncio.get_event_loop()
                    )

        base_ydl_opts = {
            'outtmpl': 'downloads/%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [progress_hook],
            'merge_output_format': 'mp4',
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios', 'web'],
                    'skip': ['hls', 'dash']
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        }

        if os.path.exists("cookies.txt"):
            base_ydl_opts['cookiefile'] = "cookies.txt"

        if query.data == "dl_audio":
            ydl_opts = {
                **base_ydl_opts,
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            }
        elif query.data == "dl_360":
            ydl_opts = {
                **base_ydl_opts,
                'format': (
                    'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]'
                    '/bestvideo[height<=360]+bestaudio'
                    '/best[height<=360]'
                    '/best'
                )
            }
        elif query.data == "dl_480":
            ydl_opts = {
                **base_ydl_opts,
                'format': (
                    'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]'
                    '/bestvideo[height<=480]+bestaudio'
                    '/best[height<=480]'
                    '/best'
                )
            }
        elif query.data == "dl_720":
            ydl_opts = {
                **base_ydl_opts,
                'format': (
                    'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]'
                    '/bestvideo[height<=720]+bestaudio'
                    '/best[height<=720]'
                    '/best'
                )
            }
        elif query.data == "dl_1080":
            ydl_opts = {
                **base_ydl_opts,
                'format': (
                    'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]'
                    '/bestvideo[height<=1080]+bestaudio'
                    '/best[height<=1080]'
                    '/best'
                )
            }

        os.makedirs("downloads", exist_ok=True)
        loop = asyncio.get_event_loop()

        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if query.data == "dl_audio":
                    filename = os.path.splitext(filename)[0] + ".mp3"
                elif not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for ext in ['.mp4', '.mkv', '.webm']:
                        if os.path.exists(base + ext):
                            filename = base + ext
                            break
                return filename, info.get('title', 'Media')

        file_path, title = await loop.run_in_executor(None, download)

        await status_msg.edit_text("📤 **Uploading file to Telegram...**", parse_mode="Markdown")

        with open(file_path, 'rb') as f:
            if query.data == "dl_audio":
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    title=title,
                    caption=f"🎵 **{title}**\n\nDownloaded via YouTube Bot",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown"
                )
            else:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    caption=f"🎥 **{title}**\n\nDownloaded via YouTube Bot",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown"
                )

        is_prem, _ = is_user_premium(user_id)
        if not is_prem:
            increment_user_quota(user_id)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Download Error: {e}")
        err_text = str(e)[:200]
        await status_msg.edit_text(
            f"❌ **Download Failed!**\n\n`{err_text}`\n\n"
            f"💡 Try a different quality or check if the video is age-restricted.",
            parse_mode="Markdown"
        )

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete file: {e}")
        gc.collect()

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("CRITICAL: Set BOT_TOKEN environment variable!")
        return

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_premium", add_premium))
    application.add_handler(CommandHandler("remove_premium", remove_premium))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False
    )

if __name__ == "__main__":
    main()
