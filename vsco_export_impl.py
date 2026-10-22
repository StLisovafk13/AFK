"""Export handlers and utilities for VSCO bot."""
from __future__ import annotations

import csv
import itertools
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Protocol, Tuple
from zipfile import ZIP_DEFLATED, ZipFile

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
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
    fetch_gallery_users: Callable[[str, int], Iterable[Dict[str, Any]]]
    fetch_items_for_map: Callable[[str, int], List[Dict[str, Any]]]
    build_rich_gallery: Callable[[List[Dict[str, Any]], str, str], str]
    build_map_users: Callable[[List[Dict[str, Any]], str], str]
    build_map_images: Callable[[List[Dict[str, Any]], str], str]
    mirror_export: Optional[
        Callable[
            [
                Path,
                str,
                int,
                str,
                str,
                bool,
                Optional[str],
                Optional[User],
            ],
            Awaitable[None],
        ]
    ] = None


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
        self._zip_trigger_size = int(os.environ.get("VSCO_EXPORT_ZIP_THRESHOLD", 45 * 1024 * 1024))
        self._telegram_limit = 50 * 1024 * 1024
        self.router = Router()
        self.router.message(Command("export"))(self._cmd_export_handler)
        self.router.callback_query(F.data.startswith("export:"))(self.on_export_click)
        self._log = logging.getLogger(__name__)

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
        city_row = [
            InlineKeyboardButton(text="🏙️ Город", callback_data="export:city"),
        ]
        return InlineKeyboardMarkup(
            inline_keyboard=[scope_row, types_row, maps_row, city_row]
        )

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
            "Экспорт VSCO:\n• CSV / Галерея\n• Карта: по пользователям или по фото\n• Отчёт по городу (галерея + карта)",
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

        if len(parts) >= 2 and parts[1] == "city":
            session.pending_action = "city_export"  # type: ignore[attr-defined]
            scope_label = "текущего чата" if session.export_scope == "chat" else "всей базы"
            await cq.answer("Введите город", cache_time=0)
            try:
                await message.answer(
                    "🏙️ Отправьте название города."
                    f" Используется область экспорта: {scope_label}."
                )
            except Exception:
                pass
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
        users_iter = iter(self._deps.fetch_gallery_users(session.export_scope, chat_id))
        try:
            first_user = next(users_iter)
        except StopIteration:
            await cq.answer("Нет данных", show_alert=True)
            return

        await cq.answer("Готовлю экспорт…", cache_time=0)
        output = session.dir / f"export_{session.export_scope}.csv"
        with output.open("w", encoding="utf-8", newline="") as fh:
            writer: Optional[csv.DictWriter[str]] = None
            for user in itertools.chain([first_user], users_iter):
                row = {
                    "username": user.get("username", ""),
                    "profile_url": user.get("profile_url", ""),
                    "lat": user.get("lat"),
                    "lon": user.get("lon"),
                    "images_count": user.get("images_count"),
                    "comments_count": user.get("comments_count"),
                    "comments": " | ".join(user.get("comments", [])),
                    "added_by": user.get("added_by_raw", ""),
                    "added_by_display": user.get("added_by", ""),
                    "added_by_link": user.get("added_by_link", ""),
                }
                if writer is None:
                    fieldnames = list(row.keys())
                    writer = csv.DictWriter(fh, fieldnames=fieldnames)
                    writer.writeheader()
                writer.writerow(row)

        await self._send_path_document(
            cq,
            output,
            base_caption=f"CSV ({'вся база' if session.export_scope == 'all' else 'текущий чат'})",
            allow_zip=False,
            chat_id=chat_id,
            export_format="csv",
            scope=session.export_scope,
        )

    async def _export_gallery(self, cq: CallbackQuery, session: SessionLike, chat_id: int) -> None:
        iterator = iter(self._deps.fetch_gallery_users(session.export_scope, chat_id))
        try:
            first_user = next(iterator)
        except StopIteration:
            await cq.answer("Нет данных", show_alert=True)
            return
        users = [first_user]
        users.extend(iterator)

        await cq.answer("Готовлю экспорт…", cache_time=0)
        html = self._deps.build_rich_gallery(
            users,
            title="VSCOLeak",
            subtitle=("All DB" if session.export_scope == "all" else "Current Chat"),
        )
        output = session.dir / f"export_gallery_{session.export_scope}.html"
        output.write_text(html, encoding="utf-8")
        await self._send_path_document(
            cq,
            output,
            base_caption=f"Галерея ({'вся база' if session.export_scope == 'all' else 'текущий чат'})",
            allow_zip=True,
            chat_id=chat_id,
            export_format="gallery",
            scope=session.export_scope,
        )

    async def _export_map(self, cq: CallbackQuery, session: SessionLike, chat_id: int, fmt: str) -> None:
        if fmt in ("map", "map_users"):
            iterator = iter(self._deps.fetch_gallery_users(session.export_scope, chat_id))
            try:
                first_user = next(iterator)
            except StopIteration:
                await cq.answer("Нет данных", show_alert=True)
                return
            users = [first_user]
            users.extend(iterator)
            await cq.answer("Готовлю экспорт…", cache_time=0)
            html = self._deps.build_map_users(
                users,
                title=f"VSCO Profiles — {'Users' if fmt != 'map_images' else 'Images'}",
            )
            output = session.dir / f"export_map_users_{session.export_scope}.html"
            export_format = "map_users"
        else:
            items = self._deps.fetch_items_for_map(session.export_scope, chat_id)
            if not items:
                await cq.answer("Нет данных", show_alert=True)
                return
            await cq.answer("Готовлю экспорт…", cache_time=0)
            html = self._deps.build_map_images(items, title="VSCO Profiles — Images")
            output = session.dir / f"export_map_images_{session.export_scope}.html"
            export_format = "map_images"

        output.write_text(html, encoding="utf-8")
        await self._send_path_document(
            cq,
            output,
            base_caption=f"Карта ({'вся база' if session.export_scope == 'all' else 'текущий чат'})",
            allow_zip=(export_format == "map_images"),
            chat_id=chat_id,
            export_format=export_format,
            scope=session.export_scope,
        )

    async def _send_path_document(
        self,
        cq: CallbackQuery,
        path: Path,
        *,
        base_caption: str,
        allow_zip: bool,
        chat_id: int,
        export_format: str,
        scope: str,
    ) -> None:
        """Send a file from disk, compressing if it exceeds Telegram limits."""

        prepared, zipped = self._prepare_document_for_sending(path, allow_zip=allow_zip)
        if prepared is None:
            warning = (
                "Файл экспорта слишком большой для отправки через Telegram. "
                "Попробуйте сузить область или отфильтруйте данные."
            )
            await cq.answer(warning, show_alert=True)
            try:
                await cq.message.answer(warning)
            except Exception:
                pass
            return

        caption = base_caption + (" (ZIP)" if zipped else "")
        await cq.message.answer_document(
            FSInputFile(prepared),
            caption=caption,
            request_timeout=self._deps.send_timeout,
        )
        chat = cq.message.chat if cq.message else None
        chat_title = None
        if chat is not None:
            chat_title = getattr(chat, "title", None) or getattr(chat, "full_name", None)
        await self._mirror_export(
            prepared,
            caption=caption,
            chat_id=chat_id,
            scope=scope,
            export_format=export_format,
            zipped=zipped,
            chat_title=chat_title,
            user=cq.from_user,
        )

    def _prepare_document_for_sending(
        self, path: Path, *, allow_zip: bool
    ) -> Tuple[Optional[Path], bool]:
        """Ensure that a file fits into Telegram limits, optionally zipping it."""

        size = path.stat().st_size
        if size < self._telegram_limit and (size <= self._zip_trigger_size or not allow_zip):
            return path, False

        if size >= self._telegram_limit and not allow_zip:
            return None, False

        if not allow_zip:
            return path if size < self._telegram_limit else None, False

        zipped_path = path.with_suffix(path.suffix + ".zip")
        if zipped_path.exists():
            zipped_path.unlink()
        with ZipFile(zipped_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(path, arcname=path.name)

        zipped_size = zipped_path.stat().st_size
        if zipped_size >= self._telegram_limit:
            return None, False

        return zipped_path, True

    async def _mirror_export(
        self,
        prepared: Path,
        *,
        caption: str,
        chat_id: int,
        scope: str,
        export_format: str,
        zipped: bool,
        chat_title: Optional[str],
        user: Optional[User],
    ) -> None:
        mirror = self._deps.mirror_export
        if mirror is None:
            return
        try:
            await mirror(
                prepared,
                caption,
                chat_id,
                scope,
                export_format,
                zipped,
                chat_title,
                user,
            )
        except Exception:
            self._log.exception("Failed to mirror export for chat %s", chat_id)
