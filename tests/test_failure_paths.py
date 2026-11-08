"""Regression coverage for device ownership and request decisions."""

import asyncio
from io import BytesIO
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_bot_views  # noqa: F401 - installs Telegram stubs for isolated tests

import bot
from users import UserDB
from wgapi import WGEasyAPI, REQUEST_TIMEOUT


class FailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = bot.users_db
        bot.users_db = UserDB(str(Path(self.temp_dir.name) / "users.db"))
        bot.users_db.add_user("alice", tg_id=101)

    def tearDown(self) -> None:
        bot.users_db.conn.close()
        bot.users_db = self.original_db
        self.temp_dir.cleanup()

    def test_deletion_keeps_user_and_records_after_midway_failure(self) -> None:
        bot.users_db.add_peer("first", "alice", "1")
        bot.users_db.add_peer("second", "alice", "2")
        with patch.object(bot.wg, "list_clients", return_value=[
            {"name": "first", "id": "1"}, {"name": "second", "id": "2"},
        ]), patch.object(bot.wg, "delete_client", side_effect=[None, RuntimeError("offline")]):
            result = asyncio.run(bot._delete_user_cascade("alice"))
        self.assertIn("Deletion stopped after 1", result)
        self.assertIsNotNone(bot.users_db.get_user("alice"))
        self.assertEqual(bot.users_db.peers_of("alice"), ["second"])

    def test_deletion_keeps_records_when_listing_fails(self) -> None:
        bot.users_db.add_peer("first", "alice", "1")
        with patch.object(bot.wg, "list_clients", side_effect=TimeoutError("offline")):
            asyncio.run(bot._delete_user_cascade("alice"))
        self.assertIsNotNone(bot.users_db.get_user("alice"))
        self.assertEqual(bot.users_db.peers_of("alice"), ["first"])

    def test_failed_persistence_removes_created_device(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private"))
        with patch.object(bot.wg, "list_clients", return_value=[]), patch.object(
            bot.wg, "create_client", return_value={"clientId": "1"}
        ), patch.object(bot.users_db, "add_peer", side_effect=sqlite3.OperationalError("disk full")), patch.object(
            bot.wg, "delete_client"
        ) as deleted:
            asyncio.run(bot._create_peer_for(update, "alice", "phone"))
        deleted.assert_called_once_with("1")
        message.reply_document.assert_not_called()
        self.assertIn("failed", message.reply_text.call_args.args[0])

    def test_custom_name_is_not_renamed(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private"))
        with patch.object(bot.wg, "list_clients", return_value=[]), patch.object(
            bot.wg, "create_client", return_value={"clientId": "1"}
        ), patch.object(bot.wg, "rename_client") as rename, patch.object(
            bot.wg, "get_client_config", return_value=(b"config", "phone.conf")
        ):
            asyncio.run(bot._create_peer_for(update, "alice", "phone"))
        rename.assert_not_called()
        self.assertEqual(bot.users_db.peer_owner("phone"), "alice")
        message.reply_document.assert_awaited_once()

    def test_config_callback_fetches_owned_device_only_once(self) -> None:
        bot.users_db.add_peer("phone", "alice", "1")
        query = SimpleNamespace(data="cfg:1", answer=AsyncMock(), edit_message_text=AsyncMock(), message=SimpleNamespace())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=101, username="alice"))
        with patch.object(bot.wg, "get_client", return_value={"id": "1", "name": "phone"}) as get_client, patch.object(
            bot.wg, "get_client_config", return_value=(b"secret", "phone.conf")
        ) as get_config, patch.object(bot, "_send_private_config", new_callable=AsyncMock) as send_config:
            asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
        get_client.assert_called_once_with("1")
        get_config.assert_called_once_with("1")
        send_config.assert_awaited_once()

    def test_config_callback_rejects_non_owner_before_download(self) -> None:
        bot.users_db.add_peer("phone", "alice", "1")
        bot.users_db.add_user("bob", tg_id=202)
        query = SimpleNamespace(data="cfg:1", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=202, username="bob"))
        with patch.object(bot.wg, "get_client", return_value={"id": "1", "name": "phone"}), patch.object(
            bot.wg, "get_client_config"
        ) as get_config:
            asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
        get_config.assert_not_called()
        query.edit_message_text.assert_awaited_once_with("You can access only your own configurations.")

    def test_device_info_callback_missing_device_uses_back_navigation(self) -> None:
        query = SimpleNamespace(data="pinfo:missing", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=101, username="alice"))
        with patch.object(bot.wg, "get_client", side_effect=LookupError("missing")) as get_client:
            asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
        get_client.assert_called_once_with("missing")
        self.assertEqual(query.edit_message_text.call_args.args, ("Device not found.",))
        self.assertIn("reply_markup", query.edit_message_text.call_args.kwargs)

    def test_device_info_callback_rejects_non_owner(self) -> None:
        bot.users_db.add_peer("phone", "alice", "1")
        bot.users_db.add_user("bob", tg_id=202)
        query = SimpleNamespace(data="pinfo:1", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=202, username="bob"))
        with patch.object(bot.wg, "get_client", return_value={"id": "1", "name": "phone"}) as get_client:
            asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
        get_client.assert_called_once_with("1")
        query.edit_message_text.assert_awaited_once_with("You can view only your own configurations.")

    def test_user_callbacks_require_admin_before_mutation(self) -> None:
        query = SimpleNamespace(data="userdisable:alice", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=101, username="alice"))
        with patch.object(bot.users_db, "disable_user") as disable:
            asyncio.run(bot.on_callback(update, SimpleNamespace(user_data={})))
        disable.assert_not_called()
        query.edit_message_text.assert_awaited_once_with("Admins only.")

    def test_approval_is_atomic_and_cannot_be_repeated(self) -> None:
        req_id = bot.users_db.add_request(101, "alice", None, None, "limit", 5)
        updated = bot.users_db.approve_request(req_id, 1, "admin")
        self.assertEqual(updated.config_limit, 5)
        self.assertEqual(bot.users_db.get_request(req_id)["status"], "approved")
        self.assertIsNone(bot.users_db.approve_request(req_id, 2, "other"))

    def test_failed_limit_approval_remains_pending(self) -> None:
        req_id = bot.users_db.add_request(101, "alice", None, None, "limit", 5)
        bot.users_db.remove_user("alice")
        with self.assertRaises(KeyError):
            bot.users_db.approve_request(req_id, 1, "admin")
        self.assertEqual(bot.users_db.get_request(req_id)["status"], "pending")

    def test_request_callback_applies_limit_and_notifies(self) -> None:
        admin = bot.users_db.add_user("admin", tg_id=999, role="admin")
        req_id = bot.users_db.add_request(101, "alice", None, None, "limit", 5)
        query = SimpleNamespace(edit_message_text=AsyncMock())
        telegram_bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "_edit_request_copies", new_callable=AsyncMock):
            asyncio.run(bot._handle_request_decision(
                query, SimpleNamespace(bot=telegram_bot), admin, "approve", req_id, None,
            ))
        self.assertEqual(bot.users_db.get_user("alice").config_limit, 5)
        self.assertEqual(bot.users_db.get_request(req_id)["status"], "approved")
        telegram_bot.send_message.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()

    def test_request_callback_declines_without_registering(self) -> None:
        admin = bot.users_db.add_user("admin", tg_id=999, role="admin")
        req_id = bot.users_db.add_request(202, "newuser", None, None)
        query = SimpleNamespace(edit_message_text=AsyncMock())
        with patch.object(bot, "_edit_request_copies", new_callable=AsyncMock):
            asyncio.run(bot._handle_request_decision(
                query, SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())),
                admin, "decline", req_id, None,
            ))
        self.assertEqual(bot.users_db.get_request(req_id)["status"], "declined")
        self.assertIsNone(bot.users_db.get_user("newuser"))

    def test_conflicting_access_approval_remains_pending(self) -> None:
        req_id = bot.users_db.add_request(202, "alice", None, None)
        with self.assertRaises(sqlite3.IntegrityError):
            bot.users_db.approve_request(req_id, 1, "admin")
        self.assertEqual(bot.users_db.get_request(req_id)["status"], "pending")

    def test_username_change_preserves_ownership(self) -> None:
        bot.users_db.add_peer("phone", "alice", "1")
        user = bot.users_db.resolve(101, "newalice")
        self.assertEqual(user.username, "newalice")
        self.assertEqual(bot.users_db.peer_owner("phone"), "newalice")

    def test_initial_admin_claim_binds_telegram_id(self) -> None:
        bot.users_db.set_bootstrap_sets(["admin"], set())
        self.assertIsNone(bot.users_db.get_user("admin"))
        self.assertEqual(bot.users_db.resolve(999, "admin").role, "admin")
        self.assertEqual(bot.users_db.get_user("admin").tg_id, 999)
        self.assertIsNone(bot.users_db.resolve(123, "admin"))

    def test_admin_allowlist_remains_active_after_restart(self) -> None:
        path = bot.users_db.path
        bot.users_db.set_bootstrap_sets(["first", "second"], set())
        self.assertEqual(bot.users_db.resolve(111, "first").role, "admin")
        bot.users_db.conn.close()
        bot.users_db = UserDB(path)
        bot.users_db.set_bootstrap_sets(["first", "second"], set())
        self.assertEqual(bot.users_db.resolve(222, "second").role, "admin")
        self.assertEqual(bot.users_db.resolve(111, "first").tg_id, 111)

    def test_existing_username_is_never_promoted_or_rebound(self) -> None:
        bot.users_db.add_user("existing", tg_id=999)
        bot.users_db.add_user("unbound")
        bot.users_db.set_bootstrap_sets(["existing", "unbound"], set())
        self.assertEqual(bot.users_db.resolve(999, "existing").role, "user")
        self.assertIsNone(bot.users_db.resolve(123, "existing"))
        self.assertEqual(bot.users_db.get_user("existing").tg_id, 999)
        self.assertEqual(bot.users_db.resolve(222, "unbound").role, "user")
        self.assertEqual(bot.users_db.get_user("unbound").tg_id, 222)

    def test_former_admin_username_cannot_grant_access_again(self) -> None:
        path = bot.users_db.path
        bot.users_db.set_bootstrap_sets(["original"], set())
        self.assertEqual(bot.users_db.resolve(999, "original").role, "admin")
        self.assertEqual(bot.users_db.resolve(999, "renamed").role, "admin")
        self.assertIsNone(bot.users_db.resolve(123, "original"))
        bot.users_db.conn.close()
        bot.users_db = UserDB(path)
        bot.users_db.set_bootstrap_sets(["original"], set())
        self.assertIsNone(bot.users_db.resolve(123, "original"))
        self.assertEqual(bot.users_db.resolve(999, "renamed").role, "admin")

    def test_promoted_admin_must_have_bound_id(self) -> None:
        bot.users_db.add_user("pending")
        with self.assertRaisesRegex(ValueError, "start the bot"):
            bot.users_db.set_role("pending", "admin")
        bot.users_db.resolve(222, "pending")
        self.assertEqual(bot.users_db.set_role("pending", "admin").tg_id, 222)

    def test_adding_admin_env_name_does_not_promote_bound_user(self) -> None:
        bot.users_db.set_bootstrap_sets([], set())
        bot.users_db.set_bootstrap_sets(["alice"], set())
        self.assertEqual(bot.users_db.get_user("alice").role, "user")
        self.assertEqual(bot.users_db.get_user("alice").tg_id, 101)
        bot.users_db.set_bootstrap_sets(["another"], set())
        self.assertEqual(bot.users_db.resolve(333, "another").role, "admin")

    def test_invalid_limit_retains_conversation_target(self) -> None:
        bot.users_db.add_user("admin", tg_id=999, role="admin")
        message = SimpleNamespace(text="-2", reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=999, username="admin"), effective_message=message, message=message)
        context = SimpleNamespace(user_data={"_ulimit_target": "alice"})
        self.assertEqual(asyncio.run(bot.userlimit_receive(update, context)), bot.WAITING_FOR_USERLIMIT)
        self.assertEqual(context.user_data["_ulimit_target"], "alice")
        message.text = "4"
        self.assertEqual(asyncio.run(bot.userlimit_receive(update, context)), bot.ConversationHandler.END)
        self.assertEqual(bot.users_db.get_user("alice").config_limit, 4)
        self.assertNotIn("_ulimit_target", context.user_data)

    def test_group_config_delivered_to_requester_privately(self) -> None:
        private_bot = SimpleNamespace(send_document=AsyncMock())
        message = SimpleNamespace(get_bot=lambda: private_bot, reply_document=AsyncMock(), reply_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(type="supergroup"), effective_user=SimpleNamespace(id=101))
        asyncio.run(bot._send_private_config(update, message, BytesIO(b"secret"), "phone.conf", "config"))
        private_bot.send_document.assert_awaited_once()
        self.assertEqual(private_bot.send_document.call_args.kwargs["chat_id"], 101)
        message.reply_document.assert_not_called()


class APITests(unittest.TestCase):
    def test_https_cookie_flags_and_timeouts(self) -> None:
        with patch.dict(os.environ, {"WG_EASY_URL": "https://wg.example", "WG_EASY_LOGIN": "user", "WG_EASY_PASSWORD": "pass"}):
            api = WGEasyAPI()
        api.session.cookies.set("session", "value", secure=True, domain="wg.example")
        response = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
        with patch.object(api.session, "post", return_value=response) as login:
            api.login()
        self.assertTrue(next(iter(api.session.cookies)).secure)
        self.assertEqual(next(iter(api.session.cookies)).domain, "wg.example")
        self.assertEqual(login.call_args.kwargs["timeout"], REQUEST_TIMEOUT)
        with patch.object(api.session, "request", return_value=response) as request:
            api._request("GET", "/api/client")
        self.assertEqual(request.call_args.kwargs["timeout"], REQUEST_TIMEOUT)

    def test_http_requires_explicit_opt_in(self) -> None:
        with patch.dict(os.environ, {"WG_EASY_URL": "http://wg.example", "WG_EASY_LOGIN": "user", "WG_EASY_PASSWORD": "pass"}, clear=True):
            with self.assertRaises(ValueError):
                WGEasyAPI()

    def test_incomplete_pagination_is_error(self) -> None:
        with patch.dict(os.environ, {"WG_EASY_URL": "https://wg.example", "WG_EASY_LOGIN": "user", "WG_EASY_PASSWORD": "pass"}):
            api = WGEasyAPI()
        with patch.object(api, "_request", return_value=SimpleNamespace(json=lambda: {"clients": [{}] * 100, "total": 5001})):
            with self.assertRaisesRegex(ValueError, "50-page"):
                api.list_clients()

    def test_configuration_filename_rejects_path(self) -> None:
        with patch.dict(os.environ, {"WG_EASY_URL": "https://wg.example", "WG_EASY_LOGIN": "user", "WG_EASY_PASSWORD": "pass"}):
            api = WGEasyAPI()
        response = SimpleNamespace(content=b"config", headers={"Content-Disposition": 'attachment; filename="../../private.conf"'})
        with patch.object(api, "_request", return_value=response):
            self.assertEqual(api.get_client_config("123")[1], "peer-123.conf")
