import os
import telebot
from flask import Flask
import threading
import anthropic

# جلب التوكنات بأمان من متغيرات البيئة
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("خطأ: لم يتم العثور على TELEGRAM_BOT_TOKEN في متغيرات البيئة!")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# إعداد خادم Flask للحفاظ على استمرار الخدمة 24/7
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!"

# شخصية المهندس أسامة (Digital Twin)
SYSTEM_PROMPT = """
أنت لست مساعداً ذكياً، ولست روبوت دردشة. أنت المهندس أسامة شخصياً (Digital Twin)، وترد على رسائل الأشخاص على تيليجرام بنفسك.

قواعد الرد الصارمة:
1. الرد المباشر والموجز: أجب على قدر السؤال فقط. لا تغرق العميل بتفاصيل كثيرة أو حشو لا يحتاجه، وكن مباشراً وعملياً تماماً مثل البشر.
2. الهوية والضمير: تحدث دائماً بصيغة المتكلم (أنا)، وكأنك أنت من تجلس وتكتب الرد بيدك. لا تقل أبداً "أنا ذكاء اصطناعي" أو "أنا مساعد أسامة".
3. التكيف اللغوي: تاقلم بذكاء مع لغة أو لهجة الطرف الآخر (جزائرية، مغاربية، خليجية، إنجليزية، أو فصحى) وبنفس النبرة (رسمية مع العملاء الرسميين، وودية مع البقية).
4. طبيعة الردود: رد بطريقة طبيعية وبسيطة كالبشر تماماً.
"""

def call_ai_model(user_message):
    """الاتصال بموديل Claude لتوليد الرد بشخصية أسامة"""
    if not client:
        return "أهلاً! مفتاح الـ API غير مضبوط جيداً."

    try:
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_message}
            ]
        )
        return response.content[0].text
    except Exception as e:
        print(f"Error calling Claude API: {e}")
        return "أهلاً بك! استلمت رسالتك، أراجعها وأرد عليك."

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_text = message.text
    reply_text = call_ai_model(user_text)
    bot.reply_to(message, reply_text)

if __name__ == "__main__":
    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)

    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    # تنظيف الجلسات القديمة وتشغيل البوت بوضعية الأمان
    bot.remove_webhook()
    print("البوت يعمل الآن ومربوط بالذكاء الاصطناعي...")
    bot.infinity_polling(skip_pending=True)
