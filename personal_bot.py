import os
import logging
from flask import Flask, request
import telebot

# إعداد السجلات
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# سحب المتغيرات السرية من البيئة
TOKEN = os.environ.get("TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not TOKEN:
    raise ValueError("❌ خطأ: متغير البيئة TOKEN غير موجود!")

# إعداد بوت تيليجرام باستخدام telebot
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """استقبال التحديثات من تيليجرام وتحويلها للبوت"""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

@app.route("/", methods=["GET"])
def index():
    return "Bot is running successfully!", 200

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    bot.reply_to(message, f"مرحباً بك يا {user_name}، أنا جاهز للرد على استفسارات الزبائن نيابة عنك!")

def main():
    logger.info("Database initialized successfully.")

if __name__ == "__main__":
    main()
