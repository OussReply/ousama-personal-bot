import os
import logging
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from google import genai
from google.genai import types

# إعداد السجلات
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# إعداد مفاتيح الاتصال
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# تهيئة عميل جوجل
client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = "أنت مساعد شخصي ذكي ومهني تقدم خدمة العملاء والدعم الفني."

# دالة الرد على الرسائل مع قائمة النماذج الاحتياطية الآمنة
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    ai_reply = None
    
    MODELS_TO_TRY = [
        "gemini-3.8-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash"
    ]
    
    for model_name in MODELS_TO_TRY:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user_text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.3
                )
            )
            if response and response.text:
                ai_reply = response.text
                break
        except Exception as e:
            logger.warning(f"Model {model_name} failed: {e}")
            continue

    if not ai_reply:
        ai_reply = "استلمنا استفسارك وتم تسجيله في نظام الدعم الفني الخاص بنا، سيتواصل معك المهندس."

    await update.message.reply_text(ai_reply)

# إعداد خادم الويب المصغر لضمان استقرار Render و UptimeRobot
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot is active and running 24/7!"

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN is not set.")
        return

    # تشغيل خادم الويب Flask في الخلفية باستخدام threading بشكل دقيق
    import threading
    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app_flask.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # بناء وتشغيل تطبيق تليجرام
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Enterprise Bot with Web Server is running and polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
