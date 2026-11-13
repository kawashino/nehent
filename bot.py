"""
Telegram бот для совместного ведения списков.
Установка: pip install python-telegram-bot==20.7
Запуск:    BOT_TOKEN=9289519649:AAF0yts5prowXf9wpaN-QtoEXV0KZSj4mbs python bot.py
"""

import os
import logging
import sqlite3
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "lists.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ConversationHandler states
WAITING_LIST_NAME = 1
WAITING_ITEM_TEXT = 2
WAITING_RENAME_LIST = 3
WAITING_EDIT_ITEM = 4

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lists (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                chat_id   INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS items (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id   INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                text      TEXT    NOT NULL,
                done      INTEGER NOT NULL DEFAULT 0,
                added_by  INTEGER,
                added_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_lists(chat_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM lists WHERE chat_id=? ORDER BY created_at DESC", (chat_id,)
        ).fetchall()


def get_list(list_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()


def get_items(list_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM items WHERE list_id=? ORDER BY id", (list_id,)
        ).fetchall()


def render_list(lst, items) -> str:
    lines = [f"📋 *{escape(lst['name'])}*\n"]
    if not items:
        lines.append("_Список пуст_")
    for item in items:
        mark = "✅" if item["done"] else "⬜"
        lines.append(f"{mark} {escape(item['text'])}")
    return "\n".join(lines)


def escape(text: str) -> str:
    """Escape Markdown V2 special chars."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def list_keyboard(list_id: int, items):
    """Inline keyboard for a list view."""
    rows = []
    for item in items:
        label = ("✅ " if item["done"] else "⬜ ") + item["text"]
        rows.append([InlineKeyboardButton(
            label, callback_data=f"toggle:{list_id}:{item['id']}"
        )])
    rows.append([
        InlineKeyboardButton("➕ Добавить", callback_data=f"add:{list_id}"),
        InlineKeyboardButton("🗑 Удалить выбранные", callback_data=f"del_done:{list_id}"),
    ])
    rows.append([
        InlineKeyboardButton("✏️ Переименовать", callback_data=f"rename:{list_id}"),
        InlineKeyboardButton("❌ Удалить список", callback_data=f"del_list:{list_id}"),
    ])
    rows.append([InlineKeyboardButton("🔙 К спискам", callback_data="show_lists")])
    return InlineKeyboardMarkup(rows)


def lists_keyboard(lists):
    rows = [[InlineKeyboardButton(f"📋 {lst['name']}", callback_data=f"open:{lst['id']}")]
            for lst in lists]
    rows.append([InlineKeyboardButton("➕ Новый список", callback_data="new_list")])
    return InlineKeyboardMarkup(rows)


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_lists(update, ctx, via_command=True)


async def cmd_lists(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_lists(update, ctx, via_command=True)


async def show_lists(update: Update, ctx: ContextTypes.DEFAULT_TYPE, via_command=False):
    chat_id = update.effective_chat.id
    lists = get_lists(chat_id)
    text = "📂 *Ваши общие списки*" if lists else "У вас ещё нет списков\\. Создайте первый\\!"
    kb = lists_keyboard(lists)
    if via_command or update.message:
        await (update.message or update.callback_query.message).reply_text(
            text, parse_mode="MarkdownV2", reply_markup=kb
        )
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode="MarkdownV2", reply_markup=kb
        )


# ─── Callback handler ─────────────────────────────────────────────────────────

async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "show_lists":
        return await show_lists(update, ctx)

    if data == "new_list":
        await q.edit_message_text("Введите название нового списка:")
        ctx.user_data["state"] = WAITING_LIST_NAME
        ctx.user_data["edit_msg_id"] = q.message.message_id
        return WAITING_LIST_NAME

    if data.startswith("open:"):
        list_id = int(data.split(":")[1])
        return await open_list(update, ctx, list_id)

    if data.startswith("toggle:"):
        _, list_id, item_id = data.split(":")
        list_id, item_id = int(list_id), int(item_id)
        with get_conn() as conn:
            conn.execute("UPDATE items SET done = 1-done WHERE id=?", (item_id,))
        return await open_list(update, ctx, list_id)

    if data.startswith("add:"):
        list_id = int(data.split(":")[1])
        ctx.user_data["state"] = WAITING_ITEM_TEXT
        ctx.user_data["target_list"] = list_id
        lst = get_list(list_id)
        await q.edit_message_text(
            f"Введите текст нового пункта для списка *{escape(lst['name'])}*:",
            parse_mode="MarkdownV2",
        )
        return WAITING_ITEM_TEXT

    if data.startswith("del_done:"):
        list_id = int(data.split(":")[1])
        with get_conn() as conn:
            conn.execute("DELETE FROM items WHERE list_id=? AND done=1", (list_id,))
        return await open_list(update, ctx, list_id)

    if data.startswith("rename:"):
        list_id = int(data.split(":")[1])
        ctx.user_data["state"] = WAITING_RENAME_LIST
        ctx.user_data["target_list"] = list_id
        await q.edit_message_text("Введите новое название списка:")
        return WAITING_RENAME_LIST

    if data.startswith("del_list:"):
        list_id = int(data.split(":")[1])
        with get_conn() as conn:
            conn.execute("DELETE FROM lists WHERE id=?", (list_id,))
        await q.answer("Список удалён", show_alert=True)
        return await show_lists(update, ctx)


async def open_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, list_id: int):
    lst = get_list(list_id)
    if not lst:
        await update.callback_query.answer("Список не найден", show_alert=True)
        return await show_lists(update, ctx)
    items = get_items(list_id)
    text = render_list(lst, items)
    kb = list_keyboard(list_id, items)
    await update.callback_query.edit_message_text(
        text, parse_mode="MarkdownV2", reply_markup=kb
    )


# ─── Message handler (ConversationHandler) ───────────────────────────────────

async def receive_list_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым. Попробуйте ещё раз:")
        return WAITING_LIST_NAME
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO lists (name, chat_id, created_by) VALUES (?,?,?)",
            (name, chat_id, user_id),
        )
        list_id = cur.lastrowid
    await update.message.delete()
    items = get_items(list_id)
    lst = get_list(list_id)
    text = render_list(lst, items)
    kb = list_keyboard(list_id, items)
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    ctx.user_data.clear()
    return ConversationHandler.END


async def receive_item_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст не может быть пустым:")
        return WAITING_ITEM_TEXT
    list_id = ctx.user_data.get("target_list")
    user_id = update.effective_user.id
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO items (list_id, text, added_by) VALUES (?,?,?)",
            (list_id, text, user_id),
        )
    await update.message.delete()
    lst = get_list(list_id)
    items = get_items(list_id)
    kb = list_keyboard(list_id, items)
    await update.message.reply_text(
        render_list(lst, items), parse_mode="MarkdownV2", reply_markup=kb
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def receive_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return WAITING_RENAME_LIST
    list_id = ctx.user_data.get("target_list")
    with get_conn() as conn:
        conn.execute("UPDATE lists SET name=? WHERE id=?", (name, list_id))
    await update.message.delete()
    lst = get_list(list_id)
    items = get_items(list_id)
    kb = list_keyboard(list_id, items)
    await update.message.reply_text(
        render_list(lst, items), parse_mode="MarkdownV2", reply_markup=kb
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Задайте BOT_TOKEN в переменных окружения")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback)],
        states={
            WAITING_LIST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_list_name)
            ],
            WAITING_ITEM_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_item_text)
            ],
            WAITING_RENAME_LIST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rename)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=False,   # ← важно: state общий для чата, не на пользователя
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("lists", cmd_lists))
    app.add_handler(conv)

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
