import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
import telebot
import anthropic
import requests

# إعداد السجلات (Logging)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - personal_bot - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# المتغيرات البيئية
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
DEFAULT_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-3-haiku-20240307")
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """تهيئة الجداول وتحديث الهيكل القديم تلقائياً"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. جدول العملاء (البوتات)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                bot_token TEXT PRIMARY KEY,
                client_name TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                anthropic_key TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 2. جدول سجل المحادثات
        cur.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                bot_token TEXT,
                chat_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ترقية الجدول القديم تلقائياً بإضافة عمود bot_token إذا لم يكن موجوداً
        cur.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS bot_token TEXT;")
        
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ تمت تهيئة وتحديث قاعدة بيانات المنصة بنجاح.")
    except Exception as e:
        logger.error(f"❌ خطأ أثناء تهيئة قاعدة البيانات: {e}")

# تشغيل التهيئة عند بدء التطبيق
init_db()

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

def get_chat_history(bot_token, chat_id, limit=10):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT role, content FROM (
                SELECT id, role, content FROM chat_history
                WHERE bot_token = %s AND chat_id = %s
                ORDER BY id DESC LIMIT %s
            ) sub ORDER BY id ASC
        ''', (bot_token, chat_id, limit))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب السجل: {e}")
        return []

def get_client(bot_token):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM clients WHERE bot_token = %s", (bot_token,))
        client = cur.fetchone()
        cur.close()
        conn.close()
        return client
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب بيانات العميل: {e}")
        return None

@app.route("/", methods=["GET"])
def index():
    return "SaaS Multi-Tenant Bot Platform is Online!", 200

@app.route("/admin/add_client", methods=["GET", "POST"])
def add_client():
    secret = request.args.get("secret")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "غير مصرح لك بالدخول"}), 403

    token = request.args.get("token", "").strip()
    name = request.args.get("name", "عميل جديد").strip()
    prompt = request.args.get("prompt", "أنت مساعد ذكي ومفيد.").strip()
    api_key = request.args.get("api_key", DEFAULT_ANTHROPIC_KEY)

    if not token:
        return jsonify({"status": "error", "message": "توكن البوت مطلوب"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO clients (bot_token, client_name, system_prompt, anthropic_key, is_active)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (bot_token) DO UPDATE SET
                client_name = EXCLUDED.client_name,
                system_prompt = EXCLUDED.system_prompt,
                anthropic_key = EXCLUDED.anthropic_key,
                is_active = TRUE
        ''', (token, name, prompt, api_key))
        conn.commit()
        cur.close()
        conn.close()

        # ربط الويب هوك تلقائياً مع تيليجرام
        host_url = request.host_url.rstrip("/").replace("http://", "https://")
        webhook_url = f"{host_url}/webhook/{token}"
        tg_res = requests.get(
            f"https://api.telegram.org/bot{token}/setWebhook",
            params={"url": webhook_url}
        ).json()

        return jsonify({
            "status": "success",
            "message": f"تم تفعيل بوت ({name}) بنجاح!",
            "webhook_url": webhook_url,
            "telegram_webhook": tg_res
        }), 200
    except Exception as e:
        logger.error(f"❌ خطأ أثناء إضافة العميل: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/admin/toggle_client", methods=["GET"])
def toggle_client():
    secret = request.args.get("secret")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "غير مصرح لك بالدخول"}), 403

    token = request.args.get("token", "").strip()
    state = request.args.get("active", "true").lower() == "true"

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE clients SET is_active = %s WHERE bot_token = %s", (state, token))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success", "bot_token": token, "is_active": state}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/webhook/<bot_token>", methods=["POST"])
def webhook(bot_token):
    client_data = get_client(bot_token)
    if not client_data or not client_data.get("is_active"):
        return "Bot inactive or not found", 200

    update_json = request.get_json(silent=True)
    if not update_json:
        return "Invalid payload", 400

    update = telebot.types.Update.de_json(update_json)
    if not update.message or not update.message.text:
        return "OK", 200

    chat_id = update.message.chat.id
    user_text = update.message.text
    bot = telebot.TeleBot(bot_token)

    # الترحيب عند أمر /start
    if user_text.strip() == "/start":
        welcome_msg = f"أهلاً بك في {client_data['client_name']}! كيف يمكنني مساعدتك اليوم؟"
        bot.send_message(chat_id, welcome_msg)
        return "OK", 200

    # حفظ رسالة المستخدم وجلب السياق
    save_message(bot_token, chat_id, "user", user_text)
    history = get_chat_history(bot_token, chat_id, limit=10)

    api_key = client_data.get("anthropic_key") or DEFAULT_ANTHROPIC_KEY
    if not api_key:
        logger.error(f"❌ مفتاح Anthropic API مفقود للعميل: {client_data['client_name']}")
        bot.send_message(chat_id, "عذراً، الخدمة قيد الصيانة حالياً.")
        return "OK", 200

    try:
        bot.send_chat_action(chat_id, "typing")
        ai_client = anthropic.Anthropic(api_key=api_key)
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=client_data["system_prompt"],
            messages=history
        )
        reply_text = response.content[0].text
        save_message(bot_token, chat_id, "assistant", reply_text)
        bot.send_message(chat_id, reply_text)
    except Exception as e:
        logger.error(f"AI Error for {client_data['client_name']}: {e}")
        bot.send_message(chat_id, "عذراً، حدث خطأ تقني مؤقت. يرجى المحاولة بعد قليل.")

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
