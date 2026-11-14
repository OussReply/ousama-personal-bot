# حفظ رد البوت في قاعدة البيانات
    save_message_to_db(chat_id, "assistant", reply_texte)

    # إرسال الرد للعميل
    envoyer_message_long(chat_id, texte_repondre)

except Exception as e:
    print("CRITICAL ERROR DURING CLAUDE CALL:")
    print(traceback.format_exc())
    bot.reply_to(message, "عذراً، أواجه ضغطاً مؤقتاً في النظام حالياً. يرجى إعادة محاولة إرسال رسالتك بعد لحظات.")


# تشغيل التطبيق وتهيئة قاعدة البيانات ورابط الويب هوك
if __name__ == "__main__":
    init_db()

    if WEBHOOK_URL:
        full_webhook_url = f"{WEBHOOK_URL.strip('/')}/{TELEGRAM_BOT_TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=full_webhook_url)
        print(f"Webhook configured successfully: {full_webhook_url}")

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
