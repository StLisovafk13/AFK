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
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import aiohttp
import io
import json

from vsco_utils import (
    dedupe_keep_order,
    extract_media_urls_from_html,
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

IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.IGNORECASE)


def _is_image_asset(url: str) -> bool:
    if not url:
        return False
    return bool(IMAGE_EXT_RE.search(url))


def _safe_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "latin-1"):
            try:
                return value.decode(encoding, errors="ignore").strip("\x00").strip()
            except Exception:
                continue
        return ""
    return str(value).strip().strip("\x00")


def _rational_to_float(value: object) -> float | None:
    try:
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            denominator = getattr(value, "denominator") or 1
            if not denominator:
                return None
            return float(getattr(value, "numerator")) / float(denominator)
        if isinstance(value, (tuple, list)) and len(value) == 2:
            numerator, denominator = value
            denominator = float(denominator) if denominator else 0.0
            if not denominator:
                return None
            return float(numerator) / denominator
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_to_decimal(coord: object, ref: object) -> float | None:
    if not coord:
        return None
    try:
        parts = list(coord)
    except Exception:
        return None
    if not parts:
        return None
    values = [_rational_to_float(part) for part in parts[:3]]
    if any(v is None for v in values):
        return None
    degrees = values[0] or 0.0
    minutes = values[1] or 0.0
    seconds = values[2] or 0.0
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    ref_str = _safe_str(ref).upper()
    if ref_str in {"S", "W"}:
        decimal *= -1
    return decimal


def _format_exposure(value: object) -> str | None:
    try:
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            numerator = getattr(value, "numerator")
            denominator = getattr(value, "denominator") or 1
            frac = Fraction(int(numerator), int(denominator)).limit_denominator()
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            numerator, denominator = value
            frac = Fraction(int(numerator), int(denominator or 1)).limit_denominator()
        else:
            seconds = float(value)
            if seconds <= 0:
                return None
            frac = Fraction(seconds).limit_denominator()
        if frac.denominator == 0:
            return None
        if frac.numerator >= frac.denominator:
            seconds = frac.numerator / frac.denominator
            formatted = f"{seconds:.2f}".rstrip("0").rstrip(".")
            return f"{formatted}s"
        return f"{frac.numerator}/{frac.denominator}s"
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _normalize_timestamp(value: object) -> str | None:
    raw = _safe_str(value)
    if not raw:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return raw


def _extract_exif_from_bytes(data: bytes) -> dict[str, object]:
    if not data or Image is None or ExifTags is None:
        return {}
    try:
        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
    except Exception:
        return {}
    if not exif:
        return {}

    camera_make = camera_model = lens_model = ""
    iso_value: int | None = None
    aperture_value: float | None = None
    focal_length: float | None = None
    focal_length_35mm: float | None = None
    exposure_value: str | None = None
    taken_at: str | None = None
    lat_value: float | None = None
    lon_value: float | None = None

    gps_info: dict[str, object] | None = None

    for tag_id, value in exif.items():
        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
        if tag_name == "Make":
            camera_make = _safe_str(value)
        elif tag_name == "Model":
            camera_model = _safe_str(value)
        elif tag_name == "LensModel":
            lens_model = _safe_str(value)
        elif tag_name == "FNumber":
            aperture_value = _rational_to_float(value)
        elif tag_name == "ApertureValue" and aperture_value is None:
            aperture_value = _rational_to_float(value)
        elif tag_name == "FocalLength":
            focal_length = _rational_to_float(value)
        elif tag_name == "FocalLengthIn35mmFilm":
            focal_length_35mm = _rational_to_float(value)
        elif tag_name in {"ISOSpeedRatings", "PhotographicSensitivity"}:
            if isinstance(value, (list, tuple)):
                for entry in value:
                    iso_candidate = _rational_to_float(entry) or _rational_to_float(getattr(entry, "value", None))
                    if iso_candidate is not None:
                        iso_value = int(round(iso_candidate))
                        break
            else:
                iso_candidate = _rational_to_float(value)
                if iso_candidate is not None:
                    iso_value = int(round(iso_candidate))
        elif tag_name in {"ExposureTime", "ShutterSpeedValue"}:
            exposure_value = exposure_value or _format_exposure(value)
        elif tag_name in {"DateTimeOriginal", "DateTime"}:
            taken_at = taken_at or _normalize_timestamp(value)
        elif tag_name == "GPSInfo" and isinstance(value, dict):
            gps_info = value

    if gps_info and ExifTags is not None:
        gps_data: dict[str, object] = {}
        for key, val in gps_info.items():
            name = ExifTags.GPSTAGS.get(key, str(key))
            gps_data[name] = val
        lat_value = _gps_to_decimal(gps_data.get("GPSLatitude"), gps_data.get("GPSLatitudeRef"))
        lon_value = _gps_to_decimal(gps_data.get("GPSLongitude"), gps_data.get("GPSLongitudeRef"))

    meta: dict[str, object] = {}
    if camera_make:
        meta["camera_make"] = camera_make
    if camera_model:
        meta["camera_model"] = camera_model
    if lens_model:
        meta["lens_model"] = lens_model
    if focal_length is not None and focal_length > 0:
        meta["focal_length_mm"] = round(focal_length, 2)
    if focal_length_35mm is not None and focal_length_35mm > 0:
        meta["focal_length_35mm"] = round(focal_length_35mm, 2)
    if aperture_value is not None and aperture_value > 0:
        meta["aperture"] = round(aperture_value, 2)
    if exposure_value:
        meta["exposure"] = exposure_value
    if iso_value is not None and iso_value > 0:
        meta["iso"] = iso_value
    if taken_at:
        meta["taken_at"] = taken_at
    if lat_value is not None:
        meta["lat"] = lat_value
    if lon_value is not None:
        meta["lon"] = lon_value

    return meta


async def _fetch_exif_bytes(
    session: aiohttp.ClientSession,
    url: str,
    max_bytes: int,
) -> bytes:
    headers = {"Range": f"bytes=0-{max(0, max_bytes - 1)}"} if max_bytes > 0 else None
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status >= 400:
                return b""
            if max_bytes <= 0:
                return await resp.read()
            collected = bytearray()
            async for chunk in resp.content.iter_chunked(8192):
                collected.extend(chunk)
                if len(collected) >= max_bytes:
                    break
            return bytes(collected)
    except Exception as exc:  # pragma: no cover - network dependent
        LOGGER.debug("Не удалось скачать данные EXIF для %s: %s", url, exc)
        return b""


async def collect_media_exif(
    media_urls: Sequence[str],
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = 128 * 1024,
    concurrency: int = 5,
) -> dict[str, dict[str, object]]:
    """Download image headers and extract EXIF metadata for gallery usage."""

    if not media_urls or Image is None or ExifTags is None:
        return {}

    image_urls = [url for url in dedupe_keep_order(media_urls) if _is_image_asset(url)]
    if not image_urls:
        return {}

    session_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        session_headers.update(headers)

    timeout = aiohttp.ClientTimeout(total=30)
    results: dict[str, dict[str, object]] = {}
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with aiohttp.ClientSession(headers=session_headers, timeout=timeout) as session:

        async def _worker(url: str) -> None:
            async with semaphore:
                data = await _fetch_exif_bytes(session, url, max_bytes)
                if not data:
                    return
                meta = _extract_exif_from_bytes(data)
                if meta:
                    results[url] = meta

        await asyncio.gather(*(_worker(url) for url in image_urls), return_exceptions=True)

    return results

try:  # pragma: no cover - optional dependency
    from PIL import ExifTags, Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - optional dependency
    ExifTags = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageFile = None  # type: ignore[assignment]


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
          exif_json TEXT DEFAULT '',
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
    if "exif_json" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN exif_json TEXT DEFAULT ''")


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
) -> list[str]:
    try:
        from playwright.async_api import async_playwright
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except Exception as exc:  # pragma: no cover - optional dependency
        LOGGER.info("Playwright недоступен, откатываемся на HTTP: %s", exc)
        return []

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

    return []


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

    urls = await _collect_with_playwright(
        profile_url,
        headers=session_headers,
        max_width=max_width,
        delay=delay,
        target_count=target_count,
    )

    if urls:
        return urls

    async with aiohttp.ClientSession(headers=session_headers) as session:
        urls = await scan_profile_media(
            session,
            profile_url,
            max_width=max_width,
            logger=LOGGER,
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


def _coerce_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def store_profile_media(
    db_path: Path,
    chat_id: int,
    username: str,
    profile_url: str,
    media_urls: Sequence[str],
    *,
    source: str = "profile-scan",
    added_by: str = "",
    media_exif: Mapping[str, Mapping[str, object]] | None = None,
) -> ScanResult:
    """Persist collected media URLs into the bot database."""

    result = ScanResult(username=username, profile_url=profile_url, media_urls=list(media_urls))
    if not media_urls:
        return result

    prepared = _prepare_urls(media_urls, max_width=2048)
    if not prepared:
        return result

    exif_map: Mapping[str, Mapping[str, object]] = media_exif or {}

    conn = connect_db(db_path)
    try:
        added_items = 0
        for url in prepared:
            meta = exif_map.get(url)
            lat_val = lon_val = None
            exif_json = ""
            if isinstance(meta, Mapping):
                lat_val = _coerce_float(meta.get("lat"))
                lon_val = _coerce_float(meta.get("lon"))
                filtered = {
                    key: value
                    for key, value in meta.items()
                    if value not in (None, "", [], {})
                }
                if filtered:
                    exif_json = json.dumps(filtered, ensure_ascii=False)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO items(
                    chat_id, username, latitude, longitude, profile_url, image_url,
                    source, source_file, added_by, exif_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    chat_id,
                    username,
                    lat_val,
                    lon_val,
                    profile_url,
                    url,
                    source,
                    "",
                    added_by,
                    exif_json,
                    utc_now_iso(),
                ),
            )
            if cur.rowcount > 0:
                added_items += 1
            if meta:
                conn.execute(
                    """
                    UPDATE items
                    SET
                        exif_json = CASE WHEN ? <> '' THEN ? ELSE exif_json END,
                        latitude = CASE WHEN latitude IS NULL AND ? IS NOT NULL THEN ? ELSE latitude END,
                        longitude = CASE WHEN longitude IS NULL AND ? IS NOT NULL THEN ? ELSE longitude END
                    WHERE chat_id=? AND username=? AND image_url=? AND profile_url=?
                    """,
                    (
                        exif_json,
                        exif_json,
                        lat_val,
                        lat_val,
                        lon_val,
                        lon_val,
                        chat_id,
                        username,
                        url,
                        profile_url,
                    ),
                )
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

    media_exif = await collect_media_exif(media_urls)

    result = store_profile_media(
        args.db,
        args.chat_id,
        username,
        profile_url,
        media_urls,
        source="profile-scan",
        media_exif=media_exif,
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
