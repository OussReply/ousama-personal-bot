import os
import traceback
import telebot
from flask import Flask, request
import anthropic
import psycopg2

# Initialize environment variables
TOKEN = "9208308526:AAHmjnPi6cVizlIAa5u3IkDAu0b_hYjZAqw"
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATABASE_URL = "postgresql://postgres:gQUEiIIrxzzouLVuCygKsJniAsbwvLaK@postgres.railway.internal:5432/railway"
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("CRITICAL: DATABASE_URL environment variable is missing or empty!")
    print(f"Connecting to database using URL starting with: {DATABASE_URL[:15]}...")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

def save_message_to_db(chat_id, role, content):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (%s, %s, %s)",
            (chat_id, role, content)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Database error: {e}")

def envoyer_message_long(chat_id, text):
    max_length = 4096
    for i in range(0, len(text), max_length):
        bot.send_message(chat_id, text[i:i+max_length])

@app.route(f"/{TOKEN}", methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        return 'Forbidden', 403
        
@bot.message_handler(func=lambda msg: True)
def echo_all(message):
    user_text = message.text
    # رد مؤقت للتأكد من أن البوت استقبل الرسالة ورد عليها
    bot.reply_to(message, f"أهلاً بك! لقد استلمت رسالتك: {user_text}")
    
@app.route("/", methods=["GET"])
def index():
    return "Pro Replay Bot is running!", 200

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    user_text = message.text
    
    try:
        save_message_to_db(chat_id, "user", user_text)
        
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            messages=[{"role": "user", "content": user_text}]
        )
        
        reply_texte = response.content[0].text
        save_message_to_db(chat_id, "assistant", reply_texte)
        envoyer_message_long(chat_id, reply_texte)
        
    except Exception as e:
        print("CRITICAL ERROR DURING CLAUDE CALL:")
        print(traceback.format_exc())
        bot.reply_to(message, "Sorry, I am experiencing temporary system pressure. Please try sending your message again in a moment.")

if __name__ == "__main__":
    init_db()
    
    if WEBHOOK_URL:
        full_webhook_url = f"{WEBHOOK_URL.strip('/')}/{TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=full_webhook_url)
        print(f"Webhook configured successfully: {full_webhook_url}")
        
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
