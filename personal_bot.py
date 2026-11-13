import os
import traceback
import psycopg2
from flask import Flask, request
import telebot
import anthropic

# 1. تهيئة التوكن والمفاتيح
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
DATABASE_URL = os.getenv("DATABASE_URL")  # رابط قاعدة البيانات من Render أو Railway

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

app = Flask(__name__)
MAX_HISTORY_LENGTH = 10  # عدد الرسائل المحفوظة في السياق لكل مستخدم

# 2. تهيئة قاعدة البيانات وإنشاء الجدول إن لم يكن موجوداً
def init_db():
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL is not set!")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")

# استرجاع سجل المحادثات الخاص بالمستخدم من قاعدة البيانات
def get_user_history(chat_id):
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        # جلب آخر رسائل للمستخدم بترتيب تصاعدي بناءً على الوقت
        cur.execute("""
            SELECT role, content FROM (
                SELECT role, content, created_at FROM chat_messages 
                WHERE chat_id = %s 
                ORDER BY id DESC 
                LIMIT %s
            ) sub ORDER BY id ASC;
        """, (chat_id, MAX_HISTORY_LENGTH))
        
        rows = cur.fetchall()
        history = [{"role": row[0], "content": row[1]} for row in rows]
        
        cur.close()
        conn.close()
        return history
    except Exception as e:
        print(f"Error fetching history: {e}")
        return []

# حفظ رسالة جديدة في قاعدة البيانات
def save_message_to_db(chat_id, role, content):
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO chat_messages (chat_id, role, content) 
            VALUES (%s, %s, %s);
        """, (chat_id, role, content))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error saving message: {e}")

# دالة لتقسيم النصوص الطويلة لتجنب خطأ تيليجرام
def send_long_message(chat_id, text):
    max_length = 4000
    if len(text) <= max_length:
        bot.send_message(chat_id, text)
    else:
        for i in range(0, len(text), max_length):
            bot.send_message(chat_id, text[i:i+max_length])

# 3. نقطة استقبال الـ Webhook من تيليجرام
@app.route('/' + TELEGRAM_BOT_TOKEN, methods=['POST'])
def getMessage():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "!", 200
    else:
        return "Forbidden", 403

# 4. الصفحة الرئيسية للتحقق من عمل السيرفر
@app.route("/")
def index():
    return "Professional Persistent Bot Server is Running!", 200

# 5. معالجة رسائل تيليجرام وتذكر السياق عبر PostgreSQL و Claude API
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    user_text = message.text

    try:
        # إظهار حالة "يكتب الآن..." للعميل
        bot.send_chat_action(chat_id, 'typing')
        
        # 1. حفظ رسالة المستخدم في قاعدة البيانات الدائمة
        save_message_to_db(chat_id, "user", user_text)
        
        # 2. جلب سجل المحادثات المحدث لهذا المستخدم
        current_history = get_user_history(chat_id)

        # 3. استدعاء Claude مع إرسال سجل المحادثة كاملاً
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1500,
            system="أنت مساعد ذكي ومحترف لخدمة الزبائن. تذكر تفاصيل المحادثة السابقة وأجب بدقة ومهذبة بلغة عربية فصحى.",
            messages=current_history
        )
        
        reply_text = response.content[0].text
        
        # 4. حفظ رد البوت في قاعدة البيانات الدائمة
        save_message_to_db(chat_id, "assistant", reply_text)
        
        # 5. إرسال الرد للعميل
        send_long_message(chat_id, reply_text)
        
    except Exception as e:
        print("CRITICAL ERROR DURING CLAUDE CALL:")
        print(traceback.format_exc())
        bot.reply_to(message, "عذراً، أواجه ضغطاً مؤقتاً في النظام حالياً. يرجى إعادة محاولة إرسال رسالتك بعد لحظات.")

# 6. تشغيل التطبيق وتهيئة قاعدة البيانات وربط الويب هوك
if __name__ == "__main__":
    init_db()  # إنشاء الجدول عند بدء التشغيل
    
    if WEBHOOK_URL:
        full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{TELEGRAM_BOT_TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=full_webhook_url)
        print(f"Webhook successfully set to: {full_webhook_url}")
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
