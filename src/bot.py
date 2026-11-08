import asyncio
import html
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageOriginHiddenUser,
    MessageOriginUser,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from users import User, UserDB
from ui_helpers import (
    apply_peer_sample,
    delete_callback_message,
    delete_message_silently,
    fetch_peer_sample,
    filter_peer_snapshots,
    format_bytes,
)
from wgapi import WGEasyAPI

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

wg = WGEasyAPI()

def _parse_env_set(name: str) -> set[str]:
    val = os.environ.get(name, "")
    out = set()
    for u in val.split(","):
        u = str(u).strip().lstrip("@").lower()
        if u:
            out.add(u)
    return out

default_limit = 3
try:
    default_limit = int(os.environ.get("DEFAULT_CONFIG_LIMIT", "3"))
except Exception:
    default_limit = 3

users_db = UserDB(os.environ.get("USERS_DB_PATH", "users.db"), default_limit=default_limit)
users_db.set_bootstrap_sets(_parse_env_set("ADMIN_USERNAMES"), _parse_env_set("USERNAMES"))

WAITING_FOR_RENAME = 3
WAITING_FOR_NEWUSER = 4
WAITING_FOR_NEWNAME = 5
WAITING_FOR_USERLIMIT = 6
WAITING_FOR_LIMITREQ = 7

PAGE_SIZE = 8
PEER_SAMPLE_INTERVAL_SECONDS = 60
MAX_PEER_NAME_LEN = 15
PEER_NAME_LEN = 8
PEER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,15}$")

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📜 Requests"],
        ["📋 Devices", "👥 Users"],
        ["➕ Add a device", "➕ Add User"],
    ],
    resize_keyboard=True,
)

USER_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📋 Devices", "➕ Add a device"],
        ["📈 Increase device limit"],
    ],
    resize_keyboard=True,
)

CANCEL_CONV_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("❌ Cancel", callback_data="cancel_conv"),
]])

PEER_SELECT_TITLE = {
    "cfg": "Select a device to download its configuration:",
    "del": "Select a device to remove:",
}

INLINE_MENU_USER = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📋 Devices", callback_data="menu:list"),
        InlineKeyboardButton("➕ Add a device", callback_data="menu:create"),
    ],
    [
        InlineKeyboardButton("📈 Increase device limit", callback_data="menu:limitreq"),
    ],
])


def _inline_menu_markup(user):
    if not users_db.is_admin(user):
        return INLINE_MENU_USER
    pending = users_db.request_count("pending")
    requests_label = "📜 Requests" + (f" · {pending} pending" if pending else "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(requests_label, callback_data="menu:requests")],
        [
            InlineKeyboardButton("📋 Devices", callback_data="menu:list"),
            InlineKeyboardButton("👥 Users", callback_data="menu:users"),
        ],
        [
            InlineKeyboardButton("➕ Add a device", callback_data="menu:create"),
            InlineKeyboardButton("➕ Add User", callback_data="menu:adduser"),
        ],
    ])

CREATE_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🙋 For myself", callback_data="create:self")],
    [InlineKeyboardButton("👥 For another user", callback_data="create:pickuser")],
    [InlineKeyboardButton("✏️ Set name yourself", callback_data="create:name")],
])

AMNEZIA_LINKS = (
    "\n\n📱 <b>Install a WireGuard client — two options:</b>\n\n"
    "<b>AmneziaWG</b>\n"
    '  <a href="https://apps.apple.com/us/app/amneziawg/id6478942365">iOS &amp; macOS</a> · '
    '<a href="https://play.google.com/store/apps/details?id=org.amnezia.awg">Android</a> · '
    '<a href="https://github.com/amnezia-vpn/amneziawg-windows-client/releases">Windows</a> · '
    '<a href="https://github.com/amnezia-vpn/amneziawg-linux-kernel-module">Linux</a>\n\n'
    "<b>WG Tunnel</b> (split tunneling)\n"
    '  <a href="https://play.google.com/store/apps/details?id=com.zaneschepke.wireguardautotunnel">Android</a> · '
    '<a href="https://wgtunnel.com/download?platform=windows">Windows</a> · '
    '<a href="https://wgtunnel.com/download?platform=linux">Linux</a>'
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_user(update: Update) -> User | None:
    u = update.effective_user
    if u is None:
        return None
    return users_db.resolve(u.id, u.username)


def is_admin(update: Update) -> bool:
    user = get_user(update)
    if user is None:
        return False
    return users_db.is_admin(user)


def _log_unauthorized(update: Update):
    u = update.effective_user
    logger.info("Ignoring unauthorized user %s (id=%s)",
                getattr(u, 'username', None), getattr(u, 'id', None))


def require_access(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = get_user(update)
        if user is None:
            _log_unauthorized(update)
            return ConversationHandler.END
        if users_db.is_disabled(user):
            if update.effective_message:
                await update.effective_message.reply_text("Your account is disabled. Contact an administrator.")
            return ConversationHandler.END
        context.user_data['_user'] = user
        return await func(update, context)
    return wrapper


def require_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = get_user(update)
        if user is None:
            _log_unauthorized(update)
            return ConversationHandler.END
        if users_db.is_disabled(user):
            if update.effective_message:
                await update.effective_message.reply_text("Your account is disabled. Contact an administrator.")
            return ConversationHandler.END
        if not users_db.is_admin(user):
            msg = update.effective_message
            if msg:
                await msg.reply_text("Admins only.")
            return ConversationHandler.END
        context.user_data['_user'] = user
        return await func(update, context)
    return wrapper


def owns_client(user: User, client: dict) -> bool:
    if users_db.is_admin(user):
        return True
    name = str(client.get('name', '')).lower()
    return users_db.peer_owner(name) == user.username


def visible_clients(user: User, clients: list) -> list:
    if users_db.is_admin(user):
        return list(clients)
    out = []
    for c in clients:
        if owns_client(user, c):
            out.append(c)
    return out


def generate_peer_name(existing_names: set[str]) -> str:
    for _ in range(5):
        name = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(PEER_NAME_LEN))
        if name not in existing_names:
            return name
    return ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(PEER_NAME_LEN))


def filter_clients(clients: list, query: str) -> list:
    if not query:
        return clients
    q = query.lower()
    return [c for c in clients if q in c.get("name", "").lower()]


def peers_keyboard(clients: list, action: str, page: int = 0) -> InlineKeyboardMarkup:
    total = len(clients)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    rows = []
    for c in clients[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        name = c.get("name", str(c["id"]))
        owner = users_db.peer_owner(name)
        owner_label = f" · @{owner}" if owner else ""
        if action == "cfg":
            rows.append([InlineKeyboardButton(f"📄 {name}{owner_label}", callback_data=f"cfg:{c['id']}")])
        else:
            rows.append([InlineKeyboardButton(
                f"🗑 {name}{owner_label}",
                callback_data=f"del:{c['id']}:{name[:40]}",
            )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"pg:{action}:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"pg:{action}:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def format_peers_text(clients: list) -> str:
    lines = []
    for c in clients:
        status = "🟢" if c.get("enabled") else "🔴"
        name = html.escape(c.get("name", "?"))
        owner = users_db.peer_owner(str(c.get("name", "")))
        addr = c.get("ipv4Address") or "N/A"
        owner_text = f" · @{html.escape(owner)}" if owner else ""
        lines.append(f"{status} <b>{name}</b>{owner_text}  —  {addr}")
    return "\n".join(lines)


def _dt_short(value, with_time=False, with_year=False):
    if not value:
        return "—"
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if with_year:
        return dt.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")
    return dt.strftime("%d.%m %H:%M" if with_time else "%d.%m")


def _is_message_not_modified_error(error):
    return "message is not modified" in str(error).lower()


async def _edit_callback_message_if_changed(query, *args, **kwargs):
    try:
        await query.edit_message_text(*args, **kwargs)
    except Exception as exc:
        if not _is_message_not_modified_error(exc):
            raise


def _compact_bytes(value):
    formatted = format_bytes(value)
    amount, _, unit = formatted.partition(" ")
    if amount.endswith(".00"):
        amount = amount[:-3]
    elif amount.endswith("0") and "." in amount:
        amount = amount[:-1]
    return f"{amount}{unit}"


def _handshake_ago(client):
    hs = client.get("latestHandshakeAt")
    if not hs:
        return "—"
    try:
        hs_dt = datetime.fromisoformat(str(hs).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    if hs_dt.tzinfo is None:
        hs_dt = hs_dt.replace(tzinfo=timezone.utc)
    sec = (datetime.now(timezone.utc) - hs_dt).total_seconds()
    if sec < 0:
        sec = 0
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{int(sec // 3600)}h"
    return f"{int(sec // 86400)}d"


def _format_request_line(r):
    status_icon = {"pending": "⏳", "approved": "✅", "declined": "❌"}.get(r.get("status"), "•")
    type_icon = "📈" if r.get("type") == "limit" else "🔑"
    if r.get("username"):
        who = "@" + r["username"]
    elif r.get("first_name") or r.get("last_name"):
        who = html.escape(f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip())
    else:
        who = f"id {r['tg_id']}"
    line = f"#{r['id']} {status_icon} {type_icon} {who}"
    if r.get("type") == "limit" and r.get("requested_limit") is not None:
        requested = _display_limit(r.get("requested_limit"))
        line += f" · → {requested}"
    line += f" · {_dt_short(r.get('requested_at'), with_time=True)}"
    if r.get("status") != "pending" and r.get("admin_username"):
        line += f" · by @{r['admin_username']}"
    return line


def _display_limit(limit):
    if limit is None:
        return "—"
    return "∞" if limit == -1 else str(limit)


def _request_action_label(r):
    if r.get("username"):
        who = "@" + r["username"]
    elif r.get("first_name") or r.get("last_name"):
        who = f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip()
    else:
        who = str(r["tg_id"])
    label = f"#{r['id']} {'📈' if r.get('type') == 'limit' else '🔑'} {who}"
    if r.get("type") == "limit" and r.get("requested_limit") is not None:
        label += f" → {_display_limit(r.get('requested_limit'))}"
    return label[:60]


def _request_history_view(page=0):
    page_size = 10
    return _filtered_request_history_view(page, "all", page_size)


def _filtered_request_history_view(page=0, status_filter="all", page_size=10):
    status = None if status_filter == "all" else status_filter
    total = users_db.request_count(status)
    pending_total = users_db.request_count("pending")
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    reqs = users_db.list_requests(page_size, offset=page * page_size, status=status)
    rows = []
    if status_filter == "pending":
        for r in reqs:
            rows.append([
                InlineKeyboardButton(_request_action_label(r), callback_data="noop"),
                InlineKeyboardButton("✅ Approve", callback_data=f"reqhist:approve:{r['id']}:{page}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"reqhist:decline:{r['id']}:{page}"),
            ])
    filter_buttons = []
    if status_filter == "pending":
        filter_buttons.append(InlineKeyboardButton(f"All ({users_db.request_count()})", callback_data="reqview:all:0"))
    else:
        filter_buttons.append(InlineKeyboardButton(f"Pending ({pending_total})", callback_data="reqview:pending:0"))
    rows.insert(0, filter_buttons)
    nav = []
    if page:
        nav.append(InlineKeyboardButton("◀ Previous page", callback_data=f"reqview:{status_filter}:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next page ▶", callback_data=f"reqview:{status_filter}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Close", callback_data="reqclose")])
    title = f"📜 Pending ({pending_total})" if status_filter == "pending" else f"📜 Request history · Pending ({pending_total})"
    text = title
    if status_filter == "pending" and not reqs:
        text += "\nNo pending requests."
    elif status_filter == "all" and not total:
        text += "\nNo requests yet."
    return text, InlineKeyboardMarkup(rows)


def _request_table_rows(page=0, status_filter="all"):
    status = None if status_filter == "all" else status_filter
    reqs = users_db.list_requests(10, offset=max(0, page) * 10, status=status)
    return [
        [
            f"#{r['id']}",
            r.get("username") or str(r["tg_id"]),
            "Limit change" if r.get("type") == "limit" else "Access",
            f"→ {_display_limit(r.get('requested_limit'))}" if r.get("type") == "limit" and r.get("requested_limit") is not None else "—",
            r["status"],
            f"@{r['admin_username']}" if r.get("admin_username") else "—",
            _dt_short(r.get("requested_at"), True),
        ]
        for r in reqs
    ]


async def _send_request_history_table(bot, chat_id, page=0, status_filter="all"):
    if status_filter != "all":
        return
    reqs = users_db.list_requests(10, offset=max(0, page) * 10)
    if not reqs:
        return
    pending_total = users_db.request_count("pending")
    table_title = f"Request history · Pending ({pending_total})"
    headers = ["ID", "User", "Request", "Limit change", "Status", "Fulfilled by", "Requested"]
    rows = _request_table_rows(page)
    fallback = "\n".join(" | ".join(str(cell) for cell in row) for row in rows)
    await _send_table_or_text(bot, chat_id, table_title, headers, rows, fallback)


def _request_history_rich_message(page=0, status_filter="all", page_size=10):
    status = None if status_filter == "all" else status_filter
    total = users_db.request_count(status)
    pending_total = users_db.request_count("pending")
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    reqs = users_db.list_requests(page_size, offset=page * page_size, status=status)
    blocks = [{
        "type": "paragraph",
        "text": f"📜 Request history · Pending ({pending_total})" if status_filter == "all" else f"📜 Pending ({pending_total})",
    }]

    if status_filter == "all" and reqs:
        rows = _request_table_rows(page)
        cells = [[{"text": header, "is_header": True} for header in ("ID", "User", "Request", "Limit change", "Status", "Fulfilled by", "Requested")]]
        cells.extend([[{"text": str(value)} for value in row] for row in rows])
        blocks.append({"type": "table", "is_bordered": True, "is_striped": True, "cells": cells})
    elif status_filter == "pending" and reqs:
        for req in reqs:
            blocks.append({
                "type": "buttons",
                "buttons": [
                    {"text": _request_action_label(req), "callback_data": "noop"},
                    {"text": "✅ Approve", "style": "success", "callback_data": f"reqhist:approve:{req['id']}:{page}"},
                    {"text": "❌ Decline", "style": "danger", "callback_data": f"reqhist:decline:{req['id']}:{page}"},
                ],
            })
    else:
        blocks.append({"type": "paragraph", "text": "No requests yet." if status_filter == "all" else "No pending requests."})

    filter_buttons = []
    if status_filter == "pending":
        filter_buttons.append({"text": f"All ({users_db.request_count()})", "callback_data": "reqview:all:0"})
    else:
        filter_buttons.append({"text": f"Pending ({pending_total})", "callback_data": "reqview:pending:0"})
    blocks.append({"type": "buttons", "buttons": filter_buttons})
    nav_buttons = []
    if page:
        nav_buttons.append({"text": "◀ Previous page", "callback_data": f"reqview:{status_filter}:{page - 1}"})
    nav_buttons.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
    if page + 1 < total_pages:
        nav_buttons.append({"text": "Next page ▶", "callback_data": f"reqview:{status_filter}:{page + 1}"})
    blocks.append({"type": "buttons", "buttons": nav_buttons})
    blocks.append({"type": "buttons", "buttons": [{"text": "Close", "callback_data": "reqclose"}]})
    return {"blocks": blocks}, page


async def _show_request_history(bot, chat_id, page=0, status_filter="all", message_id=None):
    rich, page = _request_history_rich_message(page, status_filter)
    try:
        if message_id is None:
            await bot._post("sendRichMessage", data={"chat_id": chat_id, "rich_message": json.dumps(rich)})
        else:
            await bot._post("editMessageText", data={
                "chat_id": chat_id,
                "message_id": message_id,
                "rich_message": json.dumps(rich),
            })
        return
    except Exception as exc:
        if _is_message_not_modified_error(exc):
            return
        logger.info("Rich request history unavailable; using text view: %s", exc)

    if status_filter == "all":
        await _send_request_history_table(bot, chat_id, page, status_filter)
    text, markup = _filtered_request_history_view(page, status_filter)
    if message_id is None:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=markup)
        except Exception as exc:
            if not _is_message_not_modified_error(exc):
                raise


async def _edit_request_copies(context, req, text):
    try:
        refs = json.loads(req.get("notification_refs") or "[]")
    except (TypeError, ValueError):
        refs = []
    for chat_id, message_id in refs:
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.info("Could not edit request notification %s/%s: %s", chat_id, message_id, exc)


async def _send_table_or_text(bot, chat_id, title, headers, rows, fallback):
    """Use Bot API 10.1 rich tables directly when supported; gracefully fall back."""
    rich = {
        "blocks": [
            {"type": "paragraph", "text": title},
            {"type": "table", "is_bordered": True, "is_striped": True,
             "cells": [[{"text": str(value), "is_header": True} for value in headers]] +
                      [[{"text": str(value)} for value in row] for row in rows]},
        ]
    }
    try:
        await bot._post("sendRichMessage", data={"chat_id": chat_id, "rich_message": json.dumps(rich)})
    except Exception as exc:
        logger.info("Rich table unavailable; using readable text fallback: %s", exc)
        await bot.send_message(chat_id, fallback)


async def _create_peer_for(update: Update, target: str, custom_name, message=None):
    if message is None:
        message = update.effective_message
    if message is None:
        return
    try:
        clients = wg.list_clients()
    except Exception as e:
        await message.reply_text(f"API error: {e}")
        return
    live_names = {str(c.get("name", "")).lower() for c in clients}
    db_names = set(users_db.peers_of(target))
    active = db_names & live_names
    target_user = users_db.get_user(target)
    target_limit = target_user.config_limit if target_user is not None else -1
    if target_limit != -1 and len(active) >= target_limit and not (users_db.is_admin(get_user(update)) and target == get_user(update).username):
        await message.reply_text(f"Device limit reached: {len(active)}/{target_limit}. Remove a device to make room.")
        return
    if custom_name is not None:
        if not PEER_NAME_RE.match(custom_name):
            await message.reply_text(
                f"Invalid device name. Use 1–{MAX_PEER_NAME_LEN} characters: letters, numbers, underscore (_) or hyphen (-)."
            )
            return
        if custom_name.lower() in (live_names | db_names):
            await message.reply_text(
                f"A device named <b>{html.escape(custom_name)}</b> already exists. Choose another name.",
                parse_mode="HTML",
            )
            return
        peer_name = custom_name
    else:
        peer_name = generate_peer_name(live_names | db_names)
    try:
        result = wg.create_client(peer_name)
    except Exception as e:
        await message.reply_text(f"API error: {e}")
        return
    client_id = result.get("clientId")
    try:
        if client_id:
            import hashlib
            hash_name = hashlib.sha256(str(client_id).encode()).hexdigest()[:8]
            if hash_name.lower() not in (live_names | db_names):
                try:
                    wg.rename_client(client_id, hash_name)
                    peer_name = hash_name
                except Exception:
                    pass
        users_db.add_peer(peer_name.lower(), target, client_id)
    except Exception:
        logger.warning("Failed to persist peer %s for %s", peer_name, target)
    try:
        config_data, filename = wg.get_client_config(client_id)
        await message.reply_document(
            document=BytesIO(config_data),
            filename=filename,
            caption=f"✅ Device <b>{html.escape(peer_name)}</b> created for <b>{html.escape(target)}</b>!\n\n{AMNEZIA_LINKS}",
            parse_mode="HTML",
        )
    except Exception:
        await message.reply_text(
            f"✅ Device <b>{html.escape(peer_name)}</b> created for <b>{html.escape(target)}</b>!",
            parse_mode="HTML",
        )


async def _show_peer_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user = get_user(update)
    if user is None:
        return
    try:
        clients = wg.list_clients()
    except Exception as e:
        if update.effective_message:
            await update.effective_message.reply_text(f"API error: {e}")
        return
    clients = visible_clients(user, clients)
    if not clients:
        if update.effective_message:
            await update.effective_message.reply_text("No devices found.")
        return
    context.user_data[f"{action}_filter"] = ""
    if update.effective_message:
        await update.effective_message.reply_text(
            PEER_SELECT_TITLE[action],
            reply_markup=peers_keyboard(clients, action),
        )


def _users_list_view(page=0, status_filter="all"):
    if status_filter not in {"all", "enabled", "disabled"}:
        return None
    if not users_db.list_users():
        return None
    users = users_db.list_users(None if status_filter == "all" else status_filter)
    users.sort(key=lambda user: (user.role != "admin", user.username))
    page_size = PAGE_SIZE
    total_pages = max(1, (len(users) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    snapshot_names = {str(c.get("name", "")).lower() for c in users_db.list_peer_snapshots()}
    rows = []
    filters_row = [
        InlineKeyboardButton(label, callback_data=f"usersview:{key}:0")
        for key, label in (("all", "All"), ("enabled", "Enabled"), ("disabled", "Disabled"))
    ]
    rows.append(filters_row)
    start = page * page_size
    for u in users[start:start + page_size]:
        peers = users_db.peers_of(u.username)
        peer_count = sum(1 for name in peers if name in snapshot_names)
        limit = u.config_limit if u.config_limit >= 0 else "∞"
        admin_label = " (admin)" if u.role == "admin" else ""
        label = f"{u.username}{admin_label} {peer_count}/{limit}"
        if u.status == "disabled":
            label += " ⛔"
        rows.append([InlineKeyboardButton(label, callback_data=f"user:{u.username}:{status_filter}:{page}")])
    nav = []
    if page:
        nav.append(InlineKeyboardButton("◀ Previous", callback_data=f"userspage:{status_filter}:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"userspage:{status_filter}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="cancel")])
    title = f"👥 Users · {status_filter.title()} ({len(users)}) · {page + 1}/{total_pages}"
    if users:
        title += " — tap to manage:"
    else:
        title += f"\nNo {status_filter} users."
    return title, InlineKeyboardMarkup(rows)


def _user_detail_view(username, back_target="menu:users"):
    u = users_db.get_user(username)
    if u is None:
        return None
    by_name = {
        str(c.get("name", "")).lower(): c
        for c in users_db.list_peer_snapshots()
    }
    records = users_db.peer_records(u.username)
    active = [rec for rec in records if rec["name"] in by_name]
    limit = "∞" if u.config_limit == -1 else str(u.config_limit)
    head = f"👤 <b>{html.escape(username)}</b> [{u.role}]"
    if u.status == "disabled":
        head += " ⛔ DISABLED"
    lines = [head, f"<b>Limit:</b> {len(active)}/{limit}"]
    lines.append(f"<b>TG:</b> {u.tg_id}" if u.tg_id not in (None, -1) else "<b>TG:</b> pending")
    peers_label = f"👥 Devices ({len(active)})"
    role_label = "⬇ Demote to user" if u.role == "admin" else "⬆ Promote to admin"
    status_button = InlineKeyboardButton(
        "▶ Enable" if u.status == "disabled" else "⏸ Disable",
        callback_data=f"userenable:{username}" if u.status == "disabled" else f"userdisable:{username}",
    )
    rows = [
        [InlineKeyboardButton(peers_label, callback_data=f"upeers:{username}")],
        [InlineKeyboardButton("📝 Limit", callback_data=f"ulimit:{username}"), InlineKeyboardButton("📜 Requests", callback_data=f"ureqs:{username}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"udel:{username}"), status_button],
        [InlineKeyboardButton(role_label, callback_data=f"role:{username}")],
        _back_close_row(back_target),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _back_close_row(target):
    return [InlineKeyboardButton("◀ Back", callback_data=target), InlineKeyboardButton("❌ Close", callback_data="cancel")]


def _peers_list_view(username):
    u = users_db.get_user(username)
    if u is None:
        return None
    by_name = {
        str(c.get("name", "")).lower(): c
        for c in users_db.list_peer_snapshots()
    }
    records = users_db.peer_records(u.username)
    active = [rec for rec in records if rec["name"] in by_name]
    limit = "∞" if u.config_limit == -1 else str(u.config_limit)
    lines = [f"👥 Devices of <b>{html.escape(username)}</b>"]
    if active:
        lines[0] += f" ({len(active)}/{limit})"
        lines.append("Tap a name for details:")
    else:
        lines.append("Devices: none")
    rows = []
    for rec in active:
        c = by_name[rec["name"]]
        hs = c.get("latestHandshakeAt")
        details = (
            f"{_dt_short(rec['created_at'], with_year=True)} · ↑{_compact_bytes(c.get('transferRx'))} "
            f"↓{_compact_bytes(c.get('transferTx'))} · {_handshake_ago(c)} "
            f"({_dt_short(hs, with_time=True) if hs else 'never'})"
        )
        name_budget = max(1, 64 - len(details) - 3)
        name = rec["name"]
        short_name = name if len(name) <= name_budget else f"{name[:name_budget - 1]}…"
        label = f"{short_name} · {details}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pinfo:{c['id']}")])
    rows.append(_back_close_row(f"user:{username}"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _peer_info_view(client_id, admin=True, back_target=None):
    try:
        c = wg.get_client(client_id)
    except Exception:
        return None
    snapshot = users_db.get_peer_snapshot(client_id)
    if snapshot is not None:
        c = {
            **c,
            "latestHandshakeAt": snapshot["latestHandshakeAt"],
            "transferRx": snapshot["transferRx"],
            "transferTx": snapshot["transferTx"],
        }
    name = str(c.get("name", ""))
    rec = users_db.get_peer(name)
    owner = rec["owner"] if rec else None
    status = "🟢 Enabled" if c.get("enabled") else "🔴 Disabled"
    ip = c.get("ipv4Address") or "N/A"
    created = _dt_short(rec["created_at"], with_time=True, with_year=True) if rec else "—"
    hs = c.get("latestHandshakeAt")
    parsed_hs = None
    if hs:
        try:
            parsed_hs = datetime.fromisoformat(str(hs).replace("Z", "+00:00"))
        except ValueError:
            parsed_hs = None
    hs_line = f"{_dt_short(hs, with_time=True)} ({_handshake_ago(c)} ago)" if parsed_hs is not None else "never"
    lines = [f"🔹 <b>{html.escape(name)}</b>"]
    if owner:
        lines.append(f"<b>Owner:</b> @{html.escape(owner)}")
    lines.append(f"<b>Status:</b> {status}")
    activity = "🟢 Active" if snapshot is not None and snapshot["is_active"] else "🔴 Inactive"
    lines.append(f"<b>Activity:</b> {activity}")
    lines.append(f"<b>IP:</b> {html.escape(str(ip))}")
    lines.append(f"<b>Created:</b> {created}")
    lines.append(f"<b>Last handshake:</b> {hs_line}")
    lines.append(
        f"<b>Traffic total:</b> ↑ Upload {format_bytes(c.get('transferRx'))} · "
        f"↓ Download {format_bytes(c.get('transferTx'))}"
    )
    toggle = "⏸ Disable" if c.get("enabled") else "▶ Enable"
    rows = [[InlineKeyboardButton("📥 Download configuration", callback_data=f"cfg:{client_id}")]]
    if owner or admin:
        rows.append([InlineKeyboardButton("✏️ Rename", callback_data=f"ren:{client_id}:{name[:40]}")])
    if admin:
        rows.append([InlineKeyboardButton(toggle, callback_data=f"ptog:{client_id}"), InlineKeyboardButton("🗑 Delete", callback_data=f"del:{client_id}:{name[:40]}")])
    if back_target is None:
        back_target = f"upeers:{owner}" if admin and owner else "menu:list"
    rows.append(_back_close_row(back_target))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _set_user_devices_enabled(username, enable):
    count = 0
    try:
        by_name = {str(c.get("name", "")).lower(): c for c in wg.list_clients()}
        for name in users_db.peers_of(username):
            c = by_name.get(name)
            if c is None:
                continue
            if enable:
                if c.get("enabled") == False:
                    wg.enable_client(c["id"])
                    count += 1
            else:
                if c.get("enabled"):
                    wg.disable_client(c["id"])
                    count += 1
    except Exception as e:
        return count, str(e)
    return count, None


def _delete_user_cascade(username):
    u = users_db.get_user(username)
    if u is None:
        return "User not found."
    names = users_db.peers_of(username)
    deleted = 0
    err = None
    try:
        by_name = {str(c.get("name", "")).lower(): c for c in wg.list_clients()}
        for name in names:
            c = by_name.get(name)
            if c is not None:
                wg.delete_client(c["id"])
                users_db.remove_peer(name)
                deleted += 1
    except Exception as e:
        err = str(e)
        for name in names:
            try:
                users_db.remove_peer(name)
            except Exception:
                pass
    users_db.remove_user(username)
    if err:
        return f"User deleted, but device deletion failed: {err}"
    return f"🗑 User <b>{html.escape(username)}</b> deleted with {deleted} device(s)."


# ── Commands ──────────────────────────────────────────────────────────────────

@require_access
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    if user and users_db.is_admin(user):
        await update.message.reply_text("WireGuard Manager", reply_markup=ADMIN_KEYBOARD)
    else:
        await update.message.reply_text("WireGuard Manager", reply_markup=USER_KEYBOARD)


@require_access
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    await update.message.reply_text("WireGuard Manager", reply_markup=_inline_menu_markup(user))


async def givemepowers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u is None:
        return
    user = get_user(update)
    if user is not None:
        if users_db.is_disabled(user):
            await update.effective_message.reply_text("Your account is disabled. Contact an administrator.")
            return
        limit = user.config_limit if user.config_limit != -1 else "∞"
        try:
            clients = visible_clients(user, wg.list_clients())
        except Exception as e:
            logger.warning("Failed to list peers for %s: %s", user.username, e)
            text = "Powers already granted ✅"
            own = users_db.list_requests(limit=5, tg_id=user.tg_id)
            if own:
                text = text + "\n\n📜 Your requests:\n" + "\n".join(_format_request_line(r) for r in own)
            await update.effective_message.reply_text(text, parse_mode="HTML")
            return
        text = "Powers already granted ✅\n\n" + format_peers_text(clients) + f"\n\nLimit: {len(clients)}/{limit}"
        own = users_db.list_requests(limit=5, tg_id=user.tg_id)
        if own:
            text = text + "\n\n📜 Your requests:\n" + "\n".join(_format_request_line(r) for r in own)
        await update.effective_message.reply_text(text, parse_mode="HTML")
        return
    admins = users_db.admins_with_tg()
    if not admins:
        await update.effective_message.reply_text("No admins available to review right now. Try again later.")
        return
    if users_db.has_pending_request(u.id):
        await update.effective_message.reply_text("Your request is already pending review.")
        return
    req_id = users_db.add_request(u.id, u.username, u.first_name, u.last_name)
    label = f"@{u.username}" if u.username else ((u.first_name or "") + (" " + u.last_name if u.last_name else "")).strip() or f"id {u.id}"
    refs = []
    for admin in admins:
        try:
            sent = await context.bot.send_message(
                admin.tg_id,
                f"🔔 Access request #{req_id}\nUser: {label} (id: {u.id})",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"req:approve:{req_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"req:decline:{req_id}"),
                ]]),
            )
            refs.append((sent.chat_id, sent.message_id))
        except Exception:
            logger.warning("Failed to notify admin %s", admin.username)
    users_db.set_request_notifications(req_id, refs)
    await update.effective_message.reply_text("Request sent to admins. You'll be notified when reviewed.")


@require_access
async def list_peers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    if user is None:
        return
    clients = visible_clients(user, users_db.list_peer_snapshots())
    await _show_devices(update.effective_message, clients, user=user)


def _device_list_markup(clients, user, filter_name="all", page=0):
    filters_row = [InlineKeyboardButton(label, callback_data=f"dev:{key}:0") for key, label in (("all", "All"), ("active", "Active"), ("enabled", "Enabled"), ("disabled", "Disabled"))]
    clients = filter_peer_snapshots(clients, filter_name)
    pages = max(1, (len(clients) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = [filters_row]
    for c in clients[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        name = str(c.get("name", "?"))
        owner = users_db.peer_owner(name.lower())
        hs = c.get("latestHandshakeAt")
        when = _dt_short(hs, with_time=True) if hs else "never"
        upload = _compact_bytes(c.get("transferRx"))
        download = _compact_bytes(c.get("transferTx"))
        details = f"↑{upload} ↓{download} · {_handshake_ago(c)} ({when})"
        owner_label = f" · @{owner[:8]}" if users_db.is_admin(user) and owner else ""
        name_budget = max(1, 64 - len(details) - len(owner_label) - 1)
        short_name = name if len(name) <= name_budget else f"{name[:name_budget - 1]}…"
        label = f"{short_name} {details}{owner_label}"
        if len(label) > 64:
            owner_label = ""
            name_budget = max(1, 64 - len(details) - 1)
            short_name = name if len(name) <= name_budget else f"{name[:name_budget - 1]}…"
            label = f"{short_name} {details}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pinfo:{c['id']}:{filter_name}:{page}")])
    nav = []
    if page:
        nav.append(InlineKeyboardButton("◀", callback_data=f"dev:{filter_name}:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶", callback_data=f"dev:{filter_name}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="cancel")])
    return InlineKeyboardMarkup(rows), clients, page, pages


async def _show_devices(message, clients, filter_name="all", page=0, user=None):
    markup, filtered, page, pages = _device_list_markup(clients, user, filter_name, page)
    title = _device_list_title(filter_name, filtered, page, pages, clients, user)
    await message.reply_text(
        title if filtered else f"{title}\nNo {filter_name} devices.",
        parse_mode="HTML",
        reply_markup=markup,
    )


def _device_list_title(filter_name, filtered, page, pages, clients, user):
    title = f"Devices · {filter_name.title()} ({len(filtered)}) · {page + 1}/{pages}"
    if user is not None and not users_db.is_admin(user):
        limit = "∞" if user.config_limit == -1 else str(user.config_limit)
        title += f"\n<b>Your device limit:</b> {len(clients)}/{limit}"
    return title


async def _peer_snapshot_loop():
    while True:
        try:
            clients = await asyncio.to_thread(fetch_peer_sample, wg)
            if not apply_peer_sample(users_db, clients):
                logger.warning("Unable to refresh peer snapshots; keeping previous sample")
        except Exception:
            logger.exception("Failed to persist peer snapshot")
        await asyncio.sleep(PEER_SAMPLE_INTERVAL_SECONDS)


async def _post_init(application: Application) -> None:
    application.bot_data["peer_snapshot_task"] = asyncio.create_task(
        _peer_snapshot_loop(), name="peer-snapshot-loop"
    )


async def _post_shutdown(application: Application) -> None:
    task = application.bot_data.pop("peer_snapshot_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── Create peer ────────────────────────────────────────────────────────────────

@require_access
async def create_peer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_disabled(user):
        if update.effective_message:
            await update.effective_message.reply_text("Your account is disabled. Contact an administrator.")
        return ConversationHandler.END
    if not users_db.is_admin(user):
        await _create_peer_for(update, user.username, None)
        return ConversationHandler.END
    if context.args:
        target = users_db.get_user(context.args[0])
        if target is None:
            await update.effective_message.reply_text("User not found.")
            return ConversationHandler.END
        await _create_peer_for(update, target.username, None)
        return ConversationHandler.END
    await update.effective_message.reply_text("Create device:", reply_markup=CREATE_MENU)
    return ConversationHandler.END


async def create_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_disabled(user):
        try:
            await query.edit_message_text("Your account is disabled. Contact an administrator.")
        except Exception:
            pass
        return ConversationHandler.END
    if not users_db.is_admin(user):
        await query.edit_message_text("Admins only.")
        return ConversationHandler.END
    await query.edit_message_text(
        f"Enter a device name. Use 1–{MAX_PEER_NAME_LEN} characters: letters, numbers, underscore (_) or hyphen (-).",
        reply_markup=CANCEL_CONV_KB,
    )
    return WAITING_FOR_NEWNAME


@require_admin
async def create_name_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data.get("_user") or get_user(update)
    if user is None:
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not PEER_NAME_RE.match(name):
        await update.message.reply_text("Invalid name: use 1-15 chars, letters/digits/_/- only.")
        return WAITING_FOR_NEWNAME
    await _create_peer_for(update, user.username, name)
    return ConversationHandler.END


async def userlimit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = context.user_data.get("_user") or get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_disabled(user):
        try:
            await query.edit_message_text("Your account is disabled. Contact an administrator.")
        except Exception:
            pass
        return ConversationHandler.END
    if not users_db.is_admin(user):
        await query.edit_message_text("Admins only.")
        return ConversationHandler.END
    username = query.data.split(":", 1)[1]
    if not users_db.user_exists(username):
        await query.edit_message_text("User not found.")
        return ConversationHandler.END
    context.user_data["_ulimit_target"] = username
    await query.edit_message_text(
        f"Enter new config limit for <b>{html.escape(username)}</b> (-1 = unlimited):",
        parse_mode="HTML",
        reply_markup=CANCEL_CONV_KB,
    )
    return WAITING_FOR_USERLIMIT


@require_admin
async def userlimit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = context.user_data.pop("_ulimit_target", None)
    if username is None:
        await update.message.reply_text("Nothing to change.")
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a valid integer (-1 = unlimited).")
        return WAITING_FOR_USERLIMIT
    try:
        users_db.set_limit(username, n)
    except KeyError:
        await update.message.reply_text("User not found.")
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Limit for <b>{html.escape(username)}</b> set to {n}.", parse_mode="HTML")
    return ConversationHandler.END


async def limitreq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    if cb:
        await cb.answer()
    user = get_user(update)
    if user is None:
        if cb:
            _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_disabled(user):
        msg = "Your account is disabled. Contact an administrator."
        if cb:
            try:
                await cb.edit_message_text(msg)
            except Exception:
                pass
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END
    if users_db.is_admin(user):
        msg = "Admins can change limits directly via /limit."
        if cb:
            await cb.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END
    if users_db.has_pending_request(user.tg_id, "limit"):
        msg = "Your request is already pending review."
        if cb:
            await cb.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END
    admins = users_db.admins_with_tg()
    if not admins:
        msg = "No admins available to review right now. Try again later."
        if cb:
            await cb.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END
    prompt = f"Enter the config limit you want (current: {user.config_limit}, -1 = unlimited):"
    if cb:
        await cb.edit_message_text(prompt, reply_markup=CANCEL_CONV_KB)
    else:
        await update.message.reply_text(prompt, reply_markup=CANCEL_CONV_KB)
    return WAITING_FOR_LIMITREQ


async def limitreq_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data.get("_user") or get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    user = users_db.get_user(user.username) or user
    if users_db.is_disabled(user):
        await update.message.reply_text("Your account is disabled. Contact an administrator.")
        return ConversationHandler.END
    if users_db.is_admin(user):
        await update.message.reply_text("Admins can change limits directly via /limit.")
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a valid integer (-1 = unlimited).")
        return WAITING_FOR_LIMITREQ
    if n < -1:
        await update.message.reply_text("Enter a valid integer (-1 = unlimited).")
        return WAITING_FOR_LIMITREQ
    if n == user.config_limit:
        await update.message.reply_text("You already have this limit.")
        return ConversationHandler.END
    admins = users_db.admins_with_tg()
    if not admins:
        await update.message.reply_text("No admins available to review right now. Try again later.")
        return ConversationHandler.END
    req_id = users_db.add_request(
        user.tg_id,
        user.username,
        update.effective_user.first_name,
        update.effective_user.last_name,
        req_type="limit",
        requested_limit=n,
    )
    refs = []
    for admin in admins:
        try:
            sent = await context.bot.send_message(
                admin.tg_id,
                f"🔔 Limit change request #{req_id}\nUser: @{user.username} (id: {user.tg_id})\nCurrent: {user.config_limit}\nRequested: {n}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"req:approve:{req_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"req:decline:{req_id}"),
                ]]),
            )
            refs.append((sent.chat_id, sent.message_id))
        except Exception:
            logger.warning("Failed to notify admin %s", admin.username)
    users_db.set_request_notifications(req_id, refs)
    await update.message.reply_text("Request sent to admins. You'll be notified when reviewed.")
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    if user is None:
        return ConversationHandler.END
    if update.effective_message:
        await delete_message_silently(update.effective_message)
    return ConversationHandler.END


async def cancel_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        return ConversationHandler.END
    await delete_callback_message(query)
    return ConversationHandler.END


# ── Rename peer conversation ──────────────────────────────────────────────────

async def rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_disabled(user):
        try:
            await query.edit_message_text("Your account is disabled. Contact an administrator.")
        except Exception:
            pass
        return ConversationHandler.END
    parts = query.data.split(":", 2)
    context.user_data["rename_id"] = parts[1]
    context.user_data["rename_old"] = parts[2] if len(parts) > 2 else parts[1]
    try:
        peer = wg.get_client(parts[1])
        owner = users_db.peer_owner(str(peer.get("name", "")))
    except Exception:
        owner = None
    if not users_db.is_admin(user) and owner != user.username:
        await query.edit_message_text("You can rename only your own configurations.")
        return ConversationHandler.END

    await query.message.reply_text(
        f"Enter a new device name for <b>{html.escape(context.user_data['rename_old'])}</b>.\n"
        f"Use 1–{MAX_PEER_NAME_LEN} characters: letters, numbers, underscore (_) or hyphen (-).",
        parse_mode="HTML",
        reply_markup=CANCEL_CONV_KB,
    )
    return WAITING_FOR_RENAME


async def rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    if user is None:
        return ConversationHandler.END
    new_name = update.message.text.strip()
    if not PEER_NAME_RE.fullmatch(new_name):
        await update.message.reply_text(
            f"Invalid device name. Use 1–{MAX_PEER_NAME_LEN} characters: letters, numbers, underscore (_) or hyphen (-)."
        )
        return WAITING_FOR_RENAME
    if len(new_name) > MAX_PEER_NAME_LEN:
        await update.message.reply_text(
            f"Name too long: max {MAX_PEER_NAME_LEN} characters (AmneziaWG tunnel name limit). Enter new name:"
        )
        return WAITING_FOR_RENAME

    client_id = context.user_data.get("rename_id")
    old_display = context.user_data.get("rename_old", client_id)
    try:
        current = wg.get_client(client_id)
        old_name = str(current.get("name", "")).lower()
        owner = users_db.peer_owner(old_name)
        if not users_db.is_admin(user) and owner != user.username:
            await update.message.reply_text("You can rename only your own configurations.")
            return ConversationHandler.END
        wg.rename_client(client_id, new_name)
        users_db.rename_peer(old_name, new_name)
        await update.message.reply_text(
            f"✅ <b>{html.escape(old_display)}</b> → <b>{html.escape(new_name)}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"API error: {e}")

    return ConversationHandler.END


# ── Admin user management ─────────────────────────────────────────────────────

@require_admin
async def adduser_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Forward a message from the user, or send their username (optionally with limit: username 5).",
        reply_markup=CANCEL_CONV_KB,
    )
    return WAITING_FOR_NEWUSER


@require_admin
async def adduser_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return WAITING_FOR_NEWUSER

    origin = msg.forward_origin
    if origin is not None:
        if isinstance(origin, MessageOriginHiddenUser):
            await msg.reply_text("Forwarded user is hidden by privacy settings. Send their username as text instead.")
            return WAITING_FOR_NEWUSER
        if isinstance(origin, MessageOriginUser):
            origin_user = origin.sender_user
            uname = origin_user.username
            if not uname:
                await msg.reply_text("Forwarded user has no username. Send their username as text instead.")
                return WAITING_FOR_NEWUSER
            try:
                user_added = users_db.add_user(uname, tg_id=origin_user.id)
            except ValueError:
                await msg.reply_text("User already exists.")
                return ConversationHandler.END
            await msg.reply_text(
                f"✅ User <b>{html.escape(uname)}</b> added, limit {user_added.config_limit}.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        await msg.reply_text("Forwarded message is not from a user. Send their username as text instead.")
        return WAITING_FOR_NEWUSER

    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text("Send their username (optionally with limit: username 5).")
        return WAITING_FOR_NEWUSER
    parts = text.split()
    uname_cand = parts[0].strip().lstrip("@").lower()
    limit_cand = None
    if len(parts) >= 2:
        try:
            limit_cand = int(parts[1])
        except Exception:
            limit_cand = None
    try:
        user_added = users_db.add_user(uname_cand, tg_id=None, limit=limit_cand)
        limit_out = user_added.config_limit
        await msg.reply_text(f"✅ User <b>{html.escape(uname_cand)}</b> added, limit {limit_out}.", parse_mode="HTML")
    except ValueError:
        await msg.reply_text("User already exists.")
    return ConversationHandler.END


@require_admin
async def users_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    view = _users_list_view()
    if view is None:
        await update.message.reply_text("No registered users.")
        return
    text, kb = view
    await update.message.reply_text(text, reply_markup=kb)


@require_admin
async def requests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_request_history(context.bot, update.effective_chat.id)


@require_admin
async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /limit <username> <N>")
        return
    uname = str(context.args[0]).strip().lstrip("@").lower()
    try:
        N = int(context.args[1])
    except Exception:
        await update.message.reply_text("Usage: /limit <username> <N>")
        return
    try:
        users_db.set_limit(uname, N)
        await update.message.reply_text(f"✅ Limit updated for <b>{html.escape(uname)}</b> to {N}.", parse_mode="HTML")
    except KeyError:
        await update.message.reply_text("User not found.")


@require_admin
async def deluser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /deluser <username>")
        return
    uname = str(context.args[0]).strip().lstrip("@").lower()
    await update.message.reply_text(_delete_user_cascade(uname), parse_mode="HTML")


@require_admin
async def disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /disable <username>")
        return
    target = users_db.get_user(args[0])
    if target is None:
        await update.message.reply_text("User not found.")
        return
    if target.role == "admin":
        await update.message.reply_text("Cannot disable an admin.")
        return
    admin = get_user(update)
    users_db.disable_user(target.username, disabled_by=admin.username if admin else None)
    disabled, err = _set_user_devices_enabled(target.username, False)
    if err:
        logger.warning("Failed to disable peers for %s: %s", target.username, err)
        await update.message.reply_text(f"User disabled, but device disable failed: {err}")
    else:
        await update.message.reply_text(f"⏸ User {target.username} disabled; {disabled} device(s) disabled.")


@require_admin
async def enable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /enable <username>")
        return
    target = users_db.get_user(args[0])
    if target is None:
        await update.message.reply_text("User not found.")
        return
    if target.role == "admin":
        await update.message.reply_text("Cannot change an admin's enabled status.")
        return
    users_db.enable_user(target.username)
    enabled, err = _set_user_devices_enabled(target.username, True)
    if err:
        logger.warning("Failed to enable peers for %s: %s", target.username, err)
        await update.message.reply_text(f"User enabled, but device enable failed: {err}")
    else:
        await update.message.reply_text(f"▶ User {target.username} enabled; {enabled} device(s) enabled.")


# ── Callback handler ──────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return
    data = query.data
    if data == "cancel":
        await delete_callback_message(query)
        return
    if users_db.is_disabled(user):
        try:
            await query.edit_message_text("Your account is disabled. Contact an administrator.")
        except Exception:
            pass
        return
    context.user_data['_user'] = user

    history_action = False
    history_page = 0
    if data.startswith("reqhist:"):
        history_parts = data.split(":")
        if len(history_parts) != 4 or history_parts[1] not in {"approve", "decline"}:
            return
        try:
            history_page = int(history_parts[3])
        except ValueError:
            return
        data = f"req:{history_parts[1]}:{history_parts[2]}"
        history_action = True

    if data == "noop":
        return

    if data.startswith("role:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        target_name = data.split(":", 1)[1]
        target = users_db.get_user(target_name)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        new_role = "user" if target.role == "admin" else "admin"
        try:
            users_db.set_role(target_name, new_role)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        view = _user_detail_view(target_name)
        await query.edit_message_text(view[0], parse_mode="HTML", reply_markup=view[1])
        return

    if data.startswith("reqpage:") or data.startswith("reqview:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        parts = data.split(":")
        if parts[0] == "reqpage" and len(parts) == 2:
            status_filter, page_text = "all", parts[1]
        elif len(parts) == 3 and parts[1] in {"all", "pending"}:
            status_filter, page_text = parts[1], parts[2]
        else:
            return
        try:
            page = int(page_text)
        except ValueError:
            return
        await _show_request_history(
            context.bot,
            query.message.chat_id,
            page,
            status_filter,
            message_id=query.message.message_id,
        )
        return

    if data == "reqclose":
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        await delete_callback_message(query)
        return

    if data == "create:pickuser":
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        users = users_db.list_users()
        if not users:
            await query.edit_message_text("No registered users.")
            return
        rows = []
        for u in users:
            lim = u.config_limit if u.config_limit != -1 else "∞"
            rows.append([InlineKeyboardButton(
                f"{u.username} ({u.role}, limit {lim})",
                callback_data=f"createfor:{u.username}",
            )])
        rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
        await query.edit_message_text("Create device for:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "create:self":
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        await query.edit_message_text("Creating a configuration for you…")
        await _create_peer_for(update, user.username, None, message=query.message)
        return

    if data.startswith("createfor:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        target = data.split(":", 1)[1]
        if not users_db.user_exists(target):
            await query.edit_message_text("User not found.")
            return
        await query.edit_message_text(f"Creating a device for {target}...")
        await _create_peer_for(update, target, None, message=query.message)
        return

    if data.startswith("req:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        parts = data.split(":")
        action = parts[1]
        try:
            req_id = int(parts[2])
        except Exception:
            return
        req = users_db.get_request(req_id)
        if req is None or req["status"] != "pending":
            await query.edit_message_text("Request already processed.")
            return
        if action not in ("approve", "decline"):
            return

        async def finish_request_action(result_text):
            if history_action:
                await _show_request_history(
                    context.bot,
                    query.message.chat_id,
                    history_page,
                    "pending",
                    message_id=query.message.message_id,
                )
            else:
                await query.edit_message_text(result_text)

        decision = "approved" if action == "approve" else "declined"
        if not users_db.decide_request(req_id, decision, user.tg_id, user.username):
            await query.edit_message_text("Request already processed.")
            return
        if req.get("type", "access") == "limit":
            if action == "approve":
                if not req["username"]:
                    users_db.decide_request(req_id, "approved", user.tg_id, user.username)
                    await query.edit_message_text("Request invalid (no username).")
                    return
                try:
                    users_db.set_limit(req["username"], req["requested_limit"])
                except KeyError:
                    users_db.decide_request(req_id, "approved", user.tg_id, user.username)
                    await query.edit_message_text("User no longer exists (request resolved).")
                    return
                users_db.decide_request(req_id, "approved", user.tg_id, user.username)
                await _edit_request_copies(context, req, f"✅ Request #{req_id} approved by @{user.username}.")
                try:
                    await context.bot.send_message(
                        req["tg_id"],
                        f"✅ Your config limit is now {req['requested_limit']}.",
                    )
                except Exception:
                    logger.warning("Failed to notify user %s about limit approval", req["tg_id"])
                await finish_request_action(
                    f"✅ Request #{req_id} approved: @{req['username']} limit → {req['requested_limit']}"
                )
                return
            if action == "decline":
                users_db.decide_request(req_id, "declined", user.tg_id, user.username)
                await _edit_request_copies(context, req, f"❌ Request #{req_id} declined by @{user.username}.")
                try:
                    await context.bot.send_message(
                        req["tg_id"],
                        f"❌ Your limit request was declined by @{user.username}.",
                    )
                except Exception:
                    logger.warning("Failed to notify user %s about limit decline", req["tg_id"])
                await finish_request_action(f"❌ Request #{req_id} declined.")
                return
        if action == "approve":
            uname = req["username"] or f"user{req['tg_id']}"
            try:
                created = users_db.add_user(uname, tg_id=req["tg_id"])
            except ValueError:
                users_db.decide_request(req_id, "approved", user.tg_id, user.username)
                await query.edit_message_text("User already exists (request resolved).")
                return
            users_db.decide_request(req_id, "approved", user.tg_id, user.username)
            await _edit_request_copies(context, req, f"✅ Request #{req_id} approved by @{user.username}: {created.username} registered.")
            try:
                await context.bot.send_message(
                    req["tg_id"],
                    f"✅ You are registered! Your config limit: {created.config_limit}.\nUse /start to begin.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 Open menu", callback_data="menu:home"),
                    ]]),
                )
            except Exception:
                logger.warning("Failed to notify user %s about approval", req["tg_id"])
            await finish_request_action(f"✅ Request #{req_id} approved: {created.username} registered.")
            return
        if action == "decline":
            users_db.decide_request(req_id, "declined", user.tg_id, user.username)
            await _edit_request_copies(context, req, f"❌ Request #{req_id} declined by @{user.username}.")
            try:
                await context.bot.send_message(
                    req["tg_id"],
                    f"❌ Your access request was declined by @{user.username}.",
                )
            except Exception:
                logger.warning("Failed to notify user %s about decline", req["tg_id"])
            await finish_request_action(f"❌ Request #{req_id} declined.")
            return
        return

    if data.startswith("pinfo:"):
        parts = data.split(":")
        client_id = parts[1]
        back_target = None
        if len(parts) == 4 and parts[2] in {"all", "active", "enabled", "disabled"}:
            try:
                back_target = f"dev:{parts[2]}:{int(parts[3])}"
            except ValueError:
                pass
        try:
            current_peer = wg.get_client(client_id)
            owner = users_db.peer_owner(str(current_peer.get("name", "")))
        except Exception:
            owner = None
        if not users_db.is_admin(user) and owner != user.username:
            await query.edit_message_text("You can view only your own configurations.")
            return
        view = _peer_info_view(client_id, admin=users_db.is_admin(user), back_target=back_target)
        if view is None:
            await query.edit_message_text(
                "Device not found.",
                reply_markup=InlineKeyboardMarkup([_back_close_row(back_target or ("menu:users" if users_db.is_admin(user) else "menu:list"))]),
            )
            return
        text, kb = view
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ptog:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        client_id = data.split(":", 1)[1]
        try:
            c = wg.get_client(client_id)
        except Exception:
            await query.edit_message_text(
                "Device not found.",
                reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
            )
            return
        owner = users_db.peer_owner(str(c.get("name", "")))
        try:
            if c.get("enabled"):
                wg.disable_client(client_id)
            else:
                wg.enable_client(client_id)
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
            return
        if owner is None:
            await query.edit_message_text(
                "✅ Device updated.",
                reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
            )
            return
        view = _peer_info_view(client_id)
        if view is None:
            await query.edit_message_text(
                "Device not found.",
                reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
            )
            return
        text, kb = view
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    if data.startswith("ureqs:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        target = users_db.get_user(username)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        tg = target.tg_id if target.tg_id not in (None, -1) else None
        reqs = users_db.list_requests(limit=10, tg_id=tg, username=username)
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Back", callback_data=f"user:{username}"),
            InlineKeyboardButton("❌ Close", callback_data="cancel"),
        ]])
        if not reqs:
            await query.edit_message_text(
                f"📜 Requests of <b>{html.escape(username)}</b>: none yet.",
                parse_mode="HTML",
                reply_markup=back_kb,
            )
            return
        lines = [f"📜 Requests of <b>{html.escape(username)}</b> (last {len(reqs)}):"]
        lines += [_format_request_line(r) for r in reqs]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb)
        return

    if data.startswith("upeers:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        view = _peers_list_view(username)
        if view is None:
            await query.edit_message_text("User not found.")
            return
        text, kb = view
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("usersview:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"all", "enabled", "disabled"}:
            return
        try:
            page = int(parts[2])
        except ValueError:
            return
        view = _users_list_view(page, parts[1])
        if view is None:
            await query.edit_message_text("No registered users.")
            return
        text, kb = view
        await _edit_callback_message_if_changed(query, text, reply_markup=kb)
        return

    if data.startswith("userspage:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        parts = data.split(":")
        try:
            if len(parts) == 2:
                status_filter, page = "all", int(parts[1])
            elif len(parts) == 3 and parts[1] in {"all", "enabled", "disabled"}:
                status_filter, page = parts[1], int(parts[2])
            else:
                return
        except ValueError:
            return
        view = _users_list_view(page, status_filter)
        if view is None:
            await query.edit_message_text("No registered users.")
            return
        text, kb = view
        await _edit_callback_message_if_changed(query, text, reply_markup=kb)
        return

    if data.startswith("user:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        parts = data.split(":")
        if len(parts) not in (2, 3, 4):
            return
        username = parts[1]
        back_target = "menu:users"
        if len(parts) == 3:
            try:
                back_target = f"userspage:{int(parts[2])}"
            except ValueError:
                return
        elif len(parts) == 4 and parts[2] in {"all", "enabled", "disabled"}:
            try:
                back_target = f"userspage:{parts[2]}:{int(parts[3])}"
            except ValueError:
                return
        view = _user_detail_view(username, back_target=back_target)
        if view is None:
            await query.edit_message_text("User not found.")
            return
        text, kb = view
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("udel:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        if users_db.get_user(username) is None:
            await query.edit_message_text("User not found.")
            return
        await query.edit_message_text(
            f"⚠️ Delete user <b>{html.escape(username)}</b> and all their devices?\nThis cannot be undone.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data=f"udelconfirm:{username}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
        )
        return

    if data.startswith("udelconfirm:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        msg = _delete_user_cascade(username)
        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
        )
        return

    if data.startswith("userdisable:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        target = users_db.get_user(username)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        if target.role == "admin":
            await query.edit_message_text("Cannot disable an admin.")
            return
        users_db.disable_user(username, disabled_by=user.username)
        count, err = _set_user_devices_enabled(username, False)
        if err:
            msg = f"User disabled, but device disable failed: {err}"
        else:
            msg = f"⏸ User <b>{html.escape(username)}</b> disabled; {count} device(s) disabled."
        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
        )
        return

    if data.startswith("userenable:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        target = users_db.get_user(username)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        if target.role == "admin":
            await query.edit_message_text("Cannot change an admin's enabled status.")
            return
        users_db.enable_user(username)
        count, err = _set_user_devices_enabled(username, True)
        if err:
            msg = f"User enabled, but device enable failed: {err}"
        else:
            msg = f"▶ User <b>{html.escape(username)}</b> enabled; {count} device(s) enabled."
        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
        )
        return

    # ── Inline menu ──
    if data.startswith("menu:"):
        action = data[5:]

        if action == "home":
            markup = _inline_menu_markup(user)
            await query.edit_message_text("WireGuard Manager", reply_markup=markup)
            return

        if action == "list":
            clients = visible_clients(user, users_db.list_peer_snapshots())
            markup, filtered, page, pages = _device_list_markup(clients, user)
            title = _device_list_title("all", filtered, page, pages, clients, user)
            await query.edit_message_text(title if filtered else f"{title}\nNo devices.", reply_markup=markup)

        elif action == "create":
            if users_db.is_admin(user):
                await query.edit_message_text("Create device:", reply_markup=CREATE_MENU)
            else:
                await _create_peer_for(update, user.username, None, message=query.message)
            return

        elif action == "users":
            if not users_db.is_admin(user):
                await query.edit_message_text("Admins only.")
                return
            view = _users_list_view()
            if view is None:
                await query.edit_message_text("No registered users.")
                return
            text, kb = view
            await query.edit_message_text(text, reply_markup=kb)
            return

        elif action == "adduser":
            if not users_db.is_admin(user):
                await query.edit_message_text("Admins only.")
                return
            await query.message.reply_text("Use /adduser to add a user.")
            return

        elif action == "requests":
            if not users_db.is_admin(user):
                await query.edit_message_text("Admins only.")
                return
            await _show_request_history(
                context.bot,
                query.message.chat_id,
                message_id=query.message.message_id,
            )
            return

        return

    if data.startswith("dev:"):
        try:
            _, filter_name, page_text = data.split(":")
            page = int(page_text)
            if filter_name not in {"all", "active", "enabled", "disabled"}:
                return
            clients = visible_clients(user, users_db.list_peer_snapshots())
            markup, filtered, page, pages = _device_list_markup(clients, user, filter_name, page)
            title = _device_list_title(filter_name, filtered, page, pages, clients, user)
            await query.edit_message_text(
                title if filtered else f"{title}\nNo {filter_name} devices.",
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            if not _is_message_not_modified_error(e):
                await query.edit_message_text(f"API error: {e}")
        return

    # ── Pagination ──
    if data.startswith("pg:"):
        try:
            _, act, page_str = data.split(":")
            page = int(page_str)
        except Exception:
            return
        try:
            clients = wg.list_clients()
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
            return
        clients = visible_clients(user, clients)
        q = context.user_data.get(f"{act}_filter", "")
        filtered = filter_clients(clients, q)
        if not filtered:
            await query.edit_message_text("No devices found.")
            return
        title = PEER_SELECT_TITLE[act]
        if q:
            title += f"\n🔍 <i>{html.escape(q)}</i>"
        await query.edit_message_text(
            title,
            reply_markup=peers_keyboard(filtered, act, page),
            parse_mode="HTML",
        )
        return

    # ── Config download ──
    if data.startswith("cfg:"):
        if not users_db.is_admin(user):
            try:
                selected = wg.get_client(data.split(":", 1)[1])
                if users_db.peer_owner(str(selected.get("name", ""))) != user.username:
                    await query.edit_message_text("You can access only your own configurations.")
                    return
            except Exception:
                await query.edit_message_text("Configuration not found.")
                return
        client_id = data[4:]
        try:
            client = wg.get_client(client_id)
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
            return
        if not owns_client(user, client):
            await query.edit_message_text("Access denied.")
            return
        try:
            config_data, filename = wg.get_client_config(client_id)
            await query.edit_message_text("Here is your config:")
            await query.message.reply_document(
                document=BytesIO(config_data),
                filename=filename,
                caption=AMNEZIA_LINKS,
                parse_mode="HTML",
            )
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
        return

    # ── Delete ──
    if data.startswith("del:"):
        parts = data.split(":", 2)
        client_id = parts[1]
        name = parts[2] if len(parts) > 2 else client_id
        try:
            client = wg.get_client(client_id)
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
            return
        if not owns_client(user, client):
            await query.edit_message_text("Access denied.")
            return
        await query.edit_message_text(
            f"Delete device <b>{html.escape(name)}</b>?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, delete", callback_data=f"delconfirm:{client_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ]]),
            parse_mode="HTML",
        )
        return

    if data.startswith("delconfirm:"):
        client_id = data[11:]
        try:
            client = wg.get_client(client_id)
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
            return
        if not owns_client(user, client):
            await query.edit_message_text("Access denied.")
            return
        owner = users_db.peer_owner(str(client.get("name", "")).lower())
        try:
            wg.delete_client(client_id)
            users_db.remove_peer(str(client.get("name", "")).lower())
            if owner and users_db.is_admin(user):
                await query.edit_message_text(
                    "✅ Device deleted.",
                    reply_markup=InlineKeyboardMarkup([_back_close_row(f"upeers:{owner}")]),
                )
            else:
                await query.edit_message_text("✅ Device deleted.")
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
        return


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = os.environ["BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    kb_buttons_admin = ["📋 Devices", "➕ Add a device", "👥 Users", "➕ Add User", "📜 Requests"]
    kb_buttons_user = ["📋 Devices", "➕ Add a device", "📈 Increase device limit"]
    kb_filter = filters.Text(kb_buttons_admin + kb_buttons_user)
    cancel_cb = CallbackQueryHandler(cancel_from_button, pattern="^cancel_conv$")

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text(["➕ Add a device"]), create_peer_cmd),
            CommandHandler("create", create_peer_cmd),
            CallbackQueryHandler(rename_start, pattern="^ren:"),
            CallbackQueryHandler(create_name_start, pattern="^create:name$"),
            MessageHandler(filters.Text(["➕ Add User"]), adduser_start),
            CommandHandler("adduser", adduser_start),
            CallbackQueryHandler(userlimit_start, pattern=r"^ulimit:"),
            MessageHandler(filters.Text(["📈 Increase device limit"]) & ~filters.COMMAND, limitreq_start),
            CallbackQueryHandler(limitreq_start, pattern=r"^menu:limitreq$"),
        ],
        states={
            WAITING_FOR_RENAME: [
                MessageHandler(kb_filter, cancel_conv),
                cancel_cb,
                MessageHandler(filters.TEXT & ~filters.COMMAND, rename_receive),
            ],
            WAITING_FOR_NEWNAME: [
                MessageHandler(kb_filter, cancel_conv),
                cancel_cb,
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_name_receive),
            ],
            WAITING_FOR_NEWUSER: [
                MessageHandler(kb_filter, cancel_conv),
                cancel_cb,
                MessageHandler(filters.ALL & ~filters.COMMAND, adduser_receive),
            ],
            WAITING_FOR_USERLIMIT: [
                MessageHandler(kb_filter, cancel_conv),
                cancel_cb,
                MessageHandler(filters.TEXT & ~filters.COMMAND, userlimit_receive),
            ],
            WAITING_FOR_LIMITREQ: [
                MessageHandler(kb_filter, cancel_conv),
                cancel_cb,
                MessageHandler(filters.TEXT & ~filters.COMMAND, limitreq_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            MessageHandler(kb_filter, cancel_conv),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.Text(["📋 Devices"]), list_peers))
    app.add_handler(MessageHandler(filters.Text(["👥 Users"]), users_list_cmd))
    app.add_handler(MessageHandler(filters.Text(["📜 Requests"]), requests_cmd))
    app.add_handler(CommandHandler("users", users_list_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("deluser", deluser_cmd))
    app.add_handler(CommandHandler("requests", requests_cmd))
    app.add_handler(CommandHandler("givemepowers", givemepowers_cmd))
    app.add_handler(CommandHandler("disable", disable_cmd))
    app.add_handler(CommandHandler("enable", enable_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))

    admin_env = sorted(list(_parse_env_set("ADMIN_USERNAMES")))
    logger.info("Bot started, admins: %s, users db: %s", admin_env, os.environ.get("USERS_DB_PATH", "users.db"))
    app.run_polling()


if __name__ == "__main__":
    main()
