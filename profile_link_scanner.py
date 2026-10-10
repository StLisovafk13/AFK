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
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from exif_fetcher import extract_exif_from_url
from browser_config import managed_async_browser

from vsco_utils import (
    dedupe_keep_order,
    extract_media_urls_from_html,
    extract_profile_tab_links,
    is_media_url,
    is_vsco_logo_url,
    normalize_media_url,
    normalize_vsco_profile_url,
    scan_profile_media,
    upscale_w_param,
)


LOGGER = logging.getLogger("vsco.profile_scanner")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)
DEFAULT_DB = Path("vsco_links.db")
LOCAL_TZ = timezone(timedelta(hours=3))
PARTIAL_LOAD_MARKER = "Error Loading Content"
PARTIAL_LOAD_WARNING = (
    "Профиль загружен не полностью: на странице найдено сообщение «Error Loading Content»."
)


@dataclass(slots=True)
class ScanResult:
    """Small summary that mirrors the downloader manifest ingest step."""

    username: str
    profile_url: str
    media_urls: list[str]
    added_items: int = 0
    link_added: bool = False
    profile_tabs: list[dict[str, str]] = field(default_factory=list)
    media_by_tab: dict[str, list[str]] = field(default_factory=dict)
    metadata_targets: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProfileMediaCollection:
    """Container with extracted media URLs and profile metadata."""

    media_urls: list[str]
    profile_tabs: list[dict[str, str]] = field(default_factory=list)
    media_by_tab: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


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
          extra_json TEXT DEFAULT '',
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
    link_cols = {row[1] for row in conn.execute("PRAGMA table_info(links)")}
    if "extra_json" not in link_cols:
        conn.execute("ALTER TABLE links ADD COLUMN extra_json TEXT DEFAULT ''")
    if "notified_at" not in link_cols:
        conn.execute("ALTER TABLE links ADD COLUMN notified_at TEXT DEFAULT ''")


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
) -> tuple[list[str], str]:
    try:
        from playwright.async_api import async_playwright
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except Exception as exc:  # pragma: no cover - optional dependency
        LOGGER.info("Playwright недоступен, откатываемся на HTTP: %s", exc)
        return [], ""

    gallery_url = profile_url.rstrip("/")
    if not gallery_url.endswith("/gallery"):
        gallery_url = f"{gallery_url}/gallery"

    async def _extract(page) -> tuple[list[str], str]:
        html = await page.content()
        root = getattr(page, "url", None) or gallery_url
        urls = extract_media_urls_from_html(html, max_width=max_width, root=root)
        return [u for u in urls if not is_vsco_logo_url(u)], html

    async def _scroll(page) -> tuple[list[str], str]:
        urls, last_html = await _extract(page)
        urls = dedupe_keep_order(urls)
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

            extracted, html_snapshot = await _extract(page)
            last_html = html_snapshot
            extracted = dedupe_keep_order(extracted)
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

        if not last_html:
            # Гарантируем, что у нас есть HTML последнего состояния
            fallback_urls, last_html = await _extract(page)
            urls = dedupe_keep_order(urls + fallback_urls)

        return dedupe_keep_order(urls), last_html

    browser = context = page = None
    try:
        async with managed_async_browser() as browser:
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
            return await _scroll(page)
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

    return [], ""


async def collect_profile_media(
    profile_url: str,
    *,
    max_width: int = 2048,
    delay: float = 0.4,
    target_count: int = 0,
    headers: dict[str, str] | None = None,
    include_details: bool = False,

) -> list[str] | ProfileMediaCollection:
    """Return the list of direct media URLs for a VSCO profile.

    When navigation tabs are present on the profile page we fetch media from
    every tab (e.g. ``/gallery``, ``/galleries``, ``/spaces``) to make sure the
    database contains the complete set of assets. The combined list is returned
    in ``media_urls`` while ``ProfileMediaCollection`` additionally carries the
    per-tab mapping for the caller.
    """

    session_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        session_headers.update(headers)

    normalized_profile_url = _normalize_tab_url(profile_url)
    aggregated_urls: list[str] = []
    media_by_tab: dict[str, list[str]] = {}
    tabs: Sequence[Dict[str, Any]] | None = None

    seen_tab_urls: set[str] = set()
    warnings: list[str] = []

    def _record_warning(source: str, html: str) -> None:
        if not html or PARTIAL_LOAD_MARKER not in html:
            return
        first_detected = PARTIAL_LOAD_WARNING not in warnings
        if first_detected:
            warnings.append(PARTIAL_LOAD_WARNING)
        if first_detected or not include_details:
            LOGGER.warning(
                "Неполная загрузка содержимого профиля %s (source=%s)",
                profile_url,
                source,
            )

    def _store_tab_media(tab_url: str, urls_for_tab: Iterable[str]) -> None:
        nonlocal aggregated_urls
        normalized_tab_url = _normalize_tab_url(tab_url, root=profile_url)
        if not normalized_tab_url:
            return
        cleaned = dedupe_keep_order(urls_for_tab)
        media_by_tab[normalized_tab_url] = cleaned
        if cleaned:
            seen_tab_urls.add(normalized_tab_url)
            aggregated_urls = dedupe_keep_order(aggregated_urls + cleaned)

    session: aiohttp.ClientSession | None = None
    try:
        session = aiohttp.ClientSession(headers=session_headers)
        urls, html = await scan_profile_media(
            session,
            profile_url,
            max_width=max_width,
            logger=LOGGER,
        )
        _record_warning("http", html)
        aggregated_urls = dedupe_keep_order(urls)
        if aggregated_urls:
            _store_tab_media(normalized_profile_url, aggregated_urls)
        tabs = extract_profile_tab_links(html, root=profile_url)

        if tabs:
            for tab in tabs:
                href = tab.get("href") or ""
                normalized_href = _normalize_tab_url(href, root=profile_url)
                if (
                    not normalized_href
                    or normalized_href in seen_tab_urls
                    or normalized_href == normalized_profile_url
                ):
                    continue
                seen_tab_urls.add(normalized_href)
                tab_media, tab_html = await scan_profile_media(
                    session,
                    normalized_href,
                    max_width=max_width,
                    logger=LOGGER,
                )
                _record_warning(normalized_href, tab_html)
                cleaned = dedupe_keep_order(tab_media)
                media_by_tab[normalized_href] = cleaned
                if cleaned:
                    aggregated_urls = dedupe_keep_order(aggregated_urls + cleaned)
    finally:
        if session is not None:
            await session.close()

    if not aggregated_urls:
        urls, html = await _collect_with_playwright(
            profile_url,
            headers=session_headers,
            max_width=max_width,
            delay=delay,
            target_count=target_count,
        )
        _record_warning("playwright", html)
        aggregated_urls = dedupe_keep_order(urls)
        if aggregated_urls:
            _store_tab_media(normalized_profile_url, aggregated_urls)
        if not tabs:
            tabs = extract_profile_tab_links(html, root=profile_url)
        if tabs:
            async with aiohttp.ClientSession(headers=session_headers) as session_retry:
                for tab in tabs:
                    href = tab.get("href") or ""
                    normalized_href = _normalize_tab_url(href, root=profile_url)
                    if (
                        not normalized_href
                        or normalized_href in seen_tab_urls
                        or normalized_href == normalized_profile_url
                    ):
                        continue
                    seen_tab_urls.add(normalized_href)
                    tab_media, tab_html = await scan_profile_media(
                        session_retry,
                        normalized_href,
                        max_width=max_width,
                        logger=LOGGER,
                    )
                    _record_warning(normalized_href, tab_html)
                    cleaned = dedupe_keep_order(tab_media)
                    media_by_tab[normalized_href] = cleaned
                    if cleaned:
                        aggregated_urls = dedupe_keep_order(aggregated_urls + cleaned)

    if not aggregated_urls:
        LOGGER.warning("Не удалось получить ссылки медиа для %s", profile_url)
        if include_details:
            sanitized_tabs = _sanitize_profile_tabs(tabs, root=profile_url)
            return ProfileMediaCollection(
                media_urls=[],
                profile_tabs=sanitized_tabs,
                media_by_tab={},
                warnings=warnings.copy(),
            )
        return []

    if include_details:
        sanitized_tabs = _sanitize_profile_tabs(tabs, root=profile_url)
        return ProfileMediaCollection(
            media_urls=aggregated_urls,
            profile_tabs=sanitized_tabs,
            media_by_tab=media_by_tab.copy(),
            warnings=warnings.copy(),
        )

    return aggregated_urls


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


def _normalize_tab_url(url: str, *, root: Optional[str] = None) -> str:
    candidate = (url or "").strip()
    if not candidate:
        return ""

    base = (root or "").strip()
    if base:
        candidate = urljoin(base, candidate)

    try:
        parsed = urlsplit(candidate)
    except Exception:
        return candidate

    scheme = parsed.scheme or ""
    netloc = parsed.netloc or ""

    if not netloc and base:
        try:
            base_parts = urlsplit(base)
        except Exception:
            base_parts = None
        else:
            netloc = base_parts.netloc or netloc
            if not scheme:
                scheme = base_parts.scheme or "https"

    if not scheme:
        scheme = "https"

    path = parsed.path or ""
    if path and path != "/":
        path = path.rstrip("/")

    if not netloc:
        return candidate if candidate else ""

    return urlunsplit((scheme, netloc, path or "/", parsed.query, parsed.fragment))


def _sanitize_profile_tabs(
    tabs: Optional[Sequence[Dict[str, Any]]],
    *,
    root: Optional[str] = None,
) -> list[dict[str, str]]:
    sanitized: list[dict[str, str]] = []
    if not tabs:
        return sanitized
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        href_raw = tab.get("href")
        href = str(href_raw).strip() if href_raw is not None else ""
        if not href:
            continue
        normalized_href = _normalize_tab_url(href, root=root)
        if not normalized_href:
            continue
        entry: dict[str, str] = {"href": normalized_href}
        for key in ("id", "label", "slug", "active"):
            value = tab.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                entry[key] = text
        sanitized.append(entry)
    return sanitized


def store_profile_media(
    db_path: Path,
    chat_id: int,
    username: str,
    profile_url: str,
    media_urls: Sequence[str],
    *,
    source: str = "profile-scan",
    added_by: str = "",
    profile_tabs: Optional[Sequence[Dict[str, Any]]] = None,
    media_by_tab: Optional[Mapping[str, Sequence[str]]] = None,
    meta_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
    warnings: Optional[Sequence[str]] = None,
) -> ScanResult:
    """Persist collected media URLs into the bot database."""

    sanitized_tabs = _sanitize_profile_tabs(profile_tabs, root=profile_url)
    normalized_media_by_tab: dict[str, list[str]] = {}
    if media_by_tab:
        for tab_url, urls_for_tab in media_by_tab.items():
            normalized_tab_url = _normalize_tab_url(tab_url, root=profile_url)
            if not normalized_tab_url:
                continue
            cleaned_urls = dedupe_keep_order(urls_for_tab)
            normalized_media_by_tab[normalized_tab_url] = cleaned_urls

    if not normalized_media_by_tab and media_urls:
        normalized_media_by_tab[_normalize_tab_url(profile_url)] = list(media_urls)

    result = ScanResult(
        username=username,
        profile_url=profile_url,
        media_urls=list(media_urls),
        profile_tabs=sanitized_tabs,
        media_by_tab=normalized_media_by_tab.copy(),
        metadata_targets=[],
        warnings=list(warnings) if warnings else [],
    )
    if not normalized_media_by_tab:
        return result

    conn = connect_db(db_path)
    try:
        added_items = 0
        new_items_for_meta: list[tuple[int, str]] = []
        link_inserted = False
        for tab_url, urls_for_tab in normalized_media_by_tab.items():
            prepared = _prepare_urls(urls_for_tab, max_width=2048)
            if not prepared:
                continue
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
                        tab_url,
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
                    item_id = cur.lastrowid
                    if item_id:
                        new_items_for_meta.append((item_id, url))
        cur = conn.execute(
            "INSERT OR IGNORE INTO links(chat_id,username,url,created_at) VALUES(?,?,?,?)",
            (chat_id, username, profile_url, utc_now_iso()),
        )
        if cur.rowcount > 0:
            link_inserted = True
        if sanitized_tabs:
            payload = json.dumps({"profile_tabs": sanitized_tabs}, ensure_ascii=False)
            conn.execute(
                "UPDATE links SET extra_json=?, url=? WHERE chat_id=? AND username=?",
                (payload, profile_url, chat_id, username),
            )
        elif profile_url:
            conn.execute(
                "UPDATE links SET url=? WHERE chat_id=? AND username=?",
                (profile_url, chat_id, username),
            )
        conn.commit()
    finally:
        result.added_items = added_items
        result.link_added = link_inserted or added_items > 0
        result.metadata_targets = new_items_for_meta
        conn.close()

    if meta_fetcher is not None and result.metadata_targets:
        populate_media_metadata(
            db_path,
            result.metadata_targets,
            meta_fetcher=meta_fetcher,
        )

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

    collected = await collect_profile_media(
        profile_url,
        max_width=args.max_width,
        delay=args.delay,
        target_count=args.target_count,
        include_details=True,
    )
    if isinstance(collected, ProfileMediaCollection):
        media_urls = collected.media_urls
        profile_tabs = collected.profile_tabs
        media_by_tab = collected.media_by_tab
        warnings = collected.warnings
    else:
        media_urls = collected
        profile_tabs = []
        media_by_tab = None
        warnings = []

    result = store_profile_media(
        args.db,
        args.chat_id,
        username,
        profile_url,
        media_urls,
        source="profile-scan",
        profile_tabs=profile_tabs,
        media_by_tab=media_by_tab,
        warnings=warnings,
    )
    LOGGER.info(
        "Сканирование завершено: %d новых элементов, ссылка сохранена=%s",
        result.added_items,
        result.link_added,
    )
    if result.warnings:
        LOGGER.warning(
            "⚠️ Профиль загружен не полностью, данные могут быть неполными."
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
def populate_media_metadata(
    db_path: Path,
    items: Sequence[tuple[int, str]],
    *,
    meta_fetcher: Callable[[str], Dict[str, Any]] = extract_exif_from_url,
) -> None:
    """Populate ``items.meta_json`` for the provided IDs using ``meta_fetcher``.

    This helper is intended to be called after ``store_profile_media`` commits its
    inserts, so the potentially slow EXIF extraction no longer blocks the main
    transaction. Callers may run it in a background task or sequentially when
    operating in a CLI context.
    """

    if not items:
        return

    conn = connect_db(db_path)
    try:
        for item_id, url in items:
            if not item_id:
                continue
            try:
                metadata = meta_fetcher(url)
            except Exception as exc:  # pragma: no cover - best effort metadata
                LOGGER.debug("Не удалось получить EXIF для %s: %s", url, exc)
                continue
            if not metadata:
                continue
            try:
                payload = json.dumps(metadata, ensure_ascii=False)
            except (TypeError, ValueError):
                payload = json.dumps({"raw": str(metadata)}, ensure_ascii=False)
            try:
                conn.execute(
                    "UPDATE items SET meta_json=? WHERE id=?",
                    (payload, item_id),
                )
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                LOGGER.warning(
                    "Не удалось обновить метаданные для %s (id=%s): %s",
                    url,
                    item_id,
                    exc,
                )
    finally:
        conn.close()
