# wgeasy-tg-bot

Telegram bot for managing [wg-easy](https://github.com/wg-easy/wg-easy) WireGuard peers. Create, delete, and download peer configs without opening the web UI.

## Features

- 📋 Browse devices in one paginated list with All, Active, Enabled and Disabled filters; Active means a handshake within the last 3 minutes
- Device rows show relative and absolute last-handshake times; admins also see the owner
- Device details provide configuration download and rename, plus admin management actions, with back navigation to the source list
- ➕ Create new peer — sends the `.conf` file immediately upon creation
- 🗑 Delete peer with confirmation step
- Paginated inline keyboards (8 devices per page)
- Works in both private chats (reply keyboard) and group threads (`/menu` inline keyboard)
- The main menu opens Devices; the separate Online devices and download entries are removed, and Search is no longer in the menu
- Role-based access: admins manage all peers and users; regular users see and manage only their own peers, limited by a per-user config limit
- Admins can create peers for themselves without a per-user limit, or create for another registered user; normal users create peers for themselves
- Peer names are short hashes derived from wg-easy client IDs; admins can see peer owners, and peers can be renamed in peer details (names must fit the 15-character AmneziaWG tunnel-name limit)
- Admins add users via `/adduser` (forward a message from the user or send their username, optionally with a limit), and manage them with `/users`, `/limit`, `/deluser`, `/ban`, and `/unban`
- Admins can promote users or demote admins from the user card; the last admin cannot be demoted
- Access requests: unregistered users can ask for access with `/givemepowers`; admins approve or decline right from the chat
- Access and limit requests notify all admins who have started the bot; deciding a request updates each admin's notification
- Limit change requests: users ask for a new peer limit with the "📈 Request Limit" button; admins approve or decline in chat
- Interactive user management: `/users` lists users as buttons (peers/limit); the user card leads to a dedicated peers card (one full-width row per peer with creation date and last handshake), and the peer details card holds all peer actions (config, rename, enable/disable, delete) alongside user actions (delete, limit, ban/unban, request history)
- Request history: admins review all past access/limit requests with `/requests` or the "📜 Requests" button, using paginated history; users see their own requests in `/givemepowers`
- Admin peer tables use Telegram's native rich-table display when supported, with a readable text fallback

## Setup

### 1. Create a Telegram bot

Talk to [@BotFather](https://t.me/BotFather), create a bot, and copy the token.

### 2. Configure environment

Create a `.env` file:

```env
WG_EASY_URL=https://your-wg-easy-instance.example.com
WG_EASY_LOGIN=your-login
WG_EASY_PASSWORD=your-password
BOT_TOKEN=123456:ABC-your-bot-token
ADMIN_USERNAMES=admin1,admin2
USERNAMES=alice,bob
DEFAULT_CONFIG_LIMIT=3
USERS_DB_PATH=users.db
```

| Variable | Description |
|---|---|
| `WG_EASY_URL` | Base URL of your wg-easy instance |
| `WG_EASY_LOGIN` | wg-easy username |
| `WG_EASY_PASSWORD` | wg-easy password |
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `ADMIN_USERNAMES` | Comma-separated Telegram usernames of admins (without `@`) |
| `USERNAMES` | Comma-separated Telegram usernames of regular users bootstrap (without `@`) |
| `DEFAULT_CONFIG_LIMIT` | Default per-user peer limit for newly added users (default `3`) |
| `USERS_DB_PATH` | Path to the SQLite user database (default `users.db`) |

### 3. Run with Docker Compose

The pre-built image is published to GitHub Container Registry on every push to `main`:


```yaml
services:
  bot:
    image: ghcr.io/illmouse/wgeasy-tg-bot:main
    # build: .   # uncomment to build locally instead of pulling the image
    env_file: .env
    restart: unless-stopped
    volumes:
      - ./users.db:/app/users.db
```

```bash
docker compose up -d
```

The bot stores users in a SQLite database. Mount a volume to persist `users.db` across container restarts (as shown above).

## Usage

| Context | How to use |
|---|---|
| Private chat | `/start` — shows a persistent reply keyboard |
| Group / thread | `/menu` — sends an inline keyboard that works in threads |

### Commands

| Command | Description |
|---|---|
| `/start` | Show reply keyboard (private chat) |
| `/menu` | Show inline keyboard (works everywhere) |
| `/create` | Create a new peer: regular users create for themselves; admins choose themselves or another registered user before selecting a generated or custom name. Admins can also use `/create <username>` (max 15 chars, AmneziaWG limit) |
| `/cancel` | Cancel current operation |
| `/adduser` | Add a user — forward a message from them or send their username (admin only) |
| `/users` | Interactive user list: tap a user to open their card (delete/limit/ban/role/requests actions), then `Peers` opens the peers card — one full-width row per peer — and a peer's name opens its details card with config/rename/toggle/delete actions (admin only; last admin cannot be demoted) |
| `/limit <username> <N>` | Set a user's peer limit; `-1` means unlimited (admin only) |
| `/deluser <username>` | Remove a user together with all their peers (admin only) |
| `/ban <username>` | Temporarily block a user and disable their peers (admin only) |
| `/unban <username>` | Unblock a user and re-enable their peers (admin only) |
| `/requests` | Show the last 20 access/limit requests with statuses (admin only) |
| `/givemepowers` | For anyone: show your peers, limit and request history if registered, otherwise send an access request to the admins |

### User management

Admins add users with `/adduser` by either forwarding a message from the user or sending their username as text (optionally followed by a limit, e.g. `alice 5`). Users are stored in SQLite together with their Telegram ID and username. A user added by username only becomes bound to their Telegram ID on their first message to the bot.

Users without access can request it with `/givemepowers`: the request is stored in the database and sent to all admins who have started the bot, with Approve/Decline buttons. When decided, every admin's notification is updated and the requester is notified of the outcome. Banning a user with `/ban` makes the bot respond with a temporary-blocked notice only and disables all their peers until `/unban`. Registered users can request a limit change via the "📈 Request Limit" button — such requests use the same all-admin notification and decision flow. Admins can promote users or demote admins from the user card, but cannot demote the last admin. Deleting a user with `/deluser` (or the delete action in `/users`) also deletes all their peers from wg-easy and the database. All access and limit requests are kept in the database as history: admins can review the paginated history with `/requests` or the "📜 Requests" menu button, per-user history is available from the user card, and registered users see their own requests in `/givemepowers`.

### Config file

When a peer is created or its config is downloaded, the bot sends a `.conf` file that works with:

**AmneziaWG** (obfuscation support) — [iOS & macOS](https://apps.apple.com/us/app/amneziawg/id6478942365) · [Android](https://play.google.com/store/apps/details?id=org.amnezia.awg) · [Windows](https://github.com/amnezia-vpn/amneziawg-windows-client/releases) · [Linux](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module)

**WG Tunnel** (standard WireGuard) — [Android](https://play.google.com/store/apps/details?id=com.zaneschepke.wireguardautotunnel) · [Windows](https://wgtunnel.com/download?platform=windows) · [Linux](https://wgtunnel.com/download?platform=linux)

## Requirements

- Docker & Docker Compose
- A running [wg-easy](https://github.com/wg-easy/wg-easy) instance (tested with the current API version using session-based auth)
