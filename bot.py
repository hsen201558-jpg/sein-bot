import os
import asyncio
import sqlite3
import logging
import nest_asyncio

from google import genai
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)

# ==================================
# 1. قراءة المفاتيح المحفوظة
# ==================================

TOKEN = os.environ.get("TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TOKEN or not GEMINI_API_KEY:
    raise ValueError(
        "Missing TOKEN or GEMINI_API_KEY. "
        "Check cells 2 and 5."
    )

client = genai.Client(api_key=GEMINI_API_KEY)

# ==================================
# 2. إعداد الموديلات البديلة
# ==================================

MODELS = [
    "gemini-1.5-flash",
    "gemini-2.5-flash",
    "gemini-1.5-pro"
]


# ==================================
# 3. إعداد الذاكرة SQLite
# ==================================

if os.path.exists("sein_memory.db"):
    DB_PATH = "sein_memory.db"
elif os.path.exists("memory.db"):
    DB_PATH = "memory.db"
else:
    DB_PATH = "sein_memory.db"

def init_database():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sein_messages_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def get_history(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT role, content
            FROM sein_messages_v2
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 12
        """, (str(user_id),)).fetchall()

    return list(reversed(rows))

def save_conversation(user_id, question, answer):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO sein_messages_v2
            (user_id, role, content)
            VALUES (?, 'user', ?)
        """, (str(user_id), question))

        conn.execute("""
            INSERT INTO sein_messages_v2
            (user_id, role, content)
            VALUES (?, 'model', ?)
        """, (str(user_id), answer))

        conn.commit()

# ==================================
# 4. شخصية SEIN AI
# ==================================

SYSTEM_PROMPT = """
أنت SEIN AI، مساعد ذكاء اصطناعي ذكي ومتطور.

قواعدك:
- تحدث باللهجة العراقية الطبيعية مع المستخدم.
- افهم السؤال قبل الإجابة.
- أعط إجابات دقيقة وعميقة ومفيدة.
- لا تختلق المعلومات إذا كنت لا تعرف.
- حلل المواضيع خطوة بخطوة عند الحاجة.
- تذكر سياق المحادثة السابقة واستفد منه.
- لا تكرر الإجابات بلا داعٍ.
- كن واضحاً ومباشراً وودوداً.
- إذا كان السؤال علمياً أو تقنياً، قدم إجابة صحيحة
  ومنظمة، ولا تضحي بالدقة من أجل الاختصار.
"""

# ==================================
# 5. نظام اكتشاف نفاد الحصة
# ==================================

class AllModelsUnavailable(Exception):
    pass

def is_quota_error(error):
    message = str(error).lower()
    code = getattr(error, "code", None)

    return (
        code == 429
        or "resource_exhausted" in message
        or "quota_exceeded" in message
        or "429" in message
    )

def is_model_unavailable(error):
    message = str(error).lower()
    code = getattr(error, "code", None)

    return (
        code == 404
        or "model not found" in message
        or "unsupported model" in message
        or "not supported for generatecontent" in message
    )

def generate_with_fallback(history, question):
    conversation = []

    for role, content in history:
        label = "المستخدم" if role == "user" else "SEIN AI"
        conversation.append(f"{label}: {content}")

    conversation.append(f"المستخدم: {question}")

    prompt = (
        SYSTEM_PROMPT
        + "\n\nسياق المحادثة:\n"
        + "\n".join(conversation)
        + "\n\nأجب عن آخر رسالة للمستخدم."
    )

    quota_count = 0
    last_error = None

    for model_name in MODELS:
        try:
            print(f"Trying model: {model_name}")

            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )

            answer = response.text

            if not answer:
                raise ValueError("Gemini returned an empty response.")

            print(f"Success: {model_name}")
            return answer, model_name

        except Exception as error:
            last_error = error

            if is_quota_error(error):
                quota_count += 1
                print(f"Quota exhausted: {model_name}")
                continue

            if is_model_unavailable(error):
                print(f"Model unavailable: {model_name}")
                continue

            raise

    raise AllModelsUnavailable(
        f"Quota failures: {quota_count}. Last error: {last_error}"
    )

# ==================================
# 6. أوامر البوت
# ==================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "هلا بيك، أني SEIN AI 🤖\n"
        "جاهز أساعدك وأناقشك وأجاوب على أسئلتك.\n"
        "احچي وياي براحتك."
    )

async def reset_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM sein_messages_v2 WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()

    await update.message.reply_text(
        "تم مسح ذاكرة محادثتنا والبدء من جديد."
    )

# ==================================
# 7. استقبال الرسائل والرد
# ==================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.message.text:
        return

    question = update.message.text.strip()
    user_id = str(update.effective_user.id)

    if not question:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing"
    )

    try:
        history = get_history(user_id)

        answer, used_model = await asyncio.to_thread(
            generate_with_fallback,
            history,
            question
        )

        save_conversation(user_id, question, answer)

        print(f"Answered using: {used_model}")

        await update.message.reply_text(answer)

    except AllModelsUnavailable:
        logging.exception("All Gemini models unavailable")

        await update.message.reply_text(
            "حالياً حصة نماذج Gemini غير متاحة. "
            "جرّب بعد فترة."
        )

    except Exception:
        logging.exception("Message processing error")

        await update.message.reply_text(
            "صار خطأ أثناء معالجة رسالتك. "
            "حاول مرة ثانية بعد قليل."
        )

# ==================================
# 8. تسجيل الأخطاء
# ==================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logging.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error
    )

# ==================================
# 9. منع تشغيل نسختين
# ==================================

async def stop_previous_app():
    for name in ("application", "app", "sein_app"):
        old = globals().get(name)

        if old is None:
            continue

        try:
            if old.updater and old.updater.running:
                await old.updater.stop()

            if old.running:
                await old.stop()

            await old.shutdown()

        except Exception as error:
            print(f"Old app cleanup: {error}")

# ==================================
# 10. تشغيل SEIN AI
# ==================================

async def run_sein():
    init_database()

    await stop_previous_app()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    globals()["application"] = application

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("reset", reset_memory)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    application.add_error_handler(error_handler)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        drop_pending_updates=False
    )

    print("================================")
    print("SEIN AI IS RUNNING SUCCESSFULLY")
    print("Database:", DB_PATH)
    print("Models:", len(MODELS))
    print("================================")

    try:
        await asyncio.Event().wait()

    finally:
        if application.updater.running:
            await application.updater.stop()

        if application.running:
            await application.stop()

        await application.shutdown()

if __name__ == "__main__":
    import asyncio
    asyncio.run(run_sein())



