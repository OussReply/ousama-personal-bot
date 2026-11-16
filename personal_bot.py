import os
import logging
from flask import Flask, request
import telebot
import anthropic

# إعداد السجلات
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# سحب المتغيرات السرية من البيئة
TOKEN = os.environ.get("TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not TOKEN:
    raise ValueError("❌ خطأ: متغير البيئة TOKEN غير موجود!")

# إعداد بوت تيليجرام وعميل أنثروبيك
bot = telebot.TeleBot(TOKEN, threaded=False)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

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
    bot.reply_to(message, f"مرحباً بك يا {user_name}، أنا مساعدك الذكي للرد على استفسارات الزبائن. تفضل بطرح سؤالك أو طلبك!")

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    """الرد على أي رسالة نصية من الزبائن باستخدام Claude API"""
    user_text = message.text
    user_name = message.from_user.first_name
    
    if not client:
        bot.reply_to(message, "عذراً، مفتاح الذكاء الاصطناعي غير متوفر حالياً.")
        return

    try:
        # إرسال الرسالة إلى Claude مع توجيه ليتصرف كمساعد خدمة عملاء احترافي
        response = client.messages.create(
            model="claude-3-haiku-20240307",  # نموذج سريع واقتصادي للردود
            max_tokens=300,
            system="أنت مساعد خدمة عملاء ودود واحترافي لمتجر إلكتروني. أجب عن استفسارات الزبائن بشكل لطيف، مختصر، ومفيد باللغة التي يخاطبونك بها.",
            messages=[
                {"role": "user", "content": user_text}
            ]
        )
        reply_text = response.content[0].text
        bot.reply_to(message, reply_text)
    except Exception as e:
        logger.error(f"Error communicating with Claude: {e}")
        bot.reply_to(message, "عذراً، حدث خطأ أثناء معالجة طلبك، يرجى المحاولة لاحقاً.")

def main():
    logger.info("Database initialized successfully.")

if __name__ == "__main__":
    main()
