import os
import logging
import requests
from flask import Flask, request, jsonify
import telebot
import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor

# إعداد السجلات
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
MASTER_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "admin123")

app = Flask(__name__)

# --- دوال قاعدة البيانات ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """تهيئة الجداول المركزية للعملاء والمحادثات"""
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL غير موجود!")
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                bot_token VARCHAR(255) PRIMARY KEY,
                client_name VARCHAR(255) NOT NULL,
                system_prompt TEXT NOT NULL,
                anthropic_key VARCHAR(255),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                bot_token VARCHAR(255) NOT NULL,
                chat_id BIGINT NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ تمت تهيئة قاعدة بيانات المنصة بنجاح.")
    except Exception as e:
        logger.error(f"❌ خطأ في تهيئة قاعدة البيانات: {e}")

def get_client_config(bot_token):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM clients WHERE bot_token = %s AND is_active = TRUE", (bot_token,))
        client = cur.fetchone()
        cur.close()
        conn.close()
        return client
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب إعدادات العميل: {e}")
        return None

def save_message(bot_token, chat_id, role, content):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_history (bot_token, chat_id, role, content) VALUES (%s, %s, %s, %s)",
            (bot_token, chat_id, role, content)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"❌ خطأ أثناء حفظ الرسالة: {e}")

def get_last_messages(bot_token, chat_id, limit=10):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT role, content FROM (
                SELECT id, role, content FROM chat_history 
                WHERE bot_token = %s AND chat_id = %s 
                ORDER BY id DESC LIMIT %s
            ) sub ORDER BY id ASC;
            """,
            (bot_token, chat_id, limit)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"role": row["role"], "content": row["content"]} for row in rows]
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب السجل: {e}")
        return []

init_db()

# --- أدوات الإدارة السريعة (لإضافة أو إيقاف العملاء بدون لمس الكود) ---

@app.route("/admin/add_client", methods=["GET", "POST"])
def add_client():
    """إضافة عميل جديد أو تحديث بياناته وربط الويب هوك تلقائياً"""
    secret = request.args.get("secret") or (request.json.get("secret") if request.is_json else None)
    if secret != ADMIN_SECRET:
        return jsonify({"error": "غير مصرح لك بالدخول"}), 403

    data = request.json if request.is_json else request.args
    bot_token = data.get("token")
    client_name = data.get("name")
    system_prompt = data.get("prompt")
    anthropic_key = data.get("api_key", "")

    if not all([bot_token, client_name, system_prompt]):
        return jsonify({"error": "يرجى توفير token و name و prompt"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clients (bot_token, client_name, system_prompt, anthropic_key, is_active)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (bot_token) DO UPDATE SET
                client_name = EXCLUDED.client_name,
                system_prompt = EXCLUDED.system_prompt,
                anthropic_key = EXCLUDED.anthropic_key,
                is_active = TRUE;
        """, (bot_token, client_name, system_prompt, anthropic_key or None))
        conn.commit()
        cur.close()
        conn.close()

        # ربط الويب هوك تلقائياً مع تيليجرام
        host_url = request.host_url.replace("http://", "https://").rstrip("/")
        webhook_url = f"{host_url}/webhook/{bot_token}"
        tg_res = requests.get(f"https://api.telegram.org/bot{bot_token}/setWebhook?url={webhook_url}").json()

        return jsonify({
            "status": "success",
            "message": f"تم تفعيل بوت العميل ({client_name}) بنجاح!",
            "telegram_webhook": tg_res
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/toggle_client", methods=["GET"])
def toggle_client():
    """إيقاف أو إعادة تفعيل بوت عميل معين (مثلاً عند انتهاء الفترة التجريبية)"""
    secret = request.args.get("secret")
    if secret != ADMIN_SECRET:
        return jsonify({"error": "غير مصرح لك بالدخول"}), 403

    bot_token = request.args.get("token")
    active = request.args.get("active", "false").lower() == "true"

    if not bot_token:
        return jsonify({"error": "يرجى توفير token"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE clients SET is_active = %s WHERE bot_token = %s", (active, bot_token))
        conn.commit()
        cur.close()
        conn.close()
        status_text = "مفعّل" if active else "متوقف (مقطوع)"
        return jsonify({"status": "success", "message": f"حالة البوت الآن: {status_text}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- مسار الويب هوك الديناميكي الموحد ---

@app.route("/webhook/<path:bot_token>", methods=["POST"])
def dynamic_webhook(bot_token):
    client_config = get_client_config(bot_token)
    if not client_config:
        return "Unauthorized or Inactive Bot", 403

    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        current_bot = telebot.TeleBot(bot_token, threaded=False)
        
        @current_bot.message_handler(commands=['start'])
        def handle_start(message):
            current_bot.reply_to(message, f"مرحباً بك في {client_config['client_name']}! كيف يمكنني مساعدتك اليوم؟")

        @current_bot.message_handler(func=lambda message: True)
        def handle_message(message):
            if not message.text:
                return
            chat_id = message.chat.id
            user_text = message.text

            api_key = client_config['anthropic_key'] or MASTER_ANTHROPIC_KEY
            if not api_key:
                current_bot.reply_to(message, "عذراً، خدمة الذكاء الاصطناعي غير متوفرة حالياً.")
                return

            anthropic_client = anthropic.Anthropic(api_key=api_key)

            save_message(bot_token, chat_id, "user", user_text)
            history = get_last_messages(bot_token, chat_id, limit=10)
            if not history:
                history = [{"role": "user", "content": user_text}]

            try:
                response = anthropic_client.messages.create(
                    model="claude-5.5-haiku", # تأكد من إبقاء نفس اسم النموذج الذي نجح معك
                    max_tokens=400,
                    system=client_config['system_prompt'],
                    messages=history
                )
                reply_text = response.content[0].text
                save_message(bot_token, chat_id, "assistant", reply_text)
                current_bot.reply_to(message, reply_text)
            except Exception as e:
                logger.error(f"AI Error for {client_config['client_name']}: {e}")
                current_bot.reply_to(message, "عذراً، حدث خطأ مؤقت، يرجى المحاولة بعد قليل.")

        update = telebot.types.Update.de_json(json_string)
        current_bot.process_new_updates([update])
        return "OK", 200

    return "Forbidden", 403

@app.route("/", methods=["GET"])
def index():
    return "SaaS Multi-Tenant Bot Platform is Online!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
