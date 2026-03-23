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
- Access restricted to a configurable list of Telegram usernames

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
ALLOWED_USERNAMES=alice,bob,charlie
```

| Variable | Description |
|---|---|
| `WG_EASY_URL` | Base URL of your wg-easy instance |
| `WG_EASY_LOGIN` | wg-easy username |
| `WG_EASY_PASSWORD` | wg-easy password |
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `ALLOWED_USERNAMES` | Comma-separated Telegram usernames allowed to use the bot (without `@`) |

### 3. Run with Docker Compose

The pre-built image is published to GitHub Container Registry on every push to `main`:


```yaml
services:
  bot:
    image: ghcr.io/illmouse/wgeasy-tg-bot:main
    # build: .   # uncomment to build locally instead of pulling the image
    env_file: .env
    restart: unless-stopped
```

```bash
docker compose up -d
```

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
| `/create <name>` | Create a new peer |
| `/search <name>` | Search peers by name |
| `/cancel` | Cancel current operation |

### Config file

When a peer is created or its config is downloaded, the bot sends a `.conf` file that works with:

**AmneziaWG** (obfuscation support) — [iOS & macOS](https://apps.apple.com/us/app/amneziawg/id6478942365) · [Android](https://play.google.com/store/apps/details?id=org.amnezia.awg) · [Windows](https://github.com/amnezia-vpn/amneziawg-windows-client/releases) · [Linux](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module)

**WG Tunnel** (standard WireGuard) — [Android](https://play.google.com/store/apps/details?id=com.zaneschepke.wireguardautotunnel) · [Windows](https://wgtunnel.com/download?platform=windows) · [Linux](https://wgtunnel.com/download?platform=linux)

## Requirements

- Docker & Docker Compose
- A running [wg-easy](https://github.com/wg-easy/wg-easy) instance (tested with the current API version using session-based auth)
