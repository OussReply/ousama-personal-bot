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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - omnibot_saas - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
ALERT_BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "").strip()
DEFAULT_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "ousama_verify_2026")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()

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


_failed_admin_attempts = {}
_webchat_hits = {}

ADMIN_MAX_FAILS = 5
ADMIN_LOCK_SECONDS = 300
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


def safe_compare_secret(provided_secret, expected_secret):
    if not provided_secret or not expected_secret:
        return False
    try:
        return hmac.compare_digest(
            str(provided_secret).encode("utf-8"),
            str(expected_secret).encode("utf-8")
        )
    except Exception:
        return False


def verify_meta_signature(raw_body_bytes, signature_header):
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


DEMO_QUESTION_LIMIT = int(os.environ.get("DEMO_QUESTION_LIMIT", "10"))
DEMO_LIMIT_MESSAGE = os.environ.get(
    "DEMO_LIMIT_MESSAGE",
    "✨ لقد أتممت 10 رسائل في هذه الجلسة التجريبية، وأتمنى أن تكون لمست كيف يعمل المساعد الذكي.\n"
    "إذا كنت ترغب في تجربة إضافية أو بناء بوت متكامل ومخصص بالكامل لنشاطك التجاري، يسعدني جداً تواصلك المباشر عبر زر (📞 تواصل مع موظف) لنبدأ تجهيز نسختك الخاصة."
)

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
    if safe_compare_secret(req_secret, ADMIN_SECRET):
        clear_client_session_keys()
        session["session_owner"] = "admin"
        session["admin_auth"] = True
        return True
    return False


def get_authorized_client_bot_token():
    if session.get("session_owner") != "client" or session.get("client_auth") is not True:
        return None
    bt = session.get("client_bot_token")
    return str(bt).strip() if bt else None


def generate_demo_user_token(bot_token, sender_chat_id):
    payload = f"{str(bot_token).strip()}:{str(sender_chat_id).strip()}".encode("utf-8")
    secret = str(app.secret_key).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()[:24]


def verify_demo_user_token(bot_token, sender_chat_id, provided_sig):
    if not bot_token or not sender_chat_id or not provided_sig:
        return False
    expected = generate_demo_user_token(bot_token, sender_chat_id)
    return hmac.compare_digest(expected.encode("utf-8"), str(provided_sig).strip().encode("utf-8"))


def build_demo_user_portal_url(host_url, bot_token, sender_chat_id):
    clean_host = (host_url or "").rstrip("/").replace("http://", "https://")
    if not clean_host:
        return ""
    sig = generate_demo_user_token(bot_token, sender_chat_id)
    return f"{clean_host}/client/demo/{bot_token}/{sender_chat_id}/{sig}"


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
        types.KeyboardButton("👁️ لوحتي التجريبية"),
        types.KeyboardButton("📞 تواصل مع موظف"),
        types.KeyboardButton("🔄 محادثة جديدة")
    )
    return markup


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
        return ai_reply_text.strip(), False

    extracted_data = match.group(1).strip()
    clean_reply_for_user = re.sub(pattern, "", ai_reply_text, flags=re.DOTALL).strip()
    created = False

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
        created = True

    return clean_reply_for_user, created


def generate_ai_reply(client_data, chat_id, user_text, platform="telegram", sender_label="عميل", base_url=""):
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
        clean_reply, lead_created = extract_and_notify_lead(client_data, platform, chat_id, sender_label, raw_reply)

        effective_base = (base_url or PUBLIC_BASE_URL).strip()
        if lead_created and effective_base and platform == "telegram":
            demo_url = build_demo_user_portal_url(effective_base, bot_id, chat_id)
            if demo_url:
                clean_reply += f"\n\n👁️ يمكنك معاينة طلبك في لوحتك الخاصة من هنا:\n{demo_url}"

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
        .live-banner { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; background: linear-gradient(90deg, #172554, #1e293b); border: 1px solid #38bdf8; padding: 14px 18px; border-radius: 12px; margin-bottom: 1
