import os
import io
import csv
import json
import secrets
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, request, render_template_string, redirect, url_for, session, jsonify, Response
from flask_cors import CORS
import telebot
from telebot import types
import anthropic
import requests

# --- إعداد السجلات ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - omnibot_saas - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# --- متغيرات البيئة ---
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
ALERT_BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "").strip()
DEFAULT_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "ousama_verify_2026")

# --- مجمع اتصالات PostgreSQL ---
db_pool = None
if DATABASE_URL:
    try:
        db_pool = ThreadedConnectionPool(minconn=1, maxconn=20, dsn=DATABASE_URL)
        logger.info("ThreadedConnectionPool initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to create Connection Pool: {e}")


def get_db_connection():
    if db_pool:
        return db_pool.getconn()
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    return psycopg2.connect(DATABASE_URL)


def release_db_connection(conn):
    if db_pool and conn:
        try:
            db_pool.putconn(conn)
        except Exception as e:
            logger.error(f"Error returning connection to pool: {e}")
    elif conn:
        try:
            conn.close()
        except Exception:
            pass


def init_db():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                bot_token TEXT PRIMARY KEY,
                client_name TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                welcome_message TEXT,
                notify_chat_id TEXT,
                auto_replies TEXT DEFAULT '{}',
                quick_buttons TEXT DEFAULT '[]',
                anthropic_key TEXT,
                platform TEXT DEFAULT 'telegram',
                extra_token TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS platform TEXT DEFAULT 'telegram';")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS extra_token TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS welcome_message TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS notify_chat_id TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS auto_replies TEXT DEFAULT '{}';")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS quick_buttons TEXT DEFAULT '[]';")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                bot_token TEXT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS bot_token TEXT;")
        cur.execute("ALTER TABLE chat_history ALTER COLUMN chat_id TYPE TEXT USING chat_id::TEXT;")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                bot_token TEXT NOT NULL,
                client_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                sender_chat_id TEXT NOT NULL,
                sender_label TEXT,
                message_text TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS is_read BOOLEAN DEFAULT FALSE;")

        conn.commit()
        cur.close()
        logger.info("Database schema ready.")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database init error: {e}")
    finally:
        release_db_connection(conn)


init_db()


# --- دوال قاعدة البيانات ---
def save_message(bot_id, chat_id, role, content):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_history (bot_token, chat_id, role, content) VALUES (%s, %s, %s, %s)",
            (str(bot_id), str(chat_id), str(role), str(content))
        )
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error saving message: {e}")
    finally:
        release_db_connection(conn)


def get_chat_history(bot_id, chat_id, limit=12):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT role, content FROM (
                SELECT id, role, content FROM chat_history
                WHERE bot_token = %s AND chat_id = %s
                ORDER BY id DESC LIMIT %s
            ) sub ORDER BY id ASC
        """, (str(bot_id), str(chat_id), int(limit)))
        rows = cur.fetchall()
        cur.close()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        logger.error(f"Error getting chat history: {e}")
        return []
    finally:
        release_db_connection(conn)


def clear_user_history(bot_id, chat_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM chat_history WHERE bot_token = %s AND chat_id = %s",
            (str(bot_id), str(chat_id))
        )
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error clearing user history: {e}")
    finally:
        release_db_connection(conn)


def get_client(bot_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM clients WHERE bot_token = %s", (str(bot_id),))
        client = cur.fetchone()
        cur.close()
        return client
    except Exception as e:
        logger.error(f"Error fetching client: {e}")
        return None
    finally:
        release_db_connection(conn)


def save_lead(bot_token, client_name, platform, sender_chat_id, sender_label, message_text):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO leads (bot_token, client_name, platform, sender_chat_id, sender_label, message_text, is_read)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE)
        """, (
            str(bot_token),
            str(client_name),
            str(platform),
            str(sender_chat_id),
            str(sender_label),
            str(message_text)
        ))
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error saving lead: {e}")
    finally:
        release_db_connection(conn)


# --- دالة الإرسال إلى chat_id ---
def send_to_chat_id(bot_token, target_chat_id, text):
    if not bot_token or not target_chat_id:
        return False
    try:
        bot = telebot.TeleBot(str(bot_token).strip())
        bot.send_message(str(target_chat_id).strip(), str(text))
        return True
    except Exception as e:
        logger.error(f"Error sending to chat_id ({target_chat_id}): {e}")
        return False


def resolve_target_chat_id(client_data):
    client_chat = (client_data.get("notify_chat_id") or "").strip()
    return client_chat or ADMIN_CHAT_ID


def notify_telegram_target(client_data, platform, sender_id, sender_label, user_text, is_urgent=False):
    target_chat_id = resolve_target_chat_id(client_data)
    if not target_chat_id:
        return False

    alert_token = ALERT_BOT_TOKEN if ALERT_BOT_TOKEN else (
        client_data["bot_token"] if platform == "telegram" else ""
    )
    if not alert_token:
        return False

    badge = "🚨 طلب تدخل / إشعار عاجل" if is_urgent else "🔔 إشعار جديد: وصلتك رسالة"
    msg = (
        f"{badge} ({str(platform).upper()})\n"
        f"• المشروع: {client_data['client_name']}\n"
        f"• المرسل: {sender_label}\n"
        f"• المعرف: {sender_id}\n"
        f"• النص: {user_text}"
    )
    return send_to_chat_id(alert_token, target_chat_id, msg)


def handle_incoming_notification(client_data, platform, sender_id, sender_label, user_text, is_urgent=False):
    save_lead(client_data["bot_token"], client_data["client_name"], platform, sender_id, sender_label, user_text)
    notify_telegram_target(client_data, platform, sender_id, sender_label, user_text, is_urgent=is_urgent)


# --- أزرار بوت تيليجرام ---
def build_telegram_keyboard(client_data):
    raw_btns = client_data.get("quick_buttons") or "[]"
    try:
        btn_list = json.loads(raw_btns) if isinstance(raw_btns, str) else raw_btns
    except Exception:
        btn_list = []

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    if isinstance(btn_list, list) and len(btn_list) > 0:
        buttons = [types.KeyboardButton(str(b).strip()) for b in btn_list if str(b).strip()]
        if buttons:
            markup.add(*buttons)
    markup.add(
        types.KeyboardButton("📞 تواصل مع موظف"),
        types.KeyboardButton("🔄 محادثة جديدة")
    )
    return markup


# --- العقل الذكي الموحد ---
def match_auto_reply(client_data, user_text):
    raw_rules = client_data.get("auto_replies") or "{}"
    try:
        rules = json.loads(raw_rules) if isinstance(raw_rules, str) else raw_rules
        if not isinstance(rules, dict):
            return None
        lower_text = user_text.strip().lower()
        for keyword, reply in rules.items():
            kw = str(keyword).strip().lower()
            if kw and kw in lower_text:
                return str(reply)
    except Exception:
        pass
    return None


def generate_ai_reply(client_data, chat_id, user_text):
    bot_id = client_data["bot_token"]
    save_message(bot_id, chat_id, "user", user_text)

    auto_msg = match_auto_reply(client_data, user_text)
    if auto_msg:
        save_message(bot_id, chat_id, "assistant", auto_msg)
        return auto_msg

    history = get_chat_history(bot_id, chat_id, limit=12)
    api_key = (client_data.get("anthropic_key") or "").strip() or DEFAULT_ANTHROPIC_KEY
    if not api_key:
        return "عذراً، الخدمة قيد التهيئة حالياً."

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
        logger.error(f"AI Error ({client_data['client_name']}): {e}")
        return "عذراً، حدث خطأ تقني مؤقت. يرجى المحاولة مرة أخرى."


def is_authorized():
    if not ADMIN_SECRET:
        return False
    if session.get("is_admin"):
        return True
    if request.args.get("secret", "") == ADMIN_SECRET or request.headers.get("X-Admin-Secret", "") == ADMIN_SECRET:
        session["is_admin"] = True
        return True
    return False


def set_tg_webhook(bot_token, host_url):
    webhook_url = f"{host_url}/webhook/{bot_token}"
    res = requests.get(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        params={"url": webhook_url},
        timeout=10
    )
    return res.json()


# ================= واجهة لوحة التحكم الشاملة =================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OmniBot SaaS — لوحة القيادة والإشعارات</title>
    <style>
        :root { --bg: #0b1120; --card: #1e293b; --border: #334155; --primary: #0284c7; --accent: #38bdf8; }
        body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: #f8fafc; margin: 0; padding: 18px; }
        .container { max-width: 1200px; margin: auto; }
        h1, h2, h3 { color: var(--accent); margin-top: 0; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; background: var(--card); padding: 15px 20px; border-radius: 12px; border: 1px solid var(--border); }
        .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .live-banner { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; background: linear-gradient(90deg, #172554, #1e293b); border: 1px solid #38bdf8; padding: 14px 18px; border-radius: 12px; margin-bottom: 18px; }
        .live-banner.has-unread { background: linear-gradient(90deg, #7c2d12, #1e293b); border-color: #fb923c; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 20px; }
        .stat-box { background: var(--card); padding: 16px; border-radius: 12px; border-right: 4px solid var(--accent); border-top: 1px solid var(--border); }
        .stat-num { font-size: 26px; font-weight: bold; color: #fff; }
        .stat-label { font-size: 13px; color: #94a3b8; margin-top: 4px; }
        .card { background: var(--card); padding: 20px; border-radius: 12px; margin-bottom: 22px; border: 1px solid var(--border); box-shadow: 0 4px 12px rgba(0,0,0,0.25); }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        @media (max-width: 768px) { .grid-2 { grid-template-columns: 1fr; } }
        label { display: block; margin-bottom: 6px; font-weight: 600; color: #cbd5e1; font-size: 14px; }
        input, textarea, select { width: 100%; padding: 10px 12px; margin-bottom: 14px; border-radius: 8px; border: 1px solid var(--border); background: #0f172a; color: #fff; box-sizing: border-box; font-family: inherit; }
        input:focus, textarea:focus, select:focus { outline: none; border-color: var(--accent); }
        .btn { background: var(--primary); color: white; border: none; padding: 9px 14px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px; text-decoration: none; display: inline-flex; align-items: center; gap: 5px; }
        .btn:hover { opacity: 0.92; }
        .btn-lg { padding: 12px 22px; font-size: 15px; }
        .btn-success { background: #16a34a; }
        .btn-danger { background: #dc2626; }
        .btn-warn { background: #d97706; }
        .btn-purple { background: #7c3aed; }
        .btn-dark { background: #334155; }
        .actions-cell { display: flex; flex-wrap: wrap; gap: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 11px; text-align: right; border-bottom: 1px solid var(--border); vertical-align: middle; font-size: 13px; }
        th { color: #94a3b8; background: #0f172a; }
        tr.unread-row { background: rgba(251, 146, 60, 0.12); }
        .badge { padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .active { background: #14532d; color: #4ade80; }
        .inactive { background: #7f1d1d; color: #f87171; }
        .unread-badge { background: #ea580c; color: #fff; }
        .read-badge { background: #334155; color: #cbd5e1; }
        .plat-badge { background: #1e3a8a; color: #93c5fd; padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .alert { background: #065f46; color: #a7f3d0; padding: 13px 16px; border-radius: 8px; margin-bottom: 18px; border: 1px solid #10b981; }
        .hint { font-size: 12px; color: #94a3b8; margin-top: -10px; margin-bottom: 14px; }
        .modal { display: none; position: fixed; z-index: 999; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.75); }
        .modal-content { background: var(--card); margin: 8% auto; padding: 22px; border: 1px solid var(--border); width: 90%; max-width: 660px; border-radius: 12px; }
        pre { background: #0f172a; padding: 12px; border-radius: 8px; overflow-x: auto; direction: ltr; text-align: left; color: #38bdf8; font-size: 12px; }
    </style>
</head>
<body>
<div class="container">
    <div class="top-bar">
        <div>
            <h1 style="margin:0; font-size: 21px;">🚀 OmniBot SaaS — لوحة الإدارة والإشعارات</h1>
            <span style="font-size: 12px; color: #94a3b8;">إشعارات فورية في الأدمن + تنبيهات تيليجرام عبر send_to_chat_id</span>
        </div>
        <div class="toolbar">
            <form method="POST" action="/admin/test-alert" style="margin:0;">
                <button type="submit" class="btn btn-purple">🔔 اختبار إشعار (أدمن + تيليجرام)</button>
            </form>
            <a href="/admin/export-leads" class="btn btn-success">📥 تصدير CSV</a>
            <button type="button" class="btn btn-dark" onclick="resetForm()">🧹 تفريغ الحقول</button>
            <a href="/admin/logout" class="btn btn-danger">🚪 خروج</a>
        </div>
    </div>

    <div id="liveBanner" class="live-banner {% if unread_count > 0 %}has-unread{% endif %}">
        <div>
            <strong id="liveBannerTitle">
                {% if unread_count > 0 %}
                    🔔 تنبيه: لديك {{ unread_count }} إشعار/رسالة جديدة غير مقروءة وصلتك!
                {% else %}
                    ✅ مركز الإشعارات نشط — لا توجد إشعارات جديدة غير مقروءة حالياً.
                {% endif %}
            </strong>
            <div id="liveBannerSub" style="font-size:12px; color:#cbd5e1; margin-top:3px;">
                يتم فحص وصول الإشعارات الجديدة تلقائياً كل 10 ثوانٍ وتنبيهك فوراً.
            </div>
        </div>
        <div class="toolbar">
            <form method="POST" action="/admin/mark-read" style="margin:0;">
                <button type="submit" class="btn btn-warn">✔️ تحديد الكل كمقروء</button>
            </form>
            <button type="button" class="btn" onclick="location.reload()">🔄 تحديث الآن</button>
        </div>
    </div>

    {% if msg %}
    <div class="alert">{{ msg }}</div>
    {% endif %}

    <div class="stats-grid">
        <div class="stat-box">
            <div class="stat-num" id="statUnread">{{ unread_count }}</div>
            <div class="stat-label">🔔 إشعارات جديدة غير مقروءة</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">{{ clients|length }}</div>
            <div class="stat-label">🤖 إجمالي البوتات</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">{{ active_count }}</div>
            <div class="stat-label">🟢 البوتات النشطة</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">{{ total_messages }}</div>
            <div class="stat-label">💬 الرسائل المحفوظة</div>
        </div>
    </div>

    <div class="card">
        <h2>🔔 صندوق الإشعارات والرسائل الواردة ({{ leads|length }})</h2>
        <div style="overflow-x:auto;">
        <table>
            <thead>
                <tr>
                    <th>الحالة</th>
                    <th>المشروع</th>
                    <th>القناة</th>
                    <th>المرسل / المعرف</th>
                    <th>نص الإشعار أو الرسالة</th>
                    <th>التوقيت</th>
                </tr>
            </thead>
            <tbody>
                {% for l in leads[:40] %}
                <tr class="{% if not l.is_read %}unread-row{% endif %}">
                    <td>
                        {% if not l.is_read %}
                        <span class="badge unread-badge">🆕 جديد</span>
                        {% else %}
                        <span class="badge read-badge">مقروء</span>
                        {% endif %}
                    </td>
                    <td><strong>{{ l.client_name }}</strong></td>
                    <td><span class="plat-badge">{{ (l.platform or 'telegram')|upper }}</span></td>
                    <td style="font-size:12px;">{{ l.sender_label or 'مستخدم' }}<br><code>{{ l.sender_chat_id }}</code></td>
                    <td>{{ l.message_text }}</td>
                    <td style="font-size:12px; color:#94a3b8;">{{ l.created_at }}</td>
                </tr>
                {% endfor %}
                {% if leads|length == 0 %}
                <tr><td colspan="6" style="text-align:center; color:#94a3b8;">لا توجد إشعارات أو رسائل مسجلة بعد.</td></tr>
                {% endif %}
            </tbody>
        </table>
        </div>
    </div>

    <div class="card">
        <h2>➕ إضافة أو تعديل بوت عميل</h2>
        <form method="POST" action="/admin" id="botForm">
            <div class="grid-2">
                <div>
                    <label>المنصة / القناة:</label>
                    <select id="platform" name="platform" onchange="updateLabels()">
                        <option value="telegram">تيليجرام (Telegram Bot)</option>
                        <option value="whatsapp">واتساب الرسمي (WhatsApp Cloud API)</option>
                        <option value="instagram">إنستجرام (Instagram Messaging API)</option>
                        <option value="web">موقع إلكتروني (Web Chat Widget)</option>
                    </select>
                </div>
                <div>
                    <label>اسم العميل أو المشروع:</label>
                    <input type="text" id="name" name="name" placeholder="مثال: متجر الأناقة" required>
                </div>
            </div>

            <div class="grid-2">
                <div>
                    <label id="token_label">توكن البوت (Bot Token):</label>
                    <input type="text" id="token" name="token" placeholder="123456789:ABCdef..." required>
                    <div id="token_hint" class="hint">تيليجرام: Bot Token | واتساب: Phone Number ID | إنستجرام: IG Account ID | ويب: معرف بالإنجليزية مثل my_shop</div>
                </div>
                <div>
                    <label>معرف استقبال التنبيهات (Notify Chat ID - اختياري):</label>
                    <input type="text" id="notify_chat_id" name="notify_chat_id" placeholder="اتركه فارغاً لاستخدام ADMIN_CHAT_ID">
                    <div class="hint">رقم Chat ID الخاص بك أو بالعميل لإرسال التنبيهات إليه عبر تيليجرام.</div>
                </div>
            </div>

            <div id="extra_token_div" style="display:none;">
                <label>مفتاح الوصول الدائم (Meta Permanent Access Token):</label>
                <input type="text" id="extra_token" name="extra_token" placeholder="EAABsbCS1iHgBO...">
            </div>

            <div class="grid-2">
                <div>
                    <label>رسالة الترحيب (/start - اختياري):</label>
                    <input type="text" id="welcome_message" name="welcome_message" placeholder="أهلاً بك! كيف يمكنني مساعدتك اليوم؟">
                </div>
                <div>
                    <label>أزرار تيليجرام السريعة (JSON Array - اختياري):</label>
                    <input type="text" id="quick_buttons" name="quick_buttons" value='["📦 المنتجات والأسعار", "📍 العنوان والتوصيل"]'>
                </div>
            </div>

            <label>شخصية وتعليمات البوت (System Prompt):</label>
            <textarea id="prompt" name="prompt" rows="4" placeholder="اكتب تعليمات البوت وكيفية الرد وجمع بيانات العملاء..." required></textarea>

            <div class="grid-2">
                <div>
                    <label>ردود تلقائية بالكلمات المفتاحية (JSON - اختياري):</label>
                    <textarea id="auto_replies" name="auto_replies" rows="2">{}</textarea>
                </div>
                <div>
                    <label>مفتاح Anthropic API خاص بالعميل (اختياري):</label>
                    <input type="text" id="api_key" name="api_key" placeholder="sk-ant-...">
                </div>
            </div>

            <button type="submit" class="btn btn-lg">💾 حفظ وتفعيل البوت</button>
        </form>
    </div>

    <div class="card">
        <h2>📋 البوتات المسجلة ({{ clients|length }})</h2>
        <div style="overflow-x:auto;">
        <table>
            <thead>
                <tr>
                    <th>المشروع</th>
                    <th>القناة</th>
                    <th>المعرف / التوكن</th>
                    <th>Notify Chat ID</th>
                    <th>الحالة</th>
                    <th>أزرار التحكم</th>
                </tr>
            </thead>
            <tbody>
                {% for c in clients %}
                <tr>
                    <td><strong>{{ c.client_name }}</strong></td>
                    <td><span class="plat-badge">{{ (c.platform or 'telegram')|upper }}</span></td>
                    <td style="font-family: monospace; font-size: 12px;">{{ c.bot_token[:16] }}...</td>
                    <td style="font-family: monospace; font-size: 12px;">{{ c.notify_chat_id or 'ADMIN الافتراضي' }}</td>
                    <td>
                        {% if c.is_active %}
                        <span class="badge active">🟢 نشط</span>
                        {% else %}
                        <span class="badge inactive">🔴 متوقف</span>
                        {% endif %}
                    </td>
                    <td>
                        <div class="actions-cell">
                            <button type="button" class="btn btn-warn"
                                data-name="{{ c.client_name|e }}"
                                data-token="{{ c.bot_token|e }}"
                                data-platform="{{ (c.platform or 'telegram')|e }}"
                                data-extra="{{ (c.extra_token or '')|e }}"
                                data-notify="{{ (c.notify_chat_id or '')|e }}"
                                data-welcome="{{ (c.welcome_message or '')|e }}"
                                data-buttons="{{ (c.quick_buttons or '[]')|e }}"
                                data-prompt="{{ c.system_prompt|e }}"
                                data-auto="{{ (c.auto_replies or '{}')|e }}"
                                data-key="{{ (c.anthropic_key or '')|e }}"
                                onclick="fillFormFromBtn(this)">✏️ تعديل</button>

                            <form method="POST" action="/admin/toggle" style="margin:0;">
                                <input type="hidden" name="token" value="{{ c.bot_token }}">
                                <input type="hidden" name="active" value="{{ 'false' if c.is_active else 'true' }}">
                                {% if c.is_active %}
                                <button type="submit" class="btn btn-dark">⏸️ إيقاف</button>
                                {% else %}
                                <button type="submit" class="btn btn-success">▶️ تفعيل</button>
                                {% endif %}
                            </form>

                            {% if (c.platform or 'telegram') == 'telegram' %}
                            <form method="POST" action="/admin/rehook" style="margin:0;">
                                <input type="hidden" name="token" value="{{ c.bot_token }}">
                                <button type="submit" class="btn btn-purple">🔗 ربط</button>
                            </form>
                            {% endif %}

                            {% if c.platform == 'web' %}
                            <button type="button" class="btn" onclick="showWebSnippet('{{ c.bot_token|e }}')">🌐 كود الموقع</button>
                            {% endif %}

                            <form method="POST" action="/admin/delete" style="margin:0;" onsubmit="return confirm('حذف هذا البوت نهائياً؟');">
                                <input type="hidden" name="token" value="{{ c.bot_token }}">
                                <button type="submit" class="btn btn-danger">🗑️ حذف</button>
                            </form>
                        </div>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        </div>
    </div>
</div>

<div id="webModal" class="modal">
    <div class="modal-content">
        <h3>🌐 كود دمج البوت في موقع العميل</h3>
        <p style="font-size:13px; color:#cbd5e1;">انسخ الكود التالي وضعه داخل صفحة موقع العميل قبل إغلاق <code>&lt;/body&gt;</code>:</p>
        <pre id="webCodeBox"></pre>
        <div class="toolbar">
            <button type="button" class="btn btn-success" onclick="copyWebCode()">📋 نسخ الكود</button>
            <button type="button" class="btn btn-dark" onclick="closeWebModal()">إغلاق</button>
        </div>
    </div>
</div>

<script>
    let lastKnownUnread = {{ unread_count }};

    function updateLabels() {
        const plat = document.getElementById('platform').value;
        const extraDiv = document.getElementById('extra_token_div');
        const label = document.getElementById('token_label');
        if (plat === 'whatsapp') {
            extraDiv.style.display = 'block';
            label.innerText = 'معرف رقم هاتف واتساب (Phone Number ID):';
        } else if (plat === 'instagram') {
            extraDiv.style.display = 'block';
            label.innerText = 'معرف حساب إنستجرام (Instagram Account ID):';
        } else if (plat === 'web') {
            extraDiv.style.display = 'none';
            label.innerText = 'معرف الموقع (Site ID بالإنجليزية):';
        } else {
            extraDiv.style.display = 'none';
            label.innerText = 'توكن البوت (من BotFather):';
        }
    }

    function fillFormFromBtn(btn) {
        document.getElementById('name').value = btn.dataset.name || '';
        document.getElementById('token').value = btn.dataset.token || '';
        document.getElementById('platform').value = btn.dataset.platform || 'telegram';
        document.getElementById('extra_token').value = btn.dataset.extra || '';
        document.getElementById('notify_chat_id').value = btn.dataset.notify || '';
        document.getElementById('welcome_message').value = btn.dataset.welcome || '';
        document.getElementById('quick_buttons').value = btn.dataset.buttons || '[]';
        document.getElementById('prompt').value = btn.dataset.prompt || '';
        document.getElementById('auto_replies').value = btn.dataset.auto || '{}';
        document.getElementById('api_key').value = btn.dataset.key || '';
        updateLabels();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function resetForm() {
        document.getElementById('botForm').reset();
        document.getElementById('auto_replies').value = '{}';
        document.getElementById('quick_buttons').value = '["📦 المنتجات والأسعار", "📍 العنوان والتوصيل"]';
        updateLabels();
    }

    function showWebSnippet(siteId) {
        const baseUrl = window.location.origin;
        const code = `<div id="omnibot-box" style="position:fixed;bottom:20px;left:20px;z-index:9999;background:#0f172a;color:#fff;padding:14px;border-radius:12px;border:1px solid #38bdf8;width:290px;font-family:sans-serif;">
  <div style="font-weight:bold;margin-bottom:8px;">💬 المساعد الذكي</div>
  <div id="omnibot-log" style="max-height:170px;overflow-y:auto;font-size:13px;margin-bottom:8px
