import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from google import genai
from google.genai import types

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Initialize Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

# Corporate System Instruction
SYSTEM_INSTRUCTION = """
أنت ممثل خدمة العملاء الرسمي للمهندس أسامة. 
- أسلوبك مؤسسي، احترافي، ومهذب جداً.
- يجب أن تجيب بنفس اللغة التي يكتب بها المستخدم (عربية، فرنسية، إنجليزية... إلخ).
- يمنع منعاً باتاً مناقشة الأسعار، تقديم الخصومات، أو الاتفاق على أي مبالغ مالية تلقائياً.
- إذا استفسر المستخدم عن السعر أو الخصم أو تفاصيل مالية، لخص طلبه بأدب وأخبره بأن "المهندس أسامة" سيتواصل معه شخصياً لتزويده بالعرض المالي المناسب.
"""

MODEL_NAME = "gemini-flash-latest"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    reply_text = None
    
    for model_name in MODELS_TO_TRY:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user_text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.3
                )
            )
            reply_text = response.text
            break
        except Exception as e:
            logging.warning(f"Failed with model {model_name}: {e}")
            continue

    if not reply_text:
        reply_text = "عذراً، استلمنا استفسارك وتم تسجيله في نظام الدعم الفني الخاص بنا. سيتواصل معك المهندس أسامة قريباً."

    await update.message.reply_text(reply_text)

# Dummy Web Server to satisfy Render's Port requirement
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

if __name__ == '__main__':
    # Start the dummy web server in a background thread
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()

    # Start Telegram Bot with drop_pending_updates to clear old 409 conflicts
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Enterprise Bot with Web Server & Drop Pending Updates is running...")
    app.run_polling(drop_pending_updates=True)
