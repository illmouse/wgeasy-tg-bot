import sqlite3
from dataclasses import dataclass


@dataclass
class User:
    tg_id: int | None
    username: str
    role: str
    config_limit: int
    status: str = "active"


class UserDB:
    def __init__(self, path: str, default_limit: int = 3):
        self.path = path
        self.default_limit = default_limit
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._admins = set()
        self._users = set()

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
        for statement in (
            "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE users ADD COLUMN banned_at TEXT",
            "ALTER TABLE users ADD COLUMN banned_by TEXT",
            "ALTER TABLE users ADD COLUMN banned_until TEXT",
            "ALTER TABLE requests ADD COLUMN type TEXT NOT NULL DEFAULT 'access'",
            "ALTER TABLE requests ADD COLUMN requested_limit INTEGER",
            "ALTER TABLE requests ADD COLUMN notification_refs TEXT NOT NULL DEFAULT '[]'",
        ):
            try:
                cur.execute(statement)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def set_bootstrap_sets(self, admins: set[str], users: set[str]) -> None:
        self._admins = set()
        for a in admins or set():
            s = str(a).strip().lstrip("@").lower()
            if s:
                self._admins.add(s)
        self._users = set()
        for u in users or set():
            s = str(u).strip().lstrip("@").lower()
            if s:
                self._users.add(s)

    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(
            tg_id=row["tg_id"],
            username=row["username"],
            role=row["role"],
            config_limit=row["config_limit"],
            status=row["status"] if "status" in row.keys() else "active",
        )

    def resolve(self, tg_id: int | None, username: str | None) -> User | None:
        cur = self.conn.cursor()
        if tg_id is not None:
            cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
            row = cur.fetchone()
            if row:
                return self._row_to_user(row)
        if username is not None:
            uname = str(username).strip().lstrip("@").lower()
            if not uname:
                return None
            cur.execute(
                "SELECT * FROM users WHERE tg_id IS NULL AND username = ?",
                (uname,),
            )
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE users SET tg_id = ? WHERE username = ?", (tg_id, uname))
                self.conn.commit()
                cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
                row = cur.fetchone()
                if row:
                    return self._row_to_user(row)
            if uname in self._admins:
                cur.execute(
                    "INSERT INTO users (username, tg_id, role, config_limit) VALUES (?, ?, 'admin', -1)",
                    (uname, tg_id),
                )
                self.conn.commit()
                cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
                row = cur.fetchone()
                if row:
                    return self._row_to_user(row)
            if uname in self._users:
                cur.execute(
                    "INSERT INTO users (username, tg_id, role, config_limit) VALUES (?, ?, 'user', ?)",
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
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = ?", (uname,))
        if cur.fetchone():
            raise ValueError("user already exists")
        if tg_id is not None:
            cur.execute("SELECT 1 FROM users WHERE tg_id = ?", (tg_id,))
            if cur.fetchone():
                raise ValueError("user already exists")
        cur.execute(
            "INSERT INTO users (username, tg_id, role, config_limit) VALUES (?, ?, ?, ?)",
            (uname, tg_id, role, limit),
        )
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

    def list_users(self) -> list[User]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users ORDER BY username")
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
        cur.execute(
            "INSERT INTO requests (tg_id, username, first_name, last_name, type, requested_limit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tg_id, uname, first_name, last_name, req_type, requested_limit),
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

    def list_requests(
        self,
        limit: int = 20,
        tg_id: int | None = None,
        username: str | None = None,
        offset: int = 0,
    ) -> list[dict]:
        uname = str(username).strip().lstrip("@").lower() if username is not None else None
        if uname == "":
            uname = None
        base = "SELECT * FROM requests "
        if tg_id is not None and uname is not None:
            where = "WHERE (tg_id = ? OR username = ?)"
            params = (tg_id, uname)
        elif tg_id is not None:
            where = "WHERE tg_id = ?"
            params = (tg_id,)
        elif uname is not None:
            where = "WHERE username = ?"
            params = (uname,)
        else:
            where = ""
            params = ()
        cur = self.conn.cursor()
        cur.execute(base + where + " ORDER BY id DESC LIMIT ? OFFSET ?", params + (limit, offset))
        return [self._row_to_request(r) for r in cur.fetchall()]

    def get_user(self, username: str) -> User | None:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ?", (uname,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def ban_user(self, username: str, banned_by: str | None = None) -> User:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE users SET status = 'banned', banned_at = datetime('now'), "
            "banned_by = ? WHERE username = ?",
            (banned_by, uname),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            raise KeyError(username)
        return self.get_user(uname)

    def unban_user(self, username: str) -> User:
        uname = str(username).strip().lstrip("@").lower()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE users SET status = 'active', banned_at = NULL, banned_by = NULL, "
            "banned_until = NULL WHERE username = ?",
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
        cur.execute("SELECT role FROM users WHERE username = ?", (uname,))
        existing = cur.fetchone()
        if existing is None:
            self.conn.rollback()
            raise KeyError(username)
        if existing["role"] == "admin" and role != "admin":
            cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
            if cur.fetchone()[0] <= 1:
                self.conn.rollback()
                raise ValueError("cannot demote the last admin")
        cur.execute("UPDATE users SET role = ? WHERE username = ?", (role, uname))
        self.conn.commit()
        return self.get_user(uname)

    def request_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    def is_banned(self, user: User) -> bool:
        return user.status == "banned"

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
