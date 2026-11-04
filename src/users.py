import sqlite3
from dataclasses import dataclass


@dataclass
class User:
    tg_id: int | None
    username: str
    role: str
    config_limit: int


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
