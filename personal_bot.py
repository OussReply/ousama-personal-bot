import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
from flask_cors import CORS
import telebot
import anthropic
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - omnichannel_saas - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # للسماح للمواقع الخارجية بالاتصال بـ API الدردشة

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
DEFAULT_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "ousama_verify_2026")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # إنشاء الجدول الأساسي أو ترقيته لدعم تعدد المنصات
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                bot_token TEXT PRIMARY KEY,
                client_name TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                anthropic_key TEXT,
                platform TEXT DEFAULT 'telegram',
                extra_token TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # ترقية الجداول القديمة تلقائياً دون فقدان البيانات السابقة
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS platform TEXT DEFAULT 'telegram';")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS extra_token TEXT;")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                bot_token TEXT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS bot_token TEXT;")
        # تحويل chat_id إلى TEXT ليدعم أرقام الواتساب ومعرفات المواقع وتيليجرام معاً
        cur.execute("ALTER TABLE chat_history ALTER COLUMN chat_id TYPE TEXT USING chat_id::TEXT;")

        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ تمت تهيئة وتحديث قاعدة البيانات متعددة القنوات بنجاح.")
    except Exception as e:
        logger.error(f"❌ خطأ أثناء تهيئة قاعدة البيانات: {e}")

init_db()

def save_message(bot_id, chat_id, role, content):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_history (bot_token, chat_id, role, content) VALUES (%s, %s, %s, %s)",
            (str(bot_id), str(chat_id), role, content)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"❌ خطأ أثناء حفظ الرسالة: {e}")

def get_chat_history(bot_id, chat_id, limit=10):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT role, content FROM (
                SELECT id, role, content FROM chat_history
                WHERE bot_token = %s AND chat_id = %s
                ORDER BY id DESC LIMIT %s
            ) sub ORDER BY id ASC
        ''', (str(bot_id), str(chat_id), limit))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب السجل: {e}")
        return []

def get_client(bot_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM clients WHERE bot_token = %s", (str(bot_id),))
        client = cur.fetchone()
        cur.close()
        conn.close()
        return client
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب بيانات العميل: {e}")
        return None

# --- العقل الذكي الموحد لجميع المنصات ---
def generate_ai_reply(client_data, chat_id, user_text):
    bot_id = client_data["bot_token"]
    save_message(bot_id, chat_id, "user", user_text)
    history = get_chat_history(bot_id, chat_id, limit=10)

    api_key = client_data.get("anthropic_key") or DEFAULT_ANTHROPIC_KEY
    if not api_key:
        return "عذراً، الخدمة قيد الصيانة حالياً."

    try:
        ai_client = anthropic.Anthropic(api_key=api_key)
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=client_data["system_prompt"],
            messages=history
        )
        reply_text = response.content[0].text
        save_message(bot_id, chat_id, "assistant", reply_text)
        return reply_text
    except Exception as e:
        logger.error(f"AI Error for {client_data['client_name']}: {e}")
        return "عذراً، حدث خطأ تقني مؤقت. يرجى المحاولة بعد قليل."

# --- واجهة لوحة التحكم HTML ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>لوحة إدارة المنصة متعددة القنوات (Omnichannel SaaS)</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 960px; margin: auto; }
        h1, h2 { color: #38bdf8; }
        .card { background: #1e293b; padding: 20px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        label { display: block; margin-bottom: 6px; font-weight: bold; color: #cbd5e1; }
        input, textarea, select { width: 100%; padding: 10px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; font-family: inherit; }
        button { background: #0284c7; color: white; border: none; padding: 10px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        button:hover { background: #0369a1; }
        .btn-danger { background: #dc2626; }
        .btn-success { background: #16a34a; }
        .btn-edit { background: #d97706; margin-left: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 12px; text-align: right; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; }
        .badge { padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; }
        .active { background: #14532d; color: #4ade80; }
        .inactive { background: #7f1d1d; color: #f87171; }
        .plat-badge { background: #1e3a8a; color: #93c5fd; padding: 3px 8px; border-radius: 6px; font-size: 12px; }
        .alert { background: #065f46; color: #a7f3d0; padding: 12px; border-radius: 8px; margin-bottom: 20px; }
        .hint { font-size: 12px; color: #94a3b8; margin-top: -10px; margin-bottom: 15px; }
    </style>
</head>
<body>
<div class="container">
    <h1>🚀 لوحة إدارة المنصة الذكية (متعددة القنوات)</h1>
    {% if msg %}
    <div class="alert">{{ msg }}</div>
    {% endif %}

    <div class="card">
        <h2>➕ إضافة أو تحديث بوت عميل</h2>
        <form method="POST" action="/admin?secret={{ secret }}">
            <label>المنصة / القناة المستهدفة:</label>
            <select id="platform" name="platform" onchange="updateLabels()">
                <option value="telegram">تيليجرام (Telegram Bot)</option>
                <option value="whatsapp">واتساب الرسمي (WhatsApp Cloud API)</option>
                <option value="web">موقع إلكتروني / متجر (Web Chat API)</option>
            </select>

            <label>اسم العميل أو المشروع:</label>
            <input type="text" id="name" name="name" placeholder="مثال: شركة الأفق أو متجر الأناقة" required>

            <label id="token_label">توكن البوت (من BotFather):</label>
            <input type="text" id="token" name="token" placeholder="123456789:ABCdef..." required>
            <div id="token_hint" class="hint">في تيليجرام: ضع التوكن. في واتساب: ضع Phone Number ID. في المواقع: اكتب أي معرف إنجليزي فريد مثل store_1.</div>

            <div id="extra_token_div" style="display:none;">
                <label>مفتاح الوصول الدائم لواتساب (Meta Permanent Access Token):</label>
                <input type="text" id="extra_token" name="extra_token" placeholder="EAABsbCS1iHgBO...">
            </div>

            <label>شخصية وتعليمات البوت (System Prompt):</label>
            <textarea id="prompt" name="prompt" rows="4" placeholder="اكتب دور البوت، أسلوبه، والمنتجات أو الخدمات..." required></textarea>

            <label>مفتاح Anthropic API خاص بالعميل (اختياري):</label>
            <input type="text" id="api_key" name="api_key" placeholder="sk-ant-...">

            <button type="submit">حفظ وتفعيل البوت 🚀</button>
        </form>
    </div>

    <div class="card">
        <h2>📋 البوتات المسجلة ({{ clients|length }})</h2>
        <table>
            <thead>
                <tr>
                    <th>المشروع</th>
                    <th>المنصة</th>
                    <th>المعرف / التوكن</th>
                    <th>الحالة</th>
                    <th>الإجراءات</th>
                </tr>
            </thead>
            <tbody>
                {% for c in clients %}
                <tr>
                    <td><strong>{{ c.client_name }}</strong></td>
                    <td><span class="plat-badge">{{ c.platform or 'telegram' }}</span></td>
                    <td style="font-family: monospace; font-size: 12px;">{{ c.bot_token[:14] }}...</td>
                    <td>
                        {% if c.is_active %}
                        <span class="badge active">نشط</span>
                        {% else %}
                        <span class="badge inactive">متوقف</span>
                        {% endif %}
                    </td>
                    <td>
                        <button type="button" class="btn-edit" onclick="fillForm('{{ c.client_name }}', '{{ c.bot_token }}', '{{ c.platform or 'telegram' }}', '{{ c.extra_token or '' }}', `{{ c.system_prompt }}`)">تعديل</button>
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
    function updateLabels() {
        const plat = document.getElementById('platform').value;
        const extraDiv = document.getElementById('extra_token_div');
        const label = document.getElementById('token_label');
        if (plat === 'whatsapp') {
            extraDiv.style.display = 'block';
            label.innerText = 'معرف رقم هاتف واتساب (Phone Number ID):';
        } else if (plat === 'web') {
            extraDiv.style.display = 'none';
            label.innerText = 'المعرف الفريد للموقع (Site ID - بالإنجليزية بدون مسافات):';
        } else {
            extraDiv.style.display = 'none';
            label.innerText = 'توكن البوت (من BotFather):';
        }
    }
    function fillForm(name, token, platform, extraToken, prompt) {
        document.getElementById('name').value = name;
        document.getElementById('token').value = token;
        document.getElementById('platform').value = platform;
        document.getElementById('extra_token').value = extraToken;
        document.getElementById('prompt').value = prompt;
        updateLabels();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return "Omnichannel Multi-Tenant AI SaaS Platform is Online!", 200

@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "⛔ غير مصرح لك بالدخول.", 403

    msg = request.args.get("msg", "")

    if request.method == "POST":
        platform = request.form.get("platform", "telegram").strip()
        token = request.form.get("token", "").strip()
        name = request.form.get("name", "عميل جديد").strip()
        prompt = request.form.get("prompt", "أنت مساعد ذكي.").strip()
        extra_token = request.form.get("extra_token", "").strip()
        api_key = request.form.get("api_key", "").strip() or DEFAULT_ANTHROPIC_KEY

        if token:
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO clients (bot_token, client_name, system_prompt, anthropic_key, platform, extra_token, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (bot_token) DO UPDATE SET
                        client_name = EXCLUDED.client_name,
                        system_prompt = EXCLUDED.system_prompt,
                        anthropic_key = EXCLUDED.anthropic_key,
                        platform = EXCLUDED.platform,
                        extra_token = EXCLUDED.extra_token,
                        is_active = TRUE
                ''', (token, name, prompt, api_key, platform, extra_token))
                conn.commit()
                cur.close()
                conn.close()

                host_url = request.host_url.rstrip("/").replace("http://", "https://")

                if platform == "telegram":
                    webhook_url = f"{host_url}/webhook/{token}"
                    tg_res = requests.get(
                        f"https://api.telegram.org/bot{token}/setWebhook",
                        params={"url": webhook_url}
                    ).json()
                    if tg_res.get("ok"):
                        msg = f"✅ تم حفظ وتفعيل بوت تيليجرام ({name}) بنجاح!"
                    else:
                        msg = f"⚠️ تم الحفظ، ملاحظة تيليجرام: {tg_res.get('description')}"
                elif platform == "whatsapp":
                    msg = f"✅ تم تفعيل بوت واتساب ({name})! رابط Webhook الخاص بـ Meta هو: {host_url}/webhook/whatsapp"
                elif platform == "web":
                    msg = f"✅ تم تفعيل بوت الموقع ({name})! رابط API الدردشة هو: {host_url}/api/webchat/{token}"
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

# ================= 1. قناة تيليجرام =================
@app.route("/webhook/<bot_token>", methods=["POST"])
def telegram_webhook(bot_token):
    client_data = get_client(bot_token)
    if not client_data or not client_data.get("is_active"):
        return "Bot inactive", 200

    update_json = request.get_json(silent=True)
    if not update_json:
        return "Invalid payload", 400

    update = telebot.types.Update.de_json(update_json)
    if not update.message or not update.message.text:
        return "OK", 200

    chat_id = str(update.message.chat.id)
    user_text = update.message.text
    bot = telebot.TeleBot(bot_token)

    if user_text.strip() == "/start":
        bot.send_message(chat_id, f"أهلاً بك في {client_data['client_name']}! كيف يمكنني مساعدتك اليوم؟")
        return "OK", 200

    bot.send_chat_action(chat_id, "typing")
    reply_text = generate_ai_reply(client_data, chat_id, user_text)
    bot.send_message(chat_id, reply_text)
    return "OK", 200

# ================= 2. قناة واتساب الرسمي (Meta Cloud API) =================
@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    # التحقق من Webhook عند الربط الأول في لوحة مطوري Meta
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    data = request.get_json(silent=True)
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" in entry:
            phone_number_id = entry["metadata"]["phone_number_id"]
            message = entry["messages"][0]
            if message.get("type") == "text":
                sender_phone = message["from"]
                user_text = message["text"]["body"]

                client_data = get_client(phone_number_id)
                if client_data and client_data.get("is_active") and client_data.get("extra_token"):
                    reply_text = generate_ai_reply(client_data, sender_phone, user_text)
                    wa_url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
                    headers = {
                        "Authorization": f"Bearer {client_data['extra_token']}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "messaging_product": "whatsapp",
                        "to": sender_phone,
                        "text": {"body": reply_text}
                    }
                    requests.post(wa_url, headers=headers, json=payload)
    except Exception as e:
        logger.error(f"WhatsApp Webhook Error: {e}")

    return "OK", 200

# ================= 3. قناة المواقع الإلكترونية والمتاجر (Web Chat API) =================
@app.route("/api/webchat/<site_id>", methods=["POST"])
def webchat_api(site_id):
    client_data = get_client(site_id)
    if not client_data or not client_data.get("is_active"):
        return jsonify({"error": "Bot inactive or not found"}), 404

    data = request.get_json(silent=True) or {}
    user_text = data.get("message", "").strip()
    session_id = data.get("session_id", "web_guest")

    if not user_text:
        return jsonify({"error": "Empty message"}), 400

    reply_text = generate_ai_reply(client_data, session_id, user_text)
    return jsonify({"reply": reply_text}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
        
