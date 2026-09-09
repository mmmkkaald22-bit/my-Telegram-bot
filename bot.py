import os
import sqlite3
import logging
from datetime import datetime

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
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
AUTO_POINTS_ON_SUBMIT = 5  # نقاط تلقائية عند إرسال صورة إثبات

WAITING_NAME = 1

# ============ قاعدة البيانات ============
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            chat_id INTEGER,
            points INTEGER DEFAULT 0,
            joined_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_id TEXT,
            points_given INTEGER,
            reviewed INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def upsert_user(user_id, name, username, chat_id):
    conn = get_conn()
    row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row:
        conn.execute("UPDATE users SET name=?, username=?, chat_id=? WHERE user_id=?",
                     (name, username, chat_id, user_id))
    else:
        conn.execute(
            "INSERT INTO users (user_id, name, username, chat_id, points, joined_at) VALUES (?,?,?,?,0,?)",
            (user_id, name, username, chat_id, datetime.utcnow().isoformat())
        )
    conn.commit()
    conn.close()

def add_points(user_id, delta):
    conn = get_conn()
    conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def set_points(user_id, value):
    conn = get_conn()
    conn.execute("UPDATE users SET points = ? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row

def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY points DESC").fetchall()
    conn.close()
    return rows

def log_submission(user_id, file_id, points_given):
    conn = get_conn()
    conn.execute(
        "INSERT INTO submissions (user_id, file_id, points_given, created_at) VALUES (?,?,?,?)",
        (user_id, file_id, points_given, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

# ============ أوامر المستخدم ============
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
    """المستخدم يرسل صورة كإثبات إنجاز تحدي -> نقاط تلقائية + إشعار للأدمن"""
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("سجّل أول بـ /start")
        return

    file_id = update.message.photo[-1].file_id
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

# ============ التشغيل ============
def main():
    if not BOT_TOKEN:
        raise RuntimeError("لازم تحط BOT_TOKEN كمتغير بيئة")
    init_db()

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

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
