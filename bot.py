import os
import json
import logging
import sqlite3
import asyncio
from datetime import date
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

PREMIUM_FILE = "premium_users.json"
DB_FILE = "user_quota.db"
MAX_FILE_SIZE_MB = 100  # Server Protection Limit

# SQLite Database Setup
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, referral_bonus INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS quota 
                 (user_id INTEGER, download_date TEXT, count INTEGER, PRIMARY KEY (user_id, download_date))''')
    conn.commit()
    conn.close()

init_db()

def register_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, referral_bonus) VALUES (?, 0)", (user_id,))
    conn.commit()
    conn.close()

def add_referral_bonus(referrer_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET referral_bonus = referral_bonus + 1 WHERE user_id=?", (referrer_id,))
    conn.commit()
    conn.close()

def get_user_data(user_id: int):
    today = str(date.today())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("SELECT referral_bonus FROM users WHERE user_id=?", (user_id,))
    user_row = c.fetchone()
    bonus = user_row[0] if user_row else 0

    c.execute("SELECT count FROM quota WHERE user_id=? AND download_date=?", (user_id, today))
    quota_row = c.fetchone()
    today_count = quota_row[0] if quota_row else 0
    
    conn.close()
    return today_count, bonus

def increment_user_quota(user_id: int, used_bonus: bool = False):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if used_bonus:
        c.execute("UPDATE users SET referral_bonus = referral_bonus - 1 WHERE user_id=? AND referral_bonus > 0", (user_id,))
    else:
        today = str(date.today())
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

def load_premium_users():
    if os.path.exists(PREMIUM_FILE):
        try:
            with open(PREMIUM_FILE, "r") as f:
                return set(json.load(f))
        except Exception as e:
            logger.error(f"Error loading premium file: {e}")
            return set()
    return set()

def save_premium_users(users):
    try:
        with open(PREMIUM_FILE, "w") as f:
            json.dump(list(users), f)
    except Exception as e:
        logger.error(f"Error saving premium file: {e}")

premium_users = load_premium_users()

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
        [InlineKeyboardButton("🚀 Try Other Bot (@BomssssssssBot)", url=OTHER_BOT_URL)]
    ])

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ Contact Admin", url="https://t.me/Devsahatanas")],
        [InlineKeyboardButton("⚡ Any Video Downloader Bot", url=OTHER_BOT_URL)]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id)

    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                add_referral_bonus(referrer_id)
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id, 
                        text=f"🎉 **New Referral!** User `{user.first_name}` joined using your link. You earned +1 Bonus Download!",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
        except ValueError:
            pass

    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user.id}"

    msg = (
        f"👋 **Hi {user.first_name}!**\n\n"
        f"Welcome to **YouTube Downloader Bot** 🚀\n\n"
        f"🆓 **Free Users:** 2 Downloads/24h (Max {MAX_FILE_SIZE_MB}MB)\n"
        f"⭐ **Premium Users:** Unlimited Downloads\n\n"
        f"🔗 **Your Referral Link:**\n`{ref_link}`\n"
        f"*(Invite friends to earn +1 extra download per user!)*\n\n"
        f"📩 Send me any YouTube Video link to start downloading!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def add_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_id = int(context.args[0])
        premium_users.add(target_id)
        save_premium_users(premium_users)
        await update.message.reply_text(f"✅ User `{target_id}` added to Premium!", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/add_premium <user_id>`", parse_mode="Markdown")

async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_id = int(context.args[0])
        premium_users.discard(target_id)
        save_premium_users(premium_users)
        await update.message.reply_text(f"🗑️ User `{target_id}` removed from Premium!", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/remove_premium <user_id>`", parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total_u = len(get_all_users())
    await update.message.reply_text(
        f"📊 **Bot Stats:**\n\n👥 Total Users: `{total_u}`\n⭐ Premium Members: `{len(premium_users)}`", 
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Usage: `/broadcast <message>`")
        return
    
    text = " ".join(context.args)
    users = get_all_users()
    sent, failed = 0, 0
    
    msg = await update.message.reply_text(f"⏳ Broadcasting message to {len(users)} users...")
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            
    await msg.edit_text(f"📢 **Broadcast Finished!**\n\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`", parse_mode="Markdown")

async def post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a message with `/post` to broadcast it.")
        return

    target_msg = update.message.reply_to_message
    users = get_all_users()
    sent, failed = 0, 0

    status_msg = await update.message.reply_text(f"⏳ Forwarding post to {len(users)} users...")
    for uid in users:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=target_msg.chat_id, message_id=target_msg.message_id)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(f"📢 **Post Broadcast Complete!**\n\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`", parse_mode="Markdown")

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
            "⚠️ **Access Denied!**\n\nYou must join both our channels to download videos.",
            parse_mode="Markdown",
            reply_markup=get_force_join_keyboard()
        )
        return

    is_premium = user_id in premium_users or user_id == ADMIN_ID
    today_dl, bonus = get_user_data(user_id)

    if not is_premium and today_dl >= 2 and bonus <= 0:
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        await update.message.reply_text(
            f"🚫 **Daily Limit Reached!**\n\nFree users get **2 downloads per 24 hours**.\n"
            f"🎁 **Invite friends to get +1 extra download per referral!**\n\nYour Link:\n`{ref_link}`",
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

    if is_premium:
        keyboard.append([InlineKeyboardButton("⭐ 1080p Full HD", callback_data="dl_1080")])
    else:
        keyboard.append([InlineKeyboardButton("🔒 1080p [Premium Only]", callback_data="dl_locked")])

    keyboard.append([InlineKeyboardButton("🤖 Check Other Bot", url=OTHER_BOT_URL)])

    quota_txt = "Unlimited (Premium)" if is_premium else (f"{bonus} Bonus Downloads" if today_dl >= 2 else f"{2 - today_dl}/2 remaining today")
    await update.message.reply_text(
        f"🎬 **Select Download Quality:**\n*Quota:* `{quota_txt}`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "dl_locked":
        await query.message.reply_text("🔒 **1080p is reserved for Premium Members!**\nContact @Devsahatanas to upgrade.", parse_mode="Markdown")
        return

    url = context.user_data.get('yt_url')
    if not url:
        await query.edit_message_text("❌ Session expired. Send the link again.")
        return

    status_msg = await query.message.reply_text("⏳ **Initializing Download...**", parse_mode="Markdown")

    file_path = None
    try:
        last_update = 0
        def progress_hook(d):
            nonlocal last_update
            if d['status'] == 'downloading':
                curr_time = asyncio.get_event_loop().time()
                if curr_time - last_update > 2:
                    last_update = curr_time
                    downloaded = d.get('downloaded_bytes', 0) / (1024 * 1024)
                    total = d.get('total_bytes', 0) or d.get('total_bytes_estimate', 0)
                    total_mb = total / (1024 * 1024) if total else 0
                    
                    if total_mb > MAX_FILE_SIZE_MB and not (user_id in premium_users or user_id == ADMIN_ID):
                        raise Exception(f"File size exceeds free server limit of {MAX_FILE_SIZE_MB}MB!")
                    
                    percent = d.get('_percent_str', '0%').strip()
                    msg = f"⏬ **Downloading...**\n\n📊 *Progress:* `{percent}`\n📁 *Downloaded:* `{downloaded:.1f}MB / {total_mb:.1f}MB`"
                    asyncio.run_coroutine_threadsafe(status_msg.edit_text(msg, parse_mode="Markdown"), asyncio.get_event_loop())

        base_ydl_opts = {
            'outtmpl': 'downloads/%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [progress_hook],
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'android', 'mweb'],
                    'skip': ['hls', 'dash']
                }
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
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]/best'}
        elif query.data == "dl_480":
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best'}
        elif query.data == "dl_720":
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best'}
        elif query.data == "dl_1080":
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best'}

        os.makedirs("downloads", exist_ok=True)

        loop = asyncio.get_event_loop()
        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if query.data == "dl_audio":
                    filename = os.path.splitext(filename)[0] + ".mp3"
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

        is_premium = user_id in premium_users or user_id == ADMIN_ID
        if not is_premium:
            today_dl, bonus = get_user_data(user_id)
            increment_user_quota(user_id, used_bonus=(today_dl >= 2 and bonus > 0))

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Download Error: {e}")
        await status_msg.edit_text(f"❌ **Failed to process video.**\n\nError: `{str(e)[:150]}`", parse_mode="Markdown")

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete file: {e}")

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("CRITICAL: Set BOT_TOKEN environment variable!")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_premium", add_premium))
    application.add_handler(CommandHandler("remove_premium", remove_premium))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("post", post))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
