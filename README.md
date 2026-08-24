# Telegram YouTube Downloader Bot

**Dev:** Anas

A production-ready YouTube downloader bot (720p cap) built with
`python-telegram-bot` v20+ and `yt-dlp`, tuned to run safely on Railway.app's
free tier (512MB RAM).

## Features

- Inline-keyboard status UI: `⏳ Processing...` → `📥 Downloading (720p)...` → `📤 Uploading...` → `🧹 Cleaning up...`
- "🔄 Live Status" button (popup) + "💬 Support" link button
- SQLite-backed daily quota: **2 downloads / 24h** for regular users
- Admin bypass: `/redeem 1169` (or `/admin 1169`) grants unlimited downloads
- Hard-capped resolution: `bestvideo[height<=720]+bestaudio/best[height<=720]`
- File size / duration pre-checks to avoid OOM and failed uploads
- Downloaded files are deleted in a `finally` block; `gc.collect()` runs after every job
- Single-download concurrency (`asyncio.Semaphore`) to keep RAM usage predictable

## Files

| File              | Purpose                                              |
|-------------------|-------------------------------------------------------|
| `bot.py`          | Main bot logic                                        |
| `requirements.txt`| Python dependencies                                    |
| `Procfile`        | Tells Railway to run this as a `worker` process        |
| `nixpacks.toml`   | Installs `ffmpeg` (required by yt-dlp for merging A/V) |

## Deploy on Railway

1. Push this folder to a GitHub repo.
2. Create a new Railway project → **Deploy from GitHub repo**.
3. In **Variables**, add:
   - `BOT_TOKEN` — your Telegram bot token from [@BotFather](https://t.me/BotFather)
   - `ADMIN_CODE` — optional, defaults to `1169`
   - `SUPPORT_URL` — optional, defaults to `https://t.me/DevAnas`
4. Railway will detect `nixpacks.toml` and `Procfile` automatically and deploy
   the bot as a **worker** (no public port needed — this avoids Railway's web
   health-check restarts, which is important for long-running polling bots).
5. Once deployed, message your bot `/start` on Telegram.

## Notes on the free tier

- SQLite (`bot_data.db`) and downloaded files live on Railway's **ephemeral**
  filesystem — they reset on every redeploy. Quota history resets too. If you
  need persistence across deploys, attach a Railway Volume and point
  `DB_PATH` at it.
- Only **one** video is downloaded at a time by design — this is the single
  biggest lever for staying under 512MB RAM. Increase
  `MAX_CONCURRENT_DOWNLOADS` in `bot.py` only if you upgrade your plan.
- Max upload size is capped at 50MB, matching Telegram's standard Bot API
  upload limit.

## Commands

- `/start` — welcome message & rules
- `/status` — check your remaining quota
- `/redeem <code>` or `/admin <code>` — unlock unlimited downloads
- Just paste a YouTube link to download it
