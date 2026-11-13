"""
Telegram бот для совместного ведения списков.
Требует: python-telegram-bot>=22.0
Запуск:  BOT_TOKEN=ваш_токен python bot.py
"""

import os
import logging
import sqlite3
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)
from telegram.error import BadRequest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH   = os.getenv("DB_PATH", "lists.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

WAITING_LIST_NAME = 1
WAITING_ITEM_TEXT = 2
WAITING_RENAME    = 3
WAITING_EDIT_ITEM = 4

# ── База данных ───────────────────────────────────────────────────────────────

def init_db():
    with get_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS lists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                chat_id    INTEGER NOT NULL,
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
            CREATE TABLE IF NOT EXISTS pinned (
                chat_id    INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
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


# ── DB-хелперы ────────────────────────────────────────────────────────────────

def db_get_lists(chat_id):
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM lists WHERE chat_id=? ORDER BY created_at", (chat_id,)
        ).fetchall()

def db_get_list(list_id):
    with get_conn() as c:
        return c.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()

def db_get_items(list_id):
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE list_id=? ORDER BY id", (list_id,)
        ).fetchall()

def db_get_pinned(chat_id):
    with get_conn() as c:
        row = c.execute("SELECT message_id FROM pinned WHERE chat_id=?", (chat_id,)).fetchone()
        return row["message_id"] if row else None

def db_set_pinned(chat_id, message_id):
    with get_conn() as c:
        c.execute(
            "INSERT INTO pinned(chat_id, message_id) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id",
            (chat_id, message_id),
        )


# ── Рендер ────────────────────────────────────────────────────────────────────

def esc(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def render_pinned(chat_id: int) -> str:
    """Текст закреплённого сообщения — все списки чата."""
    lists = db_get_lists(chat_id)
    if not lists:
        return "📋 *Списки пока пусты*\n\nНажмите /lists чтобы создать первый\\."
    parts = []
    for lst in lists:
        items = db_get_items(lst["id"])
        header = f"📋 *{esc(lst['name'])}*"
        if not items:
            body = "  _пусто_"
        else:
            lines = []
            for item in items:
                mark = "✅" if item["done"] else "⬜"
                lines.append(f"  {mark} {esc(item['text'])}")
            body = "\n".join(lines)
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


def render_list(lst, items) -> str:
    lines = [f"📋 *{esc(lst['name'])}*\n"]
    if not items:
        lines.append("_Список пуст_")
    for item in items:
        mark = "✅" if item["done"] else "⬜"
        lines.append(f"{mark} {esc(item['text'])}")
    return "\n".join(lines)


# ── Клавиатуры ───────────────────────────────────────────────────────────────

def kb_lists(lists):
    rows = [[InlineKeyboardButton(f"📋 {lst['name']}", callback_data=f"open:{lst['id']}")]
            for lst in lists]
    rows.append([InlineKeyboardButton("➕ Новый список", callback_data="new_list")])
    return InlineKeyboardMarkup(rows)


def kb_list(list_id, items):
    rows = []
    for item in items:
        label = ("✅ " if item["done"] else "⬜ ") + item["text"]
        rows.append([
            InlineKeyboardButton(label,  callback_data=f"toggle:{list_id}:{item['id']}"),
            InlineKeyboardButton("✏️",   callback_data=f"edit_item:{list_id}:{item['id']}"),
            InlineKeyboardButton("🗑",    callback_data=f"del_item:{list_id}:{item['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ Добавить",          callback_data=f"add:{list_id}"),
        InlineKeyboardButton("🗑 Удалить отмеченные", callback_data=f"del_done:{list_id}"),
    ])
    rows.append([
        InlineKeyboardButton("✏️ Переименовать",  callback_data=f"rename:{list_id}"),
        InlineKeyboardButton("❌ Удалить список", callback_data=f"del_list:{list_id}"),
    ])
    rows.append([InlineKeyboardButton("🔙 К спискам", callback_data="show_lists")])
    return InlineKeyboardMarkup(rows)


# ── Обновление закреплённого сообщения ───────────────────────────────────────

async def refresh_pinned(chat_id: int, app: Application):
    """Обновить или создать закреплённое сообщение."""
    text = render_pinned(chat_id)
    pinned_id = db_get_pinned(chat_id)

    if pinned_id:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=pinned_id,
                text=text,
                parse_mode="MarkdownV2",
            )
            return
        except BadRequest:
            pass  # сообщение удалено — создадим новое

    msg = await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="MarkdownV2")
    db_set_pinned(chat_id, msg.message_id)
    try:
        await app.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id,
                                       disable_notification=True)
    except BadRequest:
        pass  # нет прав на закрепление — ничего страшного


# ── Показ меню списков ────────────────────────────────────────────────────────

async def show_lists(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit=False):
    chat_id = update.effective_chat.id
    lists = db_get_lists(chat_id)
    text = "📂 *Ваши общие списки*" if lists else "У вас ещё нет списков\\. Создайте первый\\!"
    kb = kb_lists(lists)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    else:
        msg = update.message or update.callback_query.message
        await msg.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)


async def open_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, list_id: int):
    lst = db_get_list(list_id)
    if not lst:
        await update.callback_query.answer("Список не найден", show_alert=True)
        return await show_lists(update, ctx, edit=True)
    items = db_get_items(list_id)
    await update.callback_query.edit_message_text(
        render_list(lst, items), parse_mode="MarkdownV2",
        reply_markup=kb_list(list_id, items),
    )


# ── Команды ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_lists(update, ctx)
    await refresh_pinned(update.effective_chat.id, ctx.application)

async def cmd_lists(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_lists(update, ctx)


# ── Callback ──────────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data  = q.data
    chat_id = update.effective_chat.id

    if data == "show_lists":
        return await show_lists(update, ctx, edit=True)

    if data == "new_list":
        await q.edit_message_text("Введите название нового списка:")
        ctx.user_data["state"] = WAITING_LIST_NAME
        return WAITING_LIST_NAME

    if data.startswith("open:"):
        return await open_list(update, ctx, int(data.split(":")[1]))

    if data.startswith("toggle:"):
        _, list_id, item_id = data.split(":")
        with get_conn() as c:
            c.execute("UPDATE items SET done = 1-done WHERE id=?", (int(item_id),))
        await open_list(update, ctx, int(list_id))
        await refresh_pinned(chat_id, ctx.application)
        return

    if data.startswith("add:"):
        list_id = int(data.split(":")[1])
        ctx.user_data["state"]       = WAITING_ITEM_TEXT
        ctx.user_data["target_list"] = list_id
        lst = db_get_list(list_id)
        await q.edit_message_text(
            f"Введите текст нового пункта для *{esc(lst['name'])}*:",
            parse_mode="MarkdownV2",
        )
        return WAITING_ITEM_TEXT

    if data.startswith("del_item:"):
        _, list_id, item_id = data.split(":")
        with get_conn() as c:
            c.execute("DELETE FROM items WHERE id=?", (int(item_id),))
        await open_list(update, ctx, int(list_id))
        await refresh_pinned(chat_id, ctx.application)
        return

    if data.startswith("edit_item:"):
        _, list_id, item_id = data.split(":")
        ctx.user_data["state"]       = WAITING_EDIT_ITEM
        ctx.user_data["target_list"] = int(list_id)
        ctx.user_data["target_item"] = int(item_id)
        with get_conn() as c:
            item = c.execute("SELECT text FROM items WHERE id=?", (int(item_id),)).fetchone()
        await q.edit_message_text(
            f"Введите новый текст для пункта:\n\n_{esc(item['text'])}_",
            parse_mode="MarkdownV2",
        )
        return WAITING_EDIT_ITEM

    if data.startswith("del_done:"):
        list_id = int(data.split(":")[1])
        with get_conn() as c:
            c.execute("DELETE FROM items WHERE list_id=? AND done=1", (list_id,))
        await open_list(update, ctx, list_id)
        await refresh_pinned(chat_id, ctx.application)
        return

    if data.startswith("rename:"):
        list_id = int(data.split(":")[1])
        ctx.user_data["state"]       = WAITING_RENAME
        ctx.user_data["target_list"] = list_id
        await q.edit_message_text("Введите новое название списка:")
        return WAITING_RENAME

    if data.startswith("del_list:"):
        list_id = int(data.split(":")[1])
        with get_conn() as c:
            c.execute("DELETE FROM lists WHERE id=?", (list_id,))
        await q.answer("Список удалён", show_alert=True)
        await show_lists(update, ctx, edit=True)
        await refresh_pinned(chat_id, ctx.application)
        return


# ── Приём текста ──────────────────────────────────────────────────────────────

async def recv_list_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return WAITING_LIST_NAME
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO lists (name, chat_id, created_by) VALUES (?,?,?)",
            (name, update.effective_chat.id, update.effective_user.id),
        )
        list_id = cur.lastrowid
    await update.message.delete()
    lst   = db_get_list(list_id)
    items = db_get_items(list_id)
    await update.message.reply_text(
        render_list(lst, items), parse_mode="MarkdownV2",
        reply_markup=kb_list(list_id, items),
    )
    await refresh_pinned(update.effective_chat.id, ctx.application)
    ctx.user_data.clear()
    return ConversationHandler.END


async def recv_item_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст не может быть пустым:")
        return WAITING_ITEM_TEXT
    list_id = ctx.user_data.get("target_list")
    with get_conn() as c:
        c.execute(
            "INSERT INTO items (list_id, text, added_by) VALUES (?,?,?)",
            (list_id, text, update.effective_user.id),
        )
    await update.message.delete()
    lst   = db_get_list(list_id)
    items = db_get_items(list_id)
    await update.message.reply_text(
        render_list(lst, items), parse_mode="MarkdownV2",
        reply_markup=kb_list(list_id, items),
    )
    await refresh_pinned(update.effective_chat.id, ctx.application)
    ctx.user_data.clear()
    return ConversationHandler.END


async def recv_edit_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст не может быть пустым:")
        return WAITING_EDIT_ITEM
    item_id = ctx.user_data.get("target_item")
    list_id = ctx.user_data.get("target_list")
    with get_conn() as c:
        c.execute("UPDATE items SET text=? WHERE id=?", (text, item_id))
    await update.message.delete()
    lst   = db_get_list(list_id)
    items = db_get_items(list_id)
    await update.message.reply_text(
        render_list(lst, items), parse_mode="MarkdownV2",
        reply_markup=kb_list(list_id, items),
    )
    await refresh_pinned(update.effective_chat.id, ctx.application)
    ctx.user_data.clear()
    return ConversationHandler.END


async def recv_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return WAITING_RENAME
    list_id = ctx.user_data.get("target_list")
    with get_conn() as c:
        c.execute("UPDATE lists SET name=? WHERE id=?", (name, list_id))
    await update.message.delete()
    lst   = db_get_list(list_id)
    items = db_get_items(list_id)
    await update.message.reply_text(
        render_list(lst, items), parse_mode="MarkdownV2",
        reply_markup=kb_list(list_id, items),
    )
    await refresh_pinned(update.effective_chat.id, ctx.application)
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Задайте BOT_TOKEN в переменных окружения")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback)],
        states={
            WAITING_LIST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_list_name)],
            WAITING_ITEM_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_item_text)],
            WAITING_EDIT_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_edit_item)],
            WAITING_RENAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_rename)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("lists", cmd_lists))
    app.add_handler(conv)

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
