"""Bulk rescan helper for VSCO profiles stored in the bot database."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from profile_link_scanner import (
    DEFAULT_DB,
    ProfileMediaCollection,
    ScanResult,
    collect_profile_media,
    connect_db,
    store_profile_media,
    utc_now_iso,
)


LOGGER = logging.getLogger("vsco.profile_rescan")
DEFAULT_STATE_DB = Path("vsco_rescan_state.db")


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
    skipped_existing: int = 0

    def report(self) -> str:
        return (
            "Пересканировано профилей: "
            f"{self.processed} | Успешно обновлено: {self.succeeded} | "
            f"Добавлено новых медиа: {self.total_added_items} | "
            f"Пропущено по истории: {self.skipped_existing}"
        )


def connect_state_db(path: Path) -> sqlite3.Connection:
    """Create or open the rescan state database."""

    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scanned_profiles (
            profile_url TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL,
            status TEXT NOT NULL,
            added_items INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


def _load_processed_urls(state_conn: sqlite3.Connection) -> set[str]:
    rows = state_conn.execute("SELECT profile_url FROM scanned_profiles")
    return {row[0] for row in rows if row[0]}


def _mark_profile_scanned(
    state_conn: sqlite3.Connection,
    entry: ProfileEntry,
    *,
    status: str,
    added_items: int,
) -> None:
    state_conn.execute(
        """
        INSERT INTO scanned_profiles(profile_url, username, last_scanned_at, status, added_items)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(profile_url) DO UPDATE SET
            username=excluded.username,
            last_scanned_at=excluded.last_scanned_at,
            status=excluded.status,
            added_items=excluded.added_items
        """,
        (entry.profile_url, entry.username, utc_now_iso(), status, added_items),
    )
    state_conn.commit()


def _load_profiles(
    db_path: Path,
    *,
    chat_id: int | None,
    limit: int | None,
    state_conn: sqlite3.Connection,
    alphabetical: bool,
) -> tuple[list[ProfileEntry], int]:
    """Read the ``links`` table and return profiles for rescan.

    Returns a pair ``(entries, skipped_existing)`` where ``entries`` contains the
    profiles scheduled for scanning and ``skipped_existing`` tracks how many
    rows were ignored because they are already present in the state database.
    """

    conn = connect_db(db_path)
    try:
        query = "SELECT id, chat_id, username, url FROM links"
        params: list[object] = []
        if chat_id is not None:
            query += " WHERE chat_id = ?"
            params.append(chat_id)
        if alphabetical:
            query += " ORDER BY username COLLATE NOCASE ASC, id ASC"
        else:
            query += " ORDER BY id DESC"

        processed_urls = _load_processed_urls(state_conn)
        rows = conn.execute(query, params)
        entries: list[ProfileEntry] = []
        skipped_existing = 0
        seen_urls: set[str] = set()
        for row in rows:
            profile_url = row[3]
            if not profile_url:
                continue
            if profile_url in seen_urls:
                continue
            if profile_url in processed_urls:
                skipped_existing += 1
                continue

            entry = ProfileEntry(chat_id=row[1], username=row[2], profile_url=profile_url)
            entries.append(entry)
            seen_urls.add(profile_url)
            if limit is not None and limit > 0 and len(entries) >= limit:
                break

        return entries, skipped_existing
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
        collected = await collect_profile_media(
            entry.profile_url,
            max_width=max_width,
            delay=delay,
            target_count=target_count,
            include_details=True,
        )
    except Exception as exc:  # pragma: no cover - network failures
        LOGGER.error("Не удалось собрать медиа для %s: %s", entry.profile_url, exc)
        return None

    if isinstance(collected, ProfileMediaCollection):
        media_urls = collected.media_urls
        profile_tabs = collected.profile_tabs
        media_by_tab = collected.media_by_tab
        profile_description = collected.profile_description
    else:
        media_urls = collected
        profile_tabs = []
        media_by_tab = None
        profile_description = ""

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
        profile_tabs=profile_tabs,
        media_by_tab=media_by_tab,
        profile_description=profile_description,
    )


async def rescan_profiles(
    entries: Iterable[ProfileEntry],
    *,
    db_path: Path,
    concurrency: int,
    max_width: int,
    delay: float,
    target_count: int,
    state_conn: sqlite3.Connection,
) -> RescanSummary:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    state_lock = asyncio.Lock()

    async def _worker(entry: ProfileEntry) -> ScanResult | None:
        async with semaphore:
            LOGGER.info("Пересканируем профиль %s", entry.profile_url)
            try:
                result = await _rescan_single(
                    entry,
                    db_path=db_path,
                    max_width=max_width,
                    delay=delay,
                    target_count=target_count,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                LOGGER.exception("Неожиданная ошибка при пересканировании %s: %s", entry.profile_url, exc)
                result = None

            added_items = result.added_items if result else 0
            if result is None:
                status = "failed"
            elif added_items > 0:
                status = "updated"
            else:
                status = "no_media"

            async with state_lock:
                _mark_profile_scanned(
                    state_conn,
                    entry,
                    status=status,
                    added_items=added_items,
                )

            return result

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
        "--state-db",
        type=Path,
        default=DEFAULT_STATE_DB,
        help="Путь к базе состояния, чтобы не пересканировать одинаковые профили",
    )
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
        "--alphabetical",
        action="store_true",
        help="Сканировать профили в алфавитном порядке (по username)",
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

    state_conn = connect_state_db(args.state_db)
    LOGGER.info("База состояния пересканирования: %s", args.state_db)
    try:
        profiles, skipped_existing = _load_profiles(
            args.db,
            chat_id=args.chat_id,
            limit=args.limit,
            state_conn=state_conn,
            alphabetical=args.alphabetical,
        )
        if not profiles:
            LOGGER.warning("Подходящие профили не найдены")
            return RescanSummary(skipped_existing=skipped_existing)

        if skipped_existing:
            LOGGER.info(
                "Пропущено профилей, которые уже были в базе состояния: %d",
                skipped_existing,
            )
        LOGGER.info("Всего профилей для пересканирования: %d", len(profiles))

        summary = await rescan_profiles(
            profiles,
            db_path=args.db,
            concurrency=args.concurrency,
            max_width=args.max_width,
            delay=args.delay,
            target_count=args.target_count,
            state_conn=state_conn,
        )
        summary.skipped_existing = skipped_existing
        return summary
    finally:
        state_conn.close()


def main() -> None:
    args = parse_args()
    summary = asyncio.run(_async_main(args))
    print(summary.report())


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
