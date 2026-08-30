import os
import asyncio
import threading
from flask import Flask
import bot  # Import main bot file

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7 on Render!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    # Flask web server runs in background thread
    app.run(host="0.0.0.0", port=port, use_reloader=False)

async def run_bot():
    if bot.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("CRITICAL: Set BOT_TOKEN environment variable!")
        return

    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters
    )

    application = ApplicationBuilder().token(bot.BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("add_premium", bot.add_premium))
    application.add_handler(CommandHandler("remove_premium", bot.remove_premium))
    application.add_handler(CommandHandler("stats", bot.stats))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    application.add_handler(CallbackQueryHandler(bot.handle_callback))

    # Initialize and start in main thread properly
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    
    # Keep the async loop alive
    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == "__main__":
    # 1. Start Flask Server in Background Thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 2. Run Telegram Bot in Main Thread (Fixes Threading/Signal Error)
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        pass
