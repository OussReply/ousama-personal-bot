import os
import logging
import threading
from flask import Flask
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# 1. إعداد نظام السجلات (Logging) لمتابعة الأخطاء والأحداث بدقة
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 2. قراءة المتغيرات السرية من بيئة المنصة (Render)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
# يمكنك وضع معرفك الشخصي هنا أو كمتغير بيئة لحماية البوت
OWNER_ID = os.environ.get("OWNER_ID") # اختياري: معرف تيليغرام الخاص بك

if not TOKEN:
    logger.error("خطأ: لم يتم العثور على TELEGRAM_TOKEN في متغيرات البيئة!")
    exit(1)

bot = telebot.TeleBot(TOKEN)

# ==========================================
3. إعداد خادم الويب المصغر (Flask) للخدمة المجانية على Render
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is active and running successfully!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ==========================================
4. تصميم الأزرار التفاعلية (Inline Keyboards)
# ==========================================
def get_main_menu():
    markup = InlineKeyboardMarkup()
    btn_ai = InlineKeyboardButton("🤖 نموذج الذكاء الاصطناعي", callback_data="btn_ai")
    btn_help = InlineKeyboardButton("ℹ️ حول البوت", callback_data="btn_help")
    markup.add(btn_ai, btn_help)
    
    btn_contact = InlineKeyboardButton("🌐 تواصل مع المطور (خمسات)", url="https://khamsat.com")
    markup.add(btn_contact)
    return markup


# ==========================================
5. أوامر البوت والتفاعل مع المستخدمين
# ==========================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    try:
        user_name = message.from_user.first_name
        user_id = message.from_user.id
        logger.info(f"مستخدم جديد بدأ المحادثة: ID={user_id}, Name={user_name}")

        welcome_text = (
            f"مرحباً بك يا *{user_name}* في النسخة التجريبية لبوت الخدمات الذكية! 🚀\n\n"
            "هذا البوت مصمم بأحدث التقنيات ليكون نموذجاً احترافياً.\n"
            "اختر أحد الخيارات من القائمة أدناه للتعرف على الميزات:"
        )
        
        bot.send_message(
            message.chat.id, 
            welcome_text, 
            parse_mode="Markdown", 
            reply_markup=get_main_menu()
        )
    except Exception as e:
        logger.error(f"حدث خطأ في أمر start: {e}")


# 6. معالجة ضغطات الأزرار التفاعلية (Callback Query Handler)
@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    try:
        if call.data == "btn_ai":
            bot.answer_callback_query(call.id, "جاري تحضير الخدمة...")
            bot.send_message(
                call.message.chat.id, 
                "✨ هذه ميزة الذكاء الاصطناعي التجريبية. يمكنك ربط هذا القسم بأي نموذج لتقديم إجابات ذكية للعملاء!"
            )
        elif call.data == "btn_help":
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id, 
                "ℹ️ *معلومات عن البوت:*\n"
                "- مبني بلغة Python 3.12\n"
                "- يعمل على خوادم سحابية بصفة دائمة\n"
                "- مصمم بجودة عالية لخدمات الأتمتة وتطوير البوتات.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"خطأ في معالجة الأزرار: {e}")


# 7. الرد على الرسائل النصية العادية
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    try:
        text = message.text
        bot.reply_to(
            message, 
            f"لقد استلمت رسالتك: '{text}'\nاستخدم أمر /start لعرض القائمة الرئيسية والأزرار التفاعلية."
        )
    except Exception as e:
        logger.error(f"خطأ في معالجة الرسائل النصية: {e}")


# ==========================================
8. نقطة التشغيل الرئيسية (Main)
# ==========================================
if __name__ == "__main__":
    # تشغيل خادم Flask في خيط منفصل (Background Thread) للحفاظ على نشاط البوت على Render
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("تم بدء تشغيل خادم Flask بنجاح.")

    # تشغيل بوت التيليغرام
    logger.info("جاري بدء تشغيل بوت التيليغرام...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
