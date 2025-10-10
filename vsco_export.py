"""Export handlers and utilities for VSCO bot."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


class SessionLike(Protocol):
    """Protocol describing the subset of session behaviour used by exports."""

    dir: Path
    export_scope: str


@dataclass(slots=True)
class ExportDependencies:
    """A bundle of callbacks required to perform exports."""

    send_timeout: int
    ensure_user_has_access: Callable[[Message, Optional[int]], Awaitable[bool]]
    ensure_callback_access: Callable[[CallbackQuery], Awaitable[bool]]
    has_daily_data_access: Callable[[int, Optional[int]], Tuple[bool, str]]
    get_session: Callable[[int], SessionLike]
    fetch_gallery_users: Callable[[str, int], List[Dict[str, Any]]]
    fetch_items_for_map: Callable[[str, int], List[Dict[str, Any]]]
    build_rich_gallery: Callable[[List[Dict[str, Any]], str, str], str]
    build_map_users: Callable[[List[Dict[str, Any]], str], str]
    build_map_images: Callable[[List[Dict[str, Any]], str], str]


def _kb_struct(kb: InlineKeyboardMarkup | None) -> Optional[Tuple[Tuple[Tuple[str, Optional[str], Optional[str]], ...], ...]]:
    """Normalize keyboard for safe equality check."""

    if kb is None:
        return None
    return tuple(
        tuple(
            (
                btn.text,
                getattr(btn, "callback_data", None),
                getattr(btn, "url", None),
            )
            for btn in row
        )
        for row in kb.inline_keyboard
    )


class ExportManager:
    """Encapsulates all export related handlers."""

    def __init__(self, deps: ExportDependencies) -> None:
        self._deps = deps
        self.router = Router()
        self.router.message(Command("export"))(self._cmd_export_handler)
        self.router.callback_query(F.data.startswith("export:"))(self.on_export_click)

    def build_scope_keyboard(self, session: SessionLike) -> InlineKeyboardMarkup:
        scope_row = [
            InlineKeyboardButton(
                text=("✅ 📌 Текущий чат" if session.export_scope == "chat" else "📌 Текущий чат"),
                callback_data="export:scope:chat",
            ),
            InlineKeyboardButton(
                text=("✅ 🌐 Вся база" if session.export_scope == "all" else "🌐 Вся база"),
                callback_data="export:scope:all",
            ),
        ]
        types_row = [
            InlineKeyboardButton(text="📄 CSV", callback_data="export:format:csv"),
            InlineKeyboardButton(text="🖼️ Галерея", callback_data="export:format:gallery"),
        ]
        maps_row = [
            InlineKeyboardButton(text="🗺️ Карта (польз.)", callback_data="export:format:map_users"),
            InlineKeyboardButton(text="🗺️ Карта (фото)", callback_data="export:format:map_images"),
        ]
        return InlineKeyboardMarkup(inline_keyboard=[scope_row, types_row, maps_row])

    async def open_menu(self, msg: Message, user_id: Optional[int] = None) -> None:
        if not await self._deps.ensure_user_has_access(msg, user_id=user_id):
            return

        if msg.chat.type in ("group", "supergroup"):
            await msg.answer("🚫 Экспорт доступен только в личных сообщениях. Напишите мне в ЛС.")
            return

        allowed, info = self._deps.has_daily_data_access(msg.chat.id, getattr(msg.from_user, "id", None))
        if not allowed:
            await msg.answer(info)
            return

        session = self._deps.get_session(msg.chat.id)
        await msg.answer(
            "Экспорт VSCO:\n• CSV / Галерея\n• Карта: по пользователям или по фото",
            reply_markup=self.build_scope_keyboard(session),
        )

    async def _cmd_export_handler(self, msg: Message) -> None:
        await self.open_menu(msg, user_id=getattr(msg.from_user, "id", None))

    async def on_export_click(self, cq: CallbackQuery) -> None:
        if not await self._deps.ensure_callback_access(cq):
            return

        message = cq.message
        if message is None:
            await cq.answer("Не могу обработать запрос", show_alert=True)
            return

        chat_id = message.chat.id
        if message.chat.type in ("group", "supergroup"):
            await cq.answer("Экспорт доступен только в личных сообщениях. Напишите мне в ЛС.", show_alert=True)
            return

        allowed, info = self._deps.has_daily_data_access(chat_id, getattr(cq.from_user, "id", None))
        if not allowed:
            await cq.answer(info, show_alert=True)
            try:
                await message.answer(info)
            except Exception:
                pass
            return

        session = self._deps.get_session(chat_id)
        parts = (cq.data or "").split(":")
        if len(parts) >= 3 and parts[1] == "scope":
            scope = parts[2]
            if scope in ("chat", "all"):
                session.export_scope = scope
                new_kb = self.build_scope_keyboard(session)
                if _kb_struct(message.reply_markup) != _kb_struct(new_kb):
                    try:
                        await message.edit_reply_markup(reply_markup=new_kb)
                    except TelegramBadRequest as err:
                        if "message is not modified" not in str(err).lower():
                            raise
                await cq.answer("Область обновлена")
            else:
                await cq.answer("Неизвестная область", show_alert=True)
            return

        if len(parts) >= 3 and parts[1] == "format":
            fmt = parts[2]
            if fmt == "csv":
                await self._export_csv(cq, session, chat_id)
                return
            if fmt == "gallery":
                await self._export_gallery(cq, session, chat_id)
                return
            if fmt in ("map_users", "map", "map_images"):
                await self._export_map(cq, session, chat_id, fmt)
                return
            await cq.answer("Неизвестный формат", show_alert=True)
            return

        await cq.answer("Неизвестное действие", show_alert=True)

    async def _export_csv(self, cq: CallbackQuery, session: SessionLike, chat_id: int) -> None:
        users = self._deps.fetch_gallery_users(session.export_scope, chat_id)
        if not users:
            await cq.answer("Нет данных", show_alert=True)
            return

        await cq.answer("Готовлю экспорт…", cache_time=0)
        flat_rows = [
            {
                "username": u["username"],
                "profile_url": u["profile_url"],
                "lat": u["lat"],
                "lon": u["lon"],
                "images_count": u["images_count"],
                "comments_count": u["comments_count"],
                "comments": " | ".join(u["comments"]),
                "added_by": u.get("added_by_raw", ""),
                "added_by_display": u.get("added_by", ""),
                "added_by_link": u.get("added_by_link", ""),
            }
            for u in users
        ]
        output = session.dir / f"export_{session.export_scope}.csv"
        fieldnames = list(flat_rows[0].keys()) if flat_rows else []
        with output.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in flat_rows:
                writer.writerow(row)

        await cq.message.answer_document(
            BufferedInputFile(output.read_bytes(), filename=output.name),
            caption=f"CSV ({'вся база' if session.export_scope == 'all' else 'текущий чат'})",
        )

    async def _export_gallery(self, cq: CallbackQuery, session: SessionLike, chat_id: int) -> None:
        users = self._deps.fetch_gallery_users(session.export_scope, chat_id)
        if not users:
            await cq.answer("Нет данных", show_alert=True)
            return

        await cq.answer("Готовлю экспорт…", cache_time=0)
        html = self._deps.build_rich_gallery(
            users,
            title="VSCO Gallery",
            subtitle=("All DB" if session.export_scope == "all" else "Current Chat"),
        )
        output = session.dir / f"export_gallery_{session.export_scope}.html"
        output.write_text(html, encoding="utf-8")
        await cq.message.answer_document(
            FSInputFile(output),
            caption=f"Галерея ({'вся база' if session.export_scope == 'all' else 'текущий чат'})",
            request_timeout=self._deps.send_timeout,
        )

    async def _export_map(self, cq: CallbackQuery, session: SessionLike, chat_id: int, fmt: str) -> None:
        if fmt in ("map", "map_users"):
            users = self._deps.fetch_gallery_users(session.export_scope, chat_id)
            if not users:
                await cq.answer("Нет данных", show_alert=True)
                return
            await cq.answer("Готовлю экспорт…", cache_time=0)
            html = self._deps.build_map_users(
                users,
                title=f"VSCO Profiles — {'Users' if fmt != 'map_images' else 'Images'}",
            )
            output = session.dir / f"export_map_users_{session.export_scope}.html"
        else:
            items = self._deps.fetch_items_for_map(session.export_scope, chat_id)
            if not items:
                await cq.answer("Нет данных", show_alert=True)
                return
            await cq.answer("Готовлю экспорт…", cache_time=0)
            html = self._deps.build_map_images(items, title="VSCO Profiles — Images")
            output = session.dir / f"export_map_images_{session.export_scope}.html"

        output.write_text(html, encoding="utf-8")
        await cq.message.answer_document(
            FSInputFile(output),
            caption=f"Карта ({'вся база' if session.export_scope == 'all' else 'текущий чат'})",
            request_timeout=self._deps.send_timeout,
        )
