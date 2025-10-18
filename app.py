from flask import Flask
import threading
import os
import sys
import asyncio

# Добавляем путь к текущей директории
sys.path.append(os.path.dirname(__file__))

# Импортируем бота
from bot import main as bot_main

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_bot():
    print("🤖 Starting Telegram bot...")
    try:
        bot_main()
    except Exception as e:
        print(f"❌ Bot error: {e}")

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Starting Flask server on port {port}...")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Запускаем Flask
    run_flask()
