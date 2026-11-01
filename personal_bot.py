import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from google import genai

# إعداد التسجيل لمتابعة الأخطاء
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# جلب المفاتيح بأمان من بيئة العمل على Render
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# تهيئة عميل Gemini
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    
    # توجيهات نظامية مؤسسية صارمة مع حظر التسعير والخصومات
    system_instruction = (
        "أنت نظام الرد الآلي الذكي والممثل الرسمي لخدمة العملاء الخاصة بالمهندس أسامة وخدمات الأنظمة الصناعية والبرمجية. "
        "مهمتك استقبال استفسارات العملاء والشركات والرد عليها بلغة العميل تلقائياً وبأقصى درجات الاحترافية المؤسسية. "
        "يجب أن تتضمن إجابتك العناصر التالية: "
        "1. ترحيب رسمي ومهني باسم الخدمة/المؤسسة. "
        "2. تأكيد استلام الطلب وتلخيص سريع لفهم المشكلة أو الاستفسار بناءً على رسالة العميل. "
        "3. إفادة العميل بأن طلبه تم جدولته وسيقوم فريق العمل أو المهندس أسامة بمراجعته والرد عليه خلال ساعات العمل الرسمية. "
        "تنبيه صارم هام جداً: يُمنع عليك منعاً باتاً ذكر أي أسعار، أو تخمين تكاليف، أو عرض أي خصومات أو عروض مالية من رأسك مطلقاً. "
        "في حال يسأل العميل عن الأسعار أو الخصومات، أخبره بلباقة أن تحديد التكلفة والأسعار يتم حصرياً ودقيقاً بواسطة المهندس أسامة بعد مراجعة تفاصيل الطلب."
    )
    
    try:
        # إرسال الرسالة إلى نموذج Gemini مع التعليمات الجديدة
        response = gemini_client.models.generate_content(
            model='gemini-3.8-flash',
            contents=f"{system_instruction}\n\nرسالة العميل الواردة: {user_message}",
        )
        reply_text = response.text
    except Exception as e:
        reply_text = "عذراً، استلمنا استفسارك وتم تسجيله في نظام الدعم الفني الخاص بنا. سيتواصل معك المهندس أسامة قريباً."
        logging.error(f"Gemini Error: {e}")

    # إرسال الرد للعميل عبر تيليجرام
    await update.message.reply_text(reply_text)

if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # استقبال الرسائل النصية وتوجيهها لدالة المعالجة
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("Enterprise Bot with Strict Pricing Rules is running...")
    application.run_polling()
