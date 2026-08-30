import os
import gc
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
MAX_FILE_SIZE_MB = 80          # ✅ 80MB limit — Render safe
RAM_LIMIT_MB = 400             # ✅ 400MB RAM হলে নতুন download block
REFERRAL_THRESHOLD_FOR_UNLIMITED = 100

# ============================================================
# DATABASE SETUP
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    referral_count INTEGER DEFAULT 0,
                    premium_until TEXT,
                    is_banned INTEGER DEFAULT 0,
                    join_date TEXT DEFAULT CURRENT_TIMESTAMP
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS quota (
                    user_id INTEGER,
                    download_date TEXT,
                    count INTEGER,
                    PRIMARY KEY (user_id, download_date)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS download_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT,
                    quality TEXT,
                    download_time TEXT DEFAULT CURRENT_TIMESTAMP
                )''')
    conn.commit()
    conn.close()

init_db()

# ============================================================
# MEMORY CHECK — Render 500MB RAM protect
# ============================================================
def get_ram_usage_mb() -> float:
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024 * 1024)

def is_ram_safe() -> bool:
    return get_ram_usage_mb() < RAM_LIMIT_MB

def cleanup_memory():
    gc.collect()
    # downloads folder পরিষ্কার করো
    if os.path.exists("downloads"):
        for f in os.listdir("downloads"):
            fp = os.path.join("downloads", f)
            try:
                os.remove(fp)
            except Exception:
                pass

# ============================================================
# DB HELPERS
# ============================================================
def register_user(user_id: int, username: str = None, first_name: str = None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""INSERT OR IGNORE INTO users (user_id, username, first_name, referral_count)
                 VALUES (?, ?, ?, 0)""", (user_id, username, first_name))
    c.execute("""UPDATE users SET username=?, first_name=? WHERE user_id=?""",
              (username, first_name, user_id))
    conn.commit()
    conn.close()

def add_referral(referrer_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?", (referrer_id,))
    conn.commit()
    conn.close()

def is_user_banned(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])

def ban_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, referral_count) VALUES (?, 0)", (user_id,))
    c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
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

def get_total_downloads(user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM download_log WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

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

def log_download(user_id: int, title: str, quality: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO download_log (user_id, title, quality) VALUES (?, ?, ?)",
              (user_id, title, quality))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_banned=0")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_top_referrers(limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT user_id, first_name, username, referral_count
                 FROM users WHERE referral_count > 0
                 ORDER BY referral_count DESC LIMIT ?""", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_premium_expiring_soon():
    """১ দিনের মধ্যে expire হবে এমন users"""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT user_id FROM users
                 WHERE premium_until IS NOT NULL
                 AND premium_until > ? AND premium_until <= ?""", (now, tomorrow))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ============================================================
# KEYBOARDS
# ============================================================
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

# ============================================================
# /start
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name)

    # Ban check
    if is_user_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

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

    # ✅ Admin notify — নতুন user join
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"👤 *নতুন User!*\n"
                f"Name: `{user.first_name}`\n"
                f"ID: `{user.id}`\n"
                f"Username: @{user.username or 'N/A'}"
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user.id}"
    today_dl, ref_count = get_user_stats(user.id)
    is_prem, status_str = is_user_premium(user.id)

    if user.id == ADMIN_ID:
        badge = "👑 Admin"
        status_line = "♾️ Unlimited Access"
    elif is_prem:
        badge = "⭐ Premium"
        status_line = f"✅ {status_str}"
    else:
        badge = "🆓 Free"
        status_line = f"📥 Today: {today_dl}/2 downloads used"

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
        f"⬇️ 360p • 480p • 720p • 1080p • MP3\n\n"
        f"📌 /help — সব commands দেখো"
    )

    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ============================================================
# /help
# ============================================================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_banned(update.effective_user.id):
        return
    msg = (
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📖 *Bot Commands*\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 *User Commands:*\n"
        f"▸ /start — Bot শুরু করো\n"
        f"▸ /help — এই help দেখো\n"
        f"▸ /profile — তোমার account card\n"
        f"▸ /mystats — তোমার download stats\n"
        f"▸ /leaderboard — Top referrers\n\n"
        f"🎬 *Download:*\n"
        f"▸ YouTube link পাঠাও\n"
        f"▸ Quality select করো\n"
        f"▸ Free: দিনে 2টা download\n"
        f"▸ Premium/Unlimited: No limit\n\n"
        f"⭐ *Premium পেতে:*\n"
        f"▸ 100 জন referral করো\n"
        f"▸ অথবা Admin কে contact করো\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🛠️ Admin: @Devsahatanas"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ============================================================
# /profile
# ============================================================
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        return

    today_dl, ref_count = get_user_stats(user.id)
    is_prem, status_str = is_user_premium(user.id)
    total_dl = get_total_downloads(user.id)

    if user.id == ADMIN_ID:
        badge = "👑 Admin"
    elif is_prem:
        badge = "⭐ Premium"
    else:
        badge = "🆓 Free"

    filled = int((ref_count / REFERRAL_THRESHOLD_FOR_UNLIMITED) * 10)
    progress_bar = "🟩" * filled + "⬜" * (10 - filled)

    msg = (
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
        f"{progress_bar} `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ============================================================
# /mystats
# ============================================================
async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        return

    today_dl, ref_count = get_user_stats(user.id)
    total_dl = get_total_downloads(user.id)
    is_prem, status_str = is_user_premium(user.id)

    # Last 5 downloads
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT title, quality, download_time FROM download_log
                 WHERE user_id=? ORDER BY id DESC LIMIT 5""", (user.id,))
    recent = c.fetchall()
    conn.close()

    recent_text = ""
    if recent:
        for i, (title, quality, dt) in enumerate(recent, 1):
            short_title = title[:25] + "..." if len(title) > 25 else title
            recent_text += f"  {i}. `{short_title}` — {quality}\n"
    else:
        recent_text = "  _কোনো download নেই_\n"

    msg = (
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *My Stats*\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"┌ 📥 *আজকের Download:* `{today_dl}/{'∞' if is_prem else '2'}`\n"
        f"├ 📦 *Total Downloads:* `{total_dl}`\n"
        f"├ 👥 *Referrals:* `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
        f"└ ⭐ *Status:* `{status_str}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 *সাম্প্রতিক Downloads:*\n"
        f"{recent_text}"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ============================================================
# /leaderboard
# ============================================================
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_banned(update.effective_user.id):
        return

    top = get_top_referrers(10)
    if not top:
        await update.message.reply_text("📊 এখনো কোনো referral নেই!")
        return

    text = "━━━━━━━━━━━━━━━━━━━\n🏆 *Top Referrers*\n━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, (uid, fname, uname, count) in enumerate(top):
        name = fname or uname or str(uid)
        text += f"{medals[i]} *{name}* — `{count}` referrals\n"

    text += "\n━━━━━━━━━━━━━━━━━━━"
    await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# ADMIN COMMANDS
# ============================================================
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

        await update.message.reply_text(
            f"✅ User `{target_id}` — *{days} days* Premium দেওয়া হয়েছে!\n"
            f"📅 Expires: `{exp_time}`",
            parse_mode="Markdown"
        )
        # User কে notify করো
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🎉 *Congratulations!*\n\nAdmin তোমাকে *{days} দিনের Premium* দিয়েছে!\n✨ এখন থেকে Unlimited download করো!",
                parse_mode="Markdown"
            )
        except Exception:
            pass

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
        await update.message.reply_text(f"🗑️ `{target_id}` এর Premium সরানো হয়েছে!", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/remove_premium <user_id>`", parse_mode="Markdown")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_id = int(context.args[0])
        ban_user(target_id)
        await update.message.reply_text(f"🚫 User `{target_id}` banned!", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/ban <user_id>`", parse_mode="Markdown")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_id = int(context.args[0])
        unban_user(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` unbanned!", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/unban <user_id>`", parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    all_users = get_all_users()
    total_u = len(all_users)
    ram_mb = get_ram_usage_mb()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")
    banned = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until > datetime('now')")
    premium = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM download_log WHERE date(download_time)=date('now')")
    today_total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM download_log")
    all_dl = c.fetchone()[0]
    conn.close()

    ram_bar_filled = int((ram_mb / 500) * 10)
    ram_bar = "🟥" * ram_bar_filled + "⬜" * (10 - ram_bar_filled)

    msg = (
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Bot Statistics*\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 *Users:*\n"
        f"┌ Total: `{total_u}`\n"
        f"├ Premium: `{premium}`\n"
        f"├ Banned: `{banned}`\n"
        f"└ Active (unbanned): `{total_u}`\n\n"
        f"📥 *Downloads:*\n"
        f"┌ আজকে: `{today_total}`\n"
        f"└ সর্বমোট: `{all_dl}`\n\n"
        f"💾 *Server RAM:*\n"
        f"{ram_bar} `{ram_mb:.1f}MB / 500MB`\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

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

    await msg.edit_text(
        f"📢 *Broadcast Done!*\n\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`",
        parse_mode="Markdown"
    )

# ============================================================
# PREMIUM EXPIRY WARNING — Background Task
# ============================================================
async def check_premium_expiry(context: ContextTypes.DEFAULT_TYPE):
    expiring = get_premium_expiring_soon()
    for uid in expiring:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "⚠️ *Premium Expiry Warning!*\n\n"
                    "তোমার Premium আগামী *24 ঘণ্টার মধ্যে* শেষ হয়ে যাবে!\n\n"
                    "🔗 Renew করতে Admin এ contact করো: @Devsahatanas"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ============================================================
# HANDLE MESSAGE
# ============================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    register_user(user_id, user.username, user.first_name)

    if is_user_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return

    text = update.message.text.strip()

    if not ("youtube.com" in text or "youtu.be" in text):
        await update.message.reply_text("⚠️ Please send a valid YouTube link.")
        return

    # ✅ RAM check আগে করো
    if not is_ram_safe():
        cleanup_memory()
        if not is_ram_safe():
            await update.message.reply_text(
                "⚠️ *Server busy!*\n\nএখন server-এ load বেশি। একটু পরে try করো।",
                parse_mode="Markdown"
            )
            return

    joined = await check_force_join(user_id, context)
    if not joined:
        await update.message.reply_text(
            "⚠️ *Access Denied!*\n\nDownload করতে দুটো channel join করো।",
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
            f"🚫 *Daily Limit Reached!*\n\nFree users: দিনে *2টা* download।\n\n"
            f"👥 Referral: `{ref_count}/{REFERRAL_THRESHOLD_FOR_UNLIMITED}`\n"
            f"আরো `{REFERRAL_THRESHOLD_FOR_UNLIMITED - ref_count}` জন আনো → Lifetime Unlimited!\n\n"
            f"🔗 তোমার link:\n`{ref_link}`",
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
        f"🎬 *Select Quality:*\n*Status:* `{status_str}`\n💾 RAM: `{get_ram_usage_mb():.0f}MB/500MB`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ============================================================
# HANDLE CALLBACK
# ============================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "dl_locked":
        await query.message.reply_text("🔒 *1080p is Premium Only!*", parse_mode="Markdown")
        return

    url = context.user_data.get('yt_url')
    if not url:
        await query.edit_message_text("❌ Session expired. Link আবার পাঠাও।")
        return

    # ✅ Download শুরুর আগে RAM check
    if not is_ram_safe():
        cleanup_memory()
        if not is_ram_safe():
            await query.message.reply_text(
                "⚠️ *Server busy!* একটু পরে try করো।",
                parse_mode="Markdown"
            )
            return

    status_msg = await query.message.reply_text("⏳ *Initializing Download...*", parse_mode="Markdown")

    file_path = None
    quality_label = query.data.replace("dl_", "").upper()

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
                        raise Exception(f"File size {total_mb:.0f}MB — Free limit {MAX_FILE_SIZE_MB}MB!")

                    percent = d.get('_percent_str', '0%').strip()
                    ram_now = get_ram_usage_mb()
                    msg_text = (
                        f"⏬ *Downloading...*\n\n"
                        f"📊 Progress: `{percent}`\n"
                        f"📁 Size: `{downloaded:.1f}MB / {total_mb:.1f}MB`\n"
                        f"💾 RAM: `{ram_now:.0f}MB/500MB`"
                    )
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
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best'}
        elif query.data == "dl_480":
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best'}
        elif query.data == "dl_720":
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best'}
        elif query.data == "dl_1080":
            ydl_opts = {**base_ydl_opts, 'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'}

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

        await status_msg.edit_text("📤 *Uploading to Telegram...*", parse_mode="Markdown")

        with open(file_path, 'rb') as f:
            if query.data == "dl_audio":
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    title=title,
                    caption=f"🎵 *{title}*\n\nDownloaded via YouTube Bot",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown"
                )
            else:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    caption=f"🎥 *{title}*\n\nDownloaded via YouTube Bot",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown"
                )

        # ✅ Log download
        log_download(user_id, title, quality_label)

        is_prem, _ = is_user_premium(user_id)
        if not is_prem:
            increment_user_quota(user_id)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Download Error: {e}")
        await status_msg.edit_text(
            f"❌ *Download Failed!*\n\n`{str(e)[:200]}`\n\n"
            f"💡 অন্য quality try করো বা link check করো।",
            parse_mode="Markdown"
        )

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"File delete error: {e}")
        # ✅ Memory cleanup সবসময়
        cleanup_memory()

# ============================================================
# MAIN
# ============================================================
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("CRITICAL: Set BOT_TOKEN!")
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

    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("mystats", mystats))
    application.add_handler(CommandHandler("leaderboard", leaderboard))

    # Admin commands
    application.add_handler(CommandHandler("add_premium", add_premium))
    application.add_handler(CommandHandler("remove_premium", remove_premium))
    application.add_handler(CommandHandler("ban", ban_cmd))
    application.add_handler(CommandHandler("unban", unban_cmd))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))

    # Message & callback
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # ✅ Premium expiry check — প্রতি 6 ঘণ্টায়
    application.job_queue.run_repeating(
        check_premium_expiry,
        interval=21600,  # 6 hours
        first=60
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False
    )

if __name__ == "__main__":
    main()
