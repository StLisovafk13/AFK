import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from startup_broadcast import StartupBroadcast


class StartupBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "bot.db"
        with self.connect() as conn:
            for table in ("links", "items", "comments"):
                conn.execute(f"CREATE TABLE {table} (chat_id INTEGER)")
            conn.execute("INSERT INTO links VALUES (10), (10), (-100), (0)")
            conn.execute("INSERT INTO items VALUES (10), (20), (-200)")
            conn.execute("INSERT INTO comments VALUES (30)")
        self.broadcast = StartupBroadcast(self.connect, enabled=True, text="Ready <hello> & all")
        self.broadcast.initialize()
        self.bot = SimpleNamespace(send_message=AsyncMock())

    def connect(self):
        return sqlite3.connect(self.path)

    def message(self, chat_id=40, chat_type="private"):
        return Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=40, is_bot=False, first_name="Test"),
            text="/start",
        )

    def test_migration_is_idempotent_and_excludes_groups(self):
        self.broadcast.initialize()
        self.assertEqual(self.broadcast._recipients(), [10, 20, 30])

    async def test_private_chat_remembered_before_access_check_even_when_disabled(self):
        self.broadcast.enabled = False

        async def denied_handler(event, data):
            self.assertIn(40, self.broadcast._recipients())
            return False

        message = self.message()
        self.assertFalse(await self.broadcast(denied_handler, message, {}))
        await self.broadcast(denied_handler, message, {})
        reloaded = StartupBroadcast(self.connect, enabled=True, text="Restarted")
        self.assertEqual(reloaded._recipients(), [10, 20, 30, 40])

    async def test_group_message_is_not_remembered(self):
        handler = AsyncMock(return_value="handled")
        result = await self.broadcast(handler, self.message(-100, "supergroup"), {})
        self.assertEqual(result, "handled")
        self.assertEqual(self.broadcast._recipients(), [10, 20, 30])

    async def test_private_callback_is_remembered_but_inline_callback_is_not(self):
        handler = AsyncMock()
        user = User(id=40, is_bot=False, first_name="Test")
        callback = CallbackQuery(id="1", from_user=user, chat_instance="1", message=self.message(), data="menu:help")
        await self.broadcast(handler, callback, {})
        inline = CallbackQuery(id="2", from_user=user, chat_instance="2", inline_message_id="inline", data="menu:help")
        await self.broadcast(handler, inline, {})
        self.assertEqual(self.broadcast._recipients(), [10, 20, 30, 40])
        self.assertEqual(handler.await_count, 2)

    async def test_registry_failure_does_not_block_handler(self):
        handler = AsyncMock(return_value="handled")
        with patch.object(self.broadcast, "_remember", side_effect=sqlite3.OperationalError("busy")):
            with self.assertLogs("startup_broadcast", level="ERROR"):
                result = await self.broadcast(handler, self.message(), {})
        self.assertEqual(result, "handled")

    async def test_disabled_broadcast_does_not_load_or_send(self):
        self.broadcast.enabled = False
        with patch.object(self.broadcast, "_recipients") as recipients:
            await self.broadcast.send(self.bot)
        recipients.assert_not_called()
        self.bot.send_message.assert_not_awaited()

    async def test_empty_or_oversized_text_is_skipped(self):
        for text in ("", "x" * 4097, "😀" * 2049):
            self.broadcast.text = text
            with self.assertLogs("startup_broadcast", level="WARNING"):
                await self.broadcast.send(self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_each_private_chat_gets_plain_text_once(self):
        with patch("startup_broadcast.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.broadcast.send(self.bot)
        self.assertEqual([call.args[0] for call in self.bot.send_message.await_args_list], [10, 20, 30])
        for call in self.bot.send_message.await_args_list:
            self.assertEqual(call.args[1], "Ready <hello> & all")
            self.assertIsNone(call.kwargs["parse_mode"])
        self.assertEqual(sleep.await_count, 3)

    async def test_unreachable_chats_do_not_abort_broadcast(self):
        method = SendMessage(chat_id=10, text="Ready")
        self.bot.send_message.side_effect = [
            TelegramForbiddenError(method=method, message="blocked"),
            TelegramBadRequest(method=method, message="chat not found"),
            None,
        ]
        with patch("startup_broadcast.asyncio.sleep", new_callable=AsyncMock):
            with self.assertLogs("startup_broadcast", level="WARNING"):
                await self.broadcast.send(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_rate_limit_waits_and_retries_same_recipient(self):
        method = SendMessage(chat_id=10, text="Ready")
        self.bot.send_message.side_effect = [TelegramRetryAfter(method=method, message="wait", retry_after=7), None, None, None]
        with patch("startup_broadcast.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.broadcast.send(self.bot)
        self.assertEqual(sleep.await_args_list[0].args, (7,))
        self.assertEqual([call.args[0] for call in self.bot.send_message.await_args_list], [10, 10, 20, 30])

    async def test_repeated_rate_limit_is_bounded_and_honored_before_next_chat(self):
        method = SendMessage(chat_id=10, text="Ready")
        error = TelegramRetryAfter(method=method, message="wait", retry_after=7)
        self.bot.send_message.side_effect = [error, error, error, None, None]
        with patch("startup_broadcast.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertLogs("startup_broadcast", level="WARNING"):
                await self.broadcast.send(self.bot)
        self.assertEqual([call.args[0] for call in self.bot.send_message.await_args_list], [10, 10, 10, 20, 30])
        self.assertEqual([call.args for call in sleep.await_args_list[:3]], [(7,), (7,), (7,)])

    async def test_cancellation_is_not_swallowed(self):
        self.bot.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.broadcast.send(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)


if __name__ == "__main__":
    unittest.main()
