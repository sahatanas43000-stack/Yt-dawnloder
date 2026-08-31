# 🚀 YouTube Video & Audio Downloader Telegram Bot

A feature-rich, high-performance Telegram Bot built using **Python**, **python-telegram-bot**, and **yt-dlp**. It allows users to download YouTube videos in multiple resolutions and audio formats with daily limits, a referral system, and anti-bot bypass configurations.

---

## ✨ Features

* 🎥 **Multiple Quality Options:** Download in **360p**, **480p**, **720p HD**, and **1080p Full HD** (Premium).
* 🎵 **Audio Extraction:** High-quality MP3 audio download.
* ⚡ **Live Download Progress:** Real-time updates showing downloaded MBs and percentage (`%`).
* 🔒 **Force Channel Join:** Ensures users subscribe to specified Telegram channels before downloading.
* 👥 **Referral System:** Free users can invite friends via unique referral links to earn extra download quotas.
* 📊 **Daily Quota System:** SQLite-backed 2-downloads/24h limit for free users.
* ⭐ **Premium Membership:** Custom admin command to grant unlimited downloads and 1080p access.
* 📢 **Admin Broadcasting:** Broadcast text messages or forward media posts to all bot users.
* 📦 **Server Protection:** Automatic file limit (80 MB) to protect free tier servers (e.g., Render) from RAM crashes.

---

## 🛠️ Environment Variables

Set the following Environment Variables in your hosting environment (e.g., Render, Railway, Heroku):

| Variable | Description |
| :--- | :--- |
| `BOT_TOKEN` | Your Telegram Bot Token from `@BotFather` |
| `ADMIN_ID` | Your Telegram Numeric User ID |
| `PORT` | (Optional) Web server port for Flask pinging (Default: `10000`) |

---

## 🎮 Admin Commands

| Command | Usage | Description |
| :--- | :--- | :--- |
| `/add_premium` | `/add_premium <user_id> <1month><etc>` | Add a user to the Premium list |
| `/remove_premium` | `/remove_premium <user_id>` | Remove a user from the Premium list |
| `/stats` | `/stats` | View total users and premium members count |
| `/broadcast` | `/broadcast <text>` | Send a text message to all users |
| `/post` | Reply to media with `/post` | Forward/Broadcast media posts to all users |

---

## 🚀 Deployment Guide (Render)

1. **Fork/Push** this repository to your GitHub account.
2. Create a new **Web Service** on [Render](https://render.com).
3. Connect your GitHub repository.
4. Set the **Build Command**:
   ```bash
   pip install -r requirements.txt
