# wgeasy-tg-bot

Telegram bot for managing [wg-easy](https://github.com/wg-easy/wg-easy) WireGuard peers. Create, delete, and download peer configs without opening the web UI.

## Features

- 📋 List all peers with status and IP
- 🟢 Show only active peers (handshake within last 3 minutes)
- ➕ Create new peer — sends the `.conf` file immediately upon creation
- 📥 Download config for any existing peer
- 🗑 Delete peer with confirmation step
- 🔍 Search peers by name
- Paginated inline keyboards (8 peers per page)
- Works in both private chats (reply keyboard) and group threads (`/menu` inline keyboard)
- Role-based access: admins manage all peers and users; regular users see and manage only their own peers, limited by a per-user config limit
- Peer names follow the `<username>_<random hash>` pattern
- Admins add users via `/adduser` (forward a message from the user or send their username, optionally with a limit), and manage them with `/users`, `/limit`, and `/deluser`

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
| `/create` | Create a new peer; the name is auto-generated as `<username>_<hash>` (admin may pass `/create <username>` to create for another user) |
| `/search <name>` | Search peers by name |
| `/cancel` | Cancel current operation |
| `/adduser` | Add a user — forward a message from them or send their username (admin only) |
| `/users` | List users with role, limit, and Telegram ID (admin only) |
| `/limit <username> <N>` | Set a user's peer limit; `-1` means unlimited (admin only) |
| `/deluser <username>` | Remove a user (admin only) |

### User management

Admins add users with `/adduser` by either forwarding a message from the user or sending their username as text (optionally followed by a limit, e.g. `alice 5`). Users are stored in SQLite together with their Telegram ID and username. A user added by username only becomes bound to their Telegram ID on their first message to the bot.

### Config file

When a peer is created or its config is downloaded, the bot sends a `.conf` file that works with:

**AmneziaWG** (obfuscation support) — [iOS & macOS](https://apps.apple.com/us/app/amneziawg/id6478942365) · [Android](https://play.google.com/store/apps/details?id=org.amnezia.awg) · [Windows](https://github.com/amnezia-vpn/amneziawg-windows-client/releases) · [Linux](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module)

**WG Tunnel** (standard WireGuard) — [Android](https://play.google.com/store/apps/details?id=com.zaneschepke.wireguardautotunnel) · [Windows](https://wgtunnel.com/download?platform=windows) · [Linux](https://wgtunnel.com/download?platform=linux)

## Requirements

- Docker & Docker Compose
- A running [wg-easy](https://github.com/wg-easy/wg-easy) instance (tested with the current API version using session-based auth)
