import sqlite3
from dataclasses import dataclass


@dataclass
class User:
    tg_id: int | None
    username: str
    role: str
    config_limit: int
    status: str = "enabled"


class UserDB:
    def __init__(self, path: str, default_limit: int = 3):
        if default_limit < -1:
            raise ValueError("Default config limit must be -1 or greater")
        self.path = path
        self.default_limit = default_limit
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._users = set()
        self._admins = set()

    def _ensure_schema(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              username     TEXT PRIMARY KEY,
              tg_id        INTEGER UNIQUE,
              role         TEXT NOT NULL CHECK (role IN ('admin','user')),
              config_limit INTEGER NOT NULL,
              created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS peers (
              name       TEXT PRIMARY KEY,
              client_id  TEXT,
              owner      TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tg_id INTEGER NOT NULL,
              username TEXT,
              first_name TEXT,
              last_name TEXT,
              status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','declined')),
              admin_id INTEGER,
              admin_username TEXT,
              requested_at TEXT NOT NULL DEFAULT (datetime('now')),
              decided_at TEXT
            )
            """
        )
        migrations = {
            "users": {
                "status": "TEXT NOT NULL DEFAULT 'enabled'",
                "disabled_at": "TEXT",
                "disabled_by": "TEXT",
                "disabled_until": "TEXT",
            },
            "requests": {
                "type": "TEXT NOT NULL DEFAULT 'access'",
                "requested_limit": "INTEGER",
                "previous_limit": "INTEGER",
                "notification_refs": "TEXT NOT NULL DEFAULT '[]'",
            },
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        user_columns = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
        for old_name, new_name in (
            ("banned_at", "disabled_at"),
            ("banned_by", "disabled_by"),
            ("banned_until", "disabled_until"),
        ):
            if old_name in user_columns:
                cur.execute(
                    f"UPDATE users SET {new_name} = COALESCE({new_name}, {old_name})"
                )
        cur.execute("UPDATE users SET status = 'disabled' WHERE status = 'banned'")
        cur.execute("UPDATE users SET status = 'enabled' WHERE status = 'active'")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS peer_snapshots (
              client_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              enabled INTEGER NOT NULL,
              latest_handshake_at TEXT,
              transfer_rx INTEGER NOT NULL DEFAULT 0,
              transfer_tx INTEGER NOT NULL DEFAULT 0,
              is_active INTEGER NOT NULL DEFAULT 0,
              sampled_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE TABLE IF NOT EXISTS admin_username_history (username TEXT PRIMARY KEY)")
        self.conn.commit()

    @staticmethod
    def _normalize_username(username: str) -> str:
        return str(username).strip().lstrip("@").lower()

    def set_bootstrap_sets(self, admins: list[str], users: set[str]) -> None:
        """Configure username invitations without changing existing accounts."""
        self._users = {name for u in users or set() if (name := self._normalize_username(u))}
        self._admins = {name for a in admins if (name := self._normalize_username(a))}
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO admin_username_history (username) "
                "SELECT username FROM users WHERE role = 'admin'"
            )

    @staticmethod
    def _remember_admin_name(cur: sqlite3.Cursor, username: str) -> None:
        cur.execute(
            "INSERT OR IGNORE INTO admin_username_history (username) VALUES (?)", (username,)
        )

    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(
            tg_id=row["tg_id"],
            username=row["username"],
            role=row["role"],
            config_limit=row["config_limit"],
            status=row["status"] if "status" in row.keys() else "enabled",
        )

    def resolve(self, tg_id: int | None, username: str | None) -> User | None:
        cur = self.conn.cursor()
        if tg_id is not None:
            cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
            row = cur.fetchone()
            if row:
                if username:
                    new_name = str(username).strip().lstrip("@").lower()
                    if new_name and new_name != row["username"]:
                        try:
                            with self.conn:
                                old_name = row["username"]
                                if row["role"] == "admin":
                                    self._remember_admin_name(cur, old_name)
                                cur.execute("UPDATE users SET username = ? WHERE tg_id = ?", (new_name, tg_id))
                                cur.execute("UPDATE peers SET owner = ? WHERE owner = ?", (new_name, old_name))
                                cur.execute("UPDATE requests SET username = ? WHERE tg_id = ?", (new_name, tg_id))
                            cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
                            row = cur.fetchone()
                        except sqlite3.IntegrityError:
                            # A reserved username must not be assigned to another account.
                            pass
                return self._row_to_user(row)
        if username is not None:
            uname = str(username).strip().lstrip("@").lower()
            if not uname:
                return None
            cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
            row = cur.fetchone()
            if row:
                if row["tg_id"] is not None or tg_id is None:
                    return None
                cur.execute("UPDATE users SET tg_id = ? WHERE username = ?", (tg_id, uname))
                self.conn.commit()
                cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
                row = cur.fetchone()
                if row:
                    return self._row_to_user(row)
            if tg_id is None:
                return None
            if uname in self._admins and not cur.execute(
                "SELECT 1 FROM admin_username_history WHERE username = ?", (uname,)
            ).fetchone():
                with self.conn:
                    cur.execute(
                        "INSERT INTO users (username, tg_id, role, config_limit, status) "
                        "VALUES (?, ?, 'admin', -1, 'enabled')",
                        (uname, tg_id),
                    )
                    self._remember_admin_name(cur, uname)
                cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
                return self._row_to_user(cur.fetchone())
            if uname in self._users:
                cur.execute(
                    "INSERT INTO users (username, tg_id, role, config_limit, status) "
                    "VALUES (?, ?, 'user', ?, 'enabled')",
                    (uname, tg_id, self.default_limit),
                )
                self.conn.commit()
                cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
                row = cur.fetchone()
                if row:
                    return self._row_to_user(row)
            return None
        return None

    def add_user(
        self,
        username: str,
        tg_id: int | None = None,
        limit: int | None = None,
        role: str = "user",
    ) -> User:
        uname = str(username).strip().lstrip("@").lower()
        if limit is None:
            limit = self.default_limit
        if limit < -1:
            raise ValueError("Config limit must be -1 or greater")
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = ?", (uname,))
        if cur.fetchone():
            raise ValueError("user already exists")
        if tg_id is not None:
            cur.execute("SELECT 1 FROM users WHERE tg_id = ?", (tg_id,))
            if cur.fetchone():
                raise ValueError("user already exists")
        cur.execute(
            "INSERT INTO users (username, tg_id, role, config_limit, status) VALUES (?, ?, ?, ?, 'enabled')",
            (uname, tg_id, role, limit),
        )
        if role == "admin":
            self._remember_admin_name(cur, uname)
        self.conn.commit()
        cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
        row = cur.fetchone()
        return self._row_to_user(row)

    def user_exists(self, username: str) -> bool:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = ?", (uname,))
        return cur.fetchone() is not None

    def set_limit(self, username: str, limit: int) -> User:
        if limit < -1:
            raise ValueError("Config limit must be -1 or greater")
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET config_limit = ? WHERE username = ?", (limit, uname))
        self.conn.commit()
        if cur.rowcount == 0:
            raise KeyError("user not found")
        cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
        row = cur.fetchone()
        return self._row_to_user(row)

    def remove_user(self, username: str) -> bool:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM users WHERE username = ?", (uname,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_users(self, status: str | None = None) -> list[User]:
        if status not in (None, "enabled", "disabled"):
            raise ValueError("invalid user status")
        cur = self.conn.cursor()
        if status is None:
            cur.execute("SELECT * FROM users ORDER BY username")
        else:
            cur.execute("SELECT * FROM users WHERE status = ? ORDER BY username", (status,))
        rows = cur.fetchall()
        return [self._row_to_user(r) for r in rows]

    def is_admin(self, user: User) -> bool:
        return user.role == "admin"

    def has_pending_request(self, tg_id: int, req_type: str = "access") -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM requests WHERE tg_id = ? AND status = 'pending' AND type = ?",
            (tg_id, req_type),
        )
        return cur.fetchone() is not None

    def add_request(
        self,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        req_type: str = "access",
        requested_limit: int | None = None,
    ) -> int:
        uname = None
        if username is not None:
            uname = str(username).strip().lstrip("@").lower()
            if not uname:
                uname = None
        cur = self.conn.cursor()
        if req_type == "limit":
            if tg_id is not None:
                cur.execute("SELECT config_limit FROM users WHERE tg_id = ?", (tg_id,))
                user_row = cur.fetchone()
            else:
                user_row = None
            if user_row is None and uname is not None:
                cur.execute("SELECT config_limit FROM users WHERE username = ?", (uname,))
                user_row = cur.fetchone()
            if user_row is None:
                raise ValueError("Cannot create a limit request for an unknown user")
            previous_limit = int(user_row["config_limit"])
        else:
            previous_limit = None
        cur.execute(
            "INSERT INTO requests (tg_id, username, first_name, last_name, type, requested_limit, previous_limit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_id, uname, first_name, last_name, req_type, requested_limit, previous_limit),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_request_notifications(self, req_id: int, refs: list[tuple[int, int]]) -> None:
        import json
        self.conn.execute("UPDATE requests SET notification_refs = ? WHERE id = ?", (json.dumps(refs), req_id))
        self.conn.commit()

    def _row_to_request(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "tg_id": row["tg_id"],
            "username": row["username"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "status": row["status"],
            "admin_id": row["admin_id"],
            "admin_username": row["admin_username"],
            "requested_at": row["requested_at"],
            "decided_at": row["decided_at"],
            "type": row["type"],
            "requested_limit": row["requested_limit"],
            "previous_limit": row["previous_limit"] if "previous_limit" in row.keys() else None,
            "notification_refs": row["notification_refs"] if "notification_refs" in row.keys() else "[]",
        }

    def get_request(self, req_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM requests WHERE id = ?", (req_id,))
        row = cur.fetchone()
        return self._row_to_request(row) if row else None

    def decide_request(
        self,
        req_id: int,
        status: str,
        admin_id: int,
        admin_username: str,
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE requests SET status = ?, admin_id = ?, admin_username = ?, "
            "decided_at = datetime('now') WHERE id = ? AND status = 'pending'",
            (status, admin_id, admin_username, req_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def approve_request(self, req_id: int, admin_id: int, admin_username: str) -> User | None:
        """Apply a pending request and record its approval in one transaction.

        Returns None if another administrator has already decided the request.
        Invalid requests remain pending so they can be investigated.
        """
        with self.conn:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM requests WHERE id = ? AND status = 'pending'", (req_id,))
            request = cur.fetchone()
            if request is None:
                return None
            if request["type"] == "limit":
                limit = request["requested_limit"]
                if not isinstance(limit, int) or limit < -1:
                    raise ValueError("Invalid requested limit")
                cur.execute(
                    "UPDATE users SET config_limit = ? WHERE tg_id = ?",
                    (limit, request["tg_id"]),
                )
                if cur.rowcount == 0:
                    raise KeyError("User no longer exists")
                cur.execute("SELECT * FROM users WHERE tg_id = ?", (request["tg_id"],))
            elif request["type"] == "access":
                username = request["username"] or f"user{request['tg_id']}"
                username = str(username).strip().lstrip("@").lower()
                if not username:
                    raise ValueError("Invalid username")
                cur.execute(
                    "INSERT INTO users (username, tg_id, role, config_limit, status) "
                    "VALUES (?, ?, 'user', ?, 'enabled')",
                    (username, request["tg_id"], self.default_limit),
                )
                cur.execute("SELECT * FROM users WHERE tg_id = ?", (request["tg_id"],))
            else:
                raise ValueError("Unknown request type")
            row = cur.fetchone()
            cur.execute(
                "UPDATE requests SET status = 'approved', admin_id = ?, admin_username = ?, "
                "decided_at = datetime('now') WHERE id = ? AND status = 'pending'",
                (admin_id, admin_username, req_id),
            )
            return self._row_to_user(row)

    def list_requests(
        self,
        limit: int = 20,
        tg_id: int | None = None,
        username: str | None = None,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict]:
        uname = str(username).strip().lstrip("@").lower() if username is not None else None
        if uname == "":
            uname = None
        base = "SELECT * FROM requests "
        conditions = []
        params = []
        if tg_id is not None and uname is not None:
            conditions.append("(tg_id = ? OR username = ?)")
            params.extend((tg_id, uname))
        elif tg_id is not None:
            conditions.append("tg_id = ?")
            params.append(tg_id)
        elif uname is not None:
            conditions.append("username = ?")
            params.append(uname)
        if status is not None:
            if status not in ("pending", "approved", "declined"):
                raise ValueError("invalid request status")
            conditions.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cur = self.conn.cursor()
        cur.execute(base + where + " ORDER BY id DESC LIMIT ? OFFSET ?", tuple(params) + (limit, offset))
        return [self._row_to_request(r) for r in cur.fetchall()]

    def get_user(self, username: str) -> User | None:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def disable_user(self, username: str, disabled_by: str | None = None) -> User:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE users SET status = 'disabled', disabled_at = datetime('now'), "
            "disabled_by = ? WHERE username = ?",
            (disabled_by, uname),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            raise KeyError(username)
        return self.get_user(uname)

    def enable_user(self, username: str) -> User:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE users SET status = 'enabled', disabled_at = NULL, disabled_by = NULL, "
            "disabled_until = NULL WHERE username = ?",
            (uname,),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            raise KeyError(username)
        return self.get_user(uname)

    def admins_with_tg(self) -> list[User]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM users WHERE role = 'admin' AND tg_id IS NOT NULL "
            "AND tg_id != -1 ORDER BY username"
        )
        return [self._row_to_user(r) for r in cur.fetchall()]

    def set_role(self, username: str, role: str) -> User:
        if role not in ("admin", "user"):
            raise ValueError("invalid role")
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT role, tg_id FROM users WHERE username = ?", (uname,))
        existing = cur.fetchone()
        if existing is None:
            self.conn.rollback()
            raise KeyError(username)
        if role == "admin" and existing["role"] != "admin" and existing["tg_id"] is None:
            self.conn.rollback()
            raise ValueError("user must start the bot before promotion to admin")
        if existing["role"] == "admin" and role != "admin":
            cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
            if cur.fetchone()[0] <= 1:
                self.conn.rollback()
                raise ValueError("cannot demote the last admin")
        cur.execute("UPDATE users SET role = ? WHERE username = ?", (role, uname))
        if role == "admin":
            self._remember_admin_name(cur, uname)
        self.conn.commit()
        return self.get_user(uname)

    def request_count(self, status: str | None = None) -> int:
        if status is None:
            return self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        if status not in ("pending", "approved", "declined"):
            raise ValueError("invalid request status")
        return self.conn.execute("SELECT COUNT(*) FROM requests WHERE status = ?", (status,)).fetchone()[0]

    def is_disabled(self, user: User) -> bool:
        return user.status == "disabled"

    @staticmethod
    def _counter(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def update_peer_snapshots(self, clients: list[dict]) -> None:
        """Persist one successful wg-easy sample and detect RX activity deltas."""
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            previous = {
                row["client_id"]: row["transfer_rx"]
                for row in cur.execute("SELECT client_id, transfer_rx FROM peer_snapshots")
            }
            sampled_at = cur.execute("SELECT datetime('now')").fetchone()[0]
            snapshots = []
            for client in clients:
                client_id = client.get("id")
                if client_id is None:
                    client_id = client.get("clientId")
                if client_id is None:
                    continue
                client_id = str(client_id)
                transfer_rx = self._counter(client.get("transferRx"))
                transfer_tx = self._counter(client.get("transferTx"))
                old_rx = previous.get(client_id)
                is_active = old_rx is not None and transfer_rx > old_rx
                snapshots.append((
                    client_id,
                    str(client.get("name") or ""),
                    int(bool(client.get("enabled"))),
                    client.get("latestHandshakeAt"),
                    transfer_rx,
                    transfer_tx,
                    int(is_active),
                    sampled_at,
                ))
            cur.execute("DELETE FROM peer_snapshots")
            cur.executemany(
                "INSERT INTO peer_snapshots "
                "(client_id, name, enabled, latest_handshake_at, transfer_rx, transfer_tx, is_active, sampled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                snapshots,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def list_peer_snapshots(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT client_id, name, enabled, latest_handshake_at, transfer_rx, transfer_tx, is_active "
            "FROM peer_snapshots ORDER BY name"
        ).fetchall()
        return [
            {
                "id": row["client_id"],
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "latestHandshakeAt": row["latest_handshake_at"],
                "transferRx": row["transfer_rx"],
                "transferTx": row["transfer_tx"],
                "is_active": bool(row["is_active"]),
            }
            for row in rows
        ]

    def get_peer_snapshot(self, client_id: str | int) -> dict | None:
        row = self.conn.execute(
            "SELECT client_id, name, enabled, latest_handshake_at, transfer_rx, transfer_tx, is_active "
            "FROM peer_snapshots WHERE client_id = ?",
            (str(client_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["client_id"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "latestHandshakeAt": row["latest_handshake_at"],
            "transferRx": row["transfer_rx"],
            "transferTx": row["transfer_tx"],
            "is_active": bool(row["is_active"]),
        }

    def _norm_owner(self, owner: str) -> str:
        return str(owner).strip().lstrip("@").lower()

    def add_peer(self, name: str, owner: str, client_id=None) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO peers (name, client_id, owner) VALUES (?, ?, ?)",
            (str(name).lower(), None if client_id is None else str(client_id), self._norm_owner(owner)),
        )
        self.conn.commit()

    def remove_peer(self, name: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM peers WHERE name = ?", (str(name).lower(),))
        self.conn.commit()
        return cur.rowcount > 0

    def rename_peer(self, old_name: str, new_name: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE peers SET name = ? WHERE name = ?",
            (str(new_name).lower(), str(old_name).lower()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def peer_owner(self, name: str) -> str | None:
        cur = self.conn.cursor()
        cur.execute("SELECT owner FROM peers WHERE name = ?", (str(name).lower(),))
        row = cur.fetchone()
        return row["owner"] if row else None

    def get_peer(self, name: str) -> dict | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT name, client_id, owner, created_at FROM peers WHERE name = ?",
            (str(name).lower(),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "client_id": row["client_id"],
            "owner": row["owner"],
            "created_at": row["created_at"],
        }

    def peer_records(self, owner: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT name, client_id, created_at FROM peers WHERE owner = ? "
            "ORDER BY created_at DESC, name",
            (self._norm_owner(owner),),
        )
        return [
            {
                "name": row["name"],
                "client_id": row["client_id"],
                "created_at": row["created_at"],
            }
            for row in cur.fetchall()
        ]

    def peers_of(self, owner: str) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM peers WHERE owner = ? ORDER BY name", (self._norm_owner(owner),))
        return [r["name"] for r in cur.fetchall()]
