import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _Button:
    def __init__(self, text, callback_data=None, **kwargs):
        self.text = text
        self.callback_data = callback_data


class _Markup:
    def __init__(self, inline_keyboard=None, **kwargs):
        self.inline_keyboard = inline_keyboard or []


class _ReplyMarkup:
    def __init__(self, keyboard=None, **kwargs):
        self.keyboard = keyboard or []


def _install_telegram_stubs():
    telegram = types.ModuleType("telegram")
    telegram.InlineKeyboardButton = _Button
    telegram.InlineKeyboardMarkup = _Markup
    telegram.MessageOriginHiddenUser = type("MessageOriginHiddenUser", (), {})
    telegram.MessageOriginUser = type("MessageOriginUser", (), {})
    telegram.ReplyKeyboardMarkup = _ReplyMarkup
    telegram.Update = type("Update", (), {})

    ext = types.ModuleType("telegram.ext")
    ext.Application = type("Application", (), {})
    ext.CallbackQueryHandler = type("CallbackQueryHandler", (), {})
    ext.CommandHandler = type("CommandHandler", (), {})
    ext.ContextTypes = type("ContextTypes", (), {"DEFAULT_TYPE": object})
    ext.ConversationHandler = type("ConversationHandler", (), {"END": -1})
    ext.MessageHandler = type("MessageHandler", (), {})
    ext.filters = types.SimpleNamespace()
    sys.modules["telegram"] = telegram
    sys.modules["telegram.ext"] = ext


_install_telegram_stubs()
os.environ.setdefault("WG_EASY_URL", "http://localhost")
os.environ.setdefault("WG_EASY_LOGIN", "test")
os.environ.setdefault("WG_EASY_PASSWORD", "test")
_BOT_DB_DIR = tempfile.TemporaryDirectory()
os.environ["USERS_DB_PATH"] = str(Path(_BOT_DB_DIR.name) / "import.db")

import bot
from users import UserDB


class CachedListViewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = bot.users_db
        bot.users_db = UserDB(str(Path(self.temp_dir.name) / "users.db"))
        bot.users_db.add_user("alice", tg_id=101, limit=2)
        bot.users_db.add_user("bob", tg_id=202, limit=2)
        bot.users_db.disable_user("bob", disabled_by="admin")
        bot.users_db.add_peer("phone", "alice", client_id="client-1")
        bot.users_db.update_peer_snapshots([
            {
                "id": "client-1",
                "name": "phone",
                "enabled": True,
                "latestHandshakeAt": None,
                "transferRx": 1000,
                "transferTx": 2000,
            }
        ])

    def tearDown(self):
        bot.users_db.conn.close()
        bot.users_db = self.original_db
        self.temp_dir.cleanup()

    @staticmethod
    def buttons(markup):
        return [button for row in markup.inline_keyboard for button in row]

    def test_registered_user_filters_and_status_actions_render_from_database(self):
        with patch.object(bot.wg, "list_clients", side_effect=AssertionError("unexpected API read")):
            title, markup = bot._users_list_view(status_filter="enabled")
            self.assertIn("Users · Enabled (1)", title)
            enabled_buttons = self.buttons(markup)
            alice_button = next(button for button in enabled_buttons if button.callback_data.startswith("user:"))
            self.assertEqual(alice_button.callback_data, "user:alice:enabled:0")

            title, markup = bot._users_list_view(status_filter="disabled")
            self.assertIn("Users · Disabled (1)", title)
            disabled_buttons = self.buttons(markup)
            bob_button = next(button for button in disabled_buttons if button.callback_data.startswith("user:"))
            self.assertEqual(bob_button.callback_data, "user:bob:disabled:0")
            self.assertIn("⛔", bob_button.text)

            detail_text, detail_markup = bot._user_detail_view("bob")
            self.assertIn("DISABLED", detail_text)
            self.assertIn("<b>Limit:</b>", detail_text)
            self.assertIn("<b>TG:</b>", detail_text)
            action = next(button for button in self.buttons(detail_markup) if "Enable" in button.text)
            self.assertEqual(action.callback_data, "userenable:bob")
            self.assertFalse(any("Ban" in button.text for button in self.buttons(detail_markup)))

    def test_admins_are_labeled_and_sorted_before_users(self):
        bot.users_db.add_user("zadmin", tg_id=303, role="admin", limit=-1)
        _, markup = bot._users_list_view()
        user_buttons = [
            button for button in self.buttons(markup)
            if button.callback_data.startswith("user:")
        ]
        self.assertIn("zadmin (admin)", user_buttons[0].text)
        self.assertEqual(user_buttons[0].callback_data, "user:zadmin:all:0")

    def test_device_view_uses_snapshot_and_renders_traffic_totals(self):
        bot.users_db.add_user("admin", tg_id=303, role="admin", limit=3)
        user = bot.users_db.get_user("admin")
        bot.users_db.update_peer_snapshots([
            {
                "id": "client-1",
                "name": "a-very-long-device-name-that-needs-truncating",
                "enabled": True,
                "latestHandshakeAt": "2026-11-08T12:34:56Z",
                "transferRx": 1000,
                "transferTx": 2000,
            }
        ])
        bot.users_db.rename_peer("phone", "a-very-long-device-name-that-needs-truncating")
        with patch.object(bot.wg, "list_clients", side_effect=AssertionError("unexpected API read")):
            markup, clients, page, pages = bot._device_list_markup(
                bot.users_db.list_peer_snapshots(), user, "all", 0
            )
        self.assertEqual((page, pages), (0, 1))
        self.assertEqual(len(clients), 1)
        device_button = next(button for button in self.buttons(markup) if button.callback_data.startswith("pinfo:"))
        self.assertLessEqual(len(device_button.text), 64)
        self.assertIn("↑1KB", device_button.text)
        self.assertIn("↓2KB", device_button.text)
        self.assertIn("08.11 12:34", device_button.text)
        self.assertIn("@alice", device_button.text)

        _, peers_markup = bot._peers_list_view("alice")
        peer_button = next(button for button in self.buttons(peers_markup) if button.callback_data.startswith("pinfo:"))
        self.assertLessEqual(len(peer_button.text), 64)
        self.assertIn("↑1KB", peer_button.text)
        self.assertIn("↓2KB", peer_button.text)
        self.assertIn("08.11 12:34", peer_button.text)

    def test_device_card_uses_snapshot_traffic_handshake_and_activity(self):
        bot.users_db.conn.execute(
            "UPDATE peers SET created_at = ? WHERE name = ?",
            ("2025-04-03 07:25:00", "phone"),
        )
        bot.users_db.conn.commit()
        snapshot = {
            "id": "client-1",
            "name": "phone",
            "enabled": True,
            "latestHandshakeAt": "2026-11-08T12:34:56Z",
            "transferRx": 1000,
            "transferTx": 2000,
        }
        bot.users_db.update_peer_snapshots([snapshot])
        api_detail = {
            "id": "client-1",
            "name": "phone",
            "enabled": True,
            "ipv4Address": "10.10.0.23",
            "latestHandshakeAt": None,
            "transferRx": 0,
            "transferTx": 0,
        }
        with patch.object(bot.wg, "get_client", return_value=api_detail):
            inactive_text, _ = bot._peer_info_view("client-1", admin=False)

        self.assertIn("<b>Owner:</b> @alice", inactive_text)
        self.assertIn("<b>Status:</b> 🟢 Enabled", inactive_text)
        self.assertIn("<b>Activity:</b> 🔴 Inactive", inactive_text)
        self.assertIn("<b>Created:</b> 03.04.2025 07:25", inactive_text)
        self.assertIn("<b>Last handshake:</b> 08.11 12:34", inactive_text)
        self.assertIn("<b>Traffic total:</b> ↑ Upload 1.00 KB · ↓ Download 2.00 KB", inactive_text)

        snapshot["transferRx"] = 1500
        bot.users_db.update_peer_snapshots([snapshot])
        with patch.object(bot.wg, "get_client", return_value=api_detail):
            active_text, _ = bot._peer_info_view("client-1", admin=False)
        self.assertIn("<b>Activity:</b> 🟢 Active", active_text)
        self.assertIn("↑ Upload 1.50 KB", active_text)

        api_detail["enabled"] = False
        with patch.object(bot.wg, "get_client", return_value=api_detail):
            disabled_active_text, _ = bot._peer_info_view("client-1", admin=False)
        self.assertIn("<b>Status:</b> 🔴 Disabled", disabled_active_text)
        self.assertIn("<b>Activity:</b> 🟢 Active", disabled_active_text)

    def test_repeated_device_filters_ignore_message_not_modified(self):
        for filter_name in ("all", "active", "enabled", "disabled"):
            query = _NotModifiedQuery(f"dev:{filter_name}:0")
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=101, username="alice"),
                callback_query=query,
            )
            asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
            self.assertEqual(query.edit_attempts, 1, filter_name)

    def test_repeated_user_filters_ignore_message_not_modified(self):
        bot.users_db.add_user("admin", tg_id=303, role="admin", limit=-1)
        for filter_name in ("all", "enabled", "disabled"):
            query = _NotModifiedQuery(f"usersview:{filter_name}:0")
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=303, username="admin"),
                callback_query=query,
            )
            asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
            self.assertEqual(query.edit_attempts, 1, filter_name)

    def test_repeated_request_history_filter_ignores_message_not_modified(self):
        rich_bot = _NotModifiedRichBot()
        asyncio.run(bot._show_request_history(rich_bot, 10, message_id=99))
        self.assertEqual(rich_bot.edit_attempts, 1)
        self.assertFalse(rich_bot.fallback_called)

    def test_command_and_reply_keyboard_cancel_are_silent(self):
        message = _CancellableMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=101, username="alice"),
            effective_message=message,
        )
        result = asyncio.run(bot.cancel_conv(update, None))
        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertTrue(message.deleted)
        self.assertEqual(message.replies, 0)

    def test_inline_cancel_deletes_message_without_cancelled_reply(self):
        message = _CancellableMessage()
        query = _CancellableQuery(message)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=101, username="alice"),
            callback_query=query,
        )
        result = asyncio.run(bot.cancel_from_button(update, None))
        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertTrue(query.answered)
        self.assertTrue(message.deleted)
        self.assertEqual(message.replies, 0)

    def test_disabled_user_can_silently_close_an_existing_message(self):
        message = _CancellableMessage()
        query = _CancellableQuery(message, data="cancel")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=202, username="bob"),
            callback_query=query,
        )
        asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
        self.assertTrue(message.deleted)
        self.assertEqual(message.replies, 0)


class _CancellableMessage:
    def __init__(self):
        self.deleted = False
        self.replies = 0

    async def delete(self):
        self.deleted = True

    async def reply_text(self, *args, **kwargs):
        self.replies += 1


class _CancellableQuery:
    def __init__(self, message, data=None):
        self.message = message
        self.data = data
        self.answered = False

    async def answer(self):
        self.answered = True


class _NotModifiedQuery:
    def __init__(self, data):
        self.data = data
        self.edit_attempts = 0

    async def answer(self):
        pass

    async def edit_message_text(self, *args, **kwargs):
        self.edit_attempts += 1
        raise RuntimeError("BadRequest: Message is not modified")


class _NotModifiedRichBot:
    def __init__(self):
        self.edit_attempts = 0
        self.fallback_called = False

    async def _post(self, method, data):
        self.edit_attempts += 1
        raise RuntimeError("BadRequest: Message is not modified")

    async def edit_message_text(self, *args, **kwargs):
        self.fallback_called = True


if __name__ == "__main__":
    unittest.main()
