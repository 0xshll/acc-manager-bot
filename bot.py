import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


ADD_ACCOUNT_ID, ADD_GMAIL, ADD_PLATFORM, ADD_SELLER, ADD_COUNTRY, ADD_DATE, ADD_REMINDER = range(7)
EDIT_SELECT, EDIT_FIELD, EDIT_VALUE = range(7, 10)
DELETE_SELECT, DELETE_CONFIRM = range(10, 12)
SET_DEFAULT_REMINDER = 12

GMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@gmail\.com$", re.IGNORECASE)
SUPPORTED_PLATFORMS = ["Bybit", "Bitget", "MEX", "MEXC", "Other"]

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "accounts.sqlite3"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
DEFAULT_REMINDER_AFTER_DAYS = int(os.getenv("REMINDER_AFTER_DAYS", "4"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Account:
    user_id: str
    chat_id: str
    id: str
    gmail: str
    platform: str
    seller_name: str
    country: str
    creation_at: datetime
    reminder_amount: int
    reminder_unit: str
    reminded_at: str | None


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                db_id INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                gmail TEXT NOT NULL,
                platform TEXT NOT NULL,
                seller_name TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                creation_date TEXT NOT NULL,
                reminder_days INTEGER NOT NULL DEFAULT 4,
                reminder_amount INTEGER NOT NULL DEFAULT 4,
                reminder_unit TEXT NOT NULL DEFAULT 'days',
                reminded_at TEXT,
                UNIQUE(user_id, id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        migrate_accounts(conn)
        conn.commit()


def migrate_accounts(conn: sqlite3.Connection) -> None:
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "db_id" not in columns:
        conn.execute("ALTER TABLE accounts RENAME TO accounts_old")
        conn.execute(
            """
            CREATE TABLE accounts (
                db_id INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                gmail TEXT NOT NULL,
                platform TEXT NOT NULL,
                seller_name TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                creation_date TEXT NOT NULL,
                reminder_days INTEGER NOT NULL DEFAULT 4,
                reminder_amount INTEGER NOT NULL DEFAULT 4,
                reminder_unit TEXT NOT NULL DEFAULT 'days',
                reminded_at TEXT,
                UNIQUE(user_id, id)
            )
            """
        )
        old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts_old)").fetchall()}
        fallback_user = ALLOWED_USER_ID or "owner"
        fallback_chat = ALLOWED_USER_ID or ""
        rows = conn.execute("SELECT * FROM accounts_old").fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO accounts (
                    id, user_id, chat_id, gmail, platform, seller_name, country,
                    creation_date, reminder_days, reminder_amount, reminder_unit, reminded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["user_id"] if "user_id" in old_columns and row["user_id"] else fallback_user,
                    row["chat_id"] if "chat_id" in old_columns and row["chat_id"] else fallback_chat,
                    row["gmail"],
                    row["platform"],
                    row["seller_name"] if "seller_name" in old_columns else "",
                    row["country"] if "country" in old_columns else "",
                    row["creation_date"],
                    row["reminder_days"] if "reminder_days" in old_columns else DEFAULT_REMINDER_AFTER_DAYS,
                    row["reminder_amount"] if "reminder_amount" in old_columns else row["reminder_days"] if "reminder_days" in old_columns else DEFAULT_REMINDER_AFTER_DAYS,
                    row["reminder_unit"] if "reminder_unit" in old_columns else "days",
                    row["reminded_at"] if "reminded_at" in old_columns else None,
                ),
            )
        conn.execute("DROP TABLE accounts_old")


def row_to_account(row: sqlite3.Row) -> Account:
    creation_text = row["creation_date"]
    try:
        creation_at = datetime.fromisoformat(creation_text)
    except ValueError:
        creation_at = datetime.combine(date.fromisoformat(creation_text), time.min)
    return Account(
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        id=row["id"],
        gmail=row["gmail"],
        platform=row["platform"],
        seller_name=row["seller_name"],
        country=row["country"],
        creation_at=creation_at,
        reminder_amount=int(row["reminder_amount"]),
        reminder_unit=row["reminder_unit"],
        reminded_at=row["reminded_at"],
    )


def create_account(
    user_id: str,
    chat_id: str,
    account_id: str,
    gmail: str,
    platform: str,
    seller_name: str,
    country: str,
    creation_at: datetime,
    reminder_amount: int,
    reminder_unit: str,
) -> Account:
    account = Account(
        user_id=user_id,
        chat_id=chat_id,
        id=account_id.strip(),
        gmail=gmail.strip(),
        platform=platform.strip(),
        seller_name=seller_name.strip(),
        country=country.strip(),
        creation_at=creation_at,
        reminder_amount=reminder_amount,
        reminder_unit=reminder_unit,
        reminded_at=None,
    )
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO accounts (
                id, user_id, chat_id, gmail, platform, seller_name, country,
                creation_date, reminder_days, reminder_amount, reminder_unit, reminded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account.id,
                account.user_id,
                account.chat_id,
                account.gmail,
                account.platform,
                account.seller_name,
                account.country,
                account.creation_at.isoformat(timespec="minutes"),
                account.reminder_amount if account.reminder_unit == "days" else DEFAULT_REMINDER_AFTER_DAYS,
                account.reminder_amount,
                account.reminder_unit,
                account.reminded_at,
            ),
        )
        conn.commit()
    return account


def get_accounts(user_id: str) -> list[Account]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM accounts
            WHERE user_id = ?
            ORDER BY creation_date DESC, platform, gmail
            """,
            (user_id,),
        ).fetchall()
    return [row_to_account(row) for row in rows]


def get_account(user_id: str, account_id: str) -> Account | None:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE user_id = ? AND id = ?",
            (user_id, account_id),
        ).fetchone()
    return row_to_account(row) if row else None


def update_account(user_id: str, account_id: str, field: str, value: str) -> None:
    allowed_fields = {"gmail", "platform", "seller_name", "country", "creation_date"}
    if field not in allowed_fields:
        raise ValueError("Unsupported field")
    reset = ", reminded_at = NULL" if field == "creation_date" else ""
    with closing(db()) as conn:
        conn.execute(
            f"UPDATE accounts SET {field} = ?{reset} WHERE user_id = ? AND id = ?",
            (value, user_id, account_id),
        )
        conn.commit()


def update_reminder(user_id: str, account_id: str, amount: int, unit: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            UPDATE accounts
            SET reminder_amount = ?, reminder_unit = ?, reminder_days = ?, reminded_at = NULL
            WHERE user_id = ? AND id = ?
            """,
            (amount, unit, amount if unit == "days" else DEFAULT_REMINDER_AFTER_DAYS, user_id, account_id),
        )
        conn.commit()


def delete_account(user_id: str, account_id: str) -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM accounts WHERE user_id = ? AND id = ?", (user_id, account_id))
        conn.commit()


def set_setting(key: str, value: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def get_setting(key: str) -> str | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def due_accounts() -> list[Account]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE reminded_at IS NULL ORDER BY creation_date"
        ).fetchall()
    now = datetime.now()
    return [
        account
        for account in [row_to_account(row) for row in rows]
        if reminder_due_at(account) <= now
    ]


def mark_reminded(user_id: str, account_id: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            "UPDATE accounts SET reminded_at = ? WHERE user_id = ? AND id = ?",
            (datetime.now().isoformat(timespec="seconds"), user_id, account_id),
        )
        conn.commit()


def current_user_id(update: Update) -> str:
    if not update.effective_user:
        raise RuntimeError("Missing Telegram user.")
    return str(update.effective_user.id)


def current_chat_id(update: Update) -> str:
    if not update.effective_chat:
        raise RuntimeError("Missing Telegram chat.")
    return str(update.effective_chat.id)


def is_allowed(update: Update) -> bool:
    return not ALLOWED_USER_ID or current_user_id(update) == ALLOWED_USER_ID


async def guard(update: Update) -> bool:
    if is_allowed(update):
        return True
    if update.callback_query:
        await update.callback_query.answer("This bot is private.", show_alert=True)
    elif update.effective_message:
        await tracked_reply(context, update.effective_message, "This bot is private.")
    return False


async def track_bot_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    messages = context.user_data.setdefault("cleanup_messages", [])
    item = (chat_id, message_id)
    if item not in messages:
        messages.append(item)


async def tracked_reply(context: ContextTypes.DEFAULT_TYPE, message, text: str, **kwargs):
    sent = await message.reply_text(text, **kwargs)
    if not text.startswith("Reminder:"):
        await track_bot_message(context, sent.chat_id, sent.message_id)
    return sent


async def cleanup_bot_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    messages = context.user_data.pop("cleanup_messages", [])
    query = update.callback_query
    current = None
    if query and query.message:
        current = (query.message.chat_id, query.message.message_id)
    for chat_id, message_id in messages:
        if current and (chat_id, message_id) == current:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass


async def delete_chosen_message(update: Update) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    text = query.message.text or ""
    if text.startswith("Reminder:"):
        return
    try:
        await query.message.delete()
    except TelegramError:
        pass


def parse_creation_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def parse_creation_at(text: str) -> datetime | None:
    text = text.strip().lower()
    if text == "now":
        return datetime.now().replace(second=0, microsecond=0)
    if text == "today":
        return datetime.combine(date.today(), time.min)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    parsed_date = parse_creation_date(text)
    return datetime.combine(parsed_date, time.min) if parsed_date else None


def normalize_unit(unit: str) -> str | None:
    return {
        "m": "minutes",
        "min": "minutes",
        "mins": "minutes",
        "minute": "minutes",
        "minutes": "minutes",
        "h": "hours",
        "hr": "hours",
        "hrs": "hours",
        "hour": "hours",
        "hours": "hours",
        "d": "days",
        "day": "days",
        "days": "days",
    }.get(unit.strip().lower())


def parse_reminder(text: str) -> tuple[int, str] | None:
    match = re.match(r"^\s*(\d+)\s*([A-Za-z]+)\s*$", text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = normalize_unit(match.group(2))
    if not unit or amount < 1 or amount > 525600:
        return None
    return amount, unit


def reminder_delta(amount: int, unit: str) -> timedelta:
    if unit == "minutes":
        return timedelta(minutes=amount)
    if unit == "hours":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def reminder_due_at(account: Account) -> datetime:
    return account.creation_at + reminder_delta(account.reminder_amount, account.reminder_unit)


def format_reminder_period(amount: int, unit: str) -> str:
    singular = unit[:-1] if amount == 1 else unit
    return f"{amount} {singular}"


def validate_gmail(text: str) -> bool:
    return bool(GMAIL_RE.match(text.strip()))


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("View accounts", callback_data="view"),
                InlineKeyboardButton("Add account", callback_data="add"),
            ],
            [
                InlineKeyboardButton("Edit account", callback_data="edit"),
                InlineKeyboardButton("Delete account", callback_data="delete"),
            ],
            [InlineKeyboardButton("Default reminder time", callback_data="settings")],
        ]
    )


def bottom_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Start", "View accounts"],
            ["Add account", "Edit account"],
            ["Delete account", "Default reminder"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def platform_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(name, callback_data=f"platform:{name}")] for name in SUPPORTED_PLATFORMS]
    )


def reminder_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 min", callback_data="reminder:1:minutes"),
                InlineKeyboardButton("30 min", callback_data="reminder:30:minutes"),
            ],
            [
                InlineKeyboardButton("1 hour", callback_data="reminder:1:hours"),
                InlineKeyboardButton("2 hours", callback_data="reminder:2:hours"),
            ],
            [
                InlineKeyboardButton("1 day", callback_data="reminder:1:days"),
                InlineKeyboardButton("3 days", callback_data="reminder:3:days"),
            ],
        ]
    )


def account_keyboard(prefix: str, accounts: Iterable[Account]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Account ID: {account.id}", callback_data=f"{prefix}:{account.id}")]
        for account in accounts
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def field_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Gmail", callback_data=f"field:{account_id}:gmail")],
            [InlineKeyboardButton("Platform", callback_data=f"field:{account_id}:platform")],
            [InlineKeyboardButton("Seller name", callback_data=f"field:{account_id}:seller_name")],
            [InlineKeyboardButton("Country", callback_data=f"field:{account_id}:country")],
            [InlineKeyboardButton("Creation time", callback_data=f"field:{account_id}:creation_date")],
            [InlineKeyboardButton("Reminder time", callback_data=f"field:{account_id}:reminder")],
            [InlineKeyboardButton("Cancel", callback_data="cancel")],
        ]
    )


def format_account(account: Account) -> str:
    due_at = reminder_due_at(account)
    status = "reminded" if account.reminded_at else f"reminder due {due_at.strftime('%Y-%m-%d %H:%M')}"
    return (
        f"Account ID: {account.id}\n"
        f"Gmail: {account.gmail}\n"
        f"Platform: {account.platform}\n"
        f"Seller: {account.seller_name or '-'}\n"
        f"Country: {account.country or '-'}\n"
        f"Created: {account.creation_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"Reminder after: {format_reminder_period(account.reminder_amount, account.reminder_unit)}\n"
        f"Status: {status}"
    )


def get_default_reminder() -> tuple[int, str]:
    amount = get_setting("default_reminder_amount")
    unit = get_setting("default_reminder_unit")
    if amount and unit:
        parsed_unit = normalize_unit(unit)
        try:
            parsed_amount = int(amount)
        except ValueError:
            parsed_amount = 0
        if parsed_amount > 0 and parsed_unit:
            return parsed_amount, parsed_unit
    return DEFAULT_REMINDER_AFTER_DAYS, "days"


def set_default_reminder(amount: int, unit: str) -> None:
    set_setting("default_reminder_amount", str(amount))
    set_setting("default_reminder_unit", unit)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    set_setting(f"chat_id:{current_user_id(update)}", current_chat_id(update))
    await tracked_reply(context, update.effective_message, "Bottom buttons are ready.", reply_markup=bottom_menu())
    await tracked_reply(context, update.effective_message, "Trading account tracker is ready.", reply_markup=main_menu())
    return ConversationHandler.END


async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    accounts = get_accounts(current_user_id(update))
    message = update.callback_query.message if update.callback_query else update.effective_message
    if not accounts:
        await tracked_reply(context, message, "No accounts yet.", reply_markup=main_menu())
        return
    await tracked_reply(context, message, "Choose an account to view.", reply_markup=account_keyboard("viewpick", accounts))


async def view_account_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    account_id = query.data.split(":", 1)[1]
    account = get_account(current_user_id(update), account_id)
    if not account:
        await tracked_reply(context, query.message, "Account not found.", reply_markup=main_menu())
        return
    await tracked_reply(context, query.message, format_account(account), reply_markup=main_menu())


async def view_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await show_accounts(update, context)
    return ConversationHandler.END


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    accounts = get_accounts(current_user_id(update))
    if not accounts:
        await tracked_reply(context, update.effective_message, "No accounts yet.", reply_markup=bottom_menu())
        return ConversationHandler.END
    await tracked_reply(context, update.effective_message, "Choose an account to edit.", reply_markup=account_keyboard("editpick", accounts))
    return EDIT_SELECT


async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    accounts = get_accounts(current_user_id(update))
    if not accounts:
        await tracked_reply(context, update.effective_message, "No accounts yet.", reply_markup=bottom_menu())
        return ConversationHandler.END
    await tracked_reply(context, update.effective_message, "Choose an account to delete.", reply_markup=account_keyboard("deletepick", accounts))
    return DELETE_SELECT


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    action = query.data
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if action == "view":
        await show_accounts(update, context)
        return ConversationHandler.END
    if action == "add":
        context.user_data.clear()
        await tracked_reply(context, query.message, "Send the account ID you want to use.")
        return ADD_ACCOUNT_ID
    if action == "edit":
        return await edit_start(update, context)
    if action == "delete":
        return await delete_start(update, context)
    if action == "settings":
        amount, unit = get_default_reminder()
        await tracked_reply(context, query.message, 
            f"Default reminder for new accounts: {format_reminder_period(amount, unit)}.\n"
            "Send the new default, like 30 minutes, 2 hours, or 3 days."
        )
        return SET_DEFAULT_REMINDER
    return ConversationHandler.END


async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    amount, unit = get_default_reminder()
    await tracked_reply(context, update.effective_message, 
        f"Default reminder for new accounts: {format_reminder_period(amount, unit)}.\n"
        "Send the new default, like 30 minutes, 2 hours, or 3 days."
    )
    return SET_DEFAULT_REMINDER


async def set_default_reminder_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    reminder = parse_reminder(update.effective_message.text)
    if not reminder:
        await tracked_reply(context, update.effective_message, "Send a reminder like 30 minutes, 2 hours, or 3 days.")
        return SET_DEFAULT_REMINDER
    amount, unit = reminder
    set_default_reminder(amount, unit)
    await tracked_reply(context, update.effective_message, 
        f"Default reminder updated to {format_reminder_period(amount, unit)}.",
        reply_markup=main_menu(),
    )
    return ConversationHandler.END


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    context.user_data.clear()
    await tracked_reply(context, update.effective_message, "Send the account ID you want to use.")
    return ADD_ACCOUNT_ID


async def add_account_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    account_id = update.effective_message.text.strip()
    if not account_id or len(account_id) > 64:
        await tracked_reply(context, update.effective_message, "Send a non-empty account ID under 64 characters.")
        return ADD_ACCOUNT_ID
    if get_account(current_user_id(update), account_id):
        await tracked_reply(context, update.effective_message, "This account ID already exists. Send a different one.")
        return ADD_ACCOUNT_ID
    context.user_data["new_account_id"] = account_id
    await tracked_reply(context, update.effective_message, "Send the Gmail address for this account.")
    return ADD_GMAIL


async def add_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gmail = update.effective_message.text.strip()
    if not validate_gmail(gmail):
        await tracked_reply(context, update.effective_message, "Please send a valid Gmail address ending in @gmail.com.")
        return ADD_GMAIL
    context.user_data["new_gmail"] = gmail
    await tracked_reply(context, update.effective_message, "Choose the platform.", reply_markup=platform_keyboard())
    return ADD_PLATFORM


async def add_platform(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    context.user_data["new_platform"] = query.data.split(":", 1)[1]
    await tracked_reply(context, query.message, "Send the seller name you bought the KYC from.")
    return ADD_SELLER


async def add_seller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    seller_name = update.effective_message.text.strip()
    if not seller_name:
        await tracked_reply(context, update.effective_message, "Send the seller name.")
        return ADD_SELLER
    context.user_data["new_seller_name"] = seller_name
    await tracked_reply(context, update.effective_message, "Send the account country.")
    return ADD_COUNTRY


async def add_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    country = update.effective_message.text.strip()
    if not country:
        await tracked_reply(context, update.effective_message, "Send the country for this account.")
        return ADD_COUNTRY
    context.user_data["new_country"] = country
    await tracked_reply(context, update.effective_message, "Send the creation time as now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.")
    return ADD_DATE


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    creation_at = parse_creation_at(update.effective_message.text)
    if not creation_at:
        await tracked_reply(context, update.effective_message, "Use now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.")
        return ADD_DATE
    context.user_data["new_creation_at"] = creation_at
    await tracked_reply(context, update.effective_message, 
        "When should I remind you for this account?\n"
        "Choose a button, or type something like 15 minutes, 2 hours, or 3 days.",
        reply_markup=reminder_keyboard(),
    )
    return ADD_REMINDER


async def add_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        _, amount, unit = query.data.split(":", 2)
        reminder = (int(amount), unit)
        message = query.message
    else:
        reminder = parse_reminder(update.effective_message.text)
        message = update.effective_message
    if not reminder:
        await tracked_reply(context, message, "Send a reminder like 15 minutes, 2 hours, or 3 days.")
        return ADD_REMINDER
    amount, unit = reminder
    try:
        account = create_account(
            current_user_id(update),
            current_chat_id(update),
            context.user_data["new_account_id"],
            context.user_data["new_gmail"],
            context.user_data["new_platform"],
            context.user_data["new_seller_name"],
            context.user_data["new_country"],
            context.user_data["new_creation_at"],
            amount,
            unit,
        )
    except sqlite3.IntegrityError:
        await tracked_reply(context, message, "This account ID already exists. Start again with /add.")
        return ConversationHandler.END
    context.user_data.clear()
    await tracked_reply(context, message, f"Account added.\n\n{format_account(account)}", reply_markup=main_menu())
    return ConversationHandler.END

async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if query.data == "cancel":
        await tracked_reply(context, query.message, "Cancelled.", reply_markup=main_menu())
        return ConversationHandler.END
    account_id = query.data.split(":", 1)[1]
    if not get_account(current_user_id(update), account_id):
        await tracked_reply(context, query.message, "Account not found.", reply_markup=main_menu())
        return ConversationHandler.END
    await tracked_reply(context, query.message, "What do you want to edit?", reply_markup=field_keyboard(account_id))
    return EDIT_FIELD


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if query.data == "cancel":
        await tracked_reply(context, query.message, "Cancelled.", reply_markup=main_menu())
        return ConversationHandler.END
    _, account_id, field = query.data.split(":", 2)
    context.user_data["edit_account_id"] = account_id
    context.user_data["edit_field"] = field
    prompts = {
        "gmail": "Send the new Gmail address.",
        "platform": "Send the new platform name.",
        "seller_name": "Send the new seller name.",
        "country": "Send the new country.",
        "creation_date": "Send the new creation time as now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.",
        "reminder": "Send the new reminder time, like 15 minutes, 2 hours, or 3 days.",
    }
    await tracked_reply(context, query.message, prompts[field])
    return EDIT_VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data["edit_field"]
    account_id = context.user_data["edit_account_id"]
    value = update.effective_message.text.strip()
    if field == "gmail" and not validate_gmail(value):
        await tracked_reply(context, update.effective_message, "Please send a valid Gmail address ending in @gmail.com.")
        return EDIT_VALUE
    if field == "creation_date":
        parsed = parse_creation_at(value)
        if not parsed:
            await tracked_reply(context, update.effective_message, "Use now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.")
            return EDIT_VALUE
        value = parsed.isoformat(timespec="minutes")
    if field == "reminder":
        reminder = parse_reminder(value)
        if not reminder:
            await tracked_reply(context, update.effective_message, "Send a reminder like 15 minutes, 2 hours, or 3 days.")
            return EDIT_VALUE
        amount, unit = reminder
        update_reminder(current_user_id(update), account_id, amount, unit)
    else:
        update_account(current_user_id(update), account_id, field, value)
    account = get_account(current_user_id(update), account_id)
    context.user_data.clear()
    await tracked_reply(context, update.effective_message, f"Account updated.\n\n{format_account(account)}", reply_markup=main_menu())
    return ConversationHandler.END


async def delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if query.data == "cancel":
        await tracked_reply(context, query.message, "Cancelled.", reply_markup=main_menu())
        return ConversationHandler.END
    account_id = query.data.split(":", 1)[1]
    account = get_account(current_user_id(update), account_id)
    if not account:
        await tracked_reply(context, query.message, "Account not found.", reply_markup=main_menu())
        return ConversationHandler.END
    context.user_data["delete_account_id"] = account_id
    await tracked_reply(context, query.message, 
        f"Delete this account?\n\n{format_account(account)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Delete", callback_data="confirm_delete")],
                [InlineKeyboardButton("Cancel", callback_data="cancel")],
            ]
        ),
    )
    return DELETE_CONFIRM


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if query.data == "confirm_delete":
        delete_account(current_user_id(update), context.user_data["delete_account_id"])
        context.user_data.clear()
        await tracked_reply(context, query.message, "Account deleted.", reply_markup=main_menu())
    else:
        await tracked_reply(context, query.message, "Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        await tracked_reply(context, update.callback_query.message, "Cancelled.", reply_markup=main_menu())
    else:
        await cleanup_bot_messages(update, context)
        await tracked_reply(context, update.effective_message, "Cancelled.", reply_markup=main_menu())
    context.user_data.clear()
    return ConversationHandler.END

async def cleanup_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cleanup_bot_messages(update, context)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for account in due_accounts():
        chat_id = account.chat_id or get_setting(f"chat_id:{account.user_id}")
        if not chat_id:
            logger.warning("Due reminder exists for user %s, but no chat is known.", account.user_id)
            continue
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"Reminder: check this trading account.\n\n{format_account(account)}",
        )
        mark_reminded(account.user_id, account.id)
        await asyncio.sleep(0.3)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before running the bot.")
    application = Application.builder().token(BOT_TOKEN).build()
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            CommandHandler("settings", settings_start),
            MessageHandler(filters.Regex("^Start$"), start),
            MessageHandler(filters.Regex("^View accounts$"), view_start),
            MessageHandler(filters.Regex("^Add account$"), add_start),
            MessageHandler(filters.Regex("^Edit account$"), edit_start),
            MessageHandler(filters.Regex("^Delete account$"), delete_start),
            MessageHandler(filters.Regex("^Default reminder$"), settings_start),
            CallbackQueryHandler(menu_callback, pattern="^(view|add|edit|delete|settings)$"),
        ],
        states={
            ADD_ACCOUNT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_id)],
            ADD_GMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_gmail)],
            ADD_PLATFORM: [CallbackQueryHandler(add_platform, pattern="^platform:")],
            ADD_SELLER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_seller)],
            ADD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_country)],
            ADD_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_date)],
            ADD_REMINDER: [
                CallbackQueryHandler(add_reminder, pattern="^reminder:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder),
            ],
            EDIT_SELECT: [CallbackQueryHandler(edit_select, pattern="^(editpick:|cancel$)")],
            EDIT_FIELD: [CallbackQueryHandler(edit_field, pattern="^(field:|cancel$)")],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
            DELETE_SELECT: [CallbackQueryHandler(delete_select, pattern="^(deletepick:|cancel$)")],
            DELETE_CONFIRM: [CallbackQueryHandler(delete_confirm, pattern="^(confirm_delete|cancel)$")],
            SET_DEFAULT_REMINDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_default_reminder_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^cancel$")],
        allow_reentry=True,
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cleanup_text_messages), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("accounts", show_accounts))
    application.add_handler(CallbackQueryHandler(view_account_detail, pattern="^viewpick:"))
    application.add_handler(conversation)
    application.job_queue.run_repeating(reminder_job, interval=30, first=10)
    application.job_queue.run_daily(reminder_job, time=time(hour=9, minute=0))
    return application


def main() -> None:
    init_db()
    asyncio.set_event_loop(asyncio.new_event_loop())
    application = build_application()
    logger.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
