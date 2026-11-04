import html
import logging
import os
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

PAGE_SIZE = 8

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📋 Peers", "🟢 Active"],
        ["➕ Create Peer", "📥 Get Config"],
        ["🔍 Search", "🗑 Delete Peer"],
        ["👥 Users", "➕ Add User"],
    ],
    resize_keyboard=True,
)

USER_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📋 Peers", "🟢 Active"],
        ["➕ Create Peer", "📥 Get Config"],
        ["🔍 Search", "🗑 Delete Peer"],
    ],
    resize_keyboard=True,
)

CANCEL_CONV_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("❌ Cancel", callback_data="cancel_conv"),
]])

PEER_SELECT_TITLE = {
    "cfg": "Select peer to download config:",
    "del": "Select peer to delete:",
}

INLINE_MENU_ADMIN = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📋 Peers", callback_data="menu:list"),
        InlineKeyboardButton("🟢 Active", callback_data="menu:active"),
    ],
    [
        InlineKeyboardButton("➕ Create Peer", callback_data="menu:create"),
        InlineKeyboardButton("📥 Get Config", callback_data="menu:config"),
    ],
    [
        InlineKeyboardButton("🔍 Search", callback_data="menu:search"),
        InlineKeyboardButton("🗑 Delete Peer", callback_data="menu:delete"),
    ],
    [
        InlineKeyboardButton("👥 Users", callback_data="menu:users"),
        InlineKeyboardButton("➕ Add User", callback_data="menu:adduser"),
    ],
])

INLINE_MENU_USER = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📋 Peers", callback_data="menu:list"),
        InlineKeyboardButton("🟢 Active", callback_data="menu:active"),
    ],
    [
        InlineKeyboardButton("➕ Create Peer", callback_data="menu:create"),
        InlineKeyboardButton("📥 Get Config", callback_data="menu:config"),
    ],
    [
        InlineKeyboardButton("🔍 Search", callback_data="menu:search"),
        InlineKeyboardButton("🗑 Delete Peer", callback_data="menu:delete"),
    ],
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
    prefix = user.username + '_'
    return name.startswith(prefix)


def visible_clients(user: User, clients: list) -> list:
    if users_db.is_admin(user):
        return list(clients)
    out = []
    for c in clients:
        if owns_client(user, c):
            out.append(c)
    return out


def generate_peer_name(prefix: str, existing_names: set[str]) -> str:
    p = str(prefix).strip().lower()
    for _ in range(5):
        h = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        name = f"{p}_{h}"
        if name not in existing_names:
            return name
    h = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"{p}_{h}"


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
        if action == "cfg":
            rows.append([InlineKeyboardButton(f"📄 {name}", callback_data=f"cfg:{c['id']}")])
        else:
            rows.append([InlineKeyboardButton(
                f"🗑 {name}",
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
        addr = c.get("ipv4Address") or "N/A"
        lines.append(f"{status} <b>{name}</b>  —  {addr}")
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
        return
    target = user.username
    if users_db.is_admin(user) and context.args:
        candidate = str(context.args[0]).strip().lstrip("@").lower()
        if not users_db.user_exists(candidate):
            await update.effective_message.reply_text("User not found.")
            return
        target = candidate
    try:
        clients = wg.list_clients()
    except Exception as e:
        await update.effective_message.reply_text(f"API error: {e}")
        return
    prefix = f"{target}_"
    count = sum(1 for c in clients if str(c.get("name", "")).lower().startswith(prefix))
    target_limit = user.config_limit
    for u in users_db.list_users():
        if u.username == target:
            target_limit = u.config_limit
            break
    if target_limit != -1 and count >= target_limit:
        await update.effective_message.reply_text(
            f"Limit reached: {count}/{target_limit}. Delete a peer to free a slot."
        )
        return
    existing_names = {str(c.get("name", "")).lower() for c in clients}
    peer_name = generate_peer_name(target, existing_names)
    try:
        result = wg.create_client(peer_name)
        client_id = result.get("clientId")
        if client_id:
            try:
                config_data, filename = wg.get_client_config(client_id)
                await update.effective_message.reply_document(
                    document=BytesIO(config_data),
                    filename=filename,
                    caption=f"✅ Peer <b>{html.escape(peer_name)}</b> created!" + AMNEZIA_LINKS,
                    parse_mode="HTML",
                )
                return
            except Exception:
                pass
        await update.effective_message.reply_text(
            f"✅ Peer <b>{html.escape(peer_name)}</b> created!" + AMNEZIA_LINKS,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await update.effective_message.reply_text(f"API error: {e}")
        return


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
    if users_db.is_admin(user):
        await update.message.reply_text("Cancelled.", reply_markup=ADMIN_KEYBOARD)
    else:
        await update.message.reply_text("Cancelled.", reply_markup=USER_KEYBOARD)
    return ConversationHandler.END


async def cancel_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if get_user(update) is None:
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
    if not users_db.is_admin(user):
        await query.edit_message_text("Admins only.")
        return ConversationHandler.END

    parts = query.data.split(":", 2)
    context.user_data["rename_id"] = parts[1]
    context.user_data["rename_old"] = parts[2] if len(parts) > 2 else parts[1]

    await query.message.reply_text(
        f"Enter new name for <b>{html.escape(context.user_data['rename_old'])}</b>:",
        parse_mode="HTML",
        reply_markup=CANCEL_CONV_KB,
    )
    return WAITING_FOR_RENAME


@require_admin
async def rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    if not new_name:
        await update.message.reply_text("Name cannot be empty. Enter new name:")
        return WAITING_FOR_RENAME

    client_id = context.user_data.get("rename_id")
    old_name = context.user_data.get("rename_old", client_id)
    try:
        wg.rename_client(client_id, new_name)
        await update.message.reply_text(
            f"✅ <b>{html.escape(old_name)}</b> → <b>{html.escape(new_name)}</b>",
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
    users = users_db.list_users()
    if not users:
        await update.message.reply_text("No users.")
        return
    lines = []
    for u in users:
        lim = "∞" if u.config_limit == -1 else str(u.config_limit)
        tg = str(u.tg_id) if u.tg_id is not None else "pending"
        lines.append(f"<b>{html.escape(u.username)}</b> [{u.role}] limit {lim} tg:{tg}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
    ok = users_db.remove_user(uname)
    if ok:
        await update.message.reply_text("✅ User removed.")
    else:
        await update.message.reply_text("User not found.")


# ── Callback handler ──────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update)
    if user is None:
        _log_unauthorized(update)
        return
    context.user_data['_user'] = user

    data = query.data

    if data == "noop":
        return

    if data == "cancel":
        await query.edit_message_text("Cancelled.")
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
            await query.message.reply_text("Use /create or the button to create a peer.")
            return

        elif action == "search":
            await query.message.reply_text("Use /search <term> to find peers.")
            return

        elif action == "users":
            if not users_db.is_admin(user):
                await query.edit_message_text("Admins only.")
                return
            users = users_db.list_users()
            if not users:
                await query.message.reply_text("No users.")
                return
            lines = []
            for u in users:
                lim = "∞" if u.config_limit == -1 else str(u.config_limit)
                tg = str(u.tg_id) if u.tg_id is not None else "pending"
                lines.append(f"<b>{html.escape(u.username)}</b> [{u.role}] limit {lim} tg:{tg}")
            await query.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        elif action == "adduser":
            if not users_db.is_admin(user):
                await query.edit_message_text("Admins only.")
                return
            await query.message.reply_text("Use /adduser to add a user.")
            return

        elif action in ("config", "delete"):
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
        try:
            wg.delete_client(client_id)
            await query.edit_message_text("✅ Peer deleted.")
        except Exception as e:
            await query.edit_message_text(f"API error: {e}")
        return


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()

    kb_buttons_admin = ["📋 Peers", "🟢 Active", "➕ Create Peer", "📥 Get Config", "🔍 Search", "🗑 Delete Peer", "👥 Users", "➕ Add User"]
    kb_buttons_user = ["📋 Peers", "🟢 Active", "➕ Create Peer", "📥 Get Config", "🔍 Search", "🗑 Delete Peer"]
    kb_filter = filters.Text(kb_buttons_admin + kb_buttons_user)
    cancel_cb = CallbackQueryHandler(cancel_from_button, pattern="^cancel_conv$")

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text(["➕ Create Peer"]), create_peer_cmd),
            CommandHandler("create", create_peer_cmd),
            MessageHandler(filters.Text(["🔍 Search"]), search_start),
            CommandHandler("search", search_start),
            CallbackQueryHandler(rename_start, pattern="^ren:"),
            MessageHandler(filters.Text(["➕ Add User"]), adduser_start),
            CommandHandler("adduser", adduser_start),
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
            WAITING_FOR_NEWUSER: [
                MessageHandler(kb_filter, cancel_conv),
                cancel_cb,
                MessageHandler(filters.ALL & ~filters.COMMAND, adduser_receive),
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
    app.add_handler(MessageHandler(filters.Text(["📋 Peers"]), list_peers))
    app.add_handler(MessageHandler(filters.Text(["🟢 Active"]), active_peers))
    app.add_handler(MessageHandler(filters.Text(["📥 Get Config"]), config_menu))
    app.add_handler(MessageHandler(filters.Text(["🗑 Delete Peer"]), delete_menu))
    app.add_handler(MessageHandler(filters.Text(["👥 Users"]), users_list_cmd))
    app.add_handler(CommandHandler("users", users_list_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("deluser", deluser_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))

    admin_env = sorted(list(_parse_env_set("ADMIN_USERNAMES")))
    logger.info("Bot started, admins: %s, users db: %s", admin_env, os.environ.get("USERS_DB_PATH", "users.db"))
    app.run_polling()


if __name__ == "__main__":
    main()
