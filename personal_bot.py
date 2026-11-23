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


DASHBOARD_HTML = """<!DOCTYPE html>
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
            <h1 style="margin:0; font-size: 21px;">🚀 OmniBot SaaS — لوحة القيادة والطلبات</h1>
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

                            <form method="POST" action="/admin/clear-leads" style="margin:0;" onsubmit="return confirm('مسح جميع إشعارات هذا البوت فقط؟');">
                                <input type="hidden" name="token" value="{{ c.bot_token }}">
                                <button type="submit" class="btn btn-dark">🧹 مسح الإشعارات</button>
                            </form>

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
