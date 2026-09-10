import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

import psycopg2
import psycopg2.extras

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ الإعدادات ============
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL")
AUTO_POINTS_ON_SUBMIT = 5  # نقاط تلقائية عند إرسال صورة إثبات

WAITING_NAME = 1

# ============ قاعدة البيانات (Supabase / Postgres دائمة) ============
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            username TEXT,
            chat_id BIGINT,
            points INTEGER DEFAULT 0,
            joined_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            file_id TEXT,
            points_given INTEGER,
            reviewed INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def upsert_user(user_id, name, username, chat_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE users SET name=%s, username=%s, chat_id=%s WHERE user_id=%s",
                     (name, username, chat_id, user_id))
    else:
        cur.execute(
            "INSERT INTO users (user_id, name, username, chat_id, points, joined_at) VALUES (%s,%s,%s,%s,0,%s)",
            (user_id, name, username, chat_id, datetime.utcnow().isoformat())
        )
    conn.commit()
    cur.close()
    conn.close()

def add_points(user_id, delta):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET points = points + %s WHERE user_id=%s", (delta, user_id))
    conn.commit()
    cur.close()
    conn.close()

def set_points(user_id, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET points = %s WHERE user_id=%s", (value, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def get_all_users():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users ORDER BY points DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def delete_user(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()

def log_submission(user_id, file_id, points_given):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO submissions (user_id, file_id, points_given, created_at) VALUES (%s,%s,%s,%s)",
        (user_id, file_id, points_given, datetime.utcnow().isoformat())
    )
    conn.commit()
    cur.close()
    conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existing = get_user(user.id)
    if existing:
        await update.message.reply_text(f"أهلاً {existing['name']} 👋\nنقاطك الحالية: {existing['points']}")
        return ConversationHandler.END
    await update.message.reply_text("أهلاً وسهلاً! 🎉\nوش اسمك اللي تبي نسجله؟")
    return WAITING_NAME

async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = update.message.text.strip()
    upsert_user(user.id, name, user.username or "", update.effective_chat.id)
    await update.message.reply_text(f"تم تسجيلك يا {name} ✅\nنقاطك الحالية: 0")

    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            f"📝 تسجيل جديد\nالاسم: {name}\nاليوزر: @{user.username or 'لا يوجد'}\nID: {user.id}"
        )
    return ConversationHandler.END

async def my_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("لسا ما سجلت، اكتب /start أول.")
        return
    await update.message.reply_text(f"نقاطك: {user['points']} 🏆")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    لو الأدمن أرسل صورة مع كابشن يبدأ بـ /broadcast أو /challenge -> يوزع التحدي بالصورة
    غير كذا: يعتبرها المستخدم صورة إثبات -> نقاط تلقائية + إشعار للأدمن
    """
    caption = (update.message.caption or "").strip()
    file_id = update.message.photo[-1].file_id

    # ====== حالة: الأدمن يرسل تحدي مع صورة ======
    if is_admin(update) and caption.startswith("/broadcast"):
        text = caption[len("/broadcast"):].strip()
        if not text:
            await update.message.reply_text("لازم تكتب نص التحدي بعد /broadcast بنفس الرسالة.")
            return
        users = get_all_users()
        sent = 0
        for u in users:
            try:
                await context.bot.send_photo(u["user_id"], photo=file_id, caption="🔥 تحدي جديد!\n\n" + text)
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(f"تم إرسال التحدي بالصورة لـ {sent} شخص.")
        return

    if is_admin(update) and caption.startswith("/challenge"):
        parts = caption[len("/challenge"):].strip().split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("لازم تكتب: /challenge <user_id> <نص التحدي> بنفس الرسالة.")
            return
        try:
            target_id = int(parts[0])
            text = parts[1]
            await context.bot.send_photo(target_id, photo=file_id, caption="🔥 تحدي جديد لك!\n\n" + text)
            await update.message.reply_text("تم إرسال التحدي بالصورة ✅")
        except Exception as e:
            await update.message.reply_text(f"صار خطأ: {e}")
        return

    if is_admin(update) and caption.startswith("/sendgroup"):
        parts = caption[len("/sendgroup"):].strip().split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("لازم تكتب: /sendgroup <chat_id> <نص التحدي> بنفس الرسالة.")
            return
        try:
            target_chat = int(parts[0])
            text = parts[1]
            await context.bot.send_photo(target_chat, photo=file_id, caption="🔥 تحدي جديد!\n\n" + text)
            await update.message.reply_text("تم إرسال التحدي للمجموعة بالصورة ✅")
        except Exception as e:
            await update.message.reply_text(f"صار خطأ: {e}")
        return

    # ====== حالة: مستخدم يرسل صورة إثبات ======
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("سجّل أول بـ /start")
        return

    add_points(user["user_id"], AUTO_POINTS_ON_SUBMIT)
    log_submission(user["user_id"], file_id, AUTO_POINTS_ON_SUBMIT)

    await update.message.reply_text(
        f"استلمت إثباتك ✅\n+{AUTO_POINTS_ON_SUBMIT} نقاط تلقائي\nممكن الأدمن يعدل النقاط بعد المراجعة."
    )

    if ADMIN_ID:
        caption = f"📸 إثبات جديد من {user['name']} (ID: {user['user_id']})\nنقاط تلقائية: +{AUTO_POINTS_ON_SUBMIT}"
        await context.bot.send_photo(ADMIN_ID, photo=file_id, caption=caption)

# ============ أوامر الأدمن ============
def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("ما فيه مسجلين لسا.")
        return
    text = "📋 المسجلين:\n\n"
    for u in users:
        text += f"• {u['name']} (ID: {u['user_id']}) - نقاط: {u['points']}\n"
    await update.message.reply_text(text)

async def add_points_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الاستخدام: /addpoints <user_id> <عدد النقاط>"""
    if not is_admin(update):
        return
    try:
        user_id = int(context.args[0])
        delta = int(context.args[1])
        add_points(user_id, delta)
        u = get_user(user_id)
        await update.message.reply_text(f"تم. نقاط {u['name']} الآن: {u['points']}")
        await context.bot.send_message(user_id, f"🎉 حصلت على {delta} نقاط إضافية من الأدمن!")
    except Exception:
        await update.message.reply_text("الاستخدام: /addpoints <user_id> <عدد_النقاط>")

async def set_points_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الاستخدام: /setpoints <user_id> <عدد النقاط>"""
    if not is_admin(update):
        return
    try:
        user_id = int(context.args[0])
        value = int(context.args[1])
        set_points(user_id, value)
        u = get_user(user_id)
        await update.message.reply_text(f"تم. نقاط {u['name']} الآن: {u['points']}")
    except Exception:
        await update.message.reply_text("الاستخدام: /setpoints <user_id> <عدد_النقاط>")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الاستخدام: /broadcast <نص التحدي> -> يرسل للجميع"""
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /broadcast <نص التحدي>")
        return
    text = "🔥 تحدي جديد!\n\n" + " ".join(context.args)
    users = get_all_users()
    sent = 0
    for u in users:
        try:
            await context.bot.send_message(u["user_id"], text)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"تم الإرسال لـ {sent} شخص.")

async def challenge_to_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الاستخدام: /challenge <user_id> <نص التحدي> -> يرسل لشخص محدد"""
    if not is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("الاستخدام: /challenge <user_id> <نص التحدي>")
        return
    try:
        user_id = int(context.args[0])
        text = "🔥 تحدي جديد لك!\n\n" + " ".join(context.args[1:])
        await context.bot.send_message(user_id, text)
        await update.message.reply_text("تم الإرسال ✅")
    except Exception as e:
        await update.message.reply_text(f"صار خطأ: {e}")

async def delete_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الاستخدام: /deleteuser <user_id> -> يحذف مستخدم من القائمة"""
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /deleteuser <user_id>")
        return
    try:
        user_id = int(context.args[0])
        u = get_user(user_id)
        if not u:
            await update.message.reply_text("ما فيه مستخدم بهذا الرقم.")
            return
        delete_user(user_id)
        await update.message.reply_text(f"تم حذف {u['name']} ✅")
    except Exception as e:
        await update.message.reply_text(f"صار خطأ: {e}")


    """أي عضو يكتبه داخل مجموعة -> يطلع رقم المجموعة (chat_id)"""
    chat = update.effective_chat
    await update.message.reply_text(f"معرف هذه المحادثة (Chat ID):\n`{chat.id}`", parse_mode="Markdown")

async def send_to_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الاستخدام: /sendgroup <chat_id> <نص> -> يرسل لأي مجموعة البوت عضو فيها"""
    if not is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("الاستخدام: /sendgroup <chat_id> <نص التحدي>")
        return
    try:
        chat_id = int(context.args[0])
        text = "🔥 تحدي جديد!\n\n" + " ".join(context.args[1:])
        await context.bot.send_message(chat_id, text)
        await update.message.reply_text("تم الإرسال للمجموعة ✅")
    except Exception as e:
        await update.message.reply_text(f"صار خطأ: {e}")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args):
        pass  # تجاهل سجلات الطلبات حتى ما تزحم الـ logs

def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    server.serve_forever()

# ============ التشغيل ============
def main():
    if not BOT_TOKEN:
        raise RuntimeError("لازم تحط BOT_TOKEN كمتغير بيئة")
    if not DATABASE_URL:
        raise RuntimeError("لازم تحط DATABASE_URL كمتغير بيئة (رابط قاعدة بيانات Supabase)")
    init_db()

    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    reg_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={WAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)]},
        fallbacks=[]
    )

    app.add_handler(reg_handler)
    app.add_handler(CommandHandler("points", my_points))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # أوامر الأدمن
    app.add_handler(CommandHandler("users", list_users))
    app.add_handler(CommandHandler("addpoints", add_points_cmd))
    app.add_handler(CommandHandler("setpoints", set_points_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("challenge", challenge_to_cmd))
    app.add_handler(CommandHandler("groupid", group_id_cmd))
    app.add_handler(CommandHandler("sendgroup", send_to_group_cmd))
    app.add_handler(CommandHandler("deleteuser", delete_user_cmd))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
