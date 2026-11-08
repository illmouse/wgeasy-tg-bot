import asyncio
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import string
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
from device_operations import (
    DeviceOperationError,
    PartialDeletionError,
    create_owned_device,
    delete_user_devices,
    set_user_devices_enabled,
)
from bot_views import (
    PAGE_SIZE,
    back_close_row as _back_close_row,
    compact_bytes as _compact_bytes,
    dt_short as _dt_short,
    filtered_request_history_view,
    format_request_line as _format_request_line,
    handshake_ago as _handshake_ago,
    peer_info_view,
    peers_list_view,
    request_history_rich_message,
    request_table_rows,
    user_detail_view,
    users_list_view,
)
from ui_helpers import (
    apply_peer_sample,
    delete_callback_message,
    delete_message_silently,
    fetch_peer_sample,
    filter_peer_snapshots,
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
users_db.set_bootstrap_sets(os.environ.get("ADMIN_USERNAMES", "").split(","), _parse_env_set("USERNAMES"))

WAITING_FOR_RENAME = 3
WAITING_FOR_NEWUSER = 4
WAITING_FOR_NEWNAME = 5
WAITING_FOR_USERLIMIT = 6
WAITING_FOR_LIMITREQ = 7

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


def _is_message_not_modified_error(error):
    return "message is not modified" in str(error).lower()


async def _edit_callback_message_if_changed(query, *args, **kwargs):
    try:
        await query.edit_message_text(*args, **kwargs)
    except Exception as exc:
        if not _is_message_not_modified_error(exc):
            raise


def _request_history_view(page=0):
    page_size = 10
    return _filtered_request_history_view(page, "all", page_size)


def _filtered_request_history_view(page=0, status_filter="all", page_size=10):
    return filtered_request_history_view(users_db, page, status_filter, page_size)


def _request_table_rows(page=0, status_filter="all"):
    return request_table_rows(users_db, page, status_filter)


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
    return request_history_rich_message(users_db, page, status_filter, page_size)


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
        clients = await asyncio.to_thread(wg.list_clients)
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
        peer_name, client_id = await create_owned_device(
            wg, users_db, peer_name, target, auto_name=custom_name is None,
            reserved_names=live_names | db_names,
        )
    except DeviceOperationError as exc:
        await message.reply_text(str(exc))
        return
    except Exception as exc:
        await message.reply_text(f"API error: {exc}")
        return
    try:
        config_data, filename = await asyncio.to_thread(wg.get_client_config, client_id)
        await _send_private_config(
            update, message, BytesIO(config_data), filename,
            f"✅ Device <b>{html.escape(peer_name)}</b> created for <b>{html.escape(target)}</b>!\n\n{AMNEZIA_LINKS}",
        )
    except Exception:
        logger.exception("Could not deliver config for device %s", client_id)
        await message.reply_text(
            f"Device <b>{html.escape(peer_name)}</b> was created, but its config could not be delivered privately. Open a private chat with the bot and download it there.",
            parse_mode="HTML",
        )


async def _send_private_config(update: Update, message, document: BytesIO, filename: str, caption: str) -> None:
    """Send a private-key-bearing configuration only to the requesting user."""
    chat = update.effective_chat
    if chat is not None and chat.type == "private":
        await message.reply_document(document=document, filename=filename, caption=caption, parse_mode="HTML")
        return
    user = update.effective_user
    if user is None:
        raise ValueError("No requesting user for private configuration delivery")
    await message.get_bot().send_document(
        chat_id=user.id, document=document, filename=filename, caption=caption, parse_mode="HTML",
    )
    await message.reply_text("Configuration sent to your private chat with the bot.")


async def _show_peer_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user = get_user(update)
    if user is None:
        return
    try:
        clients = await asyncio.to_thread(wg.list_clients)
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
    return users_list_view(users_db, page, status_filter)


def _user_detail_view(username, back_target="menu:users"):
    return user_detail_view(users_db, username, back_target)


def _peers_list_view(username):
    return peers_list_view(users_db, username)


def _peer_info_view(client_id, admin=True, back_target=None, client=None):
    try:
        c = client if client is not None else wg.get_client(client_id)
    except Exception:
        return None
    return peer_info_view(users_db, client_id, c, admin, back_target)


async def _set_user_devices_enabled(username, enable):
    return await set_user_devices_enabled(wg, users_db, username, enable)


async def _delete_user_cascade(username):
    u = users_db.get_user(username)
    if u is None:
        return "User not found."
    try:
        deleted = await delete_user_devices(wg, users_db, username)
    except PartialDeletionError as exc:
        logger.exception("Failed to delete devices for %s", username)
        return f"Deletion stopped after {exc.deleted} device(s); user and remaining records retained: {html.escape(str(exc))}"
    users_db.remove_user(username)
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
            clients = visible_clients(user, await asyncio.to_thread(wg.list_clients))
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
    username = context.user_data.get("_ulimit_target")
    if username is None:
        await update.message.reply_text("Nothing to change.")
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a valid integer (-1 = unlimited).")
        return WAITING_FOR_USERLIMIT
    if n < -1:
        await update.message.reply_text("Enter a valid integer (-1 = unlimited).")
        return WAITING_FOR_USERLIMIT
    try:
        users_db.set_limit(username, n)
    except KeyError:
        await update.message.reply_text("User not found.")
        return ConversationHandler.END
    context.user_data.pop("_ulimit_target", None)
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
        peer = await asyncio.to_thread(wg.get_client, parts[1])
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
        current = await asyncio.to_thread(wg.get_client, client_id)
        old_name = str(current.get("name", "")).lower()
        owner = users_db.peer_owner(old_name)
        if not users_db.is_admin(user) and owner != user.username:
            await update.message.reply_text("You can rename only your own configurations.")
            return ConversationHandler.END
        await asyncio.to_thread(wg.rename_client, client_id, new_name)
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
        except ValueError:
            await msg.reply_text("Enter a valid integer limit (-1 = unlimited).")
            return WAITING_FOR_NEWUSER
        if limit_cand < -1:
            await msg.reply_text("Enter a valid integer limit (-1 = unlimited).")
            return WAITING_FOR_NEWUSER
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
    except ValueError:
        await update.message.reply_text("Usage: /limit <username> <N>")
        return
    if N < -1:
        await update.message.reply_text("Enter a valid integer (-1 = unlimited).")
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
    await update.message.reply_text(await _delete_user_cascade(uname), parse_mode="HTML")


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
    disabled, err = await _set_user_devices_enabled(target.username, False)
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
    enabled, err = await _set_user_devices_enabled(target.username, True)
    if err:
        logger.warning("Failed to enable peers for %s: %s", target.username, err)
        await update.message.reply_text(f"User enabled, but device enable failed: {err}")
    else:
        await update.message.reply_text(f"▶ User {target.username} enabled; {enabled} device(s) enabled.")


# ── Callback handler ──────────────────────────────────────────────────────────

async def _handle_request_decision(query, context, admin: User, action: str, req_id: int, history_page: int | None):
    """Apply a decision, update admin messages, and notify the requester."""
    if not users_db.is_admin(admin):
        await query.edit_message_text("Admins only.")
        return
    if action not in {"approve", "decline"}:
        return
    req = users_db.get_request(req_id)
    if req is None or req["status"] != "pending":
        await query.edit_message_text("Request already processed.")
        return

    created = None
    if action == "approve":
        try:
            created = users_db.approve_request(req_id, admin.tg_id, admin.username)
        except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
            await query.edit_message_text(f"Cannot approve request: {html.escape(str(exc))}")
            return
        if created is None:
            await query.edit_message_text("Request already processed.")
            return
    elif not users_db.decide_request(req_id, "declined", admin.tg_id, admin.username):
        await query.edit_message_text("Request already processed.")
        return

    if req.get("type", "access") == "limit":
        if action == "approve":
            admin_text = f"✅ Request #{req_id} approved by @{admin.username}."
            user_text = f"✅ Your config limit is now {req['requested_limit']}."
            result = f"✅ Request #{req_id} approved: @{req['username']} limit → {req['requested_limit']}"
        else:
            admin_text = f"❌ Request #{req_id} declined by @{admin.username}."
            user_text = f"❌ Your limit request was declined by @{admin.username}."
            result = f"❌ Request #{req_id} declined."
    elif action == "approve":
        admin_text = f"✅ Request #{req_id} approved by @{admin.username}: {created.username} registered."
        user_text = f"✅ You are registered! Your config limit: {created.config_limit}.\nUse /start to begin."
        result = f"✅ Request #{req_id} approved: {created.username} registered."
    else:
        admin_text = f"❌ Request #{req_id} declined by @{admin.username}."
        user_text = f"❌ Your access request was declined by @{admin.username}."
        result = f"❌ Request #{req_id} declined."

    try:
        await _edit_request_copies(context, req, admin_text)
    except Exception:
        logger.exception("Failed to update admin copies for request %s", req_id)
    try:
        reply_markup = None
        if action == "approve" and req.get("type", "access") != "limit":
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Open menu", callback_data="menu:home"),
            ]])
        await context.bot.send_message(req["tg_id"], user_text, reply_markup=reply_markup)
    except Exception:
        logger.warning("Failed to notify user %s about request %s", req["tg_id"], req_id)
    if history_page is not None:
        await _show_request_history(
            context.bot, query.message.chat_id, history_page, "pending",
            message_id=query.message.message_id,
        )
    else:
        await query.edit_message_text(result)

async def _authorized_callback_client(query, user, client_id, *, missing, denied="Access denied.", missing_markup=None):
    """Fetch a device once and enforce ownership before any further API action."""
    try:
        client = await asyncio.to_thread(wg.get_client, client_id)
    except Exception as exc:
        if missing_markup is None:
            await query.edit_message_text(missing(exc))
        else:
            await query.edit_message_text(missing(exc), reply_markup=missing_markup)
        return None
    if not owns_client(user, client):
        await query.edit_message_text(denied)
        return None
    return client


async def _handle_device_callback(update, context, user, data):
    query = update.callback_query
    if data in {"create:pickuser", "create:self"} or data.startswith("createfor:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        if data == "create:pickuser":
            users = users_db.list_users()
            if not users:
                await query.edit_message_text("No registered users.")
                return
            rows = []
            for u in users:
                lim = u.config_limit if u.config_limit != -1 else "∞"
                rows.append([InlineKeyboardButton(
                    f"{u.username} ({u.role}, limit {lim})", callback_data=f"createfor:{u.username}",
                )])
            rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
            await query.edit_message_text("Create device for:", reply_markup=InlineKeyboardMarkup(rows))
            return
        target = user.username if data == "create:self" else data.split(":", 1)[1]
        if data.startswith("createfor:") and not users_db.user_exists(target):
            await query.edit_message_text("User not found.")
            return
        await query.edit_message_text(
            "Creating a configuration for you…" if data == "create:self" else f"Creating a device for {target}..."
        )
        await _create_peer_for(update, target, None, message=query.message)
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
        missing_markup = InlineKeyboardMarkup([_back_close_row(
            back_target or ("menu:users" if users_db.is_admin(user) else "menu:list")
        )])
        client = await _authorized_callback_client(
            query, user, client_id, missing=lambda exc: "Device not found.",
            denied="You can view only your own configurations.", missing_markup=missing_markup,
        )
        if client is None:
            return
        view = _peer_info_view(client_id, admin=users_db.is_admin(user), back_target=back_target, client=client)
        if view is None:
            await query.edit_message_text("Device not found.", reply_markup=missing_markup)
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
            c = await asyncio.to_thread(wg.get_client, client_id)
        except Exception:
            await query.edit_message_text(
                "Device not found.", reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
            )
            return
        owner = users_db.peer_owner(str(c.get("name", "")))
        try:
            if c.get("enabled"):
                await asyncio.to_thread(wg.disable_client, client_id)
            else:
                await asyncio.to_thread(wg.enable_client, client_id)
        except Exception as exc:
            await query.edit_message_text(f"API error: {exc}")
            return
        if owner is None:
            await query.edit_message_text(
                "✅ Device updated.", reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
            )
            return
        try:
            refreshed = await asyncio.to_thread(wg.get_client, client_id)
        except Exception:
            refreshed = None
        view = _peer_info_view(client_id, client=refreshed) if refreshed is not None else None
        if view is None:
            await query.edit_message_text(
                "Device not found.", reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
            )
            return
        text, kb = view
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
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
                parse_mode="HTML", reply_markup=markup,
            )
        except Exception as exc:
            if not _is_message_not_modified_error(exc):
                await query.edit_message_text(f"API error: {exc}")
        return

    if data.startswith("pg:"):
        try:
            _, act, page_str = data.split(":")
            page = int(page_str)
        except Exception:
            return
        try:
            clients = await asyncio.to_thread(wg.list_clients)
        except Exception as exc:
            await query.edit_message_text(f"API error: {exc}")
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
            title, reply_markup=peers_keyboard(filtered, act, page), parse_mode="HTML",
        )
        return

    if data.startswith("cfg:"):
        client_id = data[4:]
        client = await _authorized_callback_client(
            query, user, client_id,
            missing=(lambda exc: f"API error: {exc}") if users_db.is_admin(user) else (lambda exc: "Configuration not found."),
            denied="You can access only your own configurations.",
        )
        if client is None:
            return
        try:
            config_data, filename = await asyncio.to_thread(wg.get_client_config, client_id)
            await _send_private_config(update, query.message, BytesIO(config_data), filename, AMNEZIA_LINKS)
            await query.edit_message_text("Configuration delivered privately.")
        except Exception as exc:
            logger.warning("Configuration delivery failed for %s: %s", client_id, exc)
            await query.edit_message_text("Could not deliver the config. Start a private chat with the bot and retry.")
        return

    if data.startswith("del:"):
        parts = data.split(":", 2)
        client_id = parts[1]
        name = parts[2] if len(parts) > 2 else client_id
        client = await _authorized_callback_client(
            query, user, client_id, missing=lambda exc: f"API error: {exc}",
        )
        if client is None:
            return
        await query.edit_message_text(
            f"Delete device <b>{html.escape(name)}</b>?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, delete", callback_data=f"delconfirm:{client_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ]]), parse_mode="HTML",
        )
        return

    if data.startswith("delconfirm:"):
        client_id = data[11:]
        client = await _authorized_callback_client(
            query, user, client_id, missing=lambda exc: f"API error: {exc}",
        )
        if client is None:
            return
        name = str(client.get("name", "")).lower()
        owner = users_db.peer_owner(name)
        try:
            await asyncio.to_thread(wg.delete_client, client_id)
            users_db.remove_peer(name)
            if owner and users_db.is_admin(user):
                await query.edit_message_text(
                    "✅ Device deleted.",
                    reply_markup=InlineKeyboardMarkup([_back_close_row(f"upeers:{owner}")]),
                )
            else:
                await query.edit_message_text("✅ Device deleted.")
        except Exception as exc:
            await query.edit_message_text(f"API error: {exc}")


async def _handle_request_callback(query, context, user, data):
    if not users_db.is_admin(user):
        await query.edit_message_text("Admins only.")
        return
    if data.startswith(("reqpage:", "reqview:")):
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
            context.bot, query.message.chat_id, page, status_filter,
            message_id=query.message.message_id,
        )
        return
    if data == "reqclose":
        await delete_callback_message(query)
        return
    history_page = None
    if data.startswith("reqhist:"):
        parts = data.split(":")
        if len(parts) != 4 or parts[1] not in {"approve", "decline"}:
            return
        try:
            history_page = int(parts[3])
        except ValueError:
            return
        data = f"req:{parts[1]}:{parts[2]}"
    parts = data.split(":")
    if len(parts) != 3:
        return
    try:
        req_id = int(parts[2])
    except ValueError:
        return
    await _handle_request_decision(query, context, user, parts[1], req_id, history_page)


async def _show_users_callback_page(query, page, status_filter):
    view = _users_list_view(page, status_filter)
    if view is None:
        await query.edit_message_text("No registered users.")
        return
    text, kb = view
    await _edit_callback_message_if_changed(query, text, reply_markup=kb)


async def _handle_user_callback(query, user, data):
    if not users_db.is_admin(user):
        await query.edit_message_text("Admins only.")
        return
    if data.startswith("role:"):
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

    if data.startswith("ureqs:"):
        username = data.split(":", 1)[1]
        target = users_db.get_user(username)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        tg = target.tg_id if target.tg_id not in (None, -1) else None
        reqs = users_db.list_requests(limit=10, tg_id=tg, username=username)
        back_kb = InlineKeyboardMarkup([_back_close_row(f"user:{username}")])
        if not reqs:
            await query.edit_message_text(
                f"📜 Requests of <b>{html.escape(username)}</b>: none yet.",
                parse_mode="HTML", reply_markup=back_kb,
            )
            return
        lines = [f"📜 Requests of <b>{html.escape(username)}</b> (last {len(reqs)}):"]
        lines += [_format_request_line(r) for r in reqs]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb)
        return

    if data.startswith("upeers:"):
        username = data.split(":", 1)[1]
        view = _peers_list_view(username)
        if view is None:
            await query.edit_message_text("User not found.")
            return
        text, kb = view
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("usersview:"):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"all", "enabled", "disabled"}:
            return
        try:
            page = int(parts[2])
        except ValueError:
            return
        await _show_users_callback_page(query, page, parts[1])
        return

    if data.startswith("userspage:"):
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
        await _show_users_callback_page(query, page, status_filter)
        return

    if data.startswith("user:"):
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
        username = data.split(":", 1)[1]
        msg = await _delete_user_cascade(username)
        await query.edit_message_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
        )
        return

    if data.startswith(("userdisable:", "userenable:")):
        enabling = data.startswith("userenable:")
        username = data.split(":", 1)[1]
        target = users_db.get_user(username)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        if target.role == "admin":
            await query.edit_message_text(
                "Cannot change an admin's enabled status." if enabling else "Cannot disable an admin."
            )
            return
        if enabling:
            users_db.enable_user(username)
        else:
            users_db.disable_user(username, disabled_by=user.username)
        count, err = await _set_user_devices_enabled(username, enabling)
        if err:
            msg = f"User {'enabled' if enabling else 'disabled'}, but device {'enable' if enabling else 'disable'} failed: {err}"
        else:
            msg = f"{'▶' if enabling else '⏸'} User <b>{html.escape(username)}</b> {'enabled' if enabling else 'disabled'}; {count} device(s) {'enabled' if enabling else 'disabled'}."
        await query.edit_message_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
        )


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

    if data == "noop":
        return

    if data.startswith(("reqhist:", "req:", "reqpage:", "reqview:")) or data == "reqclose":
        await _handle_request_callback(query, context, user, data)
        return

    if data.startswith(("role:", "ureqs:", "upeers:", "usersview:", "userspage:",
                        "user:", "udel:", "udelconfirm:", "userdisable:", "userenable:")):
        await _handle_user_callback(query, user, data)
        return

    if data in {"create:pickuser", "create:self"} or data.startswith(
        ("createfor:", "pinfo:", "ptog:", "dev:", "pg:", "cfg:", "del:", "delconfirm:")
    ):
        await _handle_device_callback(update, context, user, data)
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

    admins = [user.username for user in users_db.list_users() if users_db.is_admin(user)]
    logger.info("Bot started, admins: %s, users db: %s", admins, os.environ.get("USERS_DB_PATH", "users.db"))
    app.run_polling()


if __name__ == "__main__":
    main()
