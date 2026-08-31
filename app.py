import os
import threading
from flask import Flask
import bot

app = Flask(__name__)

@app.route('/')
def home():
    return "YouTube Downloader Bot is Running Live!"

def start_bot():
    bot.main()

if __name__ == '__main__':
    # Telegram Bot running in background thread
    t = threading.Thread(target=start_bot)
    t.daemon = True
    t.start()

    # Flask Web Server
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
