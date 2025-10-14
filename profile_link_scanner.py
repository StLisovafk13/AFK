"""VSCO profile link scanner.

This standalone helper replicates the link collection stage of the downloader
without saving any files. It resolves the public media URLs for a VSCO profile
and stores them in the bot database so that we can verify the behaviour before
integrating it into the bot workflow.
"""
from __future__ import annotations

"""Standalone helper to scan VSCO profiles and store media links in SQLite."""

import argparse
import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

import aiohttp

from vsco_utils import (
    dedupe_keep_order,
    is_media_url,
    is_vsco_logo_url,
    normalize_media_url,
    normalize_vsco_profile_url,
    playwright_scan_profile,
    upscale_w_param,
)


LOGGER = logging.getLogger("vsco.profile_scanner")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)
DEFAULT_DB = Path("vsco_links.db")
LOCAL_TZ = timezone(timedelta(hours=3))


@dataclass(slots=True)
class ScanResult:
    """Small summary that mirrors the downloader manifest ingest step."""

    username: str
    profile_url: str
    media_urls: list[str]
    added_items: int = 0
    link_added: bool = False


def utc_now_iso() -> str:
    """Return an ISO timestamp that matches the bot database format."""

    return datetime.now(LOCAL_TZ).isoformat()


def ensure_db_schema(conn: sqlite3.Connection) -> None:
    """Create the SQLite schema used by :mod:`vsco_bot` when missing."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL,
          username TEXT NOT NULL,
          url TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(chat_id, username)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL,
          username TEXT DEFAULT '',
          latitude REAL,
          longitude REAL,
          profile_url TEXT DEFAULT '',
          image_url TEXT DEFAULT '',
          source TEXT DEFAULT '',
          source_file TEXT DEFAULT '',
          added_by TEXT DEFAULT '',
          created_at TEXT NOT NULL,
          UNIQUE(chat_id, username, image_url, profile_url)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comments(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id INTEGER NOT NULL,
          chat_id INTEGER NOT NULL,
          comment TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(item_id, comment),
          FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "added_by" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN added_by TEXT DEFAULT ''")


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    ensure_db_schema(conn)
    return conn


def _extract_username_from_url(profile_url: str) -> str:
    parsed = normalize_vsco_profile_url(profile_url) or profile_url
    match = re.search(r"vsco\.co/([^/]+)/", parsed)
    if match:
        return match.group(1)
    match = re.search(r"vsco\.co/([^/?#]+)", parsed)
    if match:
        return match.group(1)
    return ""


def resolve_profile_inputs(username: str | None, profile_url: str | None) -> tuple[str, str]:
    """Normalise the CLI input and return ``(username, profile_url)``."""

    if profile_url:
        normalized = normalize_vsco_profile_url(profile_url)
        if not normalized:
            raise ValueError(f"Не удалось распознать ссылку профиля: {profile_url}")
        resolved_user = (username or _extract_username_from_url(normalized)).strip()
        if not resolved_user:
            raise ValueError("Не удалось определить username профиля")
        return resolved_user, normalized

    if not username:
        raise ValueError("Нужно указать --username или --profile-url")

    clean_username = username.strip().lstrip("@")
    if not clean_username:
        raise ValueError("Пустой username профиля")
    return clean_username, f"https://vsco.co/{clean_username}/gallery"


async def collect_profile_media(
    profile_url: str,
    *,
    max_width: int = 2048,
    delay: float = 0.4,
    target_count: int = 0,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """Return the list of direct media URLs for a VSCO profile."""

    session_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        session_headers.update(headers)

    async with aiohttp.ClientSession(headers=session_headers) as session:
        urls = await playwright_scan_profile(
            profile_url,
            max_width=max_width,
            session=session,
            logger=LOGGER,
            delay=delay,
            target_count=target_count,
        )
    if not urls:
        LOGGER.warning("Не удалось получить ссылки медиа для %s", profile_url)
    return urls


def _prepare_urls(urls: Iterable[str], *, max_width: int) -> list[str]:
    prepared: list[str] = []
    for raw in urls:
        candidate = normalize_media_url(raw, root="https://vsco.co/")
        if not candidate or not is_media_url(candidate):
            continue
        candidate = upscale_w_param(candidate, max_width)
        if is_vsco_logo_url(candidate):
            continue
        prepared.append(candidate)
    return dedupe_keep_order(prepared)


def store_profile_media(
    db_path: Path,
    chat_id: int,
    username: str,
    profile_url: str,
    media_urls: Sequence[str],
    *,
    source: str = "profile-scan",
    added_by: str = "",
) -> ScanResult:
    """Persist collected media URLs into the bot database."""

    result = ScanResult(username=username, profile_url=profile_url, media_urls=list(media_urls))
    if not media_urls:
        return result

    prepared = _prepare_urls(media_urls, max_width=2048)
    if not prepared:
        return result

    conn = connect_db(db_path)
    try:
        added_items = 0
        for url in prepared:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO items(
                    chat_id, username, latitude, longitude, profile_url, image_url,
                    source, source_file, added_by, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    chat_id,
                    username,
                    None,
                    None,
                    profile_url,
                    url,
                    source,
                    "",
                    added_by,
                    utc_now_iso(),
                ),
            )
            if cur.rowcount > 0:
                added_items += 1
        conn.execute(
            "INSERT OR IGNORE INTO links(chat_id,username,url,created_at) VALUES(?,?,?,?)",
            (chat_id, username, profile_url, utc_now_iso()),
        )
        conn.commit()
    finally:
        result.added_items = added_items
        result.link_added = added_items > 0
        conn.close()

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сканировать профиль VSCO и сохранить ссылки в БД")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--username", help="Username профиля VSCO")
    group.add_argument("--profile-url", help="Полная ссылка на профиль VSCO")
    parser.add_argument("--chat-id", type=int, default=0, help="ID чата для записи в базу")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Путь к базе данных")
    parser.add_argument("--max-width", type=int, default=2048, help="Максимальная ширина при апскейле ссылок")
    parser.add_argument("--target-count", type=int, default=0, help="Желаемое число медиа для остановки Playwright")
    parser.add_argument("--delay", type=float, default=0.4, help="Задержка между прокрутками (сек)")
    parser.add_argument("--verbose", action="store_true", help="Включить подробные логи")
    return parser.parse_args()


async def _async_main(args: argparse.Namespace) -> ScanResult:
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)

    username, profile_url = resolve_profile_inputs(args.username, args.profile_url)
    LOGGER.info("Сканируем профиль %s (%s)", username, profile_url)

    media_urls = await collect_profile_media(
        profile_url,
        max_width=args.max_width,
        delay=args.delay,
        target_count=args.target_count,
    )

    result = store_profile_media(
        args.db,
        args.chat_id,
        username,
        profile_url,
        media_urls,
        source="profile-scan",
    )
    LOGGER.info(
        "Сканирование завершено: %d новых элементов, ссылка сохранена=%s",
        result.added_items,
        result.link_added,
    )
    return result


def main() -> None:
    args = parse_args()
    try:
        result = asyncio.run(_async_main(args))
    except Exception as exc:  # pragma: no cover - CLI reporting
        LOGGER.error("Ошибка выполнения: %s", exc)
        raise SystemExit(1) from exc

    print(
        "Добавлено элементов:",
        result.added_items,
        "| Всего ссылок:",
        len(result.media_urls),
        "| Ссылка профиля сохранена:",
        "да" if result.link_added else "нет",
    )


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
