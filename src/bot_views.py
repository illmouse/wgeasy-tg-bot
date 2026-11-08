"""Read-only user and device screens for the Telegram bot."""

import html
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ui_helpers import format_bytes
from users import UserDB


PAGE_SIZE = 8


def dt_short(value, with_time=False, with_year=False):
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


def compact_bytes(value):
    formatted = format_bytes(value)
    amount, _, unit = formatted.partition(" ")
    if amount.endswith(".00"):
        amount = amount[:-3]
    elif amount.endswith("0") and "." in amount:
        amount = amount[:-1]
    return f"{amount}{unit}"


def handshake_ago(client):
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


def back_close_row(target):
    return [InlineKeyboardButton("◀ Back", callback_data=target), InlineKeyboardButton("❌ Close", callback_data="cancel")]


def users_list_view(db: UserDB, page=0, status_filter="all"):
    if status_filter not in {"all", "enabled", "disabled"}:
        return None
    if not db.list_users():
        return None
    users = db.list_users(None if status_filter == "all" else status_filter)
    users.sort(key=lambda user: (user.role != "admin", user.username))
    page_size = PAGE_SIZE
    total_pages = max(1, (len(users) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    snapshot_names = {str(c.get("name", "")).lower() for c in db.list_peer_snapshots()}
    rows = []
    filters_row = [
        InlineKeyboardButton(label, callback_data=f"usersview:{key}:0")
        for key, label in (("all", "All"), ("enabled", "Enabled"), ("disabled", "Disabled"))
    ]
    rows.append(filters_row)
    start = page * page_size
    for u in users[start:start + page_size]:
        peers = db.peers_of(u.username)
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


def user_detail_view(db: UserDB, username, back_target="menu:users"):
    u = db.get_user(username)
    if u is None:
        return None
    by_name = {
        str(c.get("name", "")).lower(): c
        for c in db.list_peer_snapshots()
    }
    records = db.peer_records(u.username)
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
        back_close_row(back_target),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def peers_list_view(db: UserDB, username):
    u = db.get_user(username)
    if u is None:
        return None
    by_name = {
        str(c.get("name", "")).lower(): c
        for c in db.list_peer_snapshots()
    }
    records = db.peer_records(u.username)
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
            f"{dt_short(rec['created_at'], with_year=True)} · ↑{compact_bytes(c.get('transferRx'))} "
            f"↓{compact_bytes(c.get('transferTx'))} · {handshake_ago(c)} "
            f"({dt_short(hs, with_time=True) if hs else 'never'})"
        )
        name_budget = max(1, 64 - len(details) - 3)
        name = rec["name"]
        short_name = name if len(name) <= name_budget else f"{name[:name_budget - 1]}…"
        label = f"{short_name} · {details}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pinfo:{c['id']}")])
    rows.append(back_close_row(f"user:{username}"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def peer_info_view(db: UserDB, client_id, client, admin=True, back_target=None):
    c = client
    snapshot = db.get_peer_snapshot(client_id)
    if snapshot is not None:
        c = {
            **c,
            "latestHandshakeAt": snapshot["latestHandshakeAt"],
            "transferRx": snapshot["transferRx"],
            "transferTx": snapshot["transferTx"],
        }
    name = str(c.get("name", ""))
    rec = db.get_peer(name)
    owner = rec["owner"] if rec else None
    status = "🟢 Enabled" if c.get("enabled") else "🔴 Disabled"
    ip = c.get("ipv4Address") or "N/A"
    created = dt_short(rec["created_at"], with_time=True, with_year=True) if rec else "—"
    hs = c.get("latestHandshakeAt")
    parsed_hs = None
    if hs:
        try:
            parsed_hs = datetime.fromisoformat(str(hs).replace("Z", "+00:00"))
        except ValueError:
            parsed_hs = None
    hs_line = f"{dt_short(hs, with_time=True)} ({handshake_ago(c)} ago)" if parsed_hs is not None else "never"
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
    rows.append(back_close_row(back_target))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def display_limit(limit):
    if limit is None:
        return "—"
    return "∞" if limit == -1 else str(limit)


def format_request_line(request):
    r = request
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
        line += f" · → {display_limit(r.get('requested_limit'))}"
    line += f" · {dt_short(r.get('requested_at'), with_time=True)}"
    if r.get("status") != "pending" and r.get("admin_username"):
        line += f" · by @{r['admin_username']}"
    return line


def request_action_label(r):
    if r.get("username"):
        who = "@" + r["username"]
    elif r.get("first_name") or r.get("last_name"):
        who = f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip()
    else:
        who = str(r["tg_id"])
    label = f"#{r['id']} {'📈' if r.get('type') == 'limit' else '🔑'} {who}"
    if r.get("type") == "limit" and r.get("requested_limit") is not None:
        label += f" → {display_limit(r.get('requested_limit'))}"
    return label[:60]


def filtered_request_history_view(db: UserDB, page=0, status_filter="all", page_size=10):
    status = None if status_filter == "all" else status_filter
    total = db.request_count(status)
    pending_total = db.request_count("pending")
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    reqs = db.list_requests(page_size, offset=page * page_size, status=status)
    rows = []
    if status_filter == "pending":
        for r in reqs:
            rows.append([
                InlineKeyboardButton(request_action_label(r), callback_data="noop"),
                InlineKeyboardButton("✅ Approve", callback_data=f"reqhist:approve:{r['id']}:{page}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"reqhist:decline:{r['id']}:{page}"),
            ])
    filter_buttons = []
    if status_filter == "pending":
        filter_buttons.append(InlineKeyboardButton(f"All ({db.request_count()})", callback_data="reqview:all:0"))
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


def request_table_rows(db: UserDB, page=0, status_filter="all"):
    status = None if status_filter == "all" else status_filter
    reqs = db.list_requests(10, offset=max(0, page) * 10, status=status)
    return [
        [
            f"#{r['id']}",
            r.get("username") or str(r["tg_id"]),
            "Limit change" if r.get("type") == "limit" else "Access",
            f"→ {display_limit(r.get('requested_limit'))}" if r.get("type") == "limit" and r.get("requested_limit") is not None else "—",
            r["status"],
            f"@{r['admin_username']}" if r.get("admin_username") else "—",
            dt_short(r.get("requested_at"), True),
        ]
        for r in reqs
    ]


def request_history_rich_message(db: UserDB, page=0, status_filter="all", page_size=10):
    status = None if status_filter == "all" else status_filter
    total = db.request_count(status)
    pending_total = db.request_count("pending")
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    reqs = db.list_requests(page_size, offset=page * page_size, status=status)
    blocks = [{
        "type": "paragraph",
        "text": f"📜 Request history · Pending ({pending_total})" if status_filter == "all" else f"📜 Pending ({pending_total})",
    }]

    if status_filter == "all" and reqs:
        rows = request_table_rows(db, page)
        cells = [[{"text": header, "is_header": True} for header in ("ID", "User", "Request", "Limit change", "Status", "Fulfilled by", "Requested")]]
        cells.extend([[{"text": str(value)} for value in row] for row in rows])
        blocks.append({"type": "table", "is_bordered": True, "is_striped": True, "cells": cells})
    elif status_filter == "pending" and reqs:
        for req in reqs:
            blocks.append({
                "type": "buttons",
                "buttons": [
                    {"text": request_action_label(req), "callback_data": "noop"},
                    {"text": "✅ Approve", "style": "success", "callback_data": f"reqhist:approve:{req['id']}:{page}"},
                    {"text": "❌ Decline", "style": "danger", "callback_data": f"reqhist:decline:{req['id']}:{page}"},
                ],
            })
    else:
        blocks.append({"type": "paragraph", "text": "No requests yet." if status_filter == "all" else "No pending requests."})

    filter_buttons = []
    if status_filter == "pending":
        filter_buttons.append({"text": f"All ({db.request_count()})", "callback_data": "reqview:all:0"})
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
