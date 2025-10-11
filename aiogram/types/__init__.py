"""Simple aiogram types stubs for tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence


@dataclass
class MessageEntity:
    type: str
    offset: int
    length: int
    url: Optional[str] = None
    language: Optional[str] = None


@dataclass
class InlineKeyboardButton:
    text: str
    callback_data: Optional[str] = None
    url: Optional[str] = None


@dataclass
class InlineKeyboardMarkup:
    inline_keyboard: List[List[InlineKeyboardButton]]


@dataclass
class ReplyKeyboardMarkup:
    keyboard: List[List["KeyboardButton"]]
    resize_keyboard: bool = False


@dataclass
class KeyboardButton:
    text: str


@dataclass
class BufferedInputFile:
    data: bytes
    filename: str


@dataclass
class FSInputFile:
    path: Path | str
    filename: Optional[str] = None


@dataclass
class User:
    id: int
    username: Optional[str] = None


@dataclass
class Chat:
    id: int
    type: str = "private"


class Message:
    """Very small async-friendly message stub."""

    def __init__(self, chat: Optional[Chat] = None, from_user: Optional[User] = None) -> None:
        self.chat = chat or Chat(0)
        self.from_user = from_user or User(0)
        self.text: Optional[str] = None
        self.entities: Optional[Sequence[MessageEntity]] = None
        self.caption: Optional[str] = None
        self.document: Any = None
        self.answer_calls: list[tuple[Any, dict[str, Any]]] = []
        self.edit_text_calls: list[tuple[Any, dict[str, Any]]] = []
        self.reply_markup: Any = None
        self.bot: Any = None

    async def answer(self, text: Any, **kwargs: Any) -> None:
        self.answer_calls.append((text, kwargs))

    async def edit_text(self, text: Any, **kwargs: Any) -> None:
        self.edit_text_calls.append((text, kwargs))


@dataclass
class CallbackQuery:
    data: Optional[str]
    message: Optional[Message]
    from_user: User

    async def answer(self, text: str = "", *, show_alert: bool = False, cache_time: int | None = None) -> None:
        return None


__all__ = [
    "MessageEntity",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "ReplyKeyboardMarkup",
    "KeyboardButton",
    "BufferedInputFile",
    "FSInputFile",
    "User",
    "Chat",
    "Message",
    "CallbackQuery",
]
