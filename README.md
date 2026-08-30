# 🚀 Telegram YouTube Downloader Bot (Render Ready)

A feature-packed, memory-optimized YouTube Downloader Telegram Bot built with Python, `python-telegram-bot`, `yt-dlp`, and Flask. Configured specifically for deployment on Render's free tier with 24/7 uptime monitoring.

## ✨ Features
* **Quality Options:** 720p HD for Free Users & 1080p Full HD for Premium Users.
* **Format Selector:** Download as MP3 Audio or Video options.
* **Force Join Guard:** Restricts access until users join required Telegram channels.
* **Zero Storage Leak:** Downloaded media is instantly wiped after delivery to keep Render's 512MB RAM/Disk safe.
* **24/7 Keep-Alive:** Built-in Flask web server compatible with UptimeRobot pinging.
* **Admin Controls:** Premium user management commands (`/add_premium`, `/remove_premium`, `/stats`).

## 📁 File Structure
* `app.py`: Flask web server entry point that keeps the app awake and spawns the bot thread.
* `bot.py`: Main Telegram Bot logic, download handlers, and UI setup.
* `requirements.txt`: Python package dependencies.
* `Procfile`: Tells Render how to start the Web application (`web: python app.py`).
* `nixpacks.toml`: Installs system dependencies like `ffmpeg` for media processing.

## ⚙️ Environment Variables Required
Set these in your Render Environment configuration:
* `BOT_TOKEN` — Telegram Bot Token from @BotFather.
* `ADMIN_ID` — Your Telegram numeric User ID.

## 👨‍💻 Maintainer
* **Developer:** [@Devsahatanas](https://t.me/Devsahatanas)
