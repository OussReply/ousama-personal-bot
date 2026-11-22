import os
import re
import io
import csv
import json
import time
import hmac
import hashlib
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
META_APP_SECRET = os.environ.get("META_APP_SECRET", "").strip()

# --- إعدادات أمان الجلسات والكوكيز وعزل الأدمن عن العميل ---
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("FORCE_HTTPS", "1") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True

app.config["ADMIN_SESSION_COOKIE_NAME"] = "omnibot_admin_session"
app.config["CLIENT_SESSION_COOKIE_NAME"] = "omnibot_client_session"


@app.before_request
def isolate_session_cookie_by_route():
    if request.path.startswith("/client"):
        app.session_cookie_name = app.config["CLIENT_SESSION_COOKIE_NAME"]
    else:
        app.session_cookie_name = app.config["ADMIN_SESSION_COOKIE_NAME"]


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# --- حماية ضد التخمين والإغراق (Rate Limiting) ---
_failed_admin_attempts = {}  # {ip: [timestamps]}
_webchat_hits = {}          # {key: [timestamps]}

ADMIN_MAX_FAILS = 5
ADMIN_LOCK_SECONDS = 300      # 5 دقائق حظر مؤقت بعد 5 محاولات خاطئة
WEBCHAT_MAX_PER_MIN = 20


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def is_admin_ip_blocked(ip):
    now = time.time()
    attempts = [t for t in _failed_admin_attempts.get(ip, []) if now - t < ADMIN_LOCK_SECONDS]
    _failed_admin_attempts[ip] = attempts
    return len(attempts) >= ADMIN_MAX_FAILS


def record_failed_admin_attempt(ip):
    now = time.time()
    attempts = [t for t in _failed_admin_attempts.get(ip, []) if now - t < ADMIN_LOCK_SECONDS]
    attempts.append(now)
    _failed_admin_attempts[ip] = attempts


def clear_failed_admin_attempts(ip):
    _failed_admin_attempts.pop(ip, None)


def is_webchat_rate_limited(key):
    now = time.time()
    hits = [t for t in _webchat_hits.get(key, []) if now - t < 60]
    if len(hits) >= WEBCHAT_MAX_PER_MIN:
        _webchat_hits[key] = hits
        return True
    hits.append(now)
    _webchat_hits[key] = hits
    return False


def verify_meta_signature(raw_body_bytes, signature_header):
    """يتحقق من توقيع Meta HMAC SHA-256 إذا تم ضبط META_APP_SECRET."""
    if not META_APP_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_sig = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        raw_body_bytes,
        hashlib.sha256
    ).hexdigest()
    received_sig = signature_header.split("=", 1)[1].strip()
    return hmac.compare_digest(expected_sig, received_sig)


# --- إعدادات حماية رصيد API للنسخة التجريبية ---
DEMO_QUESTION_LIMIT = int(os.environ.get("DEMO_QUESTION_LIMIT", "10"))
DEMO_LIMIT_MESSAGE = os.environ.get(
    "DEMO_LIMIT_MESSAGE",
    "✨ لقد أتممت 10 رسائل في هذه الجلسة التجريبية، وأتمنى أن تكون لمست كيف يعمل المساعد الذكي.\n"
    "إذا كنت ترغب في تجربة إضافية أو بناء بوت متكامل ومخصص بالكامل لنشاطك التجاري، يسعدني جداً تواصلك المباشر عبر زر (📞 تواصل مع موظف) لنبدأ تجهيز نسختك الخاصة."
)

# --- قواعد استخراج كل بيانات صناعة البوت دفعة واحدة ---
LEAD_EXTRACTION_RULES = """

[تعليمات العمل واستخراج المتطلبات - إلزامية]:
1. قدّم نفسك باحترافية ولباقة، واسأل العميل عما يريده لمشروعه.
2. اجعل ردودك مختصرة وواضحة ومفيدة، واسأل سؤالاً أو سؤالين في كل رسالة لجمع جميع البيانات التالية لصناعة البوت الخاص به:
   - اسم العميل أو اسم مشروعه/شركته.
   - وسيلة التواصل (رقم الهاتف أو الواتساب أو معرف تيليجرام).
   - نوع النشاط التجاري (متجر، مطعم، عيادة، خدمات، إلخ).
   - المنصة المطلوبة للبوت (تيليجرام، واتساب، إنستجرام، موقع إلكتروني).
   - المهام والتعليمات المطلوبة من البوت بالتفصيل (الرد على الأسئلة، عرض الأسعار، استقبال الطلبات، حجز المواعيد، إلخ).
   - أسلوب أو لهجة البوت المفضلة (رسمي، ودي، فصحى، لهجة محلية).
3. لا ترسل الكتلة البرمجية التالية إلا عندما تكتمل لديك جميع البيانات الستة السابقة من العميل:
[LEAD_READY]
• الاسم / المشروع: ...
• وسيلة التواصل: ...
• نوع النشاط: ...
• المنصة المطلوبة: ...
• المهام والتعليمات المطلوبة: ...
• أسلوب البوت: ...
[/LEAD_READY]
4. لا تذكر للعميل وجود هذه العلامات البرمجية، ولا تكرر إرسال [LEAD_READY] إذا تم جمع البيانات بالفعل.
"""

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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS client_portal_tokens (
                id SERIAL PRIMARY KEY,
                bot_token TEXT NOT NULL,
                access_token TEXT UNIQUE NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

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


def count_user_questions(bot_id, chat_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM chat_history WHERE bot_token = %s AND chat_id = %s AND role = 'user'",
            (str(bot_id), str(chat_id))
        )
        row = cur.fetchone()
        cur.close()
        return int(row["cnt"]) if row else 0
    except Exception as e:
        logger.error(f"Error counting user questions: {e}")
        return 0
    finally:
        release_db_connection(conn)


def get_chat_history(bot_id, chat_id, limit=14):
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


def clear_user_context_keep_quota(bot_id, chat_id):
    """يمسح ردود المساعد فقط لإعادة سياق الحوار مع الإبقاء على رسائل user لحماية حد الأسئلة التجريبية."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM chat_history WHERE bot_token = %s AND chat_id = %s AND role = 'assistant'",
            (str(bot_id), str(chat_id))
        )
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error resetting assistant context: {e}")
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


def has_lead_already_submitted(bot_token, sender_chat_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM leads WHERE bot_token = %s AND sender_chat_id = %s AND message_text LIKE '📋 طلب صناعة بوت كامل:%'",
            (str(bot_token), str(sender_chat_id))
        )
        row = cur.fetchone()
        cur.close()
        return (int(row["cnt"]) > 0) if row else False
    except Exception as e:
        logger.error(f"Error checking existing lead: {e}")
        return False
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


# --- دوال إدارة رموز دخول بوابة العميل ---
def issue_client_portal_token(bot_token):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "UPDATE client_portal_tokens SET is_active = FALSE WHERE bot_token = %s AND is_active = TRUE",
            (str(bot_token),)
        )
        new_token = secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO client_portal_tokens (bot_token, access_token, is_active) VALUES (%s, %s, TRUE)",
            (str(bot_token), new_token)
        )
        conn.commit()
        cur.close()
        return new_token
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error issuing client portal token: {e}")
        return None
    finally:
        release_db_connection(conn)


def get_active_client_portal_tokens_map():
    conn = None
    mapping = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT bot_token, access_token FROM client_portal_tokens WHERE is_active = TRUE ORDER BY id DESC")
        for row in cur.fetchall():
            bt = str(row["bot_token"])
            if bt not in mapping:
                mapping[bt] = row["access_token"]
        cur.close()
    except Exception as e:
        logger.error(f"Error loading portal tokens map: {e}")
    finally:
        release_db_connection(conn)
    return mapping


def validate_portal_access_token(access_token):
    if not access_token:
        return None
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT t.bot_token, c.client_name, c.platform, c.is_active AS bot_active
            FROM client_portal_tokens t
            JOIN clients c ON c.bot_token = t.bot_token
            WHERE t.access_token = %s AND t.is_active = TRUE
        """, (str(access_token).strip(),))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error validating portal token: {e}")
        return None
    finally:
        release_db_connection(conn)


# --- عزل الجلسات والصلاحيات بين الأدمن والعميل ---
def clear_admin_session_keys():
    for k in ("admin_auth", "is_admin"):
        session.pop(k, None)
    if session.get("session_owner") == "admin":
        session.pop("session_owner", None)


def clear_client_session_keys():
    for k in ("client_auth", "client_bot_token"):
        session.pop(k, None)
    if session.get("session_owner") == "client":
        session.pop("session_owner", None)


def is_admin_authorized():
    if not ADMIN_SECRET:
        return False
    if session.get("session_owner") == "admin" and session.get("admin_auth") is True:
        return True
    req_secret = request.args.get("secret", "") or request.headers.get("X-Admin-Secret", "")
    if req_secret and hmac.compare_digest(req_secret, ADMIN_SECRET):
        clear_client_session_keys()
        session["session_owner"] = "admin"
        session["admin_auth"] = True
        return True
    return False


def get_authorized_client_bot_token():
    """يرجع bot_token الخاص بالعميل فقط إذا كانت الجلسة مخصصة حصراً لدور client."""
    if session.get("session_owner") != "client" or session.get("client_auth") is not True:
        return None
    bt = session.get("client_bot_token")
    return str(bt).strip() if bt else None


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

    badge = "✅ تم استخراج بيانات عميل كاملة" if is_urgent else "🔔 إشعار جديد"
    msg = (
        f"{badge} ({str(platform).upper()})\n"
        f"• المصدر: {client_data['client_name']}\n"
        f"• حساب المرسل: {sender_label}\n"
        f"• المعرف: {sender_id}\n"
        f"-------------------------\n"
        f"{user_text}"
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


# --- العقل الذكي الموحد + استخراج البيانات + حماية الرصيد ---
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


def extract_and_notify_lead(client_data, platform, sender_id, sender_label, ai_reply_text):
    pattern = r"\[LEAD_READY\](.*?)\[/LEAD_READY\]"
    match = re.search(pattern, ai_reply_text, flags=re.DOTALL)
    if not match:
        return ai_reply_text.strip()

    extracted_data = match.group(1).strip()
    clean_reply_for_user = re.sub(pattern, "", ai_reply_text, flags=re.DOTALL).strip()

    if extracted_data and not has_lead_already_submitted(client_data["bot_token"], sender_id):
        formatted_report = f"📋 طلب صناعة بوت كامل:\n{extracted_data}"
        handle_incoming_notification(
            client_data,
            platform,
            sender_id,
            sender_label,
            formatted_report,
            is_urgent=True
        )

    return clean_reply_for_user


def generate_ai_reply(client_data, chat_id, user_text, platform="telegram", sender_label="عميل"):
    bot_id = client_data["bot_token"]

    if DEMO_QUESTION_LIMIT > 0:
        used_questions = count_user_questions(bot_id, chat_id)
        if used_questions >= DEMO_QUESTION_LIMIT:
            return DEMO_LIMIT_MESSAGE

    save_message(bot_id, chat_id, "user", user_text)
    used_now = count_user_questions(bot_id, chat_id)
    remaining = max(0, DEMO_QUESTION_LIMIT - used_now)

    auto_msg = match_auto_reply(client_data, user_text)
    if auto_msg:
        save_message(bot_id, chat_id, "assistant", auto_msg)
        return auto_msg

    history = get_chat_history(bot_id, chat_id, limit=14)
    api_key = (client_data.get("anthropic_key") or "").strip() or DEFAULT_ANTHROPIC_KEY
    if not api_key:
        return "عذراً، الخدمة قيد التهيئة حالياً."

    full_system_prompt = (
        f"{client_data['system_prompt']}\n"
        f"{LEAD_EXTRACTION_RULES}\n"
        f"(معلومة للبوت: هذا عميل في النسخة التجريبية ولديه {remaining} رسائل متبقية؛ قدّم تجربة مفيدة ومختصرة واجمع كامل متطلبات مشروعه بهدوء ووضوح)."
    )

    try:
        ai_client = anthropic.Anthropic(api_key=api_key)
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=700,
            system=full_system_prompt,
            messages=history
        )
        raw_reply = response.content[0].text
        clean_reply = extract_and_notify_lead(client_data, platform, chat_id, sender_label, raw_reply)
        save_message(bot_id, chat_id, "assistant", clean_reply)

        if DEMO_QUESTION_LIMIT > 0 and remaining == 0:
            clean_reply += "\n\n✨ (أنهيت رسائلك التجريبية الـ 10؛ اضغط على «📞 تواصل مع موظف» لإطلاق بوتك الخاص!)"

        return clean_reply
    except Exception as e:
        logger.error(f"AI Error ({client_data['client_name']}): {e}")
        return "عذراً، حدث خطأ تقني مؤقت. يرجى المحاولة مرة أخرى."


def set_tg_webhook(bot_token, host_url):
    webhook_url = f"{host_url}/webhook/{bot_token}"
    res = requests.get(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        params={"url": webhook_url},
        timeout=10
    )
    return res.json()


# ================= واجهة لوحة تحكم الأدمن =================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OmniBot SaaS — لوحة القيادة والطلبات</title>
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
        tr.unread-row { background: rgba(251, 146, 60, 0.14); }
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
        .lead-text { white-space: pre-line; line-height: 1.6; }
        .portal-link-box { background: #0f172a; border: 1px dashed #38bdf8; padding: 10px 12px; border-radius: 8px; direction: ltr; text-align: left; font-family: monospace; font-size: 12px; color: #7dd3fc; word-break: break-all; margin-bottom: 10px; }
    </style>
</head>
<body>
<div class="container">
    <div class="top-bar">
        <div>
            <h1 style="margin:0; font-size: 21px;">🚀 OmniBot SaaS — لوحة القيادة واستقبال الطلبات</h1>
            <span style="font-size: 12px; color: #94a3b8;">حد الأسئلة التجريبية: {{ demo_limit }} رسائل لكل عميل | حماية جلسات وبوابات مستقلة</span>
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
                    🔔 تنبيه: لديك {{ unread_count }} طلب/إشعار جديد مكتمل البيانات!
                {% else %}
                    ✅ مركز الطلبات نشط — لن تصلك إلا البيانات الكاملة الجاهزة لصناعة البوت.
                {% endif %}
            </strong>
            <div id="liveBannerSub" style="font-size:12px; color:#cbd5e1; margin-top:3px;">
                يتم فحص الطلبات الجديدة تلقائياً كل 10 ثوانٍ.
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

    {% if client_portal_url %}
    <div class="card" style="border-color:#38bdf8;">
        <h3>🔑 رابط دخول واجهة الإشعارات الخاص بالعميل</h3>
        <p style="font-size:13px; color:#cbd5e1;">أرسل هذا الرابط للعميل فقط؛ سيعرض له إشعارات بوته وحده دون أي صلاحيات إدارية:</p>
        <div class="portal-link-box" id="generatedPortalUrl">{{ client_portal_url }}</div>
        <button type="button" class="btn btn-success" onclick="copyText('generatedPortalUrl')">📋 نسخ رابط العميل</button>
    </div>
    {% endif %}

    <div class="stats-grid">
        <div class="stat-box">
            <div class="stat-num" id="statUnread">{{ unread_count }}</div>
            <div class="stat-label">🔔 طلبات جديدة غير مقروءة</div>
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
            <div class="stat-num">{{ demo_limit }}</div>
            <div class="stat-label">🛡️ الحد التجريبي لكل عميل</div>
        </div>
    </div>

    <div class="card">
        <h2>📋 طلبات صناعة البوتات المستخرجة ({{ leads|length }})</h2>
        <div style="overflow-x:auto;">
        <table>
            <thead>
                <tr>
                    <th>الحالة</th>
                    <th>المصدر</th>
                    <th>القناة</th>
                    <th>بيانات العميل</th>
                    <th>المواصفات والمتطلبات المستخرجة</th>
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
                    <td style="font-size:12px;">{{ l.sender_label or 'عميل' }}<br><code>{{ l.sender_chat_id }}</code></td>
                    <td class="lead-text">{{ l.message_text }}</td>
                    <td style="font-size:12px; color:#94a3b8;">{{ l.created_at }}</td>
                </tr>
                {% endfor %}
                {% if leads|length == 0 %}
                <tr><td colspan="6" style="text-align:center; color:#94a3b8;">لا توجد طلبات مكتملة مستخرجة بعد.</td></tr>
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
                    <label>اسم البوت أو المشروع:</label>
                    <input type="text" id="name" name="name" placeholder="مثال: OmniBot التجريبي" required>
                </div>
            </div>

            <div class="grid-2">
                <div>
                    <label id="token_label">توكن البوت (Bot Token):</label>
                    <input type="text" id="token" name="token" placeholder="123456789:ABCdef..." required>
                    <div id="token_hint" class="hint">تيليجرام: Bot Token | واتساب: Phone Number ID | إنستجرام: IG Account ID | ويب: معرف بالإنجليزية</div>
                </div>
                <div>
                    <label>معرف استقبال التنبيهات (Notify Chat ID - اختياري):</label>
                    <input type="text" id="notify_chat_id" name="notify_chat_id" placeholder="اتركه فارغاً لاستخدام ADMIN_CHAT_ID">
                    <div class="hint">رقم Chat ID الخاص بك أو الخاص بالعميل لإرسال التنبيهات إليه عبر تيليجرام.</div>
                </div>
            </div>

            <div id="extra_token_div" style="display:none;">
                <label>مفتاح الوصول الدائم (Meta Permanent Access Token):</label>
                <input type="text" id="extra_token" name="extra_token" placeholder="EAABsbCS1iHgBO...">
            </div>

            <div class="grid-2">
                <div>
                    <label>رسالة الترحيب (/start - اختياري):</label>
                    <input type="text" id="welcome_message" name="welcome_message" placeholder="مرحباً بك! أنا مساعدك الذكي لبناء بوت احترافي لمشروعك. أخبرني ماذا تريد؟">
                </div>
                <div>
                    <label>أزرار تيليجرام السريعة (JSON Array - اختياري):</label>
                    <input type="text" id="quick_buttons" name="quick_buttons" value='["🤖 أريد بوت لمتجري", "🏢 أريد بوت لشركتي"]'>
                </div>
            </div>

            <label>شخصية وتعليمات البوت (System Prompt):</label>
            <textarea id="prompt" name="prompt" rows="4" placeholder="أنت مستشار ذكي لخدمة العملاء وبناء البوتات الذكية. قدّم نفسك باختصار واسأل العميل عن مشروعه والمتطلبات التي يريدها في البوت..." required></textarea>

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
                    <th>بوابة العميل</th>
                    <th>الحالة</th>
                    <th>أزرار التحكم</th>
                </tr>
            </thead>
            <tbody>
                {% for c in clients %}
                {% set p_tok = portal_tokens_map.get(c.bot_token) %}
                <tr>
                    <td><strong>{{ c.client_name }}</strong></td>
                    <td><span class="plat-badge">{{ (c.platform or 'telegram')|upper }}</span></td>
                    <td style="font-family: monospace; font-size: 12px;">{{ c.bot_token[:16] }}...</td>
                    <td>
                        {% if p_tok %}
                        <span class="badge active">مفعّلة</span>
                        {% else %}
                        <span class="badge read-badge">غير مُصدرة</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if c.is_active %}
                        <span class="badge active">🟢 نشط</span>
                        {% else %}
                        <span class="badge inactive">🔴 متوقف</span>
                        {% endif %}
                    </td>
                    <td>
                        <div class="actions-cell">
                            <form method="POST" action="/admin/issue-client-portal" style="margin:0;">
                                <input type="hidden" name="token" value="{{ c.bot_token }}">
                                <button type="submit" class="btn btn-purple">🔑 رابط العميل</button>
                            </form>

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
                                <button type="submit" class="btn">🔗 ربط</button>
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
        document.getElementById('quick_buttons').value = '["🤖 أريد بوت لمتجري", "🏢 أريد بوت لشركتي"]';
        updateLabels();
    }

    function copyText(elementId) {
        const el = document.getElementById(elementId);
        if (!el) return;
        navigator.clipboard.writeText(el.textContent.trim());
        alert('تم النسخ بنجاح!');
    }

    function showWebSnippet(siteId) {
        const baseUrl = window.location.origin;
        const endScript = "</" + "script>";
        const code = `<div id="omnibot-box" style="position:fixed;bottom:20px;left:20px;z-index:9999;background:#0f172a;color:#fff;padding:14px;border-radius:12px;border:1px solid #38bdf8;width:290px;font-family:sans-serif;">
  <div style="font-weight:bold;margin-bottom:8px;">💬 المساعد الذكي</div>
  <div id="omnibot-log" style="max-height:170px;overflow-y:auto;font-size:13px;margin-bottom:8px;"></div>
  <input id="omnibot-inp" placeholder="اكتب رسالتك..." style="width:100%;padding:7px;border-radius:6px;border:none;margin-bottom:6px;box-sizing:border-box;">
  <button onclick="sendOmniMsg()" style="width:100%;padding:7px;background:#0284c7;color:#fff;border:none;border-radius:6px;cursor:pointer;">إرسال</button>
</div>
<script>
const sid = "web_" + Math.random().toString(36).slice(2, 9);
async function sendOmniMsg() {
  const inp = document.getElementById("omnibot-inp");
  const log = document.getElementById("omnibot-log");
  const txt = inp.value.trim();
  if (!txt) return;
  log.innerHTML += "<div><b>أنت:</b> " + txt + "</div>";
  inp.value = "";
  const r = await fetch("${baseUrl}/api/webchat/${siteId}", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({message: txt, session_id: sid})
  });
  const d = await r.json();
  log.innerHTML += "<div style='color:#38bdf8;'><b>البوت:</b> " + (d.reply || "") + "</div>";
  log.scrollTop = log.scrollHeight;
}
${endScript}`;
        document.getElementById('webCodeBox').textContent = code;
        document.getElementById('webModal').style.display = 'block';
    }

    function closeWebModal() {
        document.getElementById('webModal').style.display = 'none';
    }

    function copyWebCode() {
        const text = document.getElementById('webCodeBox').textContent;
        navigator.clipboard.writeText(text);
        alert('تم نسخ كود الموقع بنجاح!');
    }

    async function pollNotifications() {
        try {
            const res = await fetch('/admin/notifications-status');
            if (!res.ok) return;
            const data = await res.json();
            const unread = Number(data.unread_count || 0);
            document.getElementById('statUnread').textContent = unread;
            const banner = document.getElementById('liveBanner');
            const title = document.getElementById('liveBannerTitle');
            const sub = document.getElementById('liveBannerSub');
            if (unread > 0) {
                banner.classList.add('has-unread');
                title.textContent = `🔔 تنبيه: لديك ${unread} طلب/إشعار جديد مكتمل البيانات!`;
                if (data.latest) {
                    sub.textContent = `آخر طلب من (${data.latest.client_name}): ${data.latest.message_text.slice(0, 90)}...`;
                }
                if (unread > lastKnownUnread) {
                    lastKnownUnread = unread;
                }
            } else {
                banner.classList.remove('has-unread');
                title.textContent = '✅ مركز الطلبات نشط — لن تصلك إلا البيانات الكاملة الجاهزة لصناعة البوت.';
                sub.textContent = 'يتم فحص الطلبات الجديدة تلقائياً كل 10 ثوانٍ.';
                lastKnownUnread = 0;
            }
        } catch (e) {}
    }

    setInterval(pollNotifications, 10000);
</script>
</body>
</html>
"""


# ================= واجهة إشعارات العميل المخصصة والمعزولة =================
CLIENT_PORTAL_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>إشعارات الزبائن — {{ client_name }}</title>
    <style>
        :root { --bg: #0b1120; --card: #1e293b; --border: #334155; --primary: #0284c7; --accent: #38bdf8; }
        body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: #f8fafc; margin: 0; padding: 18px; }
        .container { max-width: 1100px; margin: auto; }
        h1, h2 { color: var(--accent); margin-top: 0; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; background: var(--card); padding: 16px 20px; border-radius: 12px; border: 1px solid var(--border); }
        .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .live-banner { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; background: linear-gradient(90deg, #172554, #1e293b); border: 1px solid #38bdf8; padding: 14px 18px; border-radius: 12px; margin-bottom: 18px; }
        .live-banner.has-unread { background: linear-gradient(90deg, #7c2d12, #1e293b); border-color: #fb923c; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 20px; }
        .stat-box { background: var(--card); padding: 16px; border-radius: 12px; border-right: 4px solid var(--accent); border-top: 1px solid var(--border); }
        .stat-num { font-size: 24px; font-weight: bold; color: #fff; }
        .stat-label { font-size: 13px; color: #94a3b8; margin-top: 4px; }
        .card { background: var(--card); padding: 20px; border-radius: 12px; margin-bottom: 22px; border: 1px solid var(--border); }
        .btn { background: var(--primary); color: white; border: none; padding: 9px 14px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px; text-decoration: none; display: inline-flex; align-items: center; gap: 5px; }
        .btn-success { background: #16a34a; }
        .btn-warn { background: #d97706; }
        .btn-danger { background: #dc2626; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 11px; text-align: right; border-bottom: 1px solid var(--border); vertical-align: middle; font-size: 13px; }
        th { color: #94a3b8; background: #0f172a; }
        tr.unread-row { background: rgba(251, 146, 60, 0.14); }
        .badge { padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .active { background: #14532d; color: #4ade80; }
        .inactive { background: #7f1d1d; color: #f87171; }
        .unread-badge { background: #ea580c; color: #fff; }
        .read-badge { background: #334155; color: #cbd5e1; }
        .plat-badge { background: #1e3a8a; color: #93c5fd; padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .alert { background: #065f46; color: #a7f3d0; padding: 12px 15px; border-radius: 8px; margin-bottom: 16px; border: 1px solid #10b981; }
        .lead-text { white-space: pre-line; line-height: 1.6; }
    </style>
</head>
<body>
<div class="container">
    <div class="top-bar">
        <div>
            <h1 style="margin:0; font-size: 20px;">📬 إشعارات الزبائن — {{ client_name }}</h1>
            <span style="font-size: 12px; color: #94a3b8;">واجهة متابعة مخصصة لبوتك فقط — القناة: {{ (platform or 'telegram')|upper }}</span>
        </div>
        <div class="toolbar">
            <a href="/client/export-leads" class="btn btn-success">📥 تصدير CSV</a>
            <a href="/client/logout" class="btn btn-danger">🚪 خروج</a>
        </div>
    </div>

    <div id="clientLiveBanner" class="live-banner {% if unread_count > 0 %}has-unread{% endif %}">
        <div>
            <strong id="clientBannerTitle">
                {% if unread_count > 0 %}
                    🔔 لديك {{ unread_count }} إشعار/طلب جديد من زبائنك!
                {% else %}
                    ✅ لا توجد إشعارات جديدة غير مقروءة حالياً.
                {% endif %}
            </strong>
            <div id="clientBannerSub" style="font-size:12px; color:#cbd5e1; margin-top:3px;">
                يتم التحديث تلقائياً كل 10 ثوانٍ.
            </div>
        </div>
        <div class="toolbar">
            <form method="POST" action="/client/mark-read" style="margin:0;">
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
            <div class="stat-num" id="clientStatUnread">{{ unread_count }}</div>
            <div class="stat-label">🔔 إشعارات جديدة</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">{{ leads|length }}</div>
            <div class="stat-label">📋 إجمالي الإشعارات المسجلة</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">
                {% if is_active %}
                <span class="badge active">🟢 البوت نشط</span>
                {% else %}
                <span class="badge inactive">🔴 البوت متوقف</span>
                {% endif %}
            </div>
            <div class="stat-label">حالة تشغيل البوت</div>
        </div>
    </div>

    <div class="card">
        <h2>📋 سجل إشعارات وطلبات الزبائن</h2>
        <div style="overflow-x:auto;">
        <table>
            <thead>
                <tr>
                    <th>الحالة</th>
                    <th>القناة</th>
                    <th>المرسل</th>
                    <th>تفاصيل الإشعار / الطلب</th>
                    <th>التوقيت</th>
                </tr>
            </thead>
            <tbody>
                {% for l in leads %}
                <tr class="{% if not l.is_read %}unread-row{% endif %}">
                    <td>
                        {% if not l.is_read %}
                        <span class="badge unread-badge">🆕 جديد</span>
                        {% else %}
                        <span class="badge read-badge">مقروء</span>
                        {% endif %}
                    </td>
                    <td><span class="plat-badge">{{ (l.platform or 'telegram')|upper }}</span></td>
                    <td style="font-size:12px;">{{ l.sender_label or 'زبون' }}<br><code>{{ l.sender_chat_id }}</code></td>
                    <td class="lead-text">{{ l.message_text }}</td>
                    <td style="font-size:12px; color:#94a3b8;">{{ l.created_at }}</td>
                </tr>
                {% endfor %}
                {% if leads|length == 0 %}
                <tr><td colspan="5" style="text-align:center; color:#94a3b8;">لا توجد إشعارات مسجلة لبوتك حتى الآن.</td></tr>
                {% endif %}
            </tbody>
        </table>
        </div>
    </div>
</div>

<script>
    async function pollClientNotifications() {
        try {
            const res = await fetch('/client/notifications-status');
            if (!res.ok) return;
            const data = await res.json();
            const unread = Number(data.unread_count || 0);
            document.getElementById('clientStatUnread').textContent = unread;
            const banner = document.getElementById('clientLiveBanner');
            const title = document.getElementById('clientBannerTitle');
            const sub = document.getElementById('clientBannerSub');
            if (unread > 0) {
                banner.classList.add('has-unread');
                title.textContent = `🔔 لديك ${unread} إشعار/طلب جديد من زبائنك!`;
                if (data.latest) {
                    sub.textContent = `آخر إشعار: ${data.latest.message_text.slice(0, 95)}...`;
                }
            } else {
                banner.classList.remove('has-unread');
                title.textContent = '✅ لا توجد إشعارات جديدة غير مقروءة حالياً.';
                sub.textContent = 'يتم التحديث تلقائياً كل 10 ثوانٍ.';
            }
        } catch (e) {}
    }
    setInterval(pollClientNotifications, 10000);
</script>
</body>
</html>
"""


# ================= مسارات النظام والإدارة =================
@app.route("/", methods=["GET"])
def index():
    return "OmniBot SaaS Platform is Online!", 200


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not ADMIN_SECRET:
        return "يرجى ضبط متغير البيئة ADMIN_SECRET أولاً في Railway.", 500

    client_ip = get_client_ip()
    if is_admin_ip_blocked(client_ip):
        return "تم حظر المحاولات مؤقتاً بسبب تكرار رمز الدخول الخاطئ. يرجى المحاولة بعد 5 دقائق.", 429

    error = ""
    if request.method == "POST":
        entered_secret = request.form.get("secret", "").strip()
        if entered_secret and hmac.compare_digest(entered_secret, ADMIN_SECRET):
            clear_failed_admin_attempts(client_ip)
            clear_client_session_keys()
            session["session_owner"] = "admin"
            session["admin_auth"] = True
            return redirect(url_for("admin_dashboard"))
        record_failed_admin_attempt(client_ip)
        error = "رمز الدخول غير صحيح."

    return f"""
    <!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><title>دخول الإدارة</title>
    <style>body{{font-family:system-ui;background:#0b1120;color:#f8fafc;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}}
    .box{{background:#1e293b;padding:28px;border-radius:12px;width:340px;border:1px solid #334155}}
    input{{width:100%;padding:10px;margin:12px 0;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#fff;box-sizing:border-box}}
    button{{width:100%;padding:10px;border:none;border-radius:8px;background:#0284c7;color:#fff;font-weight:bold;cursor:pointer}}
    .err{{color:#f87171;font-size:13px;margin-bottom:8px}}</style></head>
    <body><form method="POST" class="box"><h2>🔐 دخول لوحة الإدارة</h2>
    {"<div class='err'>" + error + "</div>" if error else ""}
    <input type="password" name="secret" placeholder="أدخل ADMIN_SECRET" required>
    <button type="submit">دخول</button></form></body></html>
    """


@app.route("/admin/logout", methods=["GET"])
def admin_logout():
    clear_admin_session_keys()
    return redirect(url_for("admin_login"))


@app.route("/admin/notifications-status", methods=["GET"])
def admin_notifications_status():
    if not is_admin_authorized():
        return jsonify({"error": "unauthorized"}), 403
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT COUNT(*) AS cnt FROM leads WHERE is_read = FALSE")
        row_u = cur.fetchone()
        unread_count = row_u["cnt"] if row_u else 0

        cur.execute("SELECT client_name, platform, message_text, created_at FROM leads ORDER BY id DESC LIMIT 1")
        latest = cur.fetchone()
        cur.close()
        return jsonify({
            "unread_count": unread_count,
            "latest": dict(latest) if latest else None
        })
    except Exception as e:
        logger.error(f"Error fetching notification status: {e}")
        return jsonify({"unread_count": 0, "latest": None})
    finally:
        release_db_connection(conn)


@app.route("/admin/mark-read", methods=["POST"])
def admin_mark_read():
    if not is_admin_authorized():
        return "غير مصرح", 403
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE leads SET is_read = TRUE WHERE is_read = FALSE")
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error marking leads read: {e}")
    finally:
        release_db_connection(conn)
    return redirect(url_for("admin_dashboard", msg="تم تحديد جميع الطلبات كمقروءة."))


@app.route("/admin/test-alert", methods=["POST"])
def admin_test_alert():
    if not is_admin_authorized():
        return "غير مصرح", 403

    sample_report = (
        "📋 طلب صناعة بوت كامل (تجريبي):\n"
        "• الاسم / المشروع: اختبار النظام\n"
        "• وسيلة التواصل: لوحة الأدمن\n"
        "• نوع النشاط: تجربة الإشعارات\n"
        "• المنصة المطلوبة: Telegram\n"
        "• المهام والتعليمات المطلوبة: التأكد من وصول التقرير النهائي فقط."
    )
    save_lead(
        "system_test",
        "اختبار المنصة",
        "admin_test",
        ADMIN_CHAT_ID or "admin",
        "لوحة التحكم",
        sample_report
    )

    sent_tg = False
    if ALERT_BOT_TOKEN and ADMIN_CHAT_ID:
        sent_tg = send_to_chat_id(ALERT_BOT_TOKEN, ADMIN_CHAT_ID, sample_report)

    if sent_tg:
        msg = "✅ تم إرسال التقرير التجريبي إلى صفحة الأدمن وإلى تيليجرام بنجاح!"
    else:
        msg = "⚠️ تم تسجيل الإشعار في صفحة الأدمن؛ تحقق من ضبط ALERT_BOT_TOKEN وADMIN_CHAT_ID لإرسال تيليجرام."

    return redirect(url_for("admin_dashboard", msg=msg))


@app.route("/admin/export-leads", methods=["GET"])
def admin_export_leads():
    if not is_admin_authorized():
        return "غير مصرح", 403

    conn = None
    rows = []
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, client_name, platform, sender_chat_id, sender_label, message_text, is_read, created_at FROM leads ORDER BY id DESC")
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        logger.error(f"Export error: {e}")
    finally:
        release_db_connection(conn)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Client", "Platform", "Sender ID", "Sender Label", "Message", "Is Read", "Created At"])
    for r in rows:
        writer.writerow([
            r["id"], r["client_name"], r["platform"], r["sender_chat_id"],
            r["sender_label"], r["message_text"], r["is_read"], r["created_at"]
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=omnibot_extracted_leads.csv"}
    )


@app.route("/admin/issue-client-portal", methods=["POST"])
def admin_issue_client_portal():
    if not is_admin_authorized():
        return "غير مصرح", 403
    bot_token = request.form.get("token", "").strip()
    if not bot_token or not get_client(bot_token):
        return redirect(url_for("admin_dashboard", msg="⚠️ تعذر العثور على البوت لتوليد رابط العميل."))

    access_tok = issue_client_portal_token(bot_token)
    if not access_tok:
        return redirect(url_for("admin_dashboard", msg="❌ فشل إنشاء رمز دخول للعميل."))

    host_url = request.host_url.rstrip("/").replace("http://", "https://")
    portal_url = f"{host_url}/client/login/{access_tok}"
    return redirect(url_for("admin_dashboard", msg="✅ تم إصدار رابط دخول خاص ومستقل للعميل.", portal_url=portal_url))


@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    if not is_admin_authorized():
        return redirect(url_for("admin_login"))

    msg = request.args.get("msg", "")
    client_portal_url = request.args.get("portal_url", "")

    if request.method == "POST":
        platform = request.form.get("platform", "telegram").strip()
        token = request.form.get("token", "").strip()
        name = request.form.get("name", "عميل جديد").strip()
        prompt = request.form.get("prompt", "أنت مساعد ذكي.").strip()
        welcome_message = request.form.get("welcome_message", "").strip()
        notify_chat_id = request.form.get("notify_chat_id", "").strip()
        auto_replies_raw = request.form.get("auto_replies", "{}").strip() or "{}"
        quick_buttons_raw = request.form.get("quick_buttons", "[]").strip() or "[]"
        extra_token = request.form.get("extra_token", "").strip()
        api_key = request.form.get("api_key", "").strip() or DEFAULT_ANTHROPIC_KEY

        try:
            parsed_auto = json.loads(auto_replies_raw)
            if not isinstance(parsed_auto, dict):
                raise ValueError("auto_replies must be dict")
            auto_replies_json = json.dumps(parsed_auto, ensure_ascii=False)
        except Exception:
            auto_replies_json = "{}"
            msg = "⚠️ تنبيه: تم ضبط الردود التلقائية على {} بسبب صيغة JSON غير صالحة."

        try:
            parsed_btns = json.loads(quick_buttons_raw)
            if not isinstance(parsed_btns, list):
                raise ValueError("quick_buttons must be list")
            quick_buttons_json = json.dumps(parsed_btns, ensure_ascii=False)
        except Exception:
            quick_buttons_json = "[]"

        if token:
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO clients (
                        bot_token, client_name, system_prompt, welcome_message,
                        notify_chat_id, auto_replies, quick_buttons, anthropic_key,
                        platform, extra_token, is_active
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (bot_token) DO UPDATE SET
                        client_name = EXCLUDED.client_name,
                        system_prompt = EXCLUDED.system_prompt,
                        welcome_message = EXCLUDED.welcome_message,
                        notify_chat_id = EXCLUDED.notify_chat_id,
                        auto_replies = EXCLUDED.auto_replies,
                        quick_buttons = EXCLUDED.quick_buttons,
                        anthropic_key = EXCLUDED.anthropic_key,
                        platform = EXCLUDED.platform,
                        extra_token = EXCLUDED.extra_token,
                        is_active = TRUE
                """, (
                    token, name, prompt, welcome_message, notify_chat_id,
                    auto_replies_json, quick_buttons_json, api_key, platform, extra_token
                ))
                conn.commit()
                cur.close()

                host_url = request.host_url.rstrip("/").replace("http://", "https://")
                if platform == "telegram":
                    tg_res = set_tg_webhook(token, host_url)
                    if tg_res.get("ok"):
                        msg = f"✅ تم حفظ وتفعيل بوت تيليجرام ({name}) وضبط Webhook بنجاح!"
                    else:
                        msg = f"⚠️ تم الحفظ، ملاحظة تيليجرام: {tg_res.get('description')}"
                elif platform == "whatsapp":
                    msg = f"✅ تم تفعيل بوت واتساب ({name}). رابط Webhook: {host_url}/webhook/whatsapp"
                elif platform == "instagram":
                    msg = f"✅ تم تفعيل بوت إنستجرام ({name}). رابط Webhook: {host_url}/webhook/instagram"
                elif platform == "web":
                    msg = f"✅ تم تفعيل بوت الموقع ({name}). رابط API: {host_url}/api/webchat/{token}"
            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"Save client error: {e}")
                msg = "❌ حدث خطأ أثناء الحفظ. يرجى التحقق من المدخلات."
            finally:
                release_db_connection(conn)

    conn = None
    clients = []
    leads = []
    unread_count = 0
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM clients ORDER BY created_at DESC")
        clients = cur.fetchall()

        cur.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 60")
        leads = cur.fetchall()

        cur.execute("SELECT COUNT(*) AS cnt FROM leads WHERE is_read = FALSE")
        r_unread = cur.fetchone()
        unread_count = r_unread["cnt"] if r_unread else 0
        cur.close()
    except Exception as e:
        logger.error(f"Dashboard load error: {e}")
    finally:
        release_db_connection(conn)

    active_count = sum(1 for c in clients if c.get("is_active"))
    portal_tokens_map = get_active_client_portal_tokens_map()

    return render_template_string(
        DASHBOARD_HTML,
        clients=clients,
        leads=leads,
        active_count=active_count,
        unread_count=unread_count,
        demo_limit=DEMO_QUESTION_LIMIT,
        portal_tokens_map=portal_tokens_map,
        client_portal_url=client_portal_url,
        msg=msg
    )


@app.route("/admin/toggle", methods=["POST"])
def admin_toggle():
    if not is_admin_authorized():
        return "غير مصرح", 403
    token = request.form.get("token", "").strip()
    state = request.form.get("active", "true").lower() == "true"

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE clients SET is_active = %s WHERE bot_token = %s", (state, token))
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Toggle error: {e}")
    finally:
        release_db_connection(conn)

    action_word = "تفعيل" if state else "إيقاف"
    return redirect(url_for("admin_dashboard", msg=f"تم {action_word} البوت بنجاح."))


@app.route("/admin/rehook", methods=["POST"])
def admin_rehook():
    if not is_admin_authorized():
        return "غير مصرح", 403
    token = request.form.get("token", "").strip()
    host_url = request.host_url.rstrip("/").replace("http://", "https://")
    tg_res = set_tg_webhook(token, host_url)
    if tg_res.get("ok"):
        return redirect(url_for("admin_dashboard", msg="✅ تم إعادة ربط Telegram Webhook بنجاح."))
    return redirect(url_for("admin_dashboard", msg=f"⚠️ فشل إعادة الربط: {tg_res.get('description')}"))


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    if not is_admin_authorized():
        return "غير مصرح", 403
    token = request.form.get("token", "").strip()

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_history WHERE bot_token = %s", (token,))
        cur.execute("DELETE FROM leads WHERE bot_token = %s", (token,))
        cur.execute("DELETE FROM client_portal_tokens WHERE bot_token = %s", (token,))
        cur.execute("DELETE FROM clients WHERE bot_token = %s", (token,))
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Delete error: {e}")
    finally:
        release_db_connection(conn)

    return redirect(url_for("admin_dashboard", msg="🗑️ تم حذف البوت وسجله بنجاح."))


# ================= مسارات بوابة العميل المعزولة =================
@app.route("/client/login/<access_token>", methods=["GET"])
def client_portal_login(access_token):
    info = validate_portal_access_token(access_token)
    if not info:
        return "رابط الدخول غير صالح أو تم إبطاله.", 403

    clear_admin_session_keys()
    session["session_owner"] = "client"
    session["client_auth"] = True
    session["client_bot_token"] = info["bot_token"]
    return redirect(url_for("client_portal_dashboard"))


@app.route("/client/logout", methods=["GET"])
def client_portal_logout():
    clear_client_session_keys()
    return "تم تسجيل الخروج من واجهة الإشعارات بنجاح.", 200


@app.route("/client/dashboard", methods=["GET"])
def client_portal_dashboard():
    bot_token = get_authorized_client_bot_token()
    if not bot_token:
        return "غير مصرح بالدخول. يرجى استخدام رابط الدخول الخاص بحسابك.", 403

    client_info = get_client(bot_token)
    if not client_info:
        return "حساب البوت غير موجود.", 404

    msg = request.args.get("msg", "")
    conn = None
    leads = []
    unread_count = 0
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM leads WHERE bot_token = %s ORDER BY id DESC LIMIT 100",
            (bot_token,)
        )
        leads = cur.fetchall()

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM leads WHERE bot_token = %s AND is_read = FALSE",
            (bot_token,)
        )
        r_unread = cur.fetchone()
        unread_count = r_unread["cnt"] if r_unread else 0
        cur.close()
    except Exception as e:
        logger.error(f"Client portal load error: {e}")
    finally:
        release_db_connection(conn)

    return render_template_string(
        CLIENT_PORTAL_HTML,
        client_name=client_info["client_name"],
        platform=client_info.get("platform") or "telegram",
        is_active=bool(client_info.get("is_active")),
        leads=leads,
        unread_count=unread_count,
        msg=msg
    )


@app.route("/client/notifications-status", methods=["GET"])
def client_notifications_status():
    bot_token = get_authorized_client_bot_token()
    if not bot_token:
        return jsonify({"error": "unauthorized"}), 403

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM leads WHERE bot_token = %s AND is_read = FALSE",
            (bot_token,)
        )
        row_u = cur.fetchone()
        unread_count = row_u["cnt"] if row_u else 0

        cur.execute(
            "SELECT platform, message_text, created_at FROM leads WHERE bot_token = %s ORDER BY id DESC LIMIT 1",
            (bot_token,)
        )
        latest = cur.fetchone()
        cur.close()
        return jsonify({
            "unread_count": unread_count,
            "latest": dict(latest) if latest else None
        })
    except Exception as e:
        logger.error(f"Error fetching client notification status: {e}")
        return jsonify({"unread_count": 0, "latest": None})
    finally:
        release_db_connection(conn)


@app.route("/client/mark-read", methods=["POST"])
def client_mark_read():
    bot_token = get_authorized_client_bot_token()
    if not bot_token:
        return "غير مصرح", 403

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE leads SET is_read = TRUE WHERE bot_token = %s AND is_read = FALSE",
            (bot_token,)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error marking client leads read: {e}")
    finally:
        release_db_connection(conn)

    return redirect(url_for("client_portal_dashboard", msg="تم تحديد جميع إشعارات بوتك كمقروءة."))


@app.route("/client/export-leads", methods=["GET"])
def client_export_leads():
    bot_token = get_authorized_client_bot_token()
    if not bot_token:
        return "غير مصرح", 403

    conn = None
    rows = []
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, platform, sender_chat_id, sender_label, message_text, is_read, created_at FROM leads WHERE bot_token = %s ORDER BY id DESC",
            (bot_token,)
        )
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        logger.error(f"Client export error: {e}")
    finally:
        release_db_connection(conn)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Platform", "Sender ID", "Sender Label", "Message", "Is Read", "Created At"])
    for r in rows:
        writer.writerow([
            r["id"], r["platform"], r["sender_chat_id"],
            r["sender_label"], r["message_text"], r["is_read"], r["created_at"]
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=client_bot_notifications.csv"}
    )


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
    user_text = update.message.text.strip()
    bot = telebot.TeleBot(bot_token)
    markup = build_telegram_keyboard(client_data)

    sender_name = (update.message.from_user.first_name if update.message.from_user else "") or "مستخدم"
    sender_user = f"@{update.message.from_user.username}" if (update.message.from_user and update.message.from_user.username) else "بدون يوزر"
    sender_label = f"{sender_name} ({sender_user})"

    if user_text == "/start":
        welcome = client_data.get("welcome_message") or (
            f"أهلاً بك في {client_data['client_name']}! 👋\n"
            f"أنا مساعدك الذكي المتخصص في تصميم البوتات الذكية للأعمال.\n"
            f"لديك {DEMO_QUESTION_LIMIT} رسائل في هذه الجلسة التجريبية؛ أخبرني ما هو مشروعك وما الذي تريده في البوت الخاص بك؟"
        )
        bot.send_message(chat_id, welcome, reply_markup=markup)
        return "OK", 200

    if user_text == "🔄 محادثة جديدة":
        clear_user_context_keep_quota(bot_token, chat_id)
        bot.send_message(chat_id, "تم تجديد سياق المحادثة. تفضل بطرح تفاصيل مشروعك:", reply_markup=markup)
        return "OK", 200

    if user_text == "📞 تواصل مع موظف":
        handle_incoming_notification(
            client_data,
            "telegram",
            chat_id,
            sender_label,
            "📞 طلب تواصل مباشر: العميل ضغط على زر (تواصل مع موظف).",
            is_urgent=True
        )
        bot.send_message(
            chat_id,
            "تم إرسال طلب تواصلك بنجاح. سيتواصل معك المختص في أقرب وقت.",
            reply_markup=markup
        )
        return "OK", 200

    bot.send_chat_action(chat_id, "typing")
    reply_text = generate_ai_reply(client_data, chat_id, user_text, platform="telegram", sender_label=sender_label)
    bot.send_message(chat_id, reply_text, reply_markup=markup)
    return "OK", 200


# ================= 2. قناة واتساب الرسمي =================
@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    raw_body = request.get_data()
    if not verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        return "Invalid signature", 403

    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
                for message in value.get("messages", []):
                    if message.get("type") == "text" and phone_number_id:
                        sender_phone = str(message.get("from", ""))
                        user_text = (message.get("text") or {}).get("body", "").strip()
                        if not user_text:
                            continue

                        client_data = get_client(phone_number_id)
                        if client_data and client_data.get("is_active") and client_data.get("extra_token"):
                            reply_text = generate_ai_reply(
                                client_data, sender_phone, user_text,
                                platform="whatsapp", sender_label=f"WhatsApp ({sender_phone})"
                            )
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
                            requests.post(wa_url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"WhatsApp webhook error: {e}")

    return "OK", 200


# ================= 3. قناة إنستجرام =================
@app.route("/webhook/instagram", methods=["GET", "POST"])
def instagram_webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    raw_body = request.get_data()
    if not verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        return "Invalid signature", 403

    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            ig_id = str(entry.get("id", ""))
            for event in entry.get("messaging", []):
                sender_id = str((event.get("sender") or {}).get("id", ""))
                message = event.get("message") or {}
                if message.get("is_echo"):
                    continue
                user_text = (message.get("text") or "").strip()
                if sender_id and user_text and ig_id:
                    client_data = get_client(ig_id)
                    if client_data and client_data.get("is_active") and client_data.get("extra_token"):
                        reply_text = generate_ai_reply(
                            client_data, sender_id, user_text,
                            platform="instagram", sender_label=f"Instagram ({sender_id})"
                        )
                        ig_url = f"https://graph.facebook.com/v18.0/{ig_id}/messages"
                        headers = {
                            "Authorization": f"Bearer {client_data['extra_token']}",
                            "Content-Type": "application/json"
                        }
                        payload = {
                            "recipient": {"id": sender_id},
                            "message": {"text": reply_text}
                        }
                        requests.post(ig_url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Instagram webhook error: {e}")

    return "OK", 200


# ================= 4. قناة موقع الويب =================
@app.route("/api/webchat/<site_id>", methods=["POST"])
def webchat_api(site_id):
    client_data = get_client(site_id)
    if not client_data or not client_data.get("is_active"):
        return jsonify({"error": "Bot inactive or not found"}), 404

    payload = request.get_json(silent=True) or {}
    user_text = str(payload.get("message", "")).strip()
    session_id = str(payload.get("session_id", "")).strip() or (get_client_ip() or "web_visitor")

    rate_key = f"{site_id}:{session_id}:{get_client_ip()}"
    if is_webchat_rate_limited(rate_key):
        return jsonify({"error": "Too many requests. Please slow down."}), 429

    if not user_text:
        return jsonify({"error": "Empty message"}), 400

    reply_text = generate_ai_reply(
        client_data, session_id, user_text,
        platform="web", sender_label=f"زائر الموقع ({session_id})"
    )
    return jsonify({"reply": reply_text}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
   
   
