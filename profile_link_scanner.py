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
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence

from bs4 import BeautifulSoup
from urllib.parse import urljoin

import aiohttp

from exif_fetcher import extract_exif_from_url

from vsco_utils import (
    dedupe_keep_order,
    extract_media_urls_from_html,
    extract_vsco_media_urls,
    is_media_url,
    is_vsco_logo_url,
    normalize_media_url,
    normalize_vsco_profile_url,
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
    sections: Dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ProfileDetails:
    """Metadata extracted from a VSCO profile page."""

    bio: str = ""
    collection_url: str = ""
    journal_url: str = ""


@dataclass(slots=True)
class ProfileScanData:
    """Result of scraping a single VSCO page."""

    profile_url: str
    media_urls: list[str]
    html: str = ""
    details: Optional[ProfileDetails] = None


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
    link_cols = {row[1] for row in conn.execute("PRAGMA table_info(links)")}
    if "profile_bio" not in link_cols:
        conn.execute("ALTER TABLE links ADD COLUMN profile_bio TEXT DEFAULT ''")
    if "collection_url" not in link_cols:
        conn.execute("ALTER TABLE links ADD COLUMN collection_url TEXT DEFAULT ''")
    if "journal_url" not in link_cols:
        conn.execute("ALTER TABLE links ADD COLUMN journal_url TEXT DEFAULT ''")
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
          meta_json TEXT DEFAULT '',
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
    if "meta_json" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN meta_json TEXT DEFAULT ''")


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


async def _collect_with_playwright(
    profile_url: str,
    *,
    headers: dict[str, str],
    max_width: int,
    delay: float,
    target_count: int,
) -> ProfileScanData:
    try:
        from playwright.async_api import async_playwright
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except Exception as exc:  # pragma: no cover - optional dependency
        LOGGER.info("Playwright недоступен, откатываемся на HTTP: %s", exc)
        return ProfileScanData(profile_url=profile_url, media_urls=[])

    gallery_url = profile_url.rstrip("/")
    if not gallery_url.endswith("/gallery"):
        gallery_url = f"{gallery_url}/gallery"

    async def _extract(page) -> list[str]:
        html = await page.content()
        root = getattr(page, "url", None) or gallery_url
        urls = extract_media_urls_from_html(html, max_width=max_width, root=root)
        return [u for u in urls if not is_vsco_logo_url(u)]

    async def _scroll(page) -> list[str]:
        urls = dedupe_keep_order(await _extract(page))
        if urls:
            LOGGER.info("Нашли %d ссылок на первом экране", len(urls))
        else:
            LOGGER.warning("На первом экране ссылки не найдены, пробуем прокрутку")

        max_scrolls = (
            999999
            if target_count == 0
            else max(30, min(999999, target_count // 2 + 20))
        )
        max_clicks = 500
        stagnation_limit = 5
        no_growth_click_limit = 3

        last_height = await page.evaluate("() => document.body.scrollHeight")
        stagnation = 0
        load_clicks = 0
        clicks_without_growth = 0
        prev_count = len(urls)

        for _ in range(max_scrolls):
            btn = page.locator("#loadMore-Button").first
            try:
                btn_exists = (await btn.count()) > 0
                btn_visible = btn_exists and (await btn.is_visible())
                disabled_attr = await btn.get_attribute("disabled") if btn_exists else None
                aria_disabled = (
                    await btn.get_attribute("aria-disabled") if btn_exists else None
                )
                btn_disabled = (
                    disabled_attr is not None
                    or (aria_disabled or "").lower() in {"true", "1"}
                )
            except Exception:
                btn_exists = btn_visible = False
                btn_disabled = True

            clicked = False
            if btn_exists and btn_visible and not btn_disabled and load_clicks < max_clicks:
                try:
                    await btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await btn.click()
                    load_clicks += 1
                    clicked = True
                    LOGGER.debug("Клик по Load More #%d", load_clicks)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        await page.wait_for_timeout(int(max(0.1, delay) * 1000))
                except Exception as exc:
                    LOGGER.debug("Не удалось кликнуть Load More: %s", exc)

            await page.evaluate(
                "() => { window.scrollBy(0, Math.floor(window.innerHeight * 0.9)); }"
            )
            await page.wait_for_timeout(int(max(0.1, delay) * 1000))

            extracted = dedupe_keep_order(await _extract(page))
            combined = dedupe_keep_order(urls + extracted)
            if len(combined) > prev_count:
                LOGGER.info("Прогресс: %d ссылок", len(combined))
            urls = combined

            new_height = await page.evaluate("() => document.body.scrollHeight")
            grew = (new_height > last_height) or (len(urls) > prev_count)

            if grew:
                stagnation = 0
                if len(urls) > prev_count:
                    clicks_without_growth = 0
                last_height = max(last_height, new_height)
                prev_count = len(urls)
            else:
                stagnation += 1
                if clicked:
                    clicks_without_growth += 1

            if target_count and len(urls) >= target_count:
                LOGGER.debug("Достигли целевого количества ссылок")
                break
            if (
                (not btn_exists or not btn_visible or btn_disabled)
                and stagnation >= stagnation_limit
            ):
                LOGGER.debug("Похоже, достигнут конец ленты")
                break
            if clicked and clicks_without_growth >= no_growth_click_limit:
                LOGGER.debug("Клики Load More не дают новых ссылок, останавливаемся")
                break

        return urls

    browser = context = page = None
    collected_html = ""
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(extra_http_headers=headers)
            page = await context.new_page()
            try:
                await page.goto(
                    gallery_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except PlaywrightTimeoutError as exc:
                LOGGER.warning(
                    "Playwright не дождался полной загрузки страницы: %s", exc
                )
            except Exception:
                # На этом этапе дальнейшая прокрутка бессмысленна.
                raise
            urls = await _scroll(page)
            try:
                collected_html = await page.content()
            except Exception:
                collected_html = ""
            return ProfileScanData(
                profile_url=gallery_url,
                media_urls=urls,
                html=collected_html,
            )
    except Exception as exc:
        LOGGER.warning("Playwright не справился: %s", exc)
    finally:
        for handle in (page, context, browser):
            if handle is None:
                continue
            try:
                await handle.close()  # type: ignore[func-returns-value]
            except Exception:
                pass

    return ProfileScanData(profile_url=profile_url, media_urls=[], html=collected_html)


async def collect_profile_data(
    profile_url: str,
    *,
    max_width: int = 2048,
    delay: float = 0.4,
    target_count: int = 0,
    headers: dict[str, str] | None = None,
) -> ProfileScanData:
    session_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        session_headers.update(headers)

    scan_data = await _collect_with_playwright(
        profile_url,
        headers=session_headers,
        max_width=max_width,
        delay=delay,
        target_count=target_count,
    )

    html = scan_data.html or ""
    collected_urls = list(scan_data.media_urls)
    root_for_details = scan_data.profile_url or profile_url
    details = scan_data.details
    if details is None and html:
        details = _extract_profile_details(html, root_for_details)

    if not collected_urls or not html:
        async with aiohttp.ClientSession(headers=session_headers) as session:
            http_html = ""
            resolved_root = profile_url
            try:
                async with session.get(profile_url, allow_redirects=True) as resp:
                    http_html = await resp.text(errors="ignore")
                    try:
                        resolved_root = str(resp.url)
                    except Exception:
                        resolved_root = profile_url
            except Exception as exc:
                LOGGER.warning("Не удалось загрузить %s через HTTP: %s", profile_url, exc)
                http_html = ""

            if http_html:
                html = html or http_html
                extracted = _extract_media_from_html(http_html, resolved_root, max_width=max_width)
                if not collected_urls:
                    collected_urls = extracted
                if details is None:
                    details = _extract_profile_details(http_html, resolved_root)

    prepared = _prepare_urls(collected_urls, max_width=max_width)
    return ProfileScanData(
        profile_url=profile_url,
        media_urls=prepared,
        html=html,
        details=details or ProfileDetails(),
    )


async def collect_profile_media(
    profile_url: str,
    *,
    max_width: int = 2048,
    delay: float = 0.4,
    target_count: int = 0,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """Return the list of direct media URLs for a VSCO profile."""

    scan = await collect_profile_data(
        profile_url,
        max_width=max_width,
        delay=delay,
        target_count=target_count,
        headers=headers,
    )

    if not scan.media_urls:
        LOGGER.warning("Не удалось получить ссылки медиа для %s", profile_url)
    return scan.media_urls


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


def _extract_media_from_html(html: str, root_url: str, *, max_width: int) -> list[str]:
    urls = extract_media_urls_from_html(html, max_width=max_width, root=root_url)
    filtered = [u for u in urls if not is_vsco_logo_url(u)]
    if filtered:
        return dedupe_keep_order(filtered)

    fallback: list[str] = []
    for candidate in extract_vsco_media_urls(html, sources=("og", "twitter", "responsive", "inline")):
        normalized = normalize_media_url(candidate, root=root_url)
        if not normalized:
            continue
        final_url = upscale_w_param(normalized, max_width)
        if is_media_url(final_url) and not is_vsco_logo_url(final_url):
            fallback.append(final_url)
    return dedupe_keep_order(fallback)


def _clean_section_url(url: str) -> str:
    cleaned = re.split(r"[?#]", url, maxsplit=1)[0]
    while cleaned.endswith("/") and not cleaned.endswith("//"):
        cleaned = cleaned[:-1]
    return cleaned


def _extract_profile_details(html: str, base_url: str) -> ProfileDetails:
    details = ProfileDetails()
    if not html:
        return details

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return details

    bio_selectors = [
        "[data-testid='ProfileDescription']",
        "[data-testid='profileDescription']",
        "div.css-1fyd93m.e19mt3zn0",
        "div.css-dhlg55.e19mt3zn0",
    ]
    for selector in bio_selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        text = node.get_text("\n", strip=True)
        if text:
            details.bio = text
            break

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not href:
            continue
        absolute = urljoin(base_url, href)
        if not details.collection_url and re.search(r"/collection/1(?:[/?#]|$)", absolute):
            details.collection_url = _clean_section_url(absolute)
        if not details.journal_url and re.search(r"/journal/(?:p/)?1(?:[/?#]|$)", absolute):
            details.journal_url = _clean_section_url(absolute)
        if details.collection_url and details.journal_url:
            break

    return details


def store_profile_media(
    db_path: Path,
    chat_id: int,
    username: str,
    profile_url: str,
    media_urls: Sequence[str],
    *,
    source: str = "profile-scan",
    added_by: str = "",
    meta_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
    section: str = "gallery",
    profile_details: Optional[ProfileDetails] = None,
) -> ScanResult:
    """Persist collected media URLs into the bot database."""

    normalized_section = (section or "gallery").strip().lower() or "gallery"
    result = ScanResult(
        username=username,
        profile_url=profile_url,
        media_urls=list(media_urls),
        sections={normalized_section: list(media_urls)},
    )
    if not media_urls:
        return result

    prepared = _prepare_urls(media_urls, max_width=2048)
    if not prepared:
        return result

    if meta_fetcher is None:
        meta_fetcher = extract_exif_from_url

    conn = connect_db(db_path)
    try:
        added_items = 0
        for url in prepared:
            created_at = utc_now_iso()
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO items(
                    chat_id, username, latitude, longitude, profile_url, image_url,
                    source, source_file, added_by, meta_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
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
                    "",
                    created_at,
                ),
            )
            if cur.rowcount > 0:
                added_items += 1
                if meta_fetcher is not None:
                    try:
                        metadata = meta_fetcher(url)
                    except Exception as exc:  # pragma: no cover - best effort metadata
                        LOGGER.debug("Не удалось получить EXIF для %s: %s", url, exc)
                        metadata = None
                    if metadata:
                        try:
                            payload = json.dumps(metadata, ensure_ascii=False)
                        except (TypeError, ValueError):
                            payload = json.dumps({"raw": str(metadata)}, ensure_ascii=False)
                        item_id = cur.lastrowid
                        if item_id:
                            conn.execute(
                                "UPDATE items SET meta_json=? WHERE id=?",
                                (payload, item_id),
                            )
        conn.execute(
            "INSERT OR IGNORE INTO links(chat_id,username,url,created_at) VALUES(?,?,?,?)",
            (chat_id, username, profile_url, utc_now_iso()),
        )
        if normalized_section == "gallery":
            conn.execute(
                "UPDATE links SET url=? WHERE chat_id=? AND username=?",
                (profile_url, chat_id, username),
            )
        if profile_details is not None:
            conn.execute(
                "UPDATE links SET profile_bio=?, collection_url=?, journal_url=? WHERE chat_id=? AND username=?",
                (
                    profile_details.bio or "",
                    profile_details.collection_url or "",
                    profile_details.journal_url or "",
                    chat_id,
                    username,
                ),
            )
        conn.commit()
    finally:
        result.added_items = added_items
        result.link_added = added_items > 0
        result.media_urls = prepared
        result.sections = {normalized_section: list(prepared)}
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

    primary_scan = await collect_profile_data(
        profile_url,
        max_width=args.max_width,
        delay=args.delay,
        target_count=args.target_count,
    )

    sections: Dict[str, list[str]] = {}
    combined_urls = list(primary_scan.media_urls)

    result = store_profile_media(
        args.db,
        args.chat_id,
        username,
        profile_url,
        primary_scan.media_urls,
        source="profile-scan",
        section="gallery",
        profile_details=primary_scan.details,
    )
    sections["gallery"] = list(primary_scan.media_urls)

    total_added = result.added_items
    any_link_added = result.link_added

    extras: list[tuple[str, str]] = []
    if primary_scan.details.collection_url:
        extras.append(("collection", primary_scan.details.collection_url))
    if primary_scan.details.journal_url:
        extras.append(("journal", primary_scan.details.journal_url))

    for section_key, section_url in extras:
        try:
            section_scan = await collect_profile_data(
                section_url,
                max_width=args.max_width,
                delay=args.delay,
                target_count=args.target_count,
            )
        except Exception as exc:  # pragma: no cover - network failure guard
            LOGGER.warning("Не удалось собрать секцию %s для %s: %s", section_key, username, exc)
            continue

        if not section_scan.media_urls:
            LOGGER.info("Секция %s не содержит медиа для %s", section_key, username)
            continue

        section_result = store_profile_media(
            args.db,
            args.chat_id,
            username,
            section_url,
            section_scan.media_urls,
            source=f"profile-scan:{section_key}",
            section=section_key,
        )
        sections[section_key] = list(section_scan.media_urls)
        combined_urls.extend(section_scan.media_urls)
        total_added += section_result.added_items
        any_link_added = any_link_added or section_result.link_added

    result.media_urls = dedupe_keep_order(combined_urls)
    result.added_items = total_added
    result.link_added = any_link_added
    result.sections = sections

    LOGGER.info(
        "Сканирование завершено: %d новых элементов, ссылка сохранена=%s",
        total_added,
        any_link_added,
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
