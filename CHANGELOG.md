# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0b] - 2026-11-04

### Added

- Role-based access control: `admin` and `user` roles, configured via the `ADMIN_USERNAMES` and `USERNAMES` environment variables.
- Regular users only see, create, download and delete their own peers, within a per-user limit (default from `DEFAULT_CONFIG_LIMIT`, default 3).
- Admin user management from the bot: `/adduser` (forward a message from the user, or send `username [limit]`), `/users`, `/limit <username> <N>`, `/deluser <username>`.
- Automatic peer names: new peers are named `<username>_<random hash>`; search matches this pattern.
- SQLite user database (`USERS_DB_PATH`, default `users.db`) storing Telegram ID, username, role and config limit; mounted as a volume in `docker-compose.yml`.

### Changed

- **Breaking:** `ALLOWED_USERNAMES` is replaced by `ADMIN_USERNAMES` and `USERNAMES` — update your `.env` before upgrading.
- Peer creation no longer asks for a name: `/create` (or the menu button) generates a unique name immediately.

## [0.1.0] - 2026-03-25

### Added

- Peer rename function and cancel action during peer creation.

### Changed

- Internal code deduplication.

## [0.0.2] - 2026-03-23

### Changed

- Improved type annotations.

## [0.0.1] - 2026-03-23

### Added

- Initial release: Telegram bot for managing wg-easy peers (list, create with custom Amnezia links, download, delete, search).