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

def process_message_async(chat_id, user_text):
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
    try:
        json_data = request.get_json(silent=True)
        if json_data and 'message' in json_data:
            msg = json_data['message']
            chat_id = msg.get('chat', {}).get('id')
            user_text = msg.get('text')
            
            if chat_id and user_text:
                threading.Thread(target=process_message_async, args=(chat_id, user_text)).start()
    except Exception as e:
        print(f"Webhook parsing error: {e}")
        
    return '', 200

@app.route("/", methods=["GET"])
def index():
    return "Pro Replay Bot is running securely!", 200

# تهيئة قاعدة البيانات عند بدء التشغيل
init_db()
          
