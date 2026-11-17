import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
import telebot
import anthropic
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - personal_bot - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
DEFAULT_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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
        cur.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS bot_token TEXT;")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ تمت تهيئة وتحديث قاعدة بيانات المنصة بنجاح.")
    except Exception as e:
        logger.error(f"❌ خطأ أثناء تهيئة قاعدة البيانات: {e}")

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

# --- واجهة لوحة التحكم HTML ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>لوحة إدارة منصة البوتات الذكية</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: auto; }
        h1, h2 { color: #38bdf8; }
        .card { background: #1e293b; padding: 20px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        label { display: block; margin-bottom: 6px; font-weight: bold; color: #cbd5e1; }
        input, textarea { width: 100%; padding: 10px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; font-family: inherit; }
        button { background: #0284c7; color: white; border: none; padding: 10px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        button:hover { background: #0369a1; }
        .btn-danger { background: #dc2626; }
        .btn-danger:hover { background: #b91c1c; }
        .btn-success { background: #16a34a; }
        .btn-success:hover { background: #15803d; }
        .btn-edit { background: #d97706; margin-left: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 12px; text-align: right; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; }
        .badge { padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; }
        .active { background: #14532d; color: #4ade80; }
        .inactive { background: #7f1d1d; color: #f87171; }
        .alert { background: #065f46; color: #a7f3d0; padding: 12px; border-radius: 8px; margin-bottom: 20px; }
    </style>
</head>
<body>
<div class="container">
    <h1>🤖 لوحة إدارة منصة البوتات (SaaS)</h1>
    {% if msg %}
    <div class="alert">{{ msg }}</div>
    {% endif %}

    <div class="card">
        <h2>➕ إضافة بوت جديد أو تحديث بوت حالي</h2>
        <form method="POST" action="/admin?secret={{ secret }}">
            <label>اسم العميل أو المشروع:</label>
            <input type="text" id="name" name="name" placeholder="مثال: متجر الأناقة أو خدمات المهندس أسامة" required>

            <label>توكن البوت (من BotFather):</label>
            <input type="text" id="token" name="token" placeholder="123456789:ABCdefGHIjklMNO..." required>

            <label>شخصية وتعليمات البوت (System Prompt):</label>
            <textarea id="prompt" name="prompt" rows="4" placeholder="اكتب هنا دور البوت، أسلوب رده، والخدمات أو الأسعار التي يقدمها..." required></textarea>

            <label>مفتاح Anthropic API خاص بالعميل (اتركه فارغاً لاستخدام مفتاح المنصة الافتراضي):</label>
            <input type="text" id="api_key" name="api_key" placeholder="sk-ant-...">

            <button type="submit">حفظ وتفعيل البوت على تيليجرام 🚀</button>
        </form>
    </div>

    <div class="card">
        <h2>📋 البوتات المسجلة في المنصة ({{ clients|length }})</h2>
        <table>
            <thead>
                <tr>
                    <th>اسم المشروع</th>
                    <th>التوكن</th>
                    <th>الحالة</th>
                    <th>الإجراءات</th>
                </tr>
            </thead>
            <tbody>
                {% for c in clients %}
                <tr>
                    <td><strong>{{ c.client_name }}</strong></td>
                    <td style="font-family: monospace; font-size: 12px;">{{ c.bot_token[:12] }}...</td>
                    <td>
                        {% if c.is_active %}
                        <span class="badge active">نشط</span>
                        {% else %}
                        <span class="badge inactive">متوقف</span>
                        {% endif %}
                    </td>
                    <td>
                        <button type="button" class="btn-edit" onclick="fillForm('{{ c.client_name }}', '{{ c.bot_token }}', `{{ c.system_prompt }}`)">تعديل</button>
                        <form method="POST" action="/admin/toggle?secret={{ secret }}" style="display:inline;">
                            <input type="hidden" name="token" value="{{ c.bot_token }}">
                            <input type="hidden" name="active" value="{{ 'false' if c.is_active else 'true' }}">
                            {% if c.is_active %}
                            <button type="submit" class="btn-danger">إيقاف</button>
                            {% else %}
                            <button type="submit" class="btn-success">تفعيل</button>
                            {% endif %}
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
<script>
    function fillForm(name, token, prompt) {
        document.getElementById('name').value = name;
        document.getElementById('token').value = token;
        document.getElementById('prompt').value = prompt;
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return "SaaS Multi-Tenant Bot Platform is Online!", 200

@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "⛔ غير مصرح لك بالدخول. تأكد من كتابة ?secret=كلمة_السر في الرابط.", 403

    msg = request.args.get("msg", "")

    if request.method == "POST":
        token = request.form.get("token", "").strip()
        name = request.form.get("name", "عميل جديد").strip()
        prompt = request.form.get("prompt", "أنت مساعد ذكي.").strip()
        api_key = request.form.get("api_key", "").strip() or DEFAULT_ANTHROPIC_KEY

        if token:
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

                host_url = request.host_url.rstrip("/").replace("http://", "https://")
                webhook_url = f"{host_url}/webhook/{token}"
                tg_res = requests.get(
                    f"https://api.telegram.org/bot{token}/setWebhook",
                    params={"url": webhook_url}
                ).json()

                if tg_res.get("ok"):
                    msg = f"✅ تم حفظ وتفعيل بوت ({name}) وربطه بتيليجرام بنجاح!"
                else:
                    msg = f"⚠️ تم حفظ البيانات، لكن تيليجرام أرجع ملاحظة: {tg_res.get('description')}"
            except Exception as e:
                msg = f"❌ حدث خطأ: {e}"

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM clients ORDER BY created_at DESC")
    clients = cur.fetchall()
    cur.close()
    conn.close()

    return render_template_string(DASHBOARD_HTML, clients=clients, secret=secret, msg=msg)

@app.route("/admin/toggle", methods=["POST"])
def admin_toggle():
    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "غير مصرح", 403

    token = request.form.get("token", "").strip()
    state = request.form.get("active", "true").lower() == "true"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE clients SET is_active = %s WHERE bot_token = %s", (state, token))
    conn.commit()
    cur.close()
    conn.close()

    status_text = "تفعيل" if state else "إيقاف"
    return redirect(url_for("admin_dashboard", secret=secret, msg=f"تم {status_text} البوت بنجاح."))

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

    if user_text.strip() == "/start":
        welcome_msg = f"أهلاً بك في {client_data['client_name']}! كيف يمكنني مساعدتك اليوم؟"
        bot.send_message(chat_id, welcome_msg)
        return "OK", 200

    save_message(bot_token, chat_id, "user", user_text)
    history = get_chat_history(bot_token, chat_id, limit=10)

    api_key = client_data.get("anthropic_key") or DEFAULT_ANTHROPIC_KEY
    if not api_key:
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
