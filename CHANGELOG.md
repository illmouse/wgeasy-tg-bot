# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0b] - 2026-11-07

### Added

- Admins can promote users to admins and demote admins to users; the last admin is protected from demotion.
- Admins may create peers for themselves without a configured per-user limit.
- Peer owner names are visible in admin peer lists and peer details.
- Access and limit request notifications are sent to all admins who have started the bot, and every admin notification is updated when a request is decided.
- Request history is paginated, and admin peer tables use Telegram's native rich-table API when available with a readable text fallback.

### Changed

- Bot menu labels now use clearer plain-language terms such as “Configurations”, “Online devices”, “Add a device”, and “Download configuration”.
- Admin peer creation now asks who the peer is for before offering name selection; admins can choose themselves or another registered user.
- Automatically generated peer names are derived from the wg-easy client ID hash; peers can be renamed from their details view.
- Admin peer lists show the owning user, and peer details identify the owner.

## [1.0.1] - 2026-11-04

### Fixed

- wg-easy master compatibility: `GET /api/client` now returns a paginated envelope (`clients`, `total`, `page`, `pageSize`) instead of a flat list — the peer list is unwrapped and all pages fetched (backward compatible with the flat list of older versions).
- Login cookie hardening: the session cookie's `Secure` flag and `Domain` attribute are now both stripped, so authentication keeps working over plain HTTP and when the server sets a cookie domain that does not match the host the bot talks to (custom wg-easy builds).

## [1.0.0] - 2026-11-04

Stable release. Rolls up all changes shipped in the `0.2.x` beta line (`0.2.0b`–`0.2.4b`) since `0.1.0`.

### Added

- Role-based access control: `admin` and `user` roles configured via the `ADMIN_USERNAMES` and `USERNAMES` environment variables; regular users only see, create, download and delete their own peers, within a per-user limit (default from `DEFAULT_CONFIG_LIMIT`, default 3).
- Admin user management from the bot: `/adduser` (forward a message from the user, or send `username [limit]`), `/users`, `/limit <username> <N>`, `/deluser <username>` (cascades: the user's peers are deleted from wg-easy and the bot database), `/ban <username>` / `/unban <username>` (temporarily block a user and disable their peers in wg-easy; unbanning re-enables them).
- Interactive user management UI: tappable user list with peer counts; user card (limit, requests, ban, delete); peers card (one full-width row per peer with name, creation date and last handshake); peer details card (owner, status, IP, creation date, last handshake, config download, rename, enable/disable, delete).
- `/givemepowers` access requests: unknown users send an approval request (stored in the database) to all reachable admins, who approve or decline right from the chat; the requester is notified of the outcome; registered users see their peers, limit and own request history.
- Limit change requests: users get an "📈 Request Limit" button to ask for a new config limit; admins approve or decline inline, the user is notified.
- Request history: `📜 Requests` admin menu button and `/requests` command show the global history; the user card has a per-user history view.
- SQLite user database (`USERS_DB_PATH`, default `users.db`) storing users, peer ownership and access/limit requests.

### Changed

- **Breaking:** `ALLOWED_USERNAMES` is replaced by `ADMIN_USERNAMES` and `USERNAMES` — update your `.env` before upgrading.
- Peer names are short random hashes (8 characters, `a-z0-9`); peer ownership is tracked in the bot's SQLite database instead of being encoded in the peer name. This removes any dependency on Telegram username length, and names always fit the 15-character AmneziaWG (Android) tunnel-name limit. Peer renames are validated against the same limit and character set.
- Admin peer creation asks how to proceed: pick a registered user from the database (auto-generated hash name) or set the full peer name manually; `/create <username>` for a specific user still works.
- Users without access no longer receive any messages from the bot; unauthorized access attempts are only logged.
- wg-easy v15 compatibility: login now uses `POST /api/auth/password`, falling back to the old `POST /api/session` (v14 and Amnezia forks); the working endpoint is remembered and re-probed on server upgrades.

## [0.2.4b] - 2026-11-04

### Changed

- User management navigation rework: the user card now leads to a dedicated peers card (one full-width row per peer, so the name, creation date and last handshake are no longer squeezed); all peer actions (get config, rename, enable/disable, delete) moved into the peer details card; peer actions no longer appear on the user card itself.
- Navigation buttons: `Users` renamed to `Back` and placed on one row with `Close`; Delete pairs with Ban/Unban, Limit pairs with Requests; after deleting a peer from its details card, admins get a back button to the user's peers card.

## [0.2.3b] - 2026-11-04

### Added

- Peer details view: tapping a peer's name in the user card shows its owner, status, IP, creation date (from the database) and last handshake, with a config download action.
- Enable/disable toggle for each peer in the user card.
- Request history: `📜 Requests` admin menu button and `/requests` command show the global request history; the user card has a per-user history button; registered users see their own requests in `/givemepowers`.

### Changed

- User card layout: one button row per peer (name, creation date, last handshake, rename / enable-disable / delete); `Users` and `Close` buttons are now full-width and stacked.

## [0.2.2b] - 2026-11-04

### Changed

- Peer names are now short random hashes (8 characters, `a-z0-9`); peer ownership is tracked in the bot's SQLite database instead of being encoded in the peer name. This removes any dependency on Telegram username length, and names always fit the 15-character AmneziaWG (Android) tunnel-name limit. Peer renames are validated against the same limit and character set.
- Admin peer creation now asks how to proceed: pick a registered user from the database (auto-generated hash name) or set the full peer name manually (`/create` without arguments or the menu button). `/create <username>` for a specific user still works.
- `/deluser <username>` now cascades: the user's peers are deleted from wg-easy and removed from the bot database along with the user record.
- `/users` (and the inline Users menu) is now interactive: every user is a tappable button showing peer count and limit; tapping a user opens their detail view with per-peer actions (config, rename, delete) and user actions (delete, change limit, ban/unban).

### Added

- `/givemepowers` — access requests: registered users get their peers and limit; unknown users send an approval request (stored in the database) to all reachable admins, who approve or decline right from the chat; the requester is notified of the outcome.
- `/ban <username>` / `/unban <username>` — temporarily block a user: the bot stops responding to them (only a "temporarily blocked" notice) and their peers are disabled in wg-easy; unbanning re-enables the peers. Banned users are marked with 🔒 in `/users`.
- Limit change requests: users get an "📈 Request Limit" button (reply keyboard and inline menu) to request a new config limit; the request is stored in the database and sent to all reachable admins with inline Approve/Decline buttons; the user is notified of the outcome.

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
