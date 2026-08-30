import os
import threading
from flask import Flask
import asyncio
import bot  # Import your telegram bot module

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7 on Render!", 200

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot.main()

if __name__ == "__main__":
    # Run Telegram Bot in a background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Run Flask App to keep Render Web Service active
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
