import os
import json
import logging
import asyncio
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

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# CONFIGURATION (Set via Render Environment Variables or replace below)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))  # Replace with your Telegram User ID

# Telegram Channels for Force Join
CHANNEL_1 = "@sahatanas"
CHANNEL_2 = "@sahatanass"
CHANNEL_1_URL = "https://t.me/sahatanas"
CHANNEL_2_URL = "https://t.me/sahatanass"
OTHER_BOT_URL = "https://t.me/BomssssssssBot"

PREMIUM_FILE = "premium_users.json"

# Persistence for Premium Users
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

# Check Force Join Status
async def check_force_join(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    for channel in [CHANNEL_1, CHANNEL_2]:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception as e:
            logger.warning(f"Could not check membership for {channel}: {e}")
            # If bot is not admin in channel, skip check to prevent soft-lock
            continue
    return True

def get_force_join_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel 1", url=CHANNEL_1_URL)],
        [InlineKeyboardButton("📢 Join Channel 2", url=CHANNEL_2_URL)],
        [InlineKeyboardButton("🚀 Try Other Bot (@BomssssssssBot)", url=OTHER_BOT_URL)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🛠️ Contact Admin", url="https://t.me/Devsahatanas")],
        [InlineKeyboardButton("⚡ Any Video Downloader Bot", url=OTHER_BOT_URL)]
    ]
    return InlineKeyboardMarkup(keyboard)

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"👋 **Hi {user.first_name}!**\n\n"
        f"Welcome to **YouTube Downloader Bot** 🚀\n\n"
        f"📹 **Free Users:** Up to 720p HD Video & MP3 Audio\n"
        f"⭐ **Premium Users:** 1080p Full HD Video\n\n"
        f"📩 Just send me any YouTube Video link to start downloading!"
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
    await update.message.reply_text(f"📊 **Bot Stats:**\n\n⭐ Total Premium Members: `{len(premium_users)}`", parse_mode="Markdown")

# Handle Input YouTube Links
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not ("youtube.com" in text or "youtu.be" in text):
        await update.message.reply_text("⚠️ Please send a valid YouTube link.")
        return

    # Check Force Join
    joined = await check_force_join(user_id, context)
    if not joined:
        await update.message.reply_text(
            "⚠️ **Access Denied!**\n\nYou must join both our channels to download videos.",
            parse_mode="Markdown",
            reply_markup=get_force_join_keyboard()
        )
        return

    # Store link in user_data
    context.user_data['yt_url'] = text

    is_premium = user_id in premium_users or user_id == ADMIN_ID
    
    keyboard = [
        [InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data="dl_audio")],
        [InlineKeyboardButton("🎥 Video (720p HD)", callback_data="dl_720")]
    ]

    if is_premium:
        keyboard.append([InlineKeyboardButton("⭐ Video (1080p Full HD)", callback_data="dl_1080")])
    else:
        keyboard.append([InlineKeyboardButton("🔒 Video (1080p) [Premium Only]", callback_data="dl_locked")])

    keyboard.append([InlineKeyboardButton("🤖 Check Other Bot", url=OTHER_BOT_URL)])

    await update.message.reply_text(
        "🎬 **Select Download Quality:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# Callback Query Handler (Download Logic & Render Zero-Storage Cleanup)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "dl_locked":
        await query.message.reply_text("🔒 **1080p is reserved for Premium Members!**\nContact @Devsahatanas to upgrade.", parse_mode="Markdown")
        return

    url = context.user_data.get('yt_url')
    if not url:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    status_msg = await query.message.reply_text("⏳ **Processing & Downloading... Please wait.**", parse_mode="Markdown")

    file_path = None
    try:
        # Dynamic Options based on Selection
        if query.data == "dl_audio":
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': 'downloads/%(id)s.%(ext)s',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True,
                'no_warnings': True,
            }
        elif query.data == "dl_720":
            ydl_opts = {
                'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best',
                'outtmpl': 'downloads/%(id)s.%(ext)s',
                'quiet': True,
                'no_warnings': True,
            }
        elif query.data == "dl_1080":
            ydl_opts = {
                'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best',
                'outtmpl': 'downloads/%(id)s.%(ext)s',
                'quiet': True,
                'no_warnings': True,
            }

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

        # Send File to User
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

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Download Error: {e}")
        await status_msg.edit_text(f"❌ **Failed to process video.**\n\nError: `{str(e)[:100]}`", parse_mode="Markdown")

    finally:
        # CRITICAL FOR RENDER: Instant Cleanup to prevent 500MB storage crash
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Cleaned up file: {file_path}")
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
