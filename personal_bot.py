import os
import traceback
import threading
import telebot
from flask import Flask, request
import anthropic
import psycopg2
from contextlib import contextmanager

TOKEN = "9208308526:AAHmjnPi6cVizlIAa5u3IkDAu0b_hYjZAqw"
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:gQUEiIIrxzzouLVuCygKsJniAsbwvLaK@postgres.railway.internal:5432/railway")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY) if CLAUDE_API_KEY else None

@contextmanager
def get_db_cursor():
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Database transaction error: {e}")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def init_db():
    try:
        with get_db_cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    role TEXT,
                    content TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database init critical error: {e}")

def save_message_to_db(chat_id, role, content):
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "INSERT INTO messages (chat_id, role, content) VALUES (%s, %s, %s)",
                (chat_id, role, content)
            )
    except Exception as e:
        print(f"Failed to save message to DB: {e}")

def envoyer_message_long(chat_id, text):
    max_length = 4096
    for i in range(0, len(text), max_length):
        bot.send_message(chat_id, text[i:i+max_length])

def process_message_async(chat_id, user_text, message_obj):
    """تنفيذ عملية Claude وقاعدة البيانات في الخلفية بعيداً عن اتصال الويب هوك"""
    try:
        print(f"بدء المعالجة الخلفية [Chat ID: {chat_id}]: {user_text}")
        save_message_to_db(chat_id, "user", user_text)
        
        if not client:
            reply_texte = "عذراً، مفتاح Claude API غير معرف في إعدادات المنصة."
        else:
            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=1000,
                messages=[{"role": "user", "content": user_text}]
            )
            reply_texte = response.content[0].text
            
        save_message_to_db(chat_id, "assistant", reply_texte)
        envoyer_message_long(chat_id, reply_texte)
        print("تم الرد وإرسال الرسالة بنجاح عبر الخلفية.")
        
    except Exception as e:
        print("CRITICAL ERROR DURING BACKGROUND PROCESSING:")
        print(traceback.format_exc())

@app.route(f"/{TOKEN}", methods=['POST'])
def webhook():
    """الاستقبال الفوري المطلق والرد بـ 200 OK لمنع أي خطأ 502"""
    try:
        json_data = request.get_json(force=True)
        update = telebot.types.Update.de_json(json_data)
        
        if update and update.message:
            chat_id = update.message.chat.id
            user_text = update.message.text
            if user_text:
                # تشغيل المعالجة فوراً في خيط منفصل والعودة فوراً لتيليجرام
                threading.Thread(target=process_message_async, args=(chat_id, user_text, update.message)).start()
                
        return 'OK', 200
    except Exception as e:
        print(f"Webhook processing error: {e}")
        return 'OK', 200  # نرجع 200 دائماً لتفادي حظر التيليجرام أو تكرار الأخطاء

@app.route("/", methods=["GET"])
def index():
    return "Pro Replay Bot is running securely!", 200

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
          
