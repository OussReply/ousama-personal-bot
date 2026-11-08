import os
import telebot
from flask import Flask
import threading

# جلب التوكن حصرياً وآمناً من متغيرات البيئة (Environment Variables) دون كتابته في الكود
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise ValueError("خطأ حرج: لم يتم العثور على TELEGRAM_BOT_TOKEN في متغيرات البيئة الخاصة بـ Render!")

bot = telebot.TeleBot(TOKEN)

# إعداد خادم Flask البسيط للحفاظ على استمرار عمل البوت على Render (Keep-Alive)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!"

# إعدادات الـ System Prompt (شخصية أسامة الرقمية - موجزة، مباشرة، ودون إفصاح عن الذكاء الاصطناعي)
SYSTEM_PROMPT = """
أنت لست مساعداً ذكياً، ولست روبوت دردشة. أنت المهندس أسامة شخصياً (Digital Twin)، وترد على رسائل الأشخاص على تيليجرام بنفسك.

قواعد الرد الصارمة:
1. الرد المباشر والموجز: أجب على قدر السؤال فقط. لا تغرق العميل بتفاصيل كثيرة أو حشو لا يحتاجه، وكن مباشراً وعملياً تماماً مثل البشر.
2. الهوية والضمير: تحدث دائماً بصيغة المتكلم (أنا)، وكأنك أنت من تجلس وتكتب الرد بيدك. لا تقل أبداً "أنا ذكاء اصطناعي" أو "أنا مساعد أسامة".
3. التكيف اللغوي: تاقلم بذكاء مع لغة أو لهجة الطرف الآخر (جزائرية، مغاربية، خليجية، إنجليزية، أو فصحى) وبنفس النبرة (رسمية مع العملاء الرسميين، وودية مع البقية).
4. طبيعة الردود: رد بطريقة طبيعية وبسيطة كالبشر تماماً.
"""

def call_ai_model(user_message):
    """دالة الاتصال بنموذج الذكاء الاصطناعي للحصول على الرد بالشخصية المحددة"""
    try:
        # يمكنك ربط نموذج Anthropic Claude هنا لاحقاً
        response_text = f"تم استلام رسالتك: {user_message}"
        return response_text
    except Exception as e:
        return "عذراً، حدث خطأ بسيط."

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_text = message.text
    reply_text = call_ai_model(user_text)
    bot.reply_to(message, reply_text)

if __name__ == "__main__":
    # تشغيل خادم الويب على البورت المخصص من Render
    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)

    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    # إزالة الـ Webhook وتجاوز أي رسائل معلقة لمنع خطأ 409 Conflict تماماً
    bot.remove_webhook()
    print("البوت يعمل الآن بأمان تام وبدون تعارض...")
    bot.infinity_polling(skip_pending=True)
