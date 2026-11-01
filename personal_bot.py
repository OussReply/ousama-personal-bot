import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from google import genai

# تحميل المتغيرات
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# إعداد عميل الذكاء الاصطناعي
client = genai.Client(api_key=GEMINI_API_KEY)

# 1. تعليمات النظام (System Prompt) مع دعم اللغات المتعددة واللهجات
SYSTEM_INSTRUCTIONS = """
Tu es l'assistant virtuel professionnel de l'ingénieur Oussama. / أنت المساعد الآلي الاحترافي للمهندس أسامة.
Règles strictes / قواعد صارمة:
1. Réponds dans la même langue ou le même dialecte que le client (Français, Arabe, Dialecte Algérien, Saoudien, ou Anglais). / أجب نفس لغة أو لهجة العميل (فرنسية، عربية، لهجة جزائرية، سعودية، أو إنجليزية).
2. L'ingénieur Oussama est spécialisé exclusivement dans :
   - La programmation et le développement de bots Telegram professionnels pour les boutiques, les entreprises et l'automatisation des services. (برمجة وتطوير بوتات تيليجرام الاحترافية للمتاجر، الشركات، وأتمتة الخدمات).
   - La rédaction et la conception de livres, manuels et dossiers techniques spécialisés. (إعداد وصياغة الكتب والدلائل والملفات التقنية المتخصصة).
3. Sois professionnel, concis et précis. / كن محترفاً، موجزاً ودقيقاً.
4. Interdiction absolue de donner des prix finaux ou des délais fermes. Dis toujours : "Je vais transmettre les détails de votre projet à l'ingénieur Oussama pour vous offrir la meilleure offre." / يمنع منعاً باتاً إعطاء أسعار نهائية أو مواعيد تسليم قاطعة، وقل دائماً إنك ستنقل التفاصيل للمهندس أسامة.
5. Pose des questions pour recueillir les besoins du client afin de les résumer pour lui et pour le propriétaire. / اسأل العميل عن تفاصيل طلبه لتلخيصها له وللمالك.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🤖 Bots Telegram (Boutiques & Entreprises)", callback_data="service_bot")],
        [InlineKeyboardButton("📚 Livres & Dossiers Techniques", callback_data="service_ebook")],
        [InlineKeyboardButton("💬 Autre demande / استفسار آخر", callback_data="service_custom")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "مرحباً بك! 👋 / Bienvenue !\n"
        "أنا المساعد الذكي للمهندس أسامة. / Je suis l'assistant virtuel de l'ingénieur Oussama.\n\n"
        "نحن متخصصون في / Nous sommes spécialisés en :\n"
        "• برمجة وتطوير بوتات تيليجرام الاحترافية للمتاجر والشركات 🤖\n"
        "• إعداد وصياغة الكتب والدلائل والملفات التقنية المخصصة 📚\n\n"
        "اختر من القائمة أدناه أو اكتب طلبك بأي لغة أو لهجة تفضلها!"
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "service_bot":
        response_text = "Parfait ! Décrivez votre boutique ou votre projet et les tâches que le bot doit automatiser. / ممتاز! أخبرني عن متجرك أو مشروعك والمهام التي تريد للبوت أن يقوم بها."
    elif query.data == "service_ebook":
        response_text = "Super ! Indiquez-moi le sujet ou le domaine technique pour lequel vous avez besoin d'un manuel ou d'un dossier. / رائع! أخبرني بالموضوع التقني الذي تحتاج فيه إلى دليل أو ملف متخصص."
    else:
        response_text = "تفضل بكتابة استفسارك بأي لغة أو لهجة وسأجيبك فوراً أو أحوله للمهندس أسامة."

    await query.edit_message_text(text=response_text)

# 2. الرد الآلي الذكي باستخدام Gemini
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user = update.message.from_user
    
    # إظهار حالة الكتابة
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        # إرسال الرسالة إلى Gemini مع التعليمات المحددة
        prompt = f"{SYSTEM_INSTRUCTIONS}\n\nرسالة العميل / Message du client: {user_text}"
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        
        ai_reply = response.text
        await update.message.reply_text(ai_reply)
        
        # إرسال إشعار لك بملخص المحادثة والطلب
        if OWNER_CHAT_ID:
            alert_msg = (
                f"📥 **طلب جديد جرى الرد عليه تلقائياً:**\n"
                f"👤 **العميل:** {user.first_name} (@{user.username or 'بدون معرف'})\n"
                f"💬 **رسالته:** {user_text}\n"
                f"🤖 **رد الذكاء:** {ai_reply}"
            )
            await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=alert_msg)
            
    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("شكراً لرسالتك! تم تسجيل طلبك وسيتواصل معك المهندس أسامة قريباً.")

def main():
    # زيادة مهلة الاتصال وقراءة البيانات لمنع انقطاع السيرفر
    app = ApplicationBuilder().token(TOKEN).read_timeout(60).connect_timeout(60).write_timeout(60).pool_timeout(60).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🚀 تم تشغيل البوت الذكي متعدد اللغات بنجاح...")
    app.run_polling()

if __name__ == "__main__":
    main()