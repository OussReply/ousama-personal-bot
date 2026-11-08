import os
import telebot
import requests
from flask import Flask

# التهيئة والتأكد من جلب التوكن الصحيح من متغيرات البيئة
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    print("خطأ: لم يتم العثور على TELEGRAM_BOT_TOKEN في متغيرات البيئة!")
    # قيمة افتراضية احتياطية لضمان عدم توقف البوت في حال فشل قراءة المتغير
    TOKEN = "9208308526:AAF3v4S3MzIa2ff_6aP7z34ba2pY1sgpCEQ"

bot = telebot.TeleBot(TOKEN)

# إعداد خادم Flask البسيط للحفاظ على استمرار عمل البوت على Render (Keep-Alive)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!"

# إعدادات الـ System Prompt الخاصة بنموذج الذكاء الاصطناعي (أنت شخصياً / Digital Twin)
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
    # يمكنك وضع رابط الـ API الخاص بك أو دمج نموذج Anthropic Claude هنا
    try:
        # مثال افتراضي لمعالجة النص أو توجيهه للنموذج
        # سيتم استخدام الـ SYSTEM_PROMPT لتوجيه أسلوب الرد بدقة
        response_text = f"تم استلام رسالتك: {user_message}"
        return response_text
    except Exception as e:
        return "عذراً، حدث خطأ بسيط في الرد، سأعاود الاتصال بك لاحقاً."

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_text = message.text
    # توليد الرد بناءً على شخصية أسامة المباشرة والموجزة
    reply_text = call_ai_model(user_text)
    bot.reply_to(message, reply_text)

if __name__ == "__main__":
    # تشغيل خادم الويب على البورت المخصص من Render
    import threading
    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)

    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    # بدء تشغيل بوت تيليجرام
    print("البوت يعمل الآن...")
    bot.infinity_polling()
