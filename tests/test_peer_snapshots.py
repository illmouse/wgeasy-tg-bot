import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_helpers import (
    apply_peer_sample,
    delete_callback_message,
    fetch_peer_sample,
    filter_peer_snapshots,
    format_bytes,
)
from users import UserDB


class PeerSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = UserDB(str(Path(self.temp_dir.name) / "users.db"))

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    @staticmethod
    def peer(rx, tx=20, peer_id="peer-1", enabled=True):
        return {
            "id": peer_id,
            "name": peer_id,
            "enabled": enabled,
            "latestHandshakeAt": "2026-11-08T10:00:00Z",
            "transferRx": rx,
            "transferTx": tx,
        }

    def snapshot(self):
        return self.db.list_peer_snapshots()[0]

    def test_rx_counter_transitions_define_activity(self):
        self.db.update_peer_snapshots([self.peer(100)])
        self.assertFalse(self.snapshot()["is_active"])

        self.db.update_peer_snapshots([self.peer(125)])
        self.assertTrue(self.snapshot()["is_active"])

        self.db.update_peer_snapshots([self.peer(125)])
        self.assertFalse(self.snapshot()["is_active"])

        self.db.update_peer_snapshots([self.peer(10)])
        self.assertFalse(self.snapshot()["is_active"])

        self.db.update_peer_snapshots([self.peer(11)])
        self.assertTrue(self.snapshot()["is_active"])

    def test_failed_poll_preserves_last_successful_snapshot(self):
        self.db.update_peer_snapshots([self.peer(100)])
        self.db.update_peer_snapshots([self.peer(125)])
        api = _FlakyAPI(RuntimeError("offline"))
        self.assertIsNone(fetch_peer_sample(api))
        self.assertFalse(apply_peer_sample(self.db, None))
        self.assertEqual(self.snapshot()["transferRx"], 125)
        self.assertTrue(self.snapshot()["is_active"])

    def test_successful_empty_sample_removes_deleted_peers(self):
        self.db.update_peer_snapshots([self.peer(100)])
        self.db.update_peer_snapshots([])
        self.assertEqual(self.db.list_peer_snapshots(), [])

    def test_cached_filters_and_decimal_traffic_format(self):
        peers = [
            {"id": "a", "is_active": True, "enabled": True},
            {"id": "b", "is_active": False, "enabled": True},
            {"id": "c", "is_active": False, "enabled": False},
        ]
        self.assertEqual([p["id"] for p in filter_peer_snapshots(peers, "active")], ["a"])
        self.assertEqual([p["id"] for p in filter_peer_snapshots(peers, "enabled")], ["a", "b"])
        self.assertEqual([p["id"] for p in filter_peer_snapshots(peers, "disabled")], ["c"])
        self.assertEqual(format_bytes(999), "999 B")
        self.assertEqual(format_bytes(1000), "1.00 KB")
        self.assertEqual(format_bytes(1_000_000), "1.00 MB")

    def test_user_status_migration_and_filters(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE users (username TEXT PRIMARY KEY, tg_id INTEGER UNIQUE, "
            "role TEXT NOT NULL, config_limit INTEGER NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), status TEXT NOT NULL DEFAULT 'active', "
            "banned_at TEXT, banned_by TEXT, banned_until TEXT)"
        )
        connection.execute(
            "INSERT INTO users (username, role, config_limit, status, banned_at, banned_by) "
            "VALUES ('alice', 'user', 3, 'banned', '2026-01-01', 'admin')"
        )
        connection.commit()
        connection.close()

        migrated = UserDB(str(legacy_path))
        self.assertEqual(migrated.get_user("alice").status, "disabled")
        self.assertEqual(
            migrated.conn.execute("SELECT disabled_by FROM users WHERE username='alice'").fetchone()[0],
            "admin",
        )
        bob = migrated.add_user("bob")
        migrated.disable_user("bob", disabled_by="admin")
        self.assertEqual([user.username for user in migrated.list_users("enabled")], [])
        self.assertEqual([user.username for user in migrated.list_users("disabled")], ["alice", "bob"])
        migrated.enable_user("bob")
        self.assertEqual([user.username for user in migrated.list_users("enabled")], ["bob"])
        migrated.conn.close()


class _FlakyAPI:
    def __init__(self, result):
        self.result = result

    def list_clients(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeMessage:
    def __init__(self, fail=False):
        self.deleted = False
        self.fail = fail

    async def delete(self):
        if self.fail:
            raise RuntimeError("cannot delete")
        self.deleted = True


class _FakeQuery:
    def __init__(self, message):
        self.message = message


class SilentCancellationTests(unittest.TestCase):
    def test_inline_cancel_deletes_prompt(self):
        message = _FakeMessage()
        asyncio.run(delete_callback_message(_FakeQuery(message)))
        self.assertTrue(message.deleted)

    def test_inline_cancel_swallows_delete_failure(self):
        message = _FakeMessage(fail=True)
        asyncio.run(delete_callback_message(_FakeQuery(message)))
        self.assertFalse(message.deleted)


if __name__ == "__main__":
    unittest.main()
