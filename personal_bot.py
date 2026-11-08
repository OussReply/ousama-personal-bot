import os
import logging
import threading
from flask import Flask
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# 1. إعداد السجلات
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 2. قراءة التوكن من بيئة Render
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.error("خطأ: لم يتم العثور على TELEGRAM_TOKEN في متغيرات البيئة!")
    exit(1)

bot = telebot.TeleBot(TOKEN)

# 3. إعداد خادم Flask للخدمة المجانية على Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Osama's Bot Assistant is active and running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# 4. تصميم الأزرار التفاعلية الاحترافية (القائمة الرئيسية)
def get_main_menu():
    markup = InlineKeyboardMarkup()
    btn_project = InlineKeyboardButton("📋 طلب مشروع (بوت / كتاب)", callback_data="btn_project")
    btn_info = InlineKeyboardButton("ℹ️ عن خدمات المهندس أسامة", callback_data="btn_info")
    markup.add(btn_project, btn_info)
    
    btn_khamsat = InlineKeyboardButton("🌐 صفحة خمسات", url="https://khamsat.com")
    markup.add(btn_khamsat)
    return markup


# 5. الترحيب عند إرسال /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
    try:
        user_name = message.from_user.first_name
        logger.info(f"مستخدم جديد بدأ المحادثة: {user_name}")

        welcome_text = (
            f"مرحباً بك يا *{user_name}* مع المساعد الشخصي للمهندس أسامة. 👨‍💻\n\n"
            "نحن متخصصون في بناء وتطوير بوتات التليغرام الاحترافية والكتب الإلكترونية المخصصة.\n\n"
            "يرجى اختيار أحد الخيارات أدناه أو كتابة تفاصيل طلبك مباشرة:"
        )
        
        bot.send_message(
            message.chat.id, 
            welcome_text, 
            parse_mode="Markdown", 
            reply_markup=get_main_menu()
        )
    except Exception as e:
        logger.error(f"خطأ في أمر start: {e}")


# 6. معالجة الضغط على الأزرار
@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    try:
        if call.data == "btn_project":
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                "📝 يرجى كتابة تفاصيل مشروعك هنا باختصار:\n"
                "- ما هو نوع البوت أو الخدمة المطلوبة؟\n"
                "- ما هي الأفكار أو الميزات التي ترغب في وجودها؟\n\n"
                "سيقوم المهندس أسامة بالاطلاع على طلبك والتواصل معك قريباً.",
                reply_markup=get_main_menu()
            )
        elif call.data == "btn_info":
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                "🛠️ *خدماتنا المتاحة:*\n"
                "- برمجة وتطوير بوتات تليغرام مخصصة ومتكاملة.\n"
                "- تصميم وتنسيق محتوى الكتب الإلكترونية.\n"
                "- حلول أتمتة المهام الرقمية.\n\n"
                "لدينا خبرة تقنية عالية لضمان تنفيذ مشروعك بأعلى جودة.",
                reply_markup=get_main_menu()
            )
    except Exception as e:
        logger.error(f"خطأ في معالجة الأزرار: {e}")


# 7. الرد الاحترافي على رسائل العميل (مرفق معه الأزرار دائماً)
@bot.message_handler(func=lambda message: True)
def handle_client_messages(message):
    try:
        reply_text = (
            "✅ تم استلام رسالتك وتفاصيل طلبك بنجاح.\n"
            "سيقوم المهندس أسامة بمراجعتها والرد عليك في أقرب وقت ممكن."
        )
        # تم إرفاق الأزرار هنا أيضاً لضمان ظهورها دائماً
        bot.reply_to(message, reply_text, reply_markup=get_main_menu())
        logger.info("تم استلام رسالة من العميل وتم الرد عليه مع إرفاق الأزرار.")
    except Exception as e:
        logger.error(f"خطأ في معالجة رسائل العميل: {e}")


# 8. تشغيل السيرفر والبوت
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("تم بدء تشغيل خادم Flask.")

    logger.info("جاري بدء تشغيل البوت...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
