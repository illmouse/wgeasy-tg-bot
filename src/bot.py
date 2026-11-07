import html
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
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

WAITING_FOR_SEARCH = 2
WAITING_FOR_RENAME = 3
WAITING_FOR_NEWUSER = 4
WAITING_FOR_NEWNAME = 5
WAITING_FOR_USERLIMIT = 6
WAITING_FOR_LIMITREQ = 7

PAGE_SIZE = 8
MAX_PEER_NAME_LEN = 15
PEER_NAME_LEN = 8
PEER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,15}$")

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📋 Configurations", "🟢 Online devices"],
        ["➕ Add a device", "📥 Download configuration"],
        ["🔍 Search devices", "🗑 Remove a device"],
        ["👥 Users", "➕ Add User"],
    ],
    resize_keyboard=True,
)

USER_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📋 Configurations", "🟢 Online devices"],
        ["➕ Add a device", "📥 Download configuration"],
        ["🔍 Search devices", "🗑 Remove a device"],
        ["📈 Request Limit"],
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

INLINE_MENU_ADMIN = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📋 Configurations", callback_data="menu:list"),
        InlineKeyboardButton("🟢 Online devices", callback_data="menu:active"),
    ],
    [
        InlineKeyboardButton("➕ Add a device", callback_data="menu:create"),
        InlineKeyboardButton("📥 Download configuration", callback_data="menu:config"),
    ],
    [
        InlineKeyboardButton("🔍 Search devices", callback_data="menu:search"),
        InlineKeyboardButton("🗑 Remove a device", callback_data="menu:delete"),
    ],
    [
        InlineKeyboardButton("👥 Users", callback_data="menu:users"),
        InlineKeyboardButton("➕ Add User", callback_data="menu:adduser"),
    ],
    [InlineKeyboardButton("📜 Requests", callback_data="menu:requests")],
])

INLINE_MENU_USER = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📋 Configurations", callback_data="menu:list"),
        InlineKeyboardButton("🟢 Online devices", callback_data="menu:active"),
    ],
    [
        InlineKeyboardButton("➕ Add a device", callback_data="menu:create"),
        InlineKeyboardButton("📥 Download configuration", callback_data="menu:config"),
    ],
    [
        InlineKeyboardButton("🔍 Search devices", callback_data="menu:search"),
        InlineKeyboardButton("🗑 Remove a device", callback_data="menu:delete"),
    ],
    [
        InlineKeyboardButton("📈 Request Limit", callback_data="menu:limitreq"),
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
        if users_db.is_banned(user):
            if update.effective_message:
                await update.effective_message.reply_text("You are temporarily blocked. Contact an administrator.")
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
        if users_db.is_banned(user):
            if update.effective_message:
                await update.effective_message.reply_text("You are temporarily blocked. Contact an administrator.")
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


def format_active_peers_text(clients: list) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=3)
    lines = []
    for c in clients:
        hs = c.get("latestHandshakeAt")
        if not hs:
            continue
        try:
            hs_dt = datetime.fromisoformat(str(hs).replace("Z", "+00:00"))
        except Exception:
            continue
        if hs_dt < cutoff:
            continue
        ago = int((datetime.now(timezone.utc) - hs_dt).total_seconds())
        name = html.escape(c.get("name", "?"))
        addr = c.get("ipv4Address") or "N/A"
        lines.append(f"🟢 <b>{name}</b>  —  {addr}  <i>({ago}s ago)</i>")
    return "\n".join(lines) if lines else ""


def _dt_short(value, with_time=False):
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
    return dt.strftime("%d.%m %H:%M" if with_time else "%d.%m")


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
        line += f" → {r['requested_limit']}"
    line += f" · {_dt_short(r.get('requested_at'), with_time=True)}"
    if r.get("status") != "pending" and r.get("admin_username"):
        line += f" · by @{r['admin_username']}"
    return line


def _request_history_view(page=0):
    page_size = 10
    total = users_db.request_count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    reqs = users_db.list_requests(page_size, offset=page * page_size)
    rows = [[InlineKeyboardButton(f"{r['id']} · {r['status']} · {r.get('username') or r['tg_id']}", callback_data="noop")] for r in reqs]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("◀", callback_data=f"reqpage:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("▶", callback_data=f"reqpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="cancel")])
    text = "📜 Request history" + (f" ({page * page_size + 1}–{min((page + 1) * page_size, total)} of {total})" if total else " (empty)")
    text += "\n" + "\n".join(_format_request_line(r) for r in reqs)
    return text, InlineKeyboardMarkup(rows)


def _request_table_rows(page=0):
    reqs = users_db.list_requests(10, offset=max(0, page) * 10)
    return [[f"#{r['id']}", r.get("username") or str(r["tg_id"]), r.get("type", "access"), r["status"], _dt_short(r.get("requested_at"), True)] for r in reqs]


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
        await message.reply_text(f"Limit reached: {len(active)}/{target_limit}. Delete a peer to free a slot.")
        return
    if custom_name is not None:
        if not PEER_NAME_RE.match(custom_name):
            await message.reply_text("Invalid name: use 1-15 chars, letters/digits/_/- only.")
            return
        if custom_name.lower() in (live_names | db_names):
            await message.reply_text(
                f"A peer named <b>{html.escape(custom_name)}</b> already exists. Choose another name.",
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
            hash_name = "wg-" + hashlib.sha256(str(client_id).encode()).hexdigest()[:8]
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
            caption=f"✅ Peer <b>{html.escape(peer_name)}</b> created for <b>{html.escape(target)}</b>!\n\n{AMNEZIA_LINKS}",
            parse_mode="HTML",
        )
    except Exception:
        await message.reply_text(
            f"✅ Peer <b>{html.escape(peer_name)}</b> created for <b>{html.escape(target)}</b>!",
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
            await update.effective_message.reply_text("No peers found.")
        return
    context.user_data[f"{action}_filter"] = ""
    if update.effective_message:
        await update.effective_message.reply_text(
            PEER_SELECT_TITLE[action],
            reply_markup=peers_keyboard(clients, action),
        )


def _users_list_view():
    users = users_db.list_users()
    if not users:
        return None
    try:
        live_names = {str(c.get("name", "")).lower() for c in wg.list_clients()}
    except Exception:
        live_names = None
    rows = []
    for u in users:
        peers = users_db.peers_of(u.username)
        if live_names is None:
            peer_count = len(peers)
        else:
            peer_count = sum(1 for name in peers if name in live_names)
        limit = u.config_limit if u.config_limit >= 0 else "∞"
        label = f"{u.username} {peer_count}/{limit}"
        if u.status == "banned":
            label += " 🔒"
        rows.append([InlineKeyboardButton(label, callback_data=f"user:{u.username}")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="cancel")])
    return "👥 Registered users — tap to manage:", InlineKeyboardMarkup(rows)


def _user_detail_view(username):
    u = users_db.get_user(username)
    if u is None:
        return None
    try:
        clients = wg.list_clients()
        by_name = {str(c.get("name", "")).lower(): c for c in clients}
        api_error = False
    except Exception:
        by_name = {}
        api_error = True
    records = users_db.peer_records(u.username)
    active = [rec for rec in records if not api_error and rec["name"] in by_name]
    limit = "∞" if u.config_limit == -1 else str(u.config_limit)
    head = f"👤 <b>{html.escape(username)}</b> [{u.role}]"
    if u.status == "banned":
        head += " 🔒 BANNED"
    lines = [head, f"Limit: {len(active)}/{limit}"]
    lines.append(f"TG: {u.tg_id}" if u.tg_id not in (None, -1) else "TG: pending")
    if api_error:
        lines.append("")
        lines.append("⚠️ Peer list unavailable (API error).")
    if u.status == "banned":
        ban_btn = InlineKeyboardButton("🔓 Unban", callback_data=f"unban:{username}")
    else:
        ban_btn = InlineKeyboardButton("🔒 Ban", callback_data=f"uban:{username}")
    peers_label = "👥 Peers" if api_error else f"👥 Peers ({len(active)})"
    role_label = "⬇ Demote to user" if u.role == "admin" else "⬆ Promote to admin"
    rows = [
        [InlineKeyboardButton("🗑 Delete", callback_data=f"udel:{username}"), ban_btn],
        [InlineKeyboardButton(role_label, callback_data=f"role:{username}")],
        [InlineKeyboardButton("📝 Limit", callback_data=f"ulimit:{username}"), InlineKeyboardButton("📜 Requests", callback_data=f"ureqs:{username}")],
        [InlineKeyboardButton(peers_label, callback_data=f"upeers:{username}")],
        _back_close_row("menu:users"),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _back_close_row(target):
    return [InlineKeyboardButton("◀ Back", callback_data=target), InlineKeyboardButton("❌ Close", callback_data="cancel")]


def _peers_list_view(username):
    u = users_db.get_user(username)
    if u is None:
        return None
    try:
        clients = wg.list_clients()
        by_name = {str(c.get("name", "")).lower(): c for c in clients}
        api_error = False
    except Exception:
        by_name = {}
        api_error = True
    records = users_db.peer_records(u.username)
    active = [rec for rec in records if not api_error and rec["name"] in by_name]
    limit = "∞" if u.config_limit == -1 else str(u.config_limit)
    lines = [f"👥 Peers of <b>{html.escape(username)}</b>"]
    if api_error:
        lines.append("⚠️ Peer list unavailable (API error).")
    elif active:
        lines[0] += f" ({len(active)}/{limit})"
        lines.append("Tap a name for details:")
    else:
        lines.append("Peers: none")
    rows = []
    for rec in active:
        c = by_name[rec["name"]]
        label = f"{rec['name']} · @{username} · {_dt_short(rec['created_at'])} · {_handshake_ago(c)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pinfo:{c['id']}")])
    rows.append(_back_close_row(f"user:{username}"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _peer_info_view(client_id, admin=True):
    try:
        c = wg.get_client(client_id)
    except Exception:
        return None
    name = str(c.get("name", ""))
    rec = users_db.get_peer(name)
    owner = rec["owner"] if rec else None
    status = "🟢 enabled" if c.get("enabled") else "🔴 disabled"
    ip = c.get("ipv4Address") or "N/A"
    created = _dt_short(rec["created_at"], with_time=True) if rec else "—"
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
        lines.append(f"Owner: @{owner}")
    lines.append(f"Status: {status}")
    lines.append(f"IP: {ip}")
    lines.append(f"Created: {created}")
    lines.append(f"Last handshake: {hs_line}")
    toggle = "⏸ Disable" if c.get("enabled") else "▶ Enable"
    rows = [[InlineKeyboardButton("📥 Download configuration", callback_data=f"cfg:{client_id}")]]
    if owner:
        rows.append([InlineKeyboardButton("✏️ Rename", callback_data=f"ren:{client_id}:{name[:40]}")])
    if admin:
        rows.append([InlineKeyboardButton(toggle, callback_data=f"ptog:{client_id}"), InlineKeyboardButton("🗑 Delete", callback_data=f"del:{client_id}:{name[:40]}")])
    if owner:
        rows.append(_back_close_row(f"upeers:{owner}"))
    else:
        rows.append([InlineKeyboardButton("👥 Users", callback_data="menu:users"), InlineKeyboardButton("❌ Close", callback_data="cancel")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _ban_unban_peers(username, enable):
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
        return f"User deleted, but peer deletion failed: {err}"
    return f"🗑 User <b>{html.escape(username)}</b> deleted with {deleted} peer(s)."


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
    if user and users_db.is_admin(user):
        await update.message.reply_text("WireGuard Manager", reply_markup=INLINE_MENU_ADMIN)
    else:
        await update.message.reply_text("WireGuard Manager", reply_markup=INLINE_MENU_USER)


async def givemepowers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u is None:
        return
    user = get_user(update)
    if user is not None:
        if users_db.is_banned(user):
            await update.effective_message.reply_text("You are temporarily blocked. Contact an administrator.")
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
    try:
        clients = wg.list_clients()
    except Exception as e:
        await update.message.reply_text(f"API error: {e}")
        return
    if user is None:
        return
    clients = visible_clients(user, clients)
    if not clients:
        await update.message.reply_text("No peers found.")
        return
    await update.message.reply_text(format_peers_text(clients), parse_mode="HTML")


@require_access
async def active_peers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    try:
        clients = wg.list_clients()
    except Exception as e:
        await update.message.reply_text(f"API error: {e}")
        return
    if user is None:
        return
    clients = visible_clients(user, clients)
    text = format_active_peers_text(clients)
    await update.message.reply_text(text or "No peers active in the last 3 minutes.", parse_mode="HTML")


@require_access
async def config_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_peer_selection(update, context, "cfg")


@require_access
async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_peer_selection(update, context, "del")


# ── Create peer ────────────────────────────────────────────────────────────────

@require_access
async def create_peer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_banned(user):
        if update.effective_message:
            await update.effective_message.reply_text("You are temporarily blocked. Contact an administrator.")
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
    await update.effective_message.reply_text("Create peer:", reply_markup=CREATE_MENU)
    return ConversationHandler.END


async def create_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_banned(user):
        try:
            await query.edit_message_text("You are temporarily blocked. Contact an administrator.")
        except Exception:
            pass
        return ConversationHandler.END
    if not users_db.is_admin(user):
        await query.edit_message_text("Admins only.")
        return ConversationHandler.END
    await query.edit_message_text("Enter peer name:", reply_markup=CANCEL_CONV_KB)
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
    if users_db.is_banned(user):
        try:
            await query.edit_message_text("You are temporarily blocked. Contact an administrator.")
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
    if users_db.is_banned(user):
        msg = "You are temporarily blocked. Contact an administrator."
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
    if users_db.is_banned(user):
        await update.message.reply_text("You are temporarily blocked. Contact an administrator.")
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


# ── Search ─────────────────────────────────────────────────────────────────────

@require_access
async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await _do_search(update, context, " ".join(context.args))
        return ConversationHandler.END
    await update.message.reply_text("Enter search term:")
    return WAITING_FOR_SEARCH


@require_access
async def search_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_search(update, context, update.message.text.strip())
    return ConversationHandler.END


async def _do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, term: str):
    user = get_user(update)
    if user is None:
        return
    if not term:
        msg = update.effective_message
        if msg:
            await msg.reply_text("Search term cannot be empty.")
        return
    try:
        clients = wg.list_clients()
    except Exception as e:
        msg = update.effective_message
        if msg:
            await msg.reply_text(f"API error: {e}")
        return

    clients = visible_clients(user, clients)
    matching = filter_clients(clients, term)
    msg = update.effective_message
    if not matching:
        if msg:
            await msg.reply_text(f"No peers matching <b>{html.escape(term)}</b>.", parse_mode="HTML")
        return

    keyboard = []
    for c in matching:
        name = c.get("name", str(c["id"]))
        row = [
            InlineKeyboardButton(f"📄 {name}", callback_data=f"cfg:{c['id']}"),
        ]
        if users_db.is_admin(user):
            row.append(InlineKeyboardButton("✏️", callback_data=f"ren:{c['id']}:{name[:40]}"))
        row.append(InlineKeyboardButton("🗑", callback_data=f"del:{c['id']}:{name[:40]}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Close", callback_data="cancel")])

    if msg:
        await msg.reply_text(
            f"Found <b>{len(matching)}</b> peer(s) matching <b>{html.escape(term)}</b>:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update)
    if user is None:
        return ConversationHandler.END
    if users_db.is_banned(user):
        if update.effective_message:
            await update.effective_message.reply_text("You are temporarily blocked. Contact an administrator.")
        return ConversationHandler.END
    if users_db.is_admin(user):
        await update.message.reply_text("Cancelled.", reply_markup=ADMIN_KEYBOARD)
    else:
        await update.message.reply_text("Cancelled.", reply_markup=USER_KEYBOARD)
    return ConversationHandler.END


async def cancel_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        return ConversationHandler.END
    if users_db.is_banned(user):
        try:
            await query.edit_message_text("You are temporarily blocked. Contact an administrator.")
        except Exception:
            pass
        return ConversationHandler.END
    await query.edit_message_text("Cancelled.")
    return ConversationHandler.END


# ── Rename peer conversation ──────────────────────────────────────────────────

async def rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return ConversationHandler.END
    if users_db.is_banned(user):
        try:
            await query.edit_message_text("You are temporarily blocked. Contact an administrator.")
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
        f"Enter new name for <b>{html.escape(context.user_data['rename_old'])}</b>:",
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
        await update.message.reply_text("Invalid name: use 1-15 chars, letters/digits/_/- only.")
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
    reqs = users_db.list_requests(20)
    if not reqs:
        await update.effective_message.reply_text("No requests yet.")
        return
    lines = [f"📜 Request history (last {len(reqs)}):"]
    lines += [_format_request_line(r) for r in reqs]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


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
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /ban <username>")
        return
    target = users_db.get_user(args[0])
    if target is None:
        await update.message.reply_text("User not found.")
        return
    if target.role == "admin":
        await update.message.reply_text("Cannot ban an admin.")
        return
    admin = get_user(update)
    users_db.ban_user(target.username, banned_by=admin.username if admin else None)
    disabled, err = _ban_unban_peers(target.username, False)
    if err:
        logger.warning("Failed to disable peers for %s: %s", target.username, err)
        await update.message.reply_text(f"User banned, but peer disable failed: {err}")
    else:
        await update.message.reply_text(f"🔒 User {target.username} banned, {disabled} peer(s) disabled.")


@require_admin
async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /unban <username>")
        return
    target = users_db.get_user(args[0])
    if target is None:
        await update.message.reply_text("User not found.")
        return
    if target.role == "admin":
        await update.message.reply_text("Cannot unban an admin.")
        return
    users_db.unban_user(target.username)
    enabled, err = _ban_unban_peers(target.username, True)
    if err:
        logger.warning("Failed to enable peers for %s: %s", target.username, err)
        await update.message.reply_text(f"User unbanned, but peer enable failed: {err}")
    else:
        await update.message.reply_text(f"🔓 User {target.username} unbanned, {enabled} peer(s) enabled.")


# ── Callback handler ──────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return
    if users_db.is_banned(user):
        try:
            await query.edit_message_text("You are temporarily blocked. Contact an administrator.")
        except Exception:
            pass
        return
    context.user_data['_user'] = user

    data = query.data

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

    if data.startswith("cfguser:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        target_name = data.split(":", 1)[1]
        owned = set(users_db.peers_of(target_name))
        try:
            clients = [c for c in wg.list_clients() if str(c.get("name", "")).lower() in owned]
        except Exception as exc:
            await query.edit_message_text(f"API error: {exc}")
            return
        if not clients:
            await query.edit_message_text(f"No configurations for @{target_name}.")
            return
        context.user_data["cfg_filter"] = ""
        await query.edit_message_text(f"Select @{target_name}'s configuration:", reply_markup=peers_keyboard(clients, "cfg"))
        return

    if data.startswith("reqpage:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            return
        text, markup = _request_history_view(page)
        if users_db.request_count():
            await _send_table_or_text(context.bot, query.message.chat_id, "Request history", ["ID", "User", "Type", "Status", "Requested"], _request_table_rows(page), "\n".join(_format_request_line(r) for r in users_db.list_requests(10, offset=page * 10)))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data == "cancel":
        await query.edit_message_text("Cancelled.")
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
        await query.edit_message_text("Create peer for:", reply_markup=InlineKeyboardMarkup(rows))
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
        await query.edit_message_text(f"Creating a peer for {target}...")
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
                await query.edit_message_text(
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
                await query.edit_message_text(f"❌ Request #{req_id} declined.")
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
                )
            except Exception:
                logger.warning("Failed to notify user %s about approval", req["tg_id"])
            await query.edit_message_text(f"✅ Request #{req_id} approved: {created.username} registered.")
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
            await query.edit_message_text(f"❌ Request #{req_id} declined.")
            return
        return

    if data.startswith("pinfo:"):
        client_id = data.split(":", 1)[1]
        try:
            current_peer = wg.get_client(client_id)
            owner = users_db.peer_owner(str(current_peer.get("name", "")))
        except Exception:
            owner = None
        if not users_db.is_admin(user) and owner != user.username:
            await query.edit_message_text("You can view only your own configurations.")
            return
        view = _peer_info_view(client_id, admin=users_db.is_admin(user))
        if view is None:
            await query.edit_message_text(
                "Peer not found.",
                reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
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
                "Peer not found.",
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
                "✅ Peer updated.",
                reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
            )
            return
        view = _peer_info_view(client_id)
        if view is None:
            await query.edit_message_text(
                "Peer not found.",
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

    if data.startswith("user:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        view = _user_detail_view(username)
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
            f"⚠️ Delete user <b>{html.escape(username)}</b> and all their peers?\nThis cannot be undone.",
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

    if data.startswith("uban:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        target = users_db.get_user(username)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        if target.role == "admin":
            await query.edit_message_text("Cannot ban an admin.")
            return
        users_db.ban_user(username, banned_by=user.username)
        count, err = _ban_unban_peers(username, False)
        if err:
            msg = f"User banned, but peer disable failed: {err}"
        else:
            msg = f"🔒 User <b>{html.escape(username)}</b> banned, {count} peer(s) disabled."
        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
        )
        return

    if data.startswith("unban:"):
        if not users_db.is_admin(user):
            await query.edit_message_text("Admins only.")
            return
        username = data.split(":", 1)[1]
        target = users_db.get_user(username)
        if target is None:
            await query.edit_message_text("User not found.")
            return
        users_db.unban_user(username)
        count, err = _ban_unban_peers(username, True)
        if err:
            msg = f"User unbanned, but peer enable failed: {err}"
        else:
            msg = f"🔓 User <b>{html.escape(username)}</b> unbanned, {count} peer(s) enabled."
        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_back_close_row("menu:users")]),
        )
        return

    # ── Inline menu ──
    if data.startswith("menu:"):
        action = data[5:]

        if action == "list":
            try:
                clients = wg.list_clients()
            except Exception as e:
                await query.message.reply_text(f"API error: {e}")
                return
            clients = visible_clients(user, clients)
            if not users_db.is_admin(user):
                rows = [[InlineKeyboardButton(f"📄 {c.get('name', '?')}", callback_data=f"pinfo:{c['id']}")] for c in clients]
                rows.append([InlineKeyboardButton("❌ Close", callback_data="cancel")])
                await query.edit_message_text("Your configurations:", reply_markup=InlineKeyboardMarkup(rows))
                return
            text = format_peers_text(clients) if clients else "No peers found."
            await query.message.reply_text(text, parse_mode="HTML")

        elif action == "active":
            try:
                clients = wg.list_clients()
            except Exception as e:
                await query.message.reply_text(f"API error: {e}")
                return
            clients = visible_clients(user, clients)
            text = format_active_peers_text(clients)
            await query.message.reply_text(text or "No peers active in the last 3 minutes.", parse_mode="HTML")

        elif action == "create":
            if users_db.is_admin(user):
                await query.edit_message_text("Create peer:", reply_markup=CREATE_MENU)
            else:
                await _create_peer_for(update, user.username, None, message=query.message)
            return

        elif action == "search":
            await query.message.reply_text("Use /search <term> to find peers.")
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
            text, markup = _request_history_view()
            if users_db.request_count():
                await _send_table_or_text(context.bot, query.message.chat_id, "Request history", ["ID", "User", "Type", "Status", "Requested"], _request_table_rows(), "\n".join(_format_request_line(r) for r in users_db.list_requests(10)))
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return

        elif action in ("config", "delete"):
            if action == "config" and users_db.is_admin(user):
                rows = [[InlineKeyboardButton(f"@{u.username}", callback_data=f"cfguser:{u.username}")] for u in users_db.list_users()]
                rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
                await query.edit_message_text("Select a user:", reply_markup=InlineKeyboardMarkup(rows))
                return
            try:
                clients = wg.list_clients()
            except Exception as e:
                await query.message.reply_text(f"API error: {e}")
                return
            clients = visible_clients(user, clients)
            if not clients:
                await query.message.reply_text("No peers found.")
                return
            act = "cfg" if action == "config" else "del"
            context.user_data[f"{act}_filter"] = ""
            await query.edit_message_text(PEER_SELECT_TITLE[act], reply_markup=peers_keyboard(clients, act))
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
            await query.edit_message_text("No peers found.")
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
            f"Delete peer <b>{html.escape(name)}</b>?",
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
                    "✅ Peer deleted.",
                    reply_markup=InlineKeyboardMarkup([_back_close_row(f"upeers:{owner}")]),
                )
            else:
                await query.edit_message_text("✅ Peer deleted.")
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
        return


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()

    kb_buttons_admin = ["📋 Configurations", "🟢 Online devices", "➕ Add a device", "📥 Download configuration", "🔍 Search devices", "🗑 Remove a device", "👥 Users", "➕ Add User"]
    kb_buttons_user = ["📋 Configurations", "🟢 Online devices", "➕ Add a device", "📥 Download configuration", "🔍 Search devices", "🗑 Remove a device", "📈 Request Limit"]
    kb_filter = filters.Text(kb_buttons_admin + kb_buttons_user)
    cancel_cb = CallbackQueryHandler(cancel_from_button, pattern="^cancel_conv$")

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text(["➕ Add a device"]), create_peer_cmd),
            CommandHandler("create", create_peer_cmd),
            MessageHandler(filters.Text(["🔍 Search devices"]), search_start),
            CommandHandler("search", search_start),
            CallbackQueryHandler(rename_start, pattern="^ren:"),
            CallbackQueryHandler(create_name_start, pattern="^create:name$"),
            MessageHandler(filters.Text(["➕ Add User"]), adduser_start),
            CommandHandler("adduser", adduser_start),
            CallbackQueryHandler(userlimit_start, pattern=r"^ulimit:"),
            MessageHandler(filters.Text(["📈 Request Limit"]) & ~filters.COMMAND, limitreq_start),
            CallbackQueryHandler(limitreq_start, pattern=r"^menu:limitreq$"),
        ],
        states={
            WAITING_FOR_SEARCH: [
                MessageHandler(kb_filter, cancel_conv),
                cancel_cb,
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_receive),
            ],
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
    app.add_handler(MessageHandler(filters.Text(["📋 Configurations"]), list_peers))
    app.add_handler(MessageHandler(filters.Text(["🟢 Online devices"]), active_peers))
    app.add_handler(MessageHandler(filters.Text(["📥 Download configuration"]), config_menu))
    app.add_handler(MessageHandler(filters.Text(["🗑 Remove a device"]), delete_menu))
    app.add_handler(MessageHandler(filters.Text(["👥 Users"]), users_list_cmd))
    app.add_handler(CommandHandler("users", users_list_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("deluser", deluser_cmd))
    app.add_handler(CommandHandler("requests", requests_cmd))
    app.add_handler(CommandHandler("givemepowers", givemepowers_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))

    admin_env = sorted(list(_parse_env_set("ADMIN_USERNAMES")))
    logger.info("Bot started, admins: %s, users db: %s", admin_env, os.environ.get("USERS_DB_PATH", "users.db"))
    app.run_polling()


if __name__ == "__main__":
    main()
