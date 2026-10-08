"""Remember private chats and send an optional announcement once per startup."""

import asyncio
import logging
import sqlite3
from typing import Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, Message


log = logging.getLogger(__name__)


class StartupBroadcast(BaseMiddleware):
    def __init__(self, db_connect: Callable[[], sqlite3.Connection], *, enabled: bool, text: str):
        self._db_connect = db_connect
        self.enabled = enabled
        self.text = text.strip()

    def initialize(self) -> None:
        """Called after the bot's existing database schema has been initialized."""
        conn = self._db_connect()
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS bot_private_chats (chat_id INTEGER PRIMARY KEY)")
                # Positive chat IDs in these tables identify previous private chats.
                # User IDs from group activity do not establish a private conversation.
                conn.execute("""
                    INSERT OR IGNORE INTO bot_private_chats (chat_id)
                    SELECT chat_id FROM links WHERE chat_id > 0
                    UNION SELECT chat_id FROM items WHERE chat_id > 0
                    UNION SELECT chat_id FROM comments WHERE chat_id > 0
                """)
        finally:
            conn.close()

    def _remember(self, chat_id: int) -> None:
        conn = self._db_connect()
        try:
            with conn:
                conn.execute("INSERT OR IGNORE INTO bot_private_chats (chat_id) VALUES (?)", (chat_id,))
        finally:
            conn.close()

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(event, (Message, CallbackQuery)) and message is not None:
            if message.chat.type == "private" and message.chat.id > 0:
                try:
                    await asyncio.to_thread(self._remember, message.chat.id)
                except Exception:
                    log.exception("Failed to remember private chat %s", message.chat.id)
        return await handler(event, data)

    def _recipients(self) -> list[int]:
        conn = self._db_connect()
        try:
            return [row[0] for row in conn.execute(
                "SELECT chat_id FROM bot_private_chats WHERE chat_id > 0 ORDER BY chat_id"
            )]
        finally:
            conn.close()

    async def send(self, bot: Bot) -> None:
        if not self.enabled:
            return
        if not self.text or len(self.text.encode("utf-16-le")) // 2 > 4096:
            log.warning("Startup broadcast skipped: text must contain 1–4096 Telegram characters")
            return
        try:
            recipients = await asyncio.to_thread(self._recipients)
        except Exception:
            log.exception("Startup broadcast skipped: cannot load private chats")
            return

        sent = 0
        failed = 0
        for chat_id in recipients:
            for attempt in range(3):
                try:
                    await bot.send_message(
                        chat_id,
                        self.text,
                        parse_mode=None,
                        disable_web_page_preview=True,
                    )
                    sent += 1
                    break
                except TelegramRetryAfter as error:
                    # Honor Telegram's pause before any further send, including
                    # the next recipient after exhausting this one's retries.
                    await asyncio.sleep(error.retry_after)
                    if attempt == 2:
                        failed += 1
                        log.warning("Startup broadcast rate limit persisted for chat %s", chat_id)
                except Exception as error:
                    # A blocked/deleted chat or a network error must not abort
                    # delivery to the remaining users or stop polling.
                    failed += 1
                    log.warning("Startup broadcast failed for chat %s (%s)", chat_id, type(error).__name__)
                    break
            await asyncio.sleep(0.05)
        log.info("Startup broadcast finished: sent=%s failed=%s", sent, failed)
