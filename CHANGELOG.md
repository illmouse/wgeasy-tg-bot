# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Peer names are now short random hashes (8 characters, `a-z0-9`); peer ownership is tracked in the bot's SQLite database instead of being encoded in the peer name. This removes any dependency on Telegram username length, and names always fit the 15-character AmneziaWG (Android) tunnel-name limit. Peer renames are validated against the same limit and character set.
- Admin peer creation now asks how to proceed: pick a registered user from the database (auto-generated hash name) or set the full peer name manually (`/create` without arguments or the menu button). `/create <username>` for a specific user still works.

### Added

- `/givemepowers` — access requests: registered users get their peers and limit; unknown users send an approval request (stored in the database) to all reachable admins, who approve or decline right from the chat; the requester is notified of the outcome.
- `/ban <username>` / `/unban <username>` — temporarily block a user: the bot stops responding to them (only a "temporarily blocked" notice) and their peers are disabled in wg-easy; unbanning re-enables the peers. Banned users are marked with 🔒 in `/users`.

## [0.2.1b] - 2026-11-04

### Fixed

- wg-easy v15 compatibility: login now uses `POST /api/auth/password`, falling back to the old `POST /api/session` (v14 and Amnezia forks); the working endpoint is remembered and re-probed on server upgrades.

### Changed

- Users without access no longer receive any messages from the bot; unauthorized access attempts are only logged.

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

- Peer rename function; rename, delete and get-config options are available after searching a peer.

### Fixed

- Cancel button during peer creation; pressed button names no longer leak into prompts.

### Changed

- Internal code deduplication.

## [0.0.2] - 2026-03-23

### Fixed

- 401 error when working through insecure plain http connection.
- Confusing annotation for the WG Tunnel app.

## [0.0.1] - 2026-03-23

### Added

- Initial release: Telegram bot for managing wg-easy peers (list, create with custom Amnezia links, download, delete, search).