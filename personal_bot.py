import os
import traceback
import threading
import telebot
from flask import Flask, request
import anthropic
import psycopg2
from contextlib import contextmanager

# إعداد المتغيرات الأساسية
TOKEN = "9208308526:AAHmjnPi6cVizlIAa5u3IkDAu0b_hYjZAqw"
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:gQUEiIIrxzzouLVuCygKsJniAsbwvLaK@postgres.railway.internal:5432/railway")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://worker-production-2de8bf40.up.railway.app")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

@contextmanager
def get_db_cursor():
    """مدير سياق آمن لفتح وإغلاق اتصالات قاعدة البيانات."""
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
    """معالجة رسالة المستخدم والاتصال بـ Claude في الخلفية لمنع حدوث Timeout."""
    try:
        print(f"معالجة الرسالة في الخلفية [Chat ID: {chat_id}]: {user_text}")
        save_message_to_db(chat_id, "user", user_text)
        
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            messages=[{"role": "user", "content": user_text}]
        )
        
        reply_texte = response.content[0].text
        save_message_to_db(chat_id, "assistant", reply_texte)
        envoyer_message_long(chat_id, reply_texte)
        print("تم إرسال الرد بنجاح.")
        
    except Exception as e:
        print("CRITICAL ERROR DURING CLAUDE/DB EXECUTION:")
        print(traceback.format_exc())
        bot.reply_to(message_obj, "عذراً، أواجه ضغطاً مؤقتاً في النظام. يرجى المحاولة بعد قليل.")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    user_text = message.text
    
    # تشغيل المعالجة في خيط خلفي (Background Thread) لكي تعود الويب هوك بـ 200 فوراً
    threading.Thread(target=process_message_async, args=(chat_id, user_text, message)).start()

@app.route(f"/{TOKEN}", methods=['POST'])
def webhook():
    """الاستقبال الفوري لطلبات تيليجرام والرد بـ 200 OK دون تأخير."""
    try:
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    except Exception as e:
        print(f"Webhook processing error: {e}")
        return 'Error', 500
     
@app.route("/", methods=["GET"])
def index():
    return "Pro Replay Bot is running securely!", 200

if __name__ == "__main__":
    init_db()
    
    if WEBHOOK_URL:
        full_webhook_url = f"{WEBHOOK_URL.strip('/')}/{TOKEN}"
        try:
            # تعيين الويب هوك مباشرة دون الحاجة لـ remove_webhook()
            bot.set_webhook(url=full_webhook_url)
            print(f"Webhook configured successfully: {full_webhook_url}")
        except Exception as e:
            print(f"Failed to set webhook: {e}")
        
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
