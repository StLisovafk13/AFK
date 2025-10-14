"""Bulk rescan helper for VSCO profiles stored in the bot database."""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from profile_link_scanner import (
    DEFAULT_DB,
    ScanResult,
    collect_profile_media,
    connect_db,
    store_profile_media,
)


LOGGER = logging.getLogger("vsco.profile_rescan")


@dataclass(slots=True)
class ProfileEntry:
    """Small value object with data taken from the ``links`` table."""

    chat_id: int
    username: str
    profile_url: str


@dataclass(slots=True)
class RescanSummary:
    """Aggregated statistics for the rescan session."""

    processed: int = 0
    succeeded: int = 0
    total_added_items: int = 0

    def report(self) -> str:
        return (
            "Пересканировано профилей: "
            f"{self.processed} | Успешно обновлено: {self.succeeded} | "
            f"Добавлено новых медиа: {self.total_added_items}"
        )


def _load_profiles(db_path: Path, *, chat_id: int | None, limit: int | None) -> list[ProfileEntry]:
    """Read the ``links`` table and return profiles for rescan."""

    conn = connect_db(db_path)
    try:
        query = "SELECT chat_id, username, url FROM links"
        params: list[object] = []
        if chat_id is not None:
            query += " WHERE chat_id = ?"
            params.append(chat_id)
        query += " ORDER BY id DESC"
        if limit is not None and limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(query, params)
        entries = [
            ProfileEntry(chat_id=row[0], username=row[1], profile_url=row[2])
            for row in rows
            if row[2]
        ]
        return entries
    finally:
        conn.close()


async def _rescan_single(
    entry: ProfileEntry,
    *,
    db_path: Path,
    max_width: int,
    delay: float,
    target_count: int,
) -> ScanResult | None:
    try:
        media_urls = await collect_profile_media(
            entry.profile_url,
            max_width=max_width,
            delay=delay,
            target_count=target_count,
        )
    except Exception as exc:  # pragma: no cover - network failures
        LOGGER.error("Не удалось собрать медиа для %s: %s", entry.profile_url, exc)
        return None

    if not media_urls:
        LOGGER.warning("Нет медиа по ссылке %s", entry.profile_url)
        return None

    return store_profile_media(
        db_path,
        entry.chat_id,
        entry.username,
        entry.profile_url,
        media_urls,
        source="rescan",
    )


async def rescan_profiles(
    entries: Iterable[ProfileEntry],
    *,
    db_path: Path,
    concurrency: int,
    max_width: int,
    delay: float,
    target_count: int,
) -> RescanSummary:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _worker(entry: ProfileEntry) -> ScanResult | None:
        async with semaphore:
            LOGGER.info("Пересканируем профиль %s", entry.profile_url)
            return await _rescan_single(
                entry,
                db_path=db_path,
                max_width=max_width,
                delay=delay,
                target_count=target_count,
            )

    tasks = [asyncio.create_task(_worker(entry)) for entry in entries]
    summary = RescanSummary(processed=len(tasks))

    for task in asyncio.as_completed(tasks):
        result = await task
        if result is None:
            continue
        summary.succeeded += 1
        summary.total_added_items += result.added_items
        LOGGER.info(
            "Профиль %s: добавлено %d новых медиа", result.profile_url, result.added_items
        )

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Пересканировать все профили из таблицы links и добавить новые медиа ссылки"
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Путь к базе данных")
    parser.add_argument(
        "--chat-id",
        type=int,
        default=None,
        help="Ограничить пересканирование одним ID чата",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Максимальное количество профилей для пересканирования",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Количество параллельных задач",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=2048,
        help="Максимальная ширина изображений при апскейле",
    )
    parser.add_argument(
        "--target-count",
        type=int,
        default=0,
        help="Желаемое количество медиа для остановки Playwright",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Задержка между прокрутками Playwright (сек)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Включить подробные логи",
    )
    return parser.parse_args()


async def _async_main(args: argparse.Namespace) -> RescanSummary:
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)

    profiles = _load_profiles(args.db, chat_id=args.chat_id, limit=args.limit)
    if not profiles:
        LOGGER.warning("Подходящие профили не найдены")
        return RescanSummary()

    LOGGER.info("Всего профилей для пересканирования: %d", len(profiles))
    return await rescan_profiles(
        profiles,
        db_path=args.db,
        concurrency=args.concurrency,
        max_width=args.max_width,
        delay=args.delay,
        target_count=args.target_count,
    )


def main() -> None:
    args = parse_args()
    summary = asyncio.run(_async_main(args))
    print(summary.report())


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
