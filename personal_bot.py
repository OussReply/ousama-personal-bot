import os
import logging
from flask import Flask, request
import telebot
import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor

# إعداد السجلات
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# سحب المتغيرات السرية من البيئة
TOKEN = os.environ.get("TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not TOKEN:
    raise ValueError("❌ خطأ: متغير البيئة TOKEN غير موجود!")

bot = telebot.TeleBot(TOKEN, threaded=False)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
app = Flask(__name__)

# --- دوال قاعدة البيانات (PostgreSQL) ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """إنشاء جدول الرسائل تلقائياً إذا لم يكن موجوداً"""
    if not DATABASE_URL:
        logger.warning("⚠️ متغير DATABASE_URL غير موجود، لن يتم حفظ الرسائل.")
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ تم تهيئة قاعدة بيانات PostgreSQL بنجاح.")
    except Exception as e:
        logger.error(f"❌ خطأ في تهيئة قاعدة البيانات: {e}")

def save_message(chat_id, role, content):
    """حفظ رسالة جديدة (من الزبون أو من البوت) في قاعدة البيانات"""
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_history (chat_id, role, content) VALUES (%s, %s, %s)",
            (chat_id, role, content)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"❌ خطأ أثناء حفظ الرسالة: {e}")

def get_last_messages(chat_id, limit=10):
    """جلب آخر 10 رسائل لهذا الزبون ليتذكرها Claude"""
    if not DATABASE_URL:
        return []
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT role, content FROM (
                SELECT id, role, content FROM chat_history 
                WHERE chat_id = %s 
                ORDER BY id DESC LIMIT %s
            ) sub ORDER BY id ASC;
            """,
            (chat_id, limit)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"role": row["role"], "content": row["content"]} for row in rows]
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب سجل المحادثة: {e}")
        return []

# تهيئة الجدول عند تشغيل التطبيق
init_db()

# --- مسارات الويب هوك والرسائل ---

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

@app.route("/", methods=["GET"])
def index():
    return "Bot with PostgreSQL Memory is running!", 200

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    welcome_text = f"مرحباً بك يا {user_name}، أنا مساعدك الذكي للرد على استفسارات الزبائن. كيف يمكنني مساعدتك اليوم؟"
    bot.reply_to(message, welcome_text)

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    chat_id = message.chat.id
    user_text = message.text

    if not client:
        bot.reply_to(message, "عذراً، مفتاح الذكاء الاصطناعي غير متوفر حالياً.")
        return

    # 1. حفظ رسالة الزبون في قاعدة البيانات
    save_message(chat_id, "user", user_text)

    # 2. جلب آخر 10 رسائل (بما فيها الرسالة الحالية) لتكوين الذاكرة
    conversation_history = get_last_messages(chat_id, limit=10)
    if not conversation_history:
        conversation_history = [{"role": "user", "content": user_text}]

    try:
        # ملاحظة: ضع هنا نفس اسم النموذج الذي نجح معك في الخطوة السابقة
        response = client.messages.create(
            model="claude-haiku-4-5-20251001", 
            max_tokens=400,
            system=(
                "أنت مساعد خدمة عملاء محترف، لبق، وودود. "
                "تتذكر سياق المحادثة السابقة مع الزبون وتبني إجاباتك عليها بذكاء. "
                "أجب بنفس لغة ولهجة الزبون (عربية فصحى، دارجة، أو فرنسية) بأسلوب مختصر، واضح، ومفيد."
            ),
            messages=conversation_history
        )
        reply_text = response.content[0].text

        # 3. حفظ رد البوت في قاعدة البيانات أيضاً ليكتمل سياق الحوار
        save_message(chat_id, "assistant", reply_text)

        bot.reply_to(message, reply_text)
    except Exception as e:
        logger.error(f"Error communicating with Claude: {e}")
        bot.reply_to(message, "عذراً، حدث خطأ تقني مؤقت، يرجى المحاولة مرة أخرى.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
