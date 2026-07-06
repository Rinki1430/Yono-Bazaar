"""
Premium Telegram Bot
=====================

A production-ready Telegram bot featuring:
    - Join request approval workflow
    - Smart, step-by-step post creator with auto-formatting
    - Instant publishing and scheduled publishing (APScheduler)
    - Admin dashboard with live statistics
    - SQLite persistence (users, join requests, scheduled posts, settings, logs)

Run with:
    pip install -r requirements.txt
    python bot.py

Author: Senior Python / Telegram Bot Engineering
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from html import escape as html_escape
from typing import Any, Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from telegram import (
    Chat,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0") or 0)
CHANNEL_ID: str = os.getenv("CHANNEL_ID", "")
DEFAULT_TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kolkata")
DB_PATH: str = os.getenv("DB_PATH", "bot_database.db")
LOG_PATH: str = os.getenv("LOG_PATH", "bot.log")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Please set it in your .env file.")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing. Please set it in your .env file.")

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("premium_bot")

# --------------------------------------------------------------------------- #
# Conversation states
# --------------------------------------------------------------------------- #

(
    CP_TEXT,
    CP_MEDIA,
    CP_FORMAT,
    CP_PREVIEW,
    CP_EDIT_TEXT,
    CP_SCHEDULE_DATE,
    CP_SCHEDULE_TIME,
    CP_SCHEDULE_TZ,
    CP_SCHEDULE_CONFIRM,
) = range(9)

EDIT_SCHEDULED_TEXT = 100

# --------------------------------------------------------------------------- #
# Database layer
# --------------------------------------------------------------------------- #


class Database:
    """Thin synchronous SQLite wrapper used from async handlers via a lock.

    SQLite access is fast/local, so we guard it with an asyncio.Lock and run
    the blocking calls in a thread executor to keep the event loop responsive.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS join_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    requested_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    media_type TEXT,
                    media_file_id TEXT,
                    parse_mode TEXT NOT NULL DEFAULT 'HTML',
                    scheduled_time_utc TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    message_id INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    async def init(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._init_schema)
        logger.info("Database initialized at %s", self.path)

    async def execute(self, query: str, params: tuple = ()) -> int:
        """Run an INSERT/UPDATE/DELETE query and return lastrowid."""

        def _run() -> int:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(query, params)
                conn.commit()
                return cur.lastrowid or 0
            finally:
                conn.close()

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def fetchone(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        def _run() -> Optional[sqlite3.Row]:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(query, params)
                return cur.fetchone()
            finally:
                conn.close()

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def fetchall(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        def _run() -> list[sqlite3.Row]:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(query, params)
                return cur.fetchall()
            finally:
                conn.close()

        async with self._lock:
            return await asyncio.to_thread(_run)


db = Database(DB_PATH)


async def log_action(level: str, action: str, details: str = "") -> None:
    """Persist an audit-log entry and mirror it into the standard logger."""
    now = datetime.utcnow().isoformat()
    try:
        await db.execute(
            "INSERT INTO logs (level, action, details, created_at) VALUES (?, ?, ?, ?)",
            (level, action, details, now),
        )
    except Exception:  # pragma: no cover - logging must never crash the bot
        logger.exception("Failed to persist log entry")
    getattr(logger, level.lower(), logger.info)("%s | %s", action, details)


async def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    existing = await db.fetchone("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if existing is None:
        await db.execute(
            "INSERT INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
            (user_id, username or "", first_name or "", datetime.utcnow().isoformat()),
        )


# --------------------------------------------------------------------------- #
# Formatting helpers (Feature 3 — Auto Editing)
# --------------------------------------------------------------------------- #


def auto_format_text(text: str) -> str:
    """Clean up raw text before publishing without changing its meaning.

    - Collapses repeated spaces/tabs
    - Removes duplicate blank lines
    - Trims leading/trailing whitespace
    - Preserves emojis, links, and hashtags untouched
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]

    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        if line == "":
            if previous_blank:
                continue
            previous_blank = True
        else:
            previous_blank = False
        cleaned.append(line)

    while cleaned and cleaned[0] == "":
        cleaned.pop(0)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()

    return "\n".join(cleaned)


FORMAT_CHOICES: dict[str, dict[str, Any]] = {
    "bold": {
        "label": "𝗕 Bold",
        "parse_mode": ParseMode.HTML,
        "wrap": lambda t: f"<b>{html_escape(t)}</b>",
    },
    "italic": {
        "label": "𝑰 Italic",
        "parse_mode": ParseMode.HTML,
        "wrap": lambda t: f"<i>{html_escape(t)}</i>",
    },
    "code": {
        "label": "💻 Code",
        "parse_mode": ParseMode.HTML,
        "wrap": lambda t: f"<code>{html_escape(t)}</code>",
    },
    "html": {
        "label": "🌐 HTML (raw)",
        "parse_mode": ParseMode.HTML,
        "wrap": lambda t: t,
    },
    "markdown": {
        "label": "✍ Markdown (raw)",
        "parse_mode": ParseMode.MARKDOWN,
        "wrap": lambda t: t,
    },
}


def build_final_text(draft: dict[str, Any]) -> str:
    """Apply auto-formatting + the chosen wrap style to the draft text."""
    cleaned = auto_format_text(draft.get("raw_text", ""))
    fmt_key = draft.get("format", "html")
    wrap_fn = FORMAT_CHOICES[fmt_key]["wrap"]
    return wrap_fn(cleaned)


# --------------------------------------------------------------------------- #
# Keyboard builders
# --------------------------------------------------------------------------- #


def kb_admin_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Statistics", callback_data="adm_stats")],
            [
                InlineKeyboardButton("📨 Pending Requests", callback_data="adm_pending"),
                InlineKeyboardButton("📅 Scheduled Posts", callback_data="adm_scheduled"),
            ],
            [InlineKeyboardButton("📝 Create Post", callback_data="adm_createpost")],
            [
                InlineKeyboardButton("⚙ Settings", callback_data="adm_settings"),
                InlineKeyboardButton("📢 Publish", callback_data="adm_publish"),
            ],
        ]
    )


def kb_home_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="adm_home")]])


def kb_join_request(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"jr_approve_{request_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"jr_reject_{request_id}"),
            ]
        ]
    )


def kb_media_step() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏭ Skip (text only)", callback_data="cp_skip_media")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cp_cancel")],
        ]
    )


def kb_format_step() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(v["label"], callback_data=f"cp_fmt_{k}")]
        for k, v in FORMAT_CHOICES.items()
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cp_cancel")])
    return InlineKeyboardMarkup(rows)


def kb_preview() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏ Edit Text", callback_data="cp_edit_text"),
                InlineKeyboardButton("🖼 Change Media", callback_data="cp_change_media"),
            ],
            [InlineKeyboardButton("👁 Preview", callback_data="cp_preview")],
            [
                InlineKeyboardButton("✅ Publish Now", callback_data="cp_publish_now"),
                InlineKeyboardButton("📅 Schedule", callback_data="cp_schedule"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cp_cancel")],
        ]
    )


def kb_confirm_schedule() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm", callback_data="cp_sched_confirm")],
            [InlineKeyboardButton("⬅ Back", callback_data="cp_preview")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cp_cancel")],
        ]
    )


def kb_scheduled_post_row(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏ Edit", callback_data=f"sch_edit_{post_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"sch_delete_{post_id}"),
                InlineKeyboardButton("▶ Publish Now", callback_data=f"sch_publish_{post_id}"),
            ]
        ]
    )


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #


def admin_only(func):
    """Decorator that blocks any handler for non-admin users."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user is None or user.id != ADMIN_ID:
            if update.callback_query:
                await update.callback_query.answer("🚫 Admins only.", show_alert=True)
            elif update.message:
                await update.message.reply_text("🚫 This command is restricted to the bot admin.")
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# Basic commands
# --------------------------------------------------------------------------- #


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username, user.first_name)
    await log_action("INFO", "User started bot", f"user_id={user.id}")

    text = (
        "✨ <b>Welcome!</b> ✨\n\n"
        "This is a premium content management bot.\n\n"
        "If you are the administrator, use /admin to open the dashboard."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_admin_dashboard(update, context)


async def show_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🛠 <b>Admin Dashboard</b>\n\n"
        "Manage your channel content, review join requests, "
        "and monitor performance — all from here.\n\n"
        "Choose an option below 👇"
    )
    if update.callback_query:
        await safe_edit(update.callback_query.message, text, kb_admin_dashboard())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_admin_dashboard())


async def safe_edit(
    message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    """Edit a message, silently ignoring 'message is not modified' errors."""
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.warning("Edit failed: %s", exc)
            try:
                await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            except TelegramError:
                logger.exception("Fallback reply also failed")


@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=kb_home_only())
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Feature 8 — Statistics
# --------------------------------------------------------------------------- #


async def gather_statistics() -> dict[str, int]:
    today = datetime.utcnow().date().isoformat()

    total_users_row = await db.fetchone("SELECT COUNT(*) AS c FROM users")
    pending_row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM join_requests WHERE status = 'pending'"
    )
    approved_today_row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM join_requests WHERE status = 'approved' AND substr(resolved_at, 1, 10) = ?",
        (today,),
    )
    rejected_today_row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM join_requests WHERE status = 'rejected' AND substr(resolved_at, 1, 10) = ?",
        (today,),
    )
    scheduled_row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM scheduled_posts WHERE status = 'pending'"
    )
    published_row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM scheduled_posts WHERE status = 'published'"
    )

    return {
        "total_users": total_users_row["c"] if total_users_row else 0,
        "pending_requests": pending_row["c"] if pending_row else 0,
        "approved_today": approved_today_row["c"] if approved_today_row else 0,
        "rejected_today": rejected_today_row["c"] if rejected_today_row else 0,
        "scheduled_posts": scheduled_row["c"] if scheduled_row else 0,
        "published_posts": published_row["c"] if published_row else 0,
    }


async def cb_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    stats = await gather_statistics()
    text = (
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{stats['total_users']}</b>\n"
        f"📨 Pending Join Requests: <b>{stats['pending_requests']}</b>\n"
        f"✅ Approved Today: <b>{stats['approved_today']}</b>\n"
        f"❌ Rejected Today: <b>{stats['rejected_today']}</b>\n"
        f"📅 Scheduled Posts: <b>{stats['scheduled_posts']}</b>\n"
        f"📢 Published Posts: <b>{stats['published_posts']}</b>\n"
    )
    await safe_edit(query.message, text, kb_home_only())


# --------------------------------------------------------------------------- #
# Feature 1 — Join Request Approval
# --------------------------------------------------------------------------- #


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request: ChatJoinRequest = update.chat_join_request
    user = request.from_user
    chat = request.chat

    row_id = await db.execute(
        """INSERT INTO join_requests (user_id, username, first_name, chat_id, status, requested_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (user.id, user.username or "", user.first_name or "", chat.id, datetime.utcnow().isoformat()),
    )
    await upsert_user(user.id, user.username, user.first_name)
    await log_action("INFO", "Join request received", f"user_id={user.id} chat_id={chat.id}")

    username_display = f"@{user.username}" if user.username else "N/A"
    text = (
        "📨 <b>New Join Request</b>\n\n"
        f"👤 Name: <b>{html_escape(user.full_name)}</b>\n"
        f"🔗 Username: {username_display}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"📢 Channel: {html_escape(chat.title or str(chat.id))}\n\n"
        "What would you like to do?"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_join_request(row_id),
        )
    except TelegramError:
        logger.exception("Failed to notify admin about join request")


async def cb_join_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("⏳ Processing...")

    action, request_id_str = query.data.rsplit("_", 1)
    request_id = int(request_id_str)

    row = await db.fetchone("SELECT * FROM join_requests WHERE id = ?", (request_id,))
    if row is None:
        await safe_edit(query.message, "⚠ This request no longer exists.")
        return
    if row["status"] != "pending":
        await safe_edit(query.message, "ℹ This request has already been resolved.")
        return

    approve = action == "jr_approve"
    try:
        if approve:
            await context.bot.approve_chat_join_request(chat_id=row["chat_id"], user_id=row["user_id"])
        else:
            await context.bot.decline_chat_join_request(chat_id=row["chat_id"], user_id=row["user_id"])
    except TelegramError as exc:
        logger.exception("Failed to resolve join request")
        await safe_edit(query.message, f"⚠ Telegram error: {html_escape(str(exc))}")
        return

    new_status = "approved" if approve else "rejected"
    await db.execute(
        "UPDATE join_requests SET status = ?, resolved_at = ? WHERE id = ?",
        (new_status, datetime.utcnow().isoformat(), request_id),
    )
    await log_action("INFO", f"Join request {new_status}", f"request_id={request_id}")

    icon = "✅" if approve else "❌"
    label = "approved" if approve else "rejected"
    await safe_edit(
        query.message,
        f"{icon} <b>Request {label}</b>\n\n"
        f"👤 {html_escape(row['first_name'] or str(row['user_id']))} has been {label}.",
    )


async def cb_admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    rows = await db.fetchall(
        "SELECT * FROM join_requests WHERE status = 'pending' ORDER BY requested_at DESC LIMIT 15"
    )
    if not rows:
        await safe_edit(query.message, "📨 <b>Pending Join Requests</b>\n\nNo pending requests. 🎉", kb_home_only())
        return

    await safe_edit(
        query.message,
        f"📨 <b>Pending Join Requests</b> ({len(rows)})\n\nSending each request below 👇",
        kb_home_only(),
    )
    for row in rows:
        username_display = f"@{row['username']}" if row["username"] else "N/A"
        text = (
            f"👤 <b>{html_escape(row['first_name'] or 'Unknown')}</b>\n"
            f"🔗 {username_display}\n"
            f"🆔 <code>{row['user_id']}</code>"
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_join_request(row["id"]),
        )


# --------------------------------------------------------------------------- #
# Feature 2, 3, 4 — Smart Post Creator / Auto-Editing / Publish
# --------------------------------------------------------------------------- #


def fresh_draft() -> dict[str, Any]:
    return {
        "raw_text": "",
        "media_type": None,
        "media_file_id": None,
        "format": "html",
    }


@admin_only
async def createpost_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"] = fresh_draft()
    text = (
        "📝 <b>Smart Post Creator</b>\n\n"
        "Step 1️⃣ of 4 — Send me the <b>text</b> of your post.\n\n"
        "You can include links, #hashtags and emojis freely 🚀"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query.message, text)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    return CP_TEXT


async def createpost_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data.setdefault("draft", fresh_draft())
    draft["raw_text"] = update.message.text or update.message.caption or ""

    text = (
        "🖼 Step 2️⃣ of 4 — Send a <b>photo, video, or document</b> to attach.\n\n"
        "Or tap ⏭ Skip to continue with text only."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_media_step())
    return CP_MEDIA


async def createpost_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data.setdefault("draft", fresh_draft())
    message = update.message

    if message.photo:
        draft["media_type"] = "photo"
        draft["media_file_id"] = message.photo[-1].file_id
    elif message.video:
        draft["media_type"] = "video"
        draft["media_file_id"] = message.video.file_id
    elif message.document:
        draft["media_type"] = "document"
        draft["media_file_id"] = message.document.file_id
    else:
        await message.reply_text(
            "⚠ Unsupported content. Please send a photo, video, document, or tap ⏭ Skip.",
            reply_markup=kb_media_step(),
        )
        return CP_MEDIA

    await message.reply_text(
        "✅ Media attached!\n\n🎨 Step 3️⃣ of 4 — Choose a formatting style:",
        reply_markup=kb_format_step(),
    )
    return CP_FORMAT


async def createpost_skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = context.user_data.setdefault("draft", fresh_draft())
    draft["media_type"] = None
    draft["media_file_id"] = None
    await safe_edit(query.message, "🎨 Step 3️⃣ of 4 — Choose a formatting style:", kb_format_step())
    return CP_FORMAT


async def createpost_choose_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("⏳ Building preview...")
    fmt_key = query.data.replace("cp_fmt_", "")
    if fmt_key not in FORMAT_CHOICES:
        await query.answer("⚠ Unknown format.", show_alert=True)
        return CP_FORMAT

    draft = context.user_data.setdefault("draft", fresh_draft())
    draft["format"] = fmt_key
    await send_preview(query.message, context, edit=True)
    return CP_PREVIEW


async def send_preview(message: Message, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> None:
    draft = context.user_data.get("draft", fresh_draft())
    final_text = build_final_text(draft)
    parse_mode = FORMAT_CHOICES[draft["format"]]["parse_mode"]

    caption = f"👁 <b>PREVIEW</b>\n\n{final_text}"
    try:
        if draft["media_type"] == "photo":
            await message.reply_photo(
                draft["media_file_id"], caption=caption, parse_mode=parse_mode, reply_markup=kb_preview()
            )
        elif draft["media_type"] == "video":
            await message.reply_video(
                draft["media_file_id"], caption=caption, parse_mode=parse_mode, reply_markup=kb_preview()
            )
        elif draft["media_type"] == "document":
            await message.reply_document(
                draft["media_file_id"], caption=caption, parse_mode=parse_mode, reply_markup=kb_preview()
            )
        else:
            if edit:
                await safe_edit(message, caption, kb_preview())
                return
            await message.reply_text(caption, parse_mode=parse_mode, reply_markup=kb_preview())
    except TelegramError as exc:
        logger.exception("Preview render failed")
        await message.reply_text(
            f"⚠ Could not render preview with this format ({html_escape(str(exc))}).\n"
            "Falling back to plain preview:\n\n" + final_text,
            reply_markup=kb_preview(),
        )


async def createpost_preview_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("🔄 Refreshing preview...")
    await send_preview(query.message, context, edit=False)
    return CP_PREVIEW


async def createpost_edit_text_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("✏ Send the new text for your post:")
    return CP_EDIT_TEXT


async def createpost_edit_text_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data.setdefault("draft", fresh_draft())
    draft["raw_text"] = update.message.text or ""
    await update.message.reply_text("✅ Text updated.")
    await send_preview(update.message, context, edit=False)
    return CP_PREVIEW


async def createpost_change_media_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "🖼 Send new photo/video/document, or tap ⏭ Skip to remove media.",
        reply_markup=kb_media_step(),
    )
    return CP_MEDIA


@admin_only
async def createpost_publish_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("📢 Publishing...")
    draft = context.user_data.get("draft", fresh_draft())

    await query.message.reply_text("⏳ Publishing your post to the channel...")
    try:
        sent = await publish_post_to_channel(context, draft)
    except TelegramError as exc:
        logger.exception("Publish failed")
        await query.message.reply_text(f"❌ Failed to publish: {html_escape(str(exc))}")
        return CP_PREVIEW

    chat = await context.bot.get_chat(CHANNEL_ID)
    now_str = datetime.now(pytz.timezone(DEFAULT_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")

    await db.execute(
        """INSERT INTO scheduled_posts
           (text, media_type, media_file_id, parse_mode, scheduled_time_utc, timezone,
            status, created_at, published_at, message_id)
           VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?, ?)""",
        (
            draft["raw_text"],
            draft["media_type"],
            draft["media_file_id"],
            FORMAT_CHOICES[draft["format"]]["parse_mode"].value,
            datetime.utcnow().isoformat(),
            DEFAULT_TIMEZONE,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
            sent.message_id,
        ),
    )
    await log_action("INFO", "Post published", f"message_id={sent.message_id}")

    success_text = (
        "✅ <b>Published Successfully!</b>\n\n"
        f"🆔 Message ID: <code>{sent.message_id}</code>\n"
        f"📢 Channel: {html_escape(chat.title or str(CHANNEL_ID))}\n"
        f"🕒 Time: {now_str}"
    )
    await query.message.reply_text(success_text, parse_mode=ParseMode.HTML, reply_markup=kb_home_only())
    context.user_data.pop("draft", None)
    return ConversationHandler.END


async def publish_post_to_channel(context: ContextTypes.DEFAULT_TYPE, draft: dict[str, Any]) -> Message:
    final_text = build_final_text(draft)
    parse_mode = FORMAT_CHOICES[draft["format"]]["parse_mode"]

    if draft["media_type"] == "photo":
        return await context.bot.send_photo(
            CHANNEL_ID, draft["media_file_id"], caption=final_text, parse_mode=parse_mode
        )
    if draft["media_type"] == "video":
        return await context.bot.send_video(
            CHANNEL_ID, draft["media_file_id"], caption=final_text, parse_mode=parse_mode
        )
    if draft["media_type"] == "document":
        return await context.bot.send_document(
            CHANNEL_ID, draft["media_file_id"], caption=final_text, parse_mode=parse_mode
        )
    return await context.bot.send_message(CHANNEL_ID, final_text, parse_mode=parse_mode)


# --------------------------------------------------------------------------- #
# Feature 5 — Scheduled Posts
# --------------------------------------------------------------------------- #


async def createpost_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📅 <b>Schedule Post</b>\n\nStep 1 — Send the date (format: <code>YYYY-MM-DD</code>):",
        parse_mode=ParseMode.HTML,
    )
    return CP_SCHEDULE_DATE


async def createpost_schedule_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("⚠ Invalid format. Please use <code>YYYY-MM-DD</code>, e.g. 2026-07-15", parse_mode=ParseMode.HTML)
        return CP_SCHEDULE_DATE

    context.user_data["sched_date"] = text
    await update.message.reply_text("🕒 Step 2 — Send the time (24h format: <code>HH:MM</code>):", parse_mode=ParseMode.HTML)
    return CP_SCHEDULE_TIME


async def createpost_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError:
        await update.message.reply_text("⚠ Invalid format. Please use <code>HH:MM</code>, e.g. 18:30", parse_mode=ParseMode.HTML)
        return CP_SCHEDULE_TIME

    context.user_data["sched_time"] = text
    await update.message.reply_text(
        "🌍 Step 3 — Send the timezone (e.g. <code>Asia/Kolkata</code>) or type <code>default</code> "
        f"to use {DEFAULT_TIMEZONE}:",
        parse_mode=ParseMode.HTML,
    )
    return CP_SCHEDULE_TZ


async def createpost_schedule_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tz_input = (update.message.text or "").strip()
    tz_name = DEFAULT_TIMEZONE if tz_input.lower() == "default" else tz_input

    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        await update.message.reply_text(
            "⚠ Unknown timezone. Please send a valid IANA timezone (e.g. Asia/Kolkata, Europe/London) "
            "or type <code>default</code>.",
            parse_mode=ParseMode.HTML,
        )
        return CP_SCHEDULE_TZ

    date_str = context.user_data["sched_date"]
    time_str = context.user_data["sched_time"]
    naive_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    localized_dt = tz.localize(naive_dt)

    if localized_dt <= datetime.now(tz):
        await update.message.reply_text("⚠ That time is in the past. Please send a future date (YYYY-MM-DD):")
        return CP_SCHEDULE_DATE

    context.user_data["sched_tz"] = tz_name
    context.user_data["sched_datetime_utc"] = localized_dt.astimezone(pytz.utc)

    confirm_text = (
        "📋 <b>Confirm Schedule</b>\n\n"
        f"🗓 Date: <b>{date_str}</b>\n"
        f"🕒 Time: <b>{time_str}</b>\n"
        f"🌍 Timezone: <b>{tz_name}</b>\n\n"
        "Publish at this time?"
    )
    await update.message.reply_text(confirm_text, parse_mode=ParseMode.HTML, reply_markup=kb_confirm_schedule())
    return CP_SCHEDULE_CONFIRM


async def createpost_schedule_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("📅 Scheduling...")

    draft = context.user_data.get("draft", fresh_draft())
    scheduled_utc: datetime = context.user_data["sched_datetime_utc"]
    tz_name: str = context.user_data["sched_tz"]

    post_id = await db.execute(
        """INSERT INTO scheduled_posts
           (text, media_type, media_file_id, parse_mode, scheduled_time_utc, timezone, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            draft["raw_text"],
            draft["media_type"],
            draft["media_file_id"],
            FORMAT_CHOICES[draft["format"]]["parse_mode"].value,
            scheduled_utc.isoformat(),
            tz_name,
            datetime.utcnow().isoformat(),
        ),
    )

    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    schedule_job(scheduler, context.application, post_id, scheduled_utc)

    await log_action("INFO", "Post scheduled", f"post_id={post_id} at={scheduled_utc.isoformat()}")

    local_dt = scheduled_utc.astimezone(pytz.timezone(tz_name))
    await query.message.reply_text(
        "✅ <b>Post Scheduled!</b>\n\n"
        f"🆔 Post ID: <code>{post_id}</code>\n"
        f"🕒 Will publish at: <b>{local_dt.strftime('%Y-%m-%d %H:%M')} ({tz_name})</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_home_only(),
    )

    context.user_data.pop("draft", None)
    for key in ("sched_date", "sched_time", "sched_tz", "sched_datetime_utc"):
        context.user_data.pop(key, None)
    return ConversationHandler.END


def schedule_job(scheduler: AsyncIOScheduler, application: Application, post_id: int, run_at_utc: datetime) -> None:
    job_id = f"post_{post_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        publish_scheduled_post,
        trigger=DateTrigger(run_date=run_at_utc, timezone=pytz.utc),
        args=[application, post_id],
        id=job_id,
        misfire_grace_time=300,
        replace_existing=True,
    )


async def publish_scheduled_post(application: Application, post_id: int) -> None:
    row = await db.fetchone("SELECT * FROM scheduled_posts WHERE id = ?", (post_id,))
    if row is None or row["status"] != "pending":
        return  # Already published/deleted — avoids duplicate publishing.

    draft = {
        "raw_text": row["text"],
        "media_type": row["media_type"],
        "media_file_id": row["media_file_id"],
        "format": next(
            (k for k, v in FORMAT_CHOICES.items() if v["parse_mode"].value == row["parse_mode"]),
            "html",
        ),
    }

    class _Ctx:
        bot = application.bot

    try:
        sent = await publish_post_to_channel(_Ctx(), draft)  # type: ignore[arg-type]
        await db.execute(
            "UPDATE scheduled_posts SET status = 'published', published_at = ?, message_id = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), sent.message_id, post_id),
        )
        await log_action("INFO", "Scheduled post published", f"post_id={post_id} message_id={sent.message_id}")
        try:
            await application.bot.send_message(
                ADMIN_ID,
                f"✅ <b>Scheduled post published!</b>\n\n🆔 Message ID: <code>{sent.message_id}</code>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            logger.exception("Failed to notify admin of successful scheduled publish")
    except TelegramError as exc:
        retry_count = row["retry_count"] + 1
        await db.execute(
            "UPDATE scheduled_posts SET retry_count = ? WHERE id = ?", (retry_count, post_id)
        )
        await log_action("ERROR", "Scheduled post publish failed", f"post_id={post_id} error={exc}")

        if retry_count <= 3:
            retry_at = datetime.utcnow().astimezone(pytz.utc)
            scheduler: AsyncIOScheduler = application.bot_data["scheduler"]
            scheduler.add_job(
                publish_scheduled_post,
                trigger="date",
                run_date=retry_at,
                args=[application, post_id],
                id=f"post_{post_id}_retry_{retry_count}",
                misfire_grace_time=300,
            )
            logger.info("Retry #%s scheduled for post %s", retry_count, post_id)
        else:
            try:
                await application.bot.send_message(
                    ADMIN_ID,
                    f"❌ <b>Failed to publish scheduled post</b> (ID {post_id}) after 3 retries.\n"
                    f"Error: {html_escape(str(exc))}",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                logger.exception("Failed to notify admin of publish failure")


# --------------------------------------------------------------------------- #
# Feature 6 — Scheduled Posts Manager
# --------------------------------------------------------------------------- #


@admin_only
async def cmd_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_scheduled_list(update, context)


async def show_scheduled_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await db.fetchall(
        "SELECT * FROM scheduled_posts WHERE status = 'pending' ORDER BY scheduled_time_utc ASC"
    )

    header = f"📅 <b>Scheduled Posts</b> ({len(rows)})"
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query.message, header, kb_home_only())
        target = update.callback_query.message
    else:
        await update.message.reply_text(header, parse_mode=ParseMode.HTML, reply_markup=kb_home_only())
        target = update.message

    if not rows:
        await target.reply_text("Nothing scheduled right now. 🎉")
        return

    for row in rows:
        tz = pytz.timezone(row["timezone"])
        local_dt = datetime.fromisoformat(row["scheduled_time_utc"]).replace(tzinfo=pytz.utc).astimezone(tz)
        preview_snippet = (row["text"][:150] + "…") if len(row["text"]) > 150 else row["text"]
        text = (
            f"🆔 <b>Post #{row['id']}</b>\n"
            f"🕒 {local_dt.strftime('%Y-%m-%d %H:%M')} ({row['timezone']})\n\n"
            f"{html_escape(preview_snippet)}"
        )
        await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_scheduled_post_row(row["id"]))


async def cb_admin_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_scheduled_list(update, context)


async def cb_scheduled_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    post_id = int(query.data.replace("sch_delete_", ""))
    await query.answer("🗑 Deleting...")

    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    job_id = f"post_{post_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    await db.execute("UPDATE scheduled_posts SET status = 'cancelled' WHERE id = ?", (post_id,))
    await log_action("INFO", "Scheduled post cancelled", f"post_id={post_id}")
    await safe_edit(query.message, f"🗑 <b>Post #{post_id} deleted.</b>")


async def cb_scheduled_publish_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    post_id = int(query.data.replace("sch_publish_", ""))
    await query.answer("📢 Publishing now...")

    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    job_id = f"post_{post_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    await publish_scheduled_post(context.application, post_id)
    await safe_edit(query.message, f"✅ <b>Post #{post_id} published immediately.</b>")


async def cb_scheduled_edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    post_id = int(query.data.replace("sch_edit_", ""))
    row = await db.fetchone("SELECT * FROM scheduled_posts WHERE id = ?", (post_id,))
    if row is None or row["status"] != "pending":
        await query.answer("⚠ This post can no longer be edited.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["editing_post_id"] = post_id
    await query.message.reply_text(
        f"✏ Send the new text for post #{post_id}:"
    )
    return EDIT_SCHEDULED_TEXT


async def scheduled_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    post_id = context.user_data.get("editing_post_id")
    if post_id is None:
        await update.message.reply_text("⚠ No post is being edited.")
        return ConversationHandler.END

    new_text = update.message.text or ""
    await db.execute("UPDATE scheduled_posts SET text = ? WHERE id = ?", (new_text, post_id))
    await log_action("INFO", "Scheduled post edited", f"post_id={post_id}")
    await update.message.reply_text(f"✅ Post #{post_id} updated.", reply_markup=kb_home_only())
    context.user_data.pop("editing_post_id", None)
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Feature 7 — Settings
# --------------------------------------------------------------------------- #


async def cb_admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = (
        "⚙ <b>Settings</b>\n\n"
        f"📢 Channel: <code>{html_escape(CHANNEL_ID)}</code>\n"
        f"🌍 Default Timezone: <code>{DEFAULT_TIMEZONE}</code>\n"
        f"👤 Admin ID: <code>{ADMIN_ID}</code>\n\n"
        "To change these values, update your <code>.env</code> file and restart the bot."
    )
    await safe_edit(query.message, text, kb_home_only())


async def cb_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_admin_dashboard(update, context)


# --------------------------------------------------------------------------- #
# Cancel / fallback for conversations
# --------------------------------------------------------------------------- #


async def createpost_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query.message, "❌ Post creation cancelled.", kb_home_only())
    else:
        await update.message.reply_text("❌ Post creation cancelled.", reply_markup=kb_home_only())
    return ConversationHandler.END


async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    if update.effective_chat:
        await context.bot.send_message(
            update.effective_chat.id, "⌛ Session timed out. Use /createpost to start again."
        )


# --------------------------------------------------------------------------- #
# Feature 12 — Global error handler
# --------------------------------------------------------------------------- #


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)
    await log_action("ERROR", "Unhandled exception", str(context.error))

    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠ Something went wrong. The error has been logged and the team notified.",
            )
    except TelegramError:
        pass

    if ADMIN_ID:
        try:
            await context.bot.send_message(
                ADMIN_ID, f"🚨 <b>Bot Error</b>\n\n<code>{html_escape(str(context.error))}</code>", parse_mode=ParseMode.HTML
            )
        except TelegramError:
            logger.exception("Failed to notify admin about error")


# --------------------------------------------------------------------------- #
# Application bootstrap
# --------------------------------------------------------------------------- #


async def restore_scheduled_jobs(application: Application) -> None:
    """Re-register scheduler jobs for pending posts after a restart."""
    scheduler: AsyncIOScheduler = application.bot_data["scheduler"]
    rows = await db.fetchall("SELECT * FROM scheduled_posts WHERE status = 'pending'")
    now_utc = datetime.now(pytz.utc)

    for row in rows:
        run_at = datetime.fromisoformat(row["scheduled_time_utc"]).replace(tzinfo=pytz.utc)
        if run_at <= now_utc:
            # Missed while offline — publish immediately.
            asyncio.create_task(publish_scheduled_post(application, row["id"]))
        else:
            schedule_job(scheduler, application, row["id"], run_at)

    logger.info("Restored %s scheduled job(s)", len(rows))


async def post_init(application: Application) -> None:
    await db.init()

    scheduler = AsyncIOScheduler(timezone=pytz.utc)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler

    await restore_scheduled_jobs(application)
    await log_action("INFO", "Bot started", "Application initialized successfully")

    try:
        await application.bot.send_message(ADMIN_ID, "🤖 <b>Bot is online!</b>", parse_mode=ParseMode.HTML)
    except TelegramError:
        logger.warning("Could not send startup notification to admin (chat may not exist yet).")


async def post_shutdown(application: Application) -> None:
    scheduler: Optional[AsyncIOScheduler] = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    logger.info("Bot shut down cleanly.")


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # --- Basic commands ---
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("scheduled", cmd_scheduled))
    application.add_handler(CommandHandler("cancel", cmd_cancel))

    # --- Join request workflow ---
    application.add_handler(ChatJoinRequestHandler(handle_join_request))
    application.add_handler(CallbackQueryHandler(cb_join_decision, pattern=r"^jr_(approve|reject)_\d+$"))

    # --- Admin dashboard navigation (outside conversations) ---
    application.add_handler(CallbackQueryHandler(cb_admin_stats, pattern="^adm_stats$"))
    application.add_handler(CallbackQueryHandler(cb_admin_pending, pattern="^adm_pending$"))
    application.add_handler(CallbackQueryHandler(cb_admin_scheduled, pattern="^adm_scheduled$"))
    application.add_handler(CallbackQueryHandler(cb_admin_settings, pattern="^adm_settings$"))
    application.add_handler(CallbackQueryHandler(cb_admin_home, pattern="^adm_home$"))

    # --- Scheduled posts manager (non-conversation actions) ---
    application.add_handler(CallbackQueryHandler(cb_scheduled_delete, pattern=r"^sch_delete_\d+$"))
    application.add_handler(CallbackQueryHandler(cb_scheduled_publish_now, pattern=r"^sch_publish_\d+$"))

    # --- Smart Post Creator conversation ---
    createpost_conv = ConversationHandler(
        entry_points=[
            CommandHandler("createpost", createpost_entry),
            CallbackQueryHandler(createpost_entry, pattern="^adm_createpost$"),
            CallbackQueryHandler(createpost_entry, pattern="^adm_publish$"),
        ],
        states={
            CP_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_receive_text)],
            CP_MEDIA: [
                CallbackQueryHandler(createpost_skip_media, pattern="^cp_skip_media$"),
                CallbackQueryHandler(createpost_cancel, pattern="^cp_cancel$"),
                MessageHandler(
                    (filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
                    createpost_receive_media,
                ),
            ],
            CP_FORMAT: [
                CallbackQueryHandler(createpost_choose_format, pattern=r"^cp_fmt_\w+$"),
                CallbackQueryHandler(createpost_cancel, pattern="^cp_cancel$"),
            ],
            CP_PREVIEW: [
                CallbackQueryHandler(createpost_edit_text_prompt, pattern="^cp_edit_text$"),
                CallbackQueryHandler(createpost_change_media_prompt, pattern="^cp_change_media$"),
                CallbackQueryHandler(createpost_preview_button, pattern="^cp_preview$"),
                CallbackQueryHandler(createpost_publish_now, pattern="^cp_publish_now$"),
                CallbackQueryHandler(createpost_schedule_start, pattern="^cp_schedule$"),
                CallbackQueryHandler(createpost_cancel, pattern="^cp_cancel$"),
            ],
            CP_EDIT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_edit_text_receive)],
            CP_SCHEDULE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_schedule_date)],
            CP_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_schedule_time)],
            CP_SCHEDULE_TZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_schedule_timezone)],
            CP_SCHEDULE_CONFIRM: [
                CallbackQueryHandler(createpost_schedule_confirm, pattern="^cp_sched_confirm$"),
                CallbackQueryHandler(createpost_preview_button, pattern="^cp_preview$"),
                CallbackQueryHandler(createpost_cancel, pattern="^cp_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CallbackQueryHandler(createpost_cancel, pattern="^cp_cancel$")],
        conversation_timeout=900,
        name="createpost_conversation",
        persistent=False,
    )
    application.add_handler(createpost_conv)

    # --- Scheduled post edit conversation ---
    edit_scheduled_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_scheduled_edit_prompt, pattern=r"^sch_edit_\d+$")],
        states={
            EDIT_SCHEDULED_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, scheduled_edit_receive)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=600,
        name="edit_scheduled_conversation",
        persistent=False,
    )
    application.add_handler(edit_scheduled_conv)

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    application = build_application()
    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
