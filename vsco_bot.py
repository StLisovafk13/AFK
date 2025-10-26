# Version: 19.0.0 — 2025-10-02
# Python: 3.11
# Telegram VSCO Toolkit Bot — v19.0.0
# Изменения (19.0.0):
# - NEW: Фоновый worker profile_link_scanner обрабатывает очередь профилей из общей БД и уведомляет чаты о новых медиа.
# - NEW: Автоматическое помещение ссылок профилей в очередь сканирования при импорте текста, CSV и HTML.
# - FIX: Совместный запуск/остановка фоновых задач загрузчика и сканера теперь синхронизированы.
#
# Ранее в 18.4.3:
# - NEW: Добавлены раздельные каналы архива и сводки (BOT_ARCHIVE_ADMIN_CHANNEL_ID, BOT_ARCHIVE_SUMMARY_CHANNEL_ID) с авторассылкой архивов и итогов.
# - NEW: Для сводочного канала формируется единый ZIP при отправке нескольких частей.
# - UI: Возвращена клавиатура экспорта /export с выбором области и форматов.
#
# Ранее в 18.4.2:
# - FIX: Telegram HTML parse — экранированы примеры с <user>/<id> в /start (теперь внутри <code> и с &lt; &gt;).
# - FIX: Заменён неподдерживаемый <span> в сообщениях на <i>.
# - Map HTML: сохранён предыдущий фикс (DOMContentLoaded + fallback CDN).
#
# Ранее в 18.4.0/18.4.1:
# - /stats: блок «Всего» (уникальные username, media с/без координат).
# - Поддержка медиа-ссылок (vs.co и vsco.co/<user>/media/<id>): извлекается image_url и привязывается к профилю.
# - /links: ссылки за последние 24ч + пагинация + CSV.
# - Парсинг комментария: сразу после ссылки через запятую до следующей ссылки.

import csv
import math
import os
import re
import sqlite3
import asyncio
import logging
from collections import Counter
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Dict, Any, Tuple, Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import (
    urljoin,
    urlparse,
)
import json
import time
import tempfile
import zipfile
from html import escape
from enum import Enum

try:
    import reverse_geocoder  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    reverse_geocoder = None  # type: ignore

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    User,
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    MessageEntity,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.utils.text_decorations import add_surrogates, remove_surrogates
import aiohttp
try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    BeautifulSoup = None  # type: ignore
import sys
import contextlib
import shlex

# ---- external utils (optional HTML export parser) ----
from vsco_parser import parse_html_file, dedupe_rows

from zip_profile import zip_router
from vsco_export import ExportDependencies, ExportManager
from vsco_utils import (
    VSCO_HOSTS,
    VSCO_PERCEPTION_HOSTS,
    VSCO_SHORT_HOSTS,
    SHORT_SLUG_RE,
    dedupe_keep_order,
    extract_vsco_media_urls,
    extract_media_urls_from_html,
    is_media_url,
    normalize_vsco_profile_url,
    resolve_vsco_short_link,
    vsco_short_slug,
    build_perception_gallery_url,
    is_vsco_logo_url,
    normalize_media_url,
    playwright_scan_profile,
    upscale_w_param,
)
from profile_link_scanner import (
    ProfileMediaCollection,
    ScanResult,
    collect_profile_media,
    store_profile_media,
)

# ---------------------- setup & logging ----------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _prepare_storage_dir(env_var: str, default: Path) -> Path:
    raw = os.getenv(env_var, "").strip()
    path = Path(raw).expanduser() if raw else default
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_WORKDIR = Path(tempfile.gettempdir()) / "vsco-bot" / "work"
DEFAULT_LOGDIR = Path("./logs")
WORKDIR = _prepare_storage_dir("BOT_WORKDIR", DEFAULT_WORKDIR)
LOGDIR = _prepare_storage_dir("BOT_LOGDIR", DEFAULT_LOGDIR)
DB_PATH = os.getenv("BOT_DB_PATH", "vsco_links.db")
SEND_TIMEOUT = int(os.getenv("BOT_SEND_TIMEOUT", "600"))
ARCHIVE_CHANNEL_ID_ENV = os.getenv("BOT_ARCHIVE_CHANNEL_ID", "").strip()
ARCHIVE_ADMIN_CHANNEL_ID_ENV = os.getenv("BOT_ARCHIVE_ADMIN_CHANNEL_ID", "").strip()
ARCHIVE_SUMMARY_CHANNEL_ID_ENV = os.getenv("BOT_ARCHIVE_SUMMARY_CHANNEL_ID", "").strip()
ARCHIVE_ADMIN_CHANNEL_ID: Optional[int | str] = None
ARCHIVE_SUMMARY_CHANNEL_ID: Optional[int | str] = None
REQUIRED_CHANNEL_ID_ENV = os.getenv("BOT_REQUIRED_CHANNEL_ID", "").strip()
REQUIRED_GROUP_ID_ENV = os.getenv("BOT_REQUIRED_GROUP_ID", "").strip()
REQUIRED_CHANNEL_LABEL = os.getenv("BOT_REQUIRED_CHANNEL_LABEL", "").strip()
REQUIRED_GROUP_LABEL = os.getenv("BOT_REQUIRED_GROUP_LABEL", "").strip()
REQUIRED_CHANNEL_LINK = os.getenv("BOT_REQUIRED_CHANNEL_LINK", "").strip()
REQUIRED_GROUP_LINK = os.getenv("BOT_REQUIRED_GROUP_LINK", "").strip()
MEDIA_PAGE_MAX_WIDTH = int(os.getenv("BOT_MEDIA_SCAN_MAX_WIDTH", "2048") or "2048")


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


BOT_ADMIN_IDS = _parse_admin_ids(os.getenv("BOT_ADMIN_IDS", ""))

_CITY_CACHE: Dict[Tuple[int, int], str] = {}


def resolve_city_label(lat: Optional[float], lon: Optional[float]) -> Optional[str]:
    if lat is None or lon is None:
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    key = (int(round(lat_f * 1000)), int(round(lon_f * 1000)))
    cached = _CITY_CACHE.get(key)
    if cached is not None:
        return cached or None
    label = ""
    if reverse_geocoder is not None:
        try:
            result = reverse_geocoder.search((lat_f, lon_f), mode=1, verbose=False)
            if result:
                entry = result[0] or {}
                name = (entry.get("name") or "").strip()
                admin1 = (entry.get("admin1") or "").strip()
                country = (entry.get("cc") or "").strip()
                parts = [p for p in (name, admin1, country) if p]
                label = ", ".join(parts)
        except Exception:
            label = ""
    if not label:
        label = f"{lat_f:.3f}, {lon_f:.3f}"
    _CITY_CACHE[key] = label
    return label or None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_coord_pair(lat_raw: Any, lon_raw: Any) -> Optional[Tuple[float, float]]:
    lat = _safe_float(lat_raw)
    lon = _safe_float(lon_raw)
    if lat is None or lon is None:
        return None
    return lat, lon


def _apply_gps_ref(value: float, ref: Any, positive_refs: Tuple[str, ...]) -> float:
    ref_str = str(ref or "").strip().upper()
    if not ref_str:
        return value
    positive = {item.upper() for item in positive_refs}
    if ref_str in positive:
        return abs(value)
    return -abs(value)


def _extract_coords_from_dict(data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    # Direct numeric fields (lat/lon)
    direct_pairs = [
        ("lat", "lon"),
        ("latitude", "longitude"),
        ("Latitude", "Longitude"),
    ]
    for lat_key, lon_key in direct_pairs:
        lat = _safe_float(data.get(lat_key))
        lon = _safe_float(data.get(lon_key))
        if lat is not None and lon is not None:
            return lat, lon

    # EXIF-specific fields with optional reference
    lat = _safe_float(data.get("GPSLatitude"))
    lon = _safe_float(data.get("GPSLongitude"))
    if lat is not None and lon is not None:
        lat = _apply_gps_ref(lat, data.get("GPSLatitudeRef") or data.get("LatitudeRef"), ("N",))
        lon = _apply_gps_ref(lon, data.get("GPSLongitudeRef") or data.get("LongitudeRef"), ("E",))
        return lat, lon

    lat = _safe_float(data.get("GeoLatitude"))
    lon = _safe_float(data.get("GeoLongitude"))
    if lat is not None and lon is not None:
        return lat, lon

    gps_position = data.get("GPSPosition")
    if isinstance(gps_position, str):
        parts = re.split(r"[ ,;]+", gps_position.strip())
        if len(parts) >= 2:
            lat = _safe_float(parts[0])
            lon = _safe_float(parts[1])
            if lat is not None and lon is not None:
                return lat, lon

    return None


def extract_coordinates_from_meta(meta: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    to_visit: List[Any] = [meta]
    seen: set[int] = set()
    while to_visit:
        current = to_visit.pop()
        if isinstance(current, dict):
            obj_id = id(current)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            coords = _extract_coords_from_dict(current)
            if coords:
                return coords
            for value in current.values():
                if isinstance(value, dict):
                    to_visit.append(value)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, dict):
                            to_visit.append(item)
        elif isinstance(current, (list, tuple)):
            for item in current:
                if isinstance(item, dict):
                    to_visit.append(item)
    return None


_MODEL_KEYS = {"Model", "DeviceModel", "CameraModelName"}


def _normalize_model_label(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    return " ".join(value.split())


def extract_camera_models_from_meta(meta: Dict[str, Any]) -> List[str]:
    models: List[str] = []
    to_visit: List[Any] = [meta]
    seen: set[int] = set()
    while to_visit:
        current = to_visit.pop()
        if isinstance(current, dict):
            obj_id = id(current)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            for key, value in current.items():
                if key in _MODEL_KEYS and isinstance(value, str):
                    normalized = _normalize_model_label(value)
                    if normalized:
                        models.append(normalized)
                if isinstance(value, dict):
                    to_visit.append(value)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, dict):
                            to_visit.append(item)
        elif isinstance(current, (list, tuple)):
            for item in current:
                if isinstance(item, dict):
                    to_visit.append(item)
    return models


def _update_geo_bucket(
    buckets: Dict[Tuple[int, int], Dict[str, float]],
    lat: float,
    lon: float,
    *,
    precision: int = 5,
) -> None:
    scale = 10 ** precision
    key = (int(round(lat * scale)), int(round(lon * scale)))
    bucket = buckets.setdefault(key, {"count": 0, "lat_sum": 0.0, "lon_sum": 0.0})
    bucket["count"] = int(bucket.get("count", 0)) + 1
    bucket["lat_sum"] = float(bucket.get("lat_sum", 0.0)) + lat
    bucket["lon_sum"] = float(bucket.get("lon_sum", 0.0)) + lon


def _select_primary_location(
    buckets: Dict[Tuple[int, int], Dict[str, float]]
) -> Tuple[Optional[float], Optional[float], int]:
    best_lat: Optional[float] = None
    best_lon: Optional[float] = None
    best_count = 0
    for data in buckets.values():
        count = int(data.get("count", 0) or 0)
        if count <= 0:
            continue
        lat_sum = float(data.get("lat_sum", 0.0))
        lon_sum = float(data.get("lon_sum", 0.0))
        lat_avg = lat_sum / count
        lon_avg = lon_sum / count
        if (
            best_lat is None
            or count > best_count
            or (count == best_count and (lat_avg, lon_avg) < (best_lat, best_lon))
        ):
            best_lat = lat_avg
            best_lon = lon_avg
            best_count = count
    return best_lat, best_lon, best_count


def dataset_token_pairs(source: Optional[str], source_file: Optional[str]) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    src = (source or "").strip()
    src_file = (source_file or "").strip()
    if not src and not src_file:
        return tokens
    combined_value = f"{src}|{src_file}"
    label = ""
    if src_file:
        try:
            path = Path(src_file)
            label = path.name or src_file
        except Exception:
            label = src_file
    if not label and src:
        label = src
    if not label:
        label = combined_value or "данные"
    tokens.append((combined_value, label))
    if src:
        simple_value = f"{src}|"
        if simple_value != combined_value or src.lower() != label.lower():
            tokens.append((simple_value, src))
    return tokens

LOCAL_TZ = timezone(timedelta(hours=3))
_LOCAL_TZ_OFFSET = LOCAL_TZ.utcoffset(None) or timedelta()
if _LOCAL_TZ_OFFSET == timedelta():
    LOCAL_TZ_LABEL = "UTC"
else:
    total_minutes = int(_LOCAL_TZ_OFFSET.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    LOCAL_TZ_LABEL = f"UTC{sign}{hours:02d}:{minutes:02d}"


def is_admin_id(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    try:
        return int(user_id) in BOT_ADMIN_IDS
    except Exception:
        return False


def require_admin(msg: Message) -> bool:
    user = getattr(msg, "from_user", None)
    user_id = getattr(user, "id", None) if user else None
    return is_admin_id(user_id)

def setup_logging():
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    )

    ch = logging.StreamHandler(); ch.setLevel(level); ch.setFormatter(fmt)
    fh = RotatingFileHandler(LOGDIR / "vsco-bot.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(level); fh.setFormatter(fmt)

    root.handlers.clear()
    root.addHandler(ch)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("vsco-bot")


_PRIVATE_CHAT_LINK_RE = re.compile(r"^https?://t\.me/c/(\d{1,15})(?:/|$)", re.IGNORECASE)


def _normalize_positive_chat_id(value: int) -> int:
    """Convert positive identifiers into Telegram chat ids with a leading ``-``.

    Operators usually obtain chat identifiers for required communities from
    three different sources:

    1. ``-1001234567890`` — already valid and returned unchanged elsewhere.
    2. ``1001234567890`` — absolute value from logs/``Chat.id``.
    3. ``1234567890`` — fragment from ``https://t.me/c/<id>/...`` links.
    4. ``123456`` — legacy basic groups where the minus sign was omitted.

    The third variant uses the documented formula
    ``chat_id = -1000000000000 - fragment``.
    Smaller numbers are assumed to be legacy/basic group ids that simply miss
    the minus sign.
    """

    if value >= 1_000_000_000_000:
        # Already an absolute chat id (e.g. 1001234567890).
        return -value
    if value >= 1_000_000_000:
        # Fragment from t.me/c links (e.g. 1234567890).
        return -1_000_000_000_000 - value
    # Legacy/basic group identifier without the minus sign.
    return -value


def _parse_channel_id_value(raw: str, *, env_name: str) -> Optional[int | str]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        return text

    link_match = _PRIVATE_CHAT_LINK_RE.match(text)
    if link_match:
        fragment = int(link_match.group(1))
        chat_id = _normalize_positive_chat_id(fragment)
        log.info("Resolved %s private link fragment %s to chat id %s", env_name, fragment, chat_id)
        return chat_id

    digits = text.lstrip("+-")
    if digits.isdigit():
        number = int(digits)
        if text.startswith("-"):
            return -number
        normalized = _normalize_positive_chat_id(number)
        if normalized != number:
            log.info("Normalized %s value %s to chat id %s", env_name, text, normalized)
            return normalized
        return number

    log.warning("%s is not a numeric id, using raw value: %s", env_name, text)
    return text


def _resolve_channel_ids() -> None:
    global ARCHIVE_ADMIN_CHANNEL_ID, ARCHIVE_SUMMARY_CHANNEL_ID

    admin_raw = ARCHIVE_ADMIN_CHANNEL_ID_ENV or ARCHIVE_CHANNEL_ID_ENV
    summary_raw = ARCHIVE_SUMMARY_CHANNEL_ID_ENV

    if admin_raw:
        env_name = "BOT_ARCHIVE_ADMIN_CHANNEL_ID" if ARCHIVE_ADMIN_CHANNEL_ID_ENV else "BOT_ARCHIVE_CHANNEL_ID"
        ARCHIVE_ADMIN_CHANNEL_ID = _parse_channel_id_value(admin_raw, env_name=env_name)

    if summary_raw:
        ARCHIVE_SUMMARY_CHANNEL_ID = _parse_channel_id_value(summary_raw, env_name="BOT_ARCHIVE_SUMMARY_CHANNEL_ID")


_resolve_channel_ids()


@dataclass(frozen=True)
class RequiredChat:
    chat_id: int | str
    label: str
    invite_link: str


def _make_required_chat(
    raw_id: str,
    *,
    env_name: str,
    label: str,
    invite_link: str,
) -> Optional[RequiredChat]:
    chat_id = _parse_channel_id_value(raw_id, env_name=env_name)
    if chat_id is None:
        return None

    display_label = label.strip() if label else ""
    link = invite_link.strip() if invite_link else ""

    if not display_label:
        if link:
            display_label = link
        elif isinstance(chat_id, str) and chat_id.startswith("@"):
            display_label = chat_id
        else:
            display_label = str(chat_id)

    if not link and isinstance(chat_id, str) and chat_id.startswith("@"):
        link = f"https://t.me/{chat_id[1:]}"

    return RequiredChat(chat_id=chat_id, label=display_label, invite_link=link)


REQUIRED_CHATS: List[RequiredChat] = []

_required_channel = _make_required_chat(
    REQUIRED_CHANNEL_ID_ENV,
    env_name="BOT_REQUIRED_CHANNEL_ID",
    label=REQUIRED_CHANNEL_LABEL,
    invite_link=REQUIRED_CHANNEL_LINK,
)
if _required_channel:
    REQUIRED_CHATS.append(_required_channel)

_required_group = _make_required_chat(
    REQUIRED_GROUP_ID_ENV,
    env_name="BOT_REQUIRED_GROUP_ID",
    label=REQUIRED_GROUP_LABEL,
    invite_link=REQUIRED_GROUP_LINK,
)
if _required_group:
    REQUIRED_CHATS.append(_required_group)


ACCESS_CACHE_TTL = int(os.getenv("BOT_REQUIRED_ACCESS_CACHE_TTL", "30") or "30")
_ACCESS_CACHE: Dict[int, Tuple[float, bool]] = {}
_ACCESS_PENDING: Dict[int, asyncio.Task[bool]] = {}


def _access_message_html() -> str:
    if not REQUIRED_CHATS:
        return ""

    lines = [
        "🚫 <b>Доступ ограничен</b>.",
        "Для использования бота вступите в следующие сообщества:",
    ]
    for chat in REQUIRED_CHATS:
        if chat.invite_link:
            link = escape(chat.invite_link, quote=True)
            label = escape(chat.label)
            lines.append(f"• <a href=\"{link}\">{label}</a>")
        else:
            lines.append(f"• {escape(chat.label)}")
    lines.append("После вступления повторите команду.")
    return "\n".join(lines)


ACCESS_MESSAGE_HTML = _access_message_html()


def _is_positive_membership_status(member: Any) -> bool:
    status = getattr(member, "status", None)
    if status in {"creator", "administrator", "member"}:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def _check_required_chat_membership(chat: RequiredChat, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat.chat_id, user_id)
    except TelegramBadRequest as err:
        log.warning(
            "Failed to check membership for user %s in %s: %s",
            user_id,
            chat.chat_id,
            err,
        )
        return False
    except TelegramNetworkError as err:
        log.error(
            "Network error while checking membership for %s in %s",
            user_id,
            chat.chat_id,
            exc_info=err,
        )
        return False

    return _is_positive_membership_status(member)


async def _is_user_allowed(user_id: int) -> bool:
    if not REQUIRED_CHATS:
        return True

    now = time.time()
    cached = _ACCESS_CACHE.get(user_id)
    if cached and now - cached[0] <= ACCESS_CACHE_TTL:
        return cached[1]

    pending = _ACCESS_PENDING.get(user_id)
    if pending:
        return await pending

    async def _compute_and_cache() -> bool:
        tasks = [
            asyncio.create_task(_check_required_chat_membership(chat, user_id))
            for chat in REQUIRED_CHATS
        ]
        allowed = True
        try:
            for task in asyncio.as_completed(tasks):
                result = await task
                if not result:
                    allowed = False
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                with contextlib.suppress(Exception):
                    await asyncio.gather(*tasks, return_exceptions=True)
            _ACCESS_CACHE[user_id] = (time.time(), allowed)
        return allowed

    task = asyncio.create_task(_compute_and_cache())
    _ACCESS_PENDING[user_id] = task
    try:
        return await task
    finally:
        _ACCESS_PENDING.pop(user_id, None)


async def ensure_user_has_access(message: Message, user_id: Optional[int] = None) -> bool:
    if not REQUIRED_CHATS:
        return True

    if user_id is None:
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None) if user else None
    if user_id is None:
        return False

    if is_admin_id(user_id):
        return True

    allowed = await _is_user_allowed(user_id)
    if allowed:
        return True

    if ACCESS_MESSAGE_HTML:
        with contextlib.suppress(Exception):
            await message.answer(ACCESS_MESSAGE_HTML)
    return False


async def ensure_callback_access(cq: CallbackQuery) -> bool:
    if not REQUIRED_CHATS:
        return True

    user = cq.from_user
    user_id = getattr(user, "id", None) if user else None
    if user_id is None:
        return False

    if is_admin_id(user_id):
        return True

    allowed = await _is_user_allowed(user_id)
    if allowed:
        return True

    text = ACCESS_MESSAGE_HTML or "Доступ ограничен."
    with contextlib.suppress(Exception):
        await cq.answer(text, show_alert=True)
    if cq.message:
        with contextlib.suppress(Exception):
            await cq.message.answer(text)
    return False


# ---------------------- VSCO constants ----------------------
VSCO_RESERVED = {
    "", "discover", "search", "images", "image", "media", "terms", "privacy",
    "about", "gallery", "videos", "press", "company", "legal", "pricing",
    "signin", "login", "signup", "api"
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TG_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
URL_RE = re.compile(r'(https?://[^\s<>"\'\]\)]+)', re.IGNORECASE)
MEDIA_PATH_RE = re.compile(r"^/([^/]+)/media/([A-Za-z0-9]+)")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VSCO-Bot/1.0; +https://example.org/bot)"
}

# ---------------------- DB ----------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = db_connect()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS links(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER NOT NULL,
      username TEXT NOT NULL,
      url TEXT NOT NULL,
      extra_json TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      UNIQUE(chat_id, username)
    )""")
    conn.execute("""
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
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS comments(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      item_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      comment TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(item_id, comment),
      FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
    )""")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "added_by" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN added_by TEXT DEFAULT ''")
    if "meta_json" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN meta_json TEXT DEFAULT ''")
    link_cols = {row[1] for row in conn.execute("PRAGMA table_info(links)")}
    if "extra_json" not in link_cols:
        conn.execute("ALTER TABLE links ADD COLUMN extra_json TEXT DEFAULT ''")
    conn.commit(); conn.close()
    log.info("DB initialized at %s", DB_PATH)

# ---------------------- helpers ----------------------
def utc_now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat()

def _since_utc_iso(days: int) -> str:
    return (datetime.now(LOCAL_TZ) - timedelta(days=days)).isoformat()


def resolve_added_by(user: Optional[User]) -> str:
    if user is None:
        return ""
    if user.username:
        return f"@{user.username}"
    full_name = (user.full_name or "").strip()
    return full_name


def added_by_display_and_link(value: str) -> Tuple[str, Optional[str]]:
    raw = (value or "").strip()
    if not raw:
        return "", None
    handle = raw[1:] if raw.startswith("@") else raw
    if TG_USERNAME_RE.match(handle):
        return f"@{handle}", f"https://t.me/{handle}"
    return raw, None


def added_by_html(value: str) -> str:
    display, link = added_by_display_and_link(value)
    if not display:
        return ""
    if link:
        return f"<a href=\"{escape(link)}\">{escape(display)}</a>"
    return escape(display)


def parse_profile_tabs_payload(raw: str) -> List[Dict[str, str]]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []

    candidates: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        maybe_tabs = payload.get("profile_tabs") or payload.get("tabs")
        if isinstance(maybe_tabs, list):
            candidates = [entry for entry in maybe_tabs if isinstance(entry, dict)]
    elif isinstance(payload, list):
        candidates = [entry for entry in payload if isinstance(entry, dict)]

    sanitized: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for tab in candidates:
        href_raw = tab.get("href")
        href = str(href_raw).strip() if href_raw is not None else ""
        if not href:
            continue
        tab_id_raw = tab.get("id")
        tab_id = str(tab_id_raw).strip() if tab_id_raw is not None else ""
        key = (tab_id.lower(), href)
        if key in seen:
            continue
        seen.add(key)
        entry: Dict[str, str] = {"href": href}
        if tab_id:
            entry["id"] = tab_id
        for field_name in ("label", "slug", "active"):
            value = tab.get(field_name)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                entry[field_name] = text
        sanitized.append(entry)
    return sanitized


@dataclass
class ProfileArchiveInfo:
    username: str
    profile_url: str = ""
    added_by_raw: str = ""
    last_created_at: Optional[str] = None
    comments: List[str] = field(default_factory=list)
    items_count: int = 0


@dataclass
class ArchiveSummaryData:
    username: Optional[str] = None
    display_name: Optional[str] = None
    profile_url: Optional[str] = None
    total_media: Optional[int] = None
    comments: List[str] = field(default_factory=list)
    added_by_html: Optional[str] = None
    last_created_at: Optional[str] = None


def fetch_profile_archive_info(username: Optional[str], *, max_items: int = 120) -> Optional[ProfileArchiveInfo]:
    clean = (username or "").strip()
    if not clean:
        return None

    conn = db_connect()
    try:
        rows = conn.execute(
            """
            SELECT id, profile_url, added_by, created_at
            FROM items
            WHERE username = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (clean, max_items),
        ).fetchall()
        if not rows:
            return None

        info = ProfileArchiveInfo(username=clean)
        info.items_count = len(rows)
        for _, profile_url, added_by, created_at in rows:
            if not info.profile_url and isinstance(profile_url, str) and profile_url.strip():
                info.profile_url = profile_url.strip()
            if not info.added_by_raw and isinstance(added_by, str) and added_by.strip():
                info.added_by_raw = added_by.strip()
            if not info.last_created_at and isinstance(created_at, str) and created_at.strip():
                info.last_created_at = created_at.strip()
            if info.profile_url and info.added_by_raw and info.last_created_at:
                break

        item_ids = [int(row[0]) for row in rows if row and row[0] is not None]
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            seen: set[str] = set()
            for (comment,) in conn.execute(
                f"SELECT comment FROM comments WHERE item_id IN ({placeholders}) ORDER BY id ASC",
                item_ids,
            ):
                if not isinstance(comment, str):
                    continue
                trimmed = comment.strip()
                if trimmed and trimmed not in seen:
                    info.comments.append(trimmed)
                    seen.add(trimmed)
        return info
    finally:
        conn.close()

DAILY_PROFILE_LIMIT = 20


def _daily_profile_count(chat_id: int) -> int:
    since = _since_utc_iso(1)
    conn = db_connect()
    username_key_sql = "LOWER(TRIM(COALESCE(username,'')))"
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT NULLIF({username_key_sql}, ''))
            FROM items
            WHERE chat_id = ? AND created_at >= ?
            """,
            (chat_id, since),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return 0
    return int(row[0] or 0)


def has_daily_data_access(chat_id: int, user_id: Optional[int] = None) -> Tuple[bool, str]:
    """Check whether a chat accumulated enough fresh items for export/download."""
    profiles_added = _daily_profile_count(chat_id)

    counters = f"новых профилей — {profiles_added}/{DAILY_PROFILE_LIMIT}"

    if is_admin_id(user_id):
        text = (
            "👑 Администратор: лимиты отключены. Доступ к экспорту и скачиванию всегда открыт.\n"
            f"За последние 24 часа: {counters}."
        )
        return True, text

    allowed = profiles_added >= DAILY_PROFILE_LIMIT

    if allowed:
        text = (
            "✅ Доступ к экспорту и скачиванию активен на текущие сутки.\n"
            f"За последние 24 часа: {counters}. Лимит обновляется ежедневно."
        )
    else:
        text = (
            "🚫 Нужно добавить за последние 24 часа минимум "
            f"{DAILY_PROFILE_LIMIT} новых профилей.\n"
            f"Сейчас: {counters}. Лимит обнуляется каждый день."
        )

    return allowed, text

def is_vsco_url(u: str) -> bool:
    try:
        p = urlparse(u); host = (p.netloc or "").lower()
        return host in VSCO_HOSTS or host in VSCO_SHORT_HOSTS or host in VSCO_PERCEPTION_HOSTS
    except Exception:
        return False

def username_from_vsco_co(url: str) -> Optional[str]:
    try:
        p = urlparse(url)
        if (p.netloc or "").lower() not in VSCO_HOSTS: return None
        segs = [s for s in (p.path or "").split("/") if s]
        if not segs: return None
        cand = segs[0]
        if cand.lower() in VSCO_RESERVED: return None
        if USERNAME_RE.match(cand): return cand
    except Exception:
        pass
    return None

def classify_vsco_path(url: str) -> Dict[str, Any]:
    """
    Возвращает:
      {'kind':'media','username':..., 'media_id':..., 'final_url':...} или
      {'kind':'profile','username':..., 'final_url':...} или {}
    """
    try:
        p = urlparse(url)
        path = p.path or ""
        host = (p.netloc or "").lower()
        if host in VSCO_PERCEPTION_HOSTS:
            m = SHORT_SLUG_RE.match(path)
            if m:
                slug = m.group(1)
                if slug:
                    return {
                        "kind": "perception",
                        "slug": slug,
                        "final_url": build_perception_gallery_url(slug),
                    }
        m = MEDIA_PATH_RE.match(path)
        if m:
            u, mid = m.group(1), m.group(2)
            if USERNAME_RE.match(u):
                return {"kind": "media", "username": u, "media_id": mid, "final_url": url}
        # профиль
        u = username_from_vsco_co(url)
        if u:
            return {"kind": "profile", "username": u, "final_url": f"https://vsco.co/{u}"}
    except Exception:
        pass
    return {}

async def resolve_vsco_short(url: str, session: aiohttp.ClientSession) -> str:
    return await resolve_vsco_short_link(
        url,
        session,
        headers=HEADERS,
        request_kwargs={"timeout": 10},
    )

async def fetch_media_asset_urls(
    url: str,
    session: aiohttp.ClientSession,
    *,
    max_width: int = MEDIA_PAGE_MAX_WIDTH,
) -> List[str]:
    """Сканирует страницу медиа VSCO и возвращает медиа-URL с апскейлом ?w=."""

    html = ""
    try:
        async with session.get(url, allow_redirects=True, timeout=12, headers=HEADERS) as resp:
            html = await resp.text(errors="ignore")
    except Exception as e:
        log.warning("fetch_media_asset_urls failed: %s", e)
        return []

    urls = extract_media_urls_from_html(html, max_width=max_width, root=url)
    if not urls:
        fallback_sources = extract_vsco_media_urls(html, sources=("og", "twitter", "responsive", "inline"))
        for candidate in fallback_sources:
            normalized = normalize_media_url(candidate)
            if not normalized:
                continue
            final_url = upscale_w_param(normalized, max_width)
            if not is_vsco_logo_url(final_url) and is_media_url(final_url):
                urls.append(final_url)

    return dedupe_keep_order(urls)

# === Парсер "ссылка, комментарий до следующей ссылки" ========================
def _describe_entities_for_log(entities: Optional[Sequence[MessageEntity]]) -> List[Dict[str, Any]]:
    if not entities:
        return []

    described: List[Dict[str, Any]] = []
    for entity in entities:
        entity_type = getattr(entity, "type", "")
        if hasattr(entity_type, "value"):
            entity_type = entity_type.value
        described.append(
            {
                "type": str(entity_type or ""),
                "offset": getattr(entity, "offset", None),
                "length": getattr(entity, "length", None),
                "url": getattr(entity, "url", None),
            }
        )

    return described


def _expand_text_with_entities(text: Optional[str], entities: Optional[Sequence[MessageEntity]]) -> str:
    if not text:
        return ""
    if not entities:
        return text

    surrogate_text = add_surrogates(text)
    parts: List[str] = []
    cursor_units = 0
    total_units = len(surrogate_text) // 2

    for entity in sorted(entities, key=lambda e: getattr(e, "offset", 0) or 0):
        offset_units = max(int(getattr(entity, "offset", 0) or 0), 0)
        length_units = max(int(getattr(entity, "length", 0) or 0), 0)

        if offset_units < cursor_units:
            continue
        if offset_units > total_units:
            break

        if offset_units > cursor_units:
            start = cursor_units * 2
            end = offset_units * 2
            parts.append(remove_surrogates(surrogate_text[start:end]))

        end_units = min(offset_units + length_units, total_units)
        start_bytes = offset_units * 2
        end_bytes = end_units * 2
        entity_slice = surrogate_text[start_bytes:end_bytes]
        entity_text = remove_surrogates(entity_slice)

        entity_type = getattr(entity, "type", "")
        if hasattr(entity_type, "value"):
            entity_type = entity_type.value
        entity_type = str(entity_type or "")

        replacement = entity_text
        if entity_type == "text_link" and getattr(entity, "url", None):
            replacement = entity.url or ""
        elif entity_type == "url":
            replacement = entity_text

        parts.append(replacement)
        cursor_units = end_units

    if cursor_units < total_units:
        start = cursor_units * 2
        parts.append(remove_surrogates(surrogate_text[start:]))

    return "".join(parts)


def parse_vsco_pairs_from_message(text: Optional[str], entities: Optional[Sequence[MessageEntity]]) -> List[Dict[str, str]]:
    log.debug(
        "parse_vsco_pairs_from_message: raw_text=%r, entities=%s",
        text,
        _describe_entities_for_log(entities),
    )
    expanded = _expand_text_with_entities(text, entities)
    pairs = _parse_vsco_pairs(expanded)
    log.debug(
        "parse_vsco_pairs_from_message: expanded_text=%r, parsed_pairs=%s",
        expanded,
        pairs,
    )
    return pairs


def _parse_vsco_pairs(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not text:
        log.debug("_parse_vsco_pairs: empty text, nothing to parse")
        return out
    log.debug("_parse_vsco_pairs: scanning text length=%d", len(text))
    matches = [m for m in URL_RE.finditer(text) if is_vsco_url(m.group(0))]
    if not matches:
        log.debug("_parse_vsco_pairs: no VSCO URLs found")
    for i, m in enumerate(matches):
        raw = m.group(0)
        url_trimmed = raw.rstrip('.,);!?]')
        url_end = m.start() + len(url_trimmed)
        j = url_end
        while j < len(text) and text[j].isspace():
            j += 1
        comment = ""
        next_start = matches[i+1].start() if i + 1 < len(matches) else len(text)
        if j < len(text):
            if text[j] == ',':
                k = j + 1
                while k < len(text) and text[k].isspace():
                    k += 1
                comment_start = k
            else:
                comment_start = j
            if comment_start < next_start:
                comment = text[comment_start:next_start].strip()
        out.append({"url": url_trimmed, "comment": comment})
        log.debug(
            "_parse_vsco_pairs: match #%d url=%s comment=%r", i + 1, url_trimmed, comment
        )
    return out

def parse_vsco_pairs_from_cell(cell: str) -> List[Dict[str, str]]:
    if not isinstance(cell, str): return []
    return _parse_vsco_pairs(cell)

async def normalize_vsco_pairs(pairs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Нормализация:
    - короткие vs.co -> финальные
    - media: добавляем image_url (если удалось извлечь), profile_url нормализуем
    - profile: только username + profile_url
    Возвращает элементы:
      {'username':..., 'url': <profile_url>, 'comment':..., 'image_url': <'' или ссылка на картинку>}
    """
    if not pairs:
        log.debug("normalize_vsco_pairs: no pairs provided")
        return []
    log.debug("normalize_vsco_pairs: start with %d pair(s)", len(pairs))
    res: List[Dict[str, str]] = []
    async with aiohttp.ClientSession() as s:
        for pair in pairs:
            u = pair.get("url", ""); c = (pair.get("comment") or "").strip()
            if not is_vsco_url(u):
                log.debug("normalize_vsco_pairs: skip non-VSCO url=%s", u)
                continue

            p = urlparse(u); host = (p.netloc or "").lower()
            final = u
            if host in VSCO_SHORT_HOSTS:
                final = await resolve_vsco_short(u, s)
                log.debug("normalize_vsco_pairs: resolved short url %s -> %s", u, final)

            info = classify_vsco_path(final)
            if not info:
                usr = username_from_vsco_co(final)
                if usr:
                    res.append({"username": usr, "url": f"https://vsco.co/{usr}", "comment": c, "image_url": ""})
                    log.debug(
                        "normalize_vsco_pairs: fallback username=%s from url=%s", usr, final
                    )
                continue

            if info["kind"] == "profile":
                username = info["username"]
                profile_url = info["final_url"]
                gallery_url = normalize_vsco_profile_url(profile_url) or f"{profile_url.rstrip('/')}/gallery"
                assets: List[str] = []
                try:
                    assets = await playwright_scan_profile(
                        gallery_url,
                        max_width=MEDIA_PAGE_MAX_WIDTH,
                        session=s,
                        logger=log,
                    )
                except Exception:
                    log.exception("playwright_scan_profile failed for profile %s", profile_url)
                    assets = []

                if not assets:
                    res.append({"username": username, "url": profile_url, "comment": c, "image_url": ""})
                    log.debug(
                        "normalize_vsco_pairs: profile=%s no assets found, added placeholder",
                        profile_url,
                    )
                else:
                    for asset in assets:
                        res.append({"username": username, "url": profile_url, "comment": c, "image_url": asset})
                    log.debug(
                        "normalize_vsco_pairs: profile=%s appended %d asset(s)",
                        profile_url,
                        len(assets),
                    )
            elif info["kind"] == "media":
                usr = info["username"]
                profile_url = f"https://vsco.co/{usr}"
                asset_urls = await fetch_media_asset_urls(final, s)
                if not asset_urls:
                    asset_urls = [final]
                log.debug(
                    "normalize_vsco_pairs: media url=%s resolved to %d asset(s)",
                    final,
                    len(asset_urls),
                )
                for asset in asset_urls:
                    res.append({"username": usr, "url": profile_url, "comment": c, "image_url": asset})
            elif info["kind"] == "perception":
                slug = info["slug"]
                res.append({"username": slug, "url": info["final_url"], "comment": c, "image_url": ""})
                log.debug(
                    "normalize_vsco_pairs: perception slug=%s final_url=%s",
                    slug,
                    info["final_url"],
                )
    uniq = {(r["username"], r["url"], r["comment"], r.get("image_url","")): r for r in res}
    log.debug(
        "normalize_vsco_pairs: produced %d unique record(s) from %d input(s)",
        len(uniq),
        len(res),
    )
    return list(uniq.values())


def _profile_slug_from_url(profile_url: str) -> str:
    parsed = urlparse(profile_url)
    segments = [segment for segment in (parsed.path or "").split("/") if segment]
    candidate = segments[0] if segments else ""
    if candidate.lower() == "gallery" and len(segments) >= 2:
        candidate = segments[-2]
    if not candidate and parsed.netloc:
        candidate = parsed.netloc.split(".")[0]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", candidate)
    return safe or "profile"


def persist_profile_media_urls(
    pairs: Sequence[Dict[str, str]],
    base_dir: Path,
) -> List[Path]:
    grouped: Dict[str, List[str]] = {}
    for entry in pairs:
        profile_url = (entry.get("url") or "").strip()
        image_url = (entry.get("image_url") or "").strip()
        if not profile_url or not image_url:
            continue
        if is_vsco_logo_url(image_url):
            continue
        grouped.setdefault(profile_url, []).append(image_url)

    saved: List[Path] = []
    if not grouped:
        return saved

    profiles_root = base_dir / "profiles"
    for profile_url, urls in grouped.items():
        deduped = dedupe_keep_order(urls)
        if not deduped:
            continue
        slug = _profile_slug_from_url(profile_url)
        dest_dir = profiles_root / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "urls_extracted.txt"
        with dest.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(deduped) + "\n")
        saved.append(dest)
    return saved


def format_profile_urls_notice(paths: Sequence[Path], base_dir: Path) -> str:
    if not paths:
        return ""
    lines: List[str] = []
    for path in paths:
        try:
            display = path.relative_to(base_dir)
        except ValueError:
            display = path
        lines.append(f"• <code>{escape(str(display))}</code>")
    return "\nФайлы со ссылками:\n" + "\n".join(lines)


# ---------------------- DB ops ----------------------
def add_vsco_link_legacy(username: str, url: str, chat_id: int, conn: sqlite3.Connection) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO links(chat_id,username,url,created_at) VALUES(?,?,?,?)",
        (chat_id, username, url, utc_now_iso())
    )
    return cur.rowcount > 0


def format_new_links_block(links: Sequence[str]) -> str:
    uniq_links = [link for link in dict.fromkeys(links or []) if link]
    if not uniq_links:
        return ""
    html_links = []
    for link in uniq_links:
        href = escape(link, quote=True)
        text = escape(link)
        html_links.append(f"<a href=\"{href}\">{text}</a>")
    return "\nНовые ссылки:\n" + "\n".join(html_links)

def _get_item_id(conn: sqlite3.Connection, username: str, profile_url: str, image_url: str) -> Optional[int]:
    row = conn.execute(
        """SELECT id FROM items
           WHERE username=? AND COALESCE(profile_url,'')=COALESCE(?, '')
             AND COALESCE(image_url,'')=COALESCE(?, '')
           LIMIT 1""",
        (username, profile_url, image_url)
    ).fetchone()
    return row[0] if row else None

def _insert_item(conn: sqlite3.Connection, chat_id: int, username: str, profile_url: str,
                 image_url: str, latitude: Optional[float], longitude: Optional[float],
                 source: str, source_file: Optional[str], added_by: str) -> int:
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO items(chat_id,username,latitude,longitude,profile_url,image_url,source,source_file,added_by,meta_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            chat_id,
            username,
            latitude,
            longitude,
            profile_url,
            image_url,
            source or "",
            source_file or "",
            added_by or "",
            "",
            utc_now_iso(),
        )
    )
    return cur.lastrowid

def upsert_items_with_comments(chat_id: int, pairs: List[Dict[str,str]], source: str, source_file: Optional[str], added_by: str) -> Tuple[int,int,List[str]]:
    if not pairs:
        log.debug(
            "upsert_items_with_comments: chat_id=%s source=%s no pairs provided",
            chat_id,
            source,
        )
        return (0,0,[])
    log.debug(
        "upsert_items_with_comments: chat_id=%s source=%s source_file=%s added_by=%s pairs=%d",
        chat_id,
        source,
        source_file,
        added_by,
        len(pairs),
    )
    conn = db_connect()
    added_items = added_comments = 0
    new_links: List[str] = []
    try:
        for r in pairs:
            username = (r.get("username") or "").lstrip("@")
            profile_url = r.get("url") or ""
            image_url = (r.get("image_url") or "").strip()
            comment = (r.get("comment") or "").strip()
            if not username or not profile_url: continue

            log.debug(
                "upsert_items_with_comments: processing username=%s profile=%s image=%s comment_present=%s",
                username,
                profile_url,
                image_url,
                bool(comment),
            )
            item_id = _get_item_id(conn, username, profile_url, image_url)
            if item_id is None:
                item_id = _insert_item(conn, chat_id, username, profile_url, image_url, None, None, source, source_file, added_by)
                added_items += 1
                log.debug(
                    "upsert_items_with_comments: inserted new item id=%s username=%s",
                    item_id,
                    username,
                )

            if comment:
                conn.execute(
                    "INSERT OR IGNORE INTO comments(item_id,chat_id,comment,created_at) VALUES(?,?,?,?)",
                    (item_id, chat_id, comment, utc_now_iso())
                )
                if conn.total_changes > 0:
                    added_comments += 1
                    log.debug(
                        "upsert_items_with_comments: added comment for item_id=%s", item_id
                    )

            if add_vsco_link_legacy(username, profile_url, chat_id, conn):
                new_links.append(profile_url)
                log.debug(
                    "upsert_items_with_comments: stored legacy link %s", profile_url
                )

        conn.commit()
        log.debug(
            "upsert_items_with_comments: committed items=%d comments=%d links=%d",
            added_items,
            added_comments,
            len(new_links),
        )
        return (added_items, added_comments, new_links)
    finally:
        conn.close()

async def _maybe_schedule_profile_scans(
    chat_id: int,
    records: Iterable[Dict[str, Any]],
    new_links: Sequence[str],
    *,
    added_by: str,
    source: str,
) -> None:
    if not new_links:
        return

    profile_map: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_url = (record.get("url") or record.get("profile_url") or "").strip()
        if not raw_url:
            continue
        normalized_url = normalize_vsco_profile_url(raw_url) or raw_url
        username = (record.get("username") or "").strip().lstrip("@")
        if not username:
            username = username_from_vsco_co(normalized_url) or ""
        if not username:
            continue
        entry = profile_map.setdefault(
            normalized_url,
            {"username": username, "has_media": False},
        )
        image_url = (record.get("image_url") or "").strip()
        if image_url:
            entry["has_media"] = True

    scheduled = 0
    for link in dict.fromkeys(new_links):
        normalized_link = normalize_vsco_profile_url(link) or link
        data = profile_map.get(normalized_link)
        username = (data or {}).get("username") or username_from_vsco_co(normalized_link) or ""
        if not username:
            continue
        if data and data.get("has_media"):
            continue
        job = ProfileScanJob(
            chat_id=chat_id,
            username=username,
            profile_url=normalized_link,
            source=source,
            added_by=added_by,
        )
        try:
            scheduled_now = await _enqueue_profile_scan(job)
        except Exception:
            log.exception(
                "Failed to enqueue profile scan: chat_id=%s profile_url=%s",
                chat_id,
                normalized_link,
            )
            continue
        if scheduled_now:
            scheduled += 1

    if scheduled:
        log.info(
            "Queued %s background profile scan job(s) for chat_id=%s",
            scheduled,
            chat_id,
        )


def insert_full_rows_from_html(chat_id: int, rows: List[Dict[str,Any]], source_file: str, added_by: str) -> Tuple[int, List[str]]:
    if not rows:
        log.debug(
            "insert_full_rows_from_html: chat_id=%s source_file=%s empty rows",
            chat_id,
            source_file,
        )
        return (0, [])
    log.debug(
        "insert_full_rows_from_html: chat_id=%s source_file=%s rows=%d",
        chat_id,
        source_file,
        len(rows),
    )
    conn = db_connect()
    added = 0
    new_links: List[str] = []
    try:
        for r in rows:
            username = (r.get("username") or "").lstrip("@")
            profile_url = r.get("profile_url") or (f"https://vsco.co/{username}" if username else "")
            image_url = r.get("image_url") or ""
            lat = r.get("latitude"); lon = r.get("longitude")
            try: lat = float(lat) if lat not in ("", None, "None") else None
            except Exception: lat = None
            try: lon = float(lon) if lon not in ("", None, "None") else None
            except Exception: lon = None
            if not username or not profile_url:
                continue

            if _get_item_id(conn, username, profile_url, image_url) is None:
                new_id = _insert_item(conn, chat_id, username, profile_url, image_url, lat, lon, "html", source_file, added_by)
                added += 1
                log.debug(
                    "insert_full_rows_from_html: inserted item id=%s username=%s profile=%s",
                    new_id,
                    username,
                    profile_url,
                )

            if add_vsco_link_legacy(username, profile_url, chat_id, conn):
                new_links.append(profile_url)
                log.debug(
                    "insert_full_rows_from_html: stored legacy link %s", profile_url
                )
        conn.commit()
        log.debug(
            "insert_full_rows_from_html: committed %d item(s) %d new link(s)",
            added,
            len(new_links),
        )
        return (added, new_links)
    finally:
        conn.close()


def ingest_download_results(job: "DLJob", user_dir: Path) -> Tuple[int, bool]:
    """Ingest downloaded VSCO media links from the downloader manifest into the DB.

    Returns a tuple ``(items_added, profile_link_added)``.
    """

    log.debug(
        "ingest_download_results: job_id=%s user_dir=%s",
        job.id,
        user_dir,
    )
    manifest_path = user_dir / "manifest.json"
    manifest_meta: Dict[str, Any] = {}
    items_data: List[Dict[str, Any]] = []

    if manifest_path.exists():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Job #%s: failed to read manifest %s", job.id, manifest_path)
        else:
            if isinstance(raw, dict):
                manifest_meta = raw
                maybe_items = raw.get("items")
                if isinstance(maybe_items, list):
                    items_data = [x for x in maybe_items if isinstance(x, dict)]
            elif isinstance(raw, list):
                items_data = [x for x in raw if isinstance(x, dict)]
            log.debug(
                "ingest_download_results: manifest loaded items=%d",
                len(items_data),
            )

    if not items_data:
        urls_path = user_dir / "urls_extracted.txt"
        if urls_path.exists():
            try:
                urls = [line.strip() for line in urls_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except Exception:
                urls = []
            if urls:
                items_data = [{"url": u, "ok": True} for u in urls]
                log.debug(
                    "ingest_download_results: fallback to urls_extracted.txt entries=%d",
                    len(items_data),
                )

    username = _extract_username_from_target(job.target)
    if not username:
        candidate = manifest_meta.get("username") if isinstance(manifest_meta, dict) else None
        if isinstance(candidate, str) and candidate.strip():
            username = candidate.strip()
            log.debug(
                "ingest_download_results: username resolved from manifest=%s",
                username,
            )

    profile_url = ""
    manifest_profile = manifest_meta.get("profile_url") if isinstance(manifest_meta, dict) else None
    if isinstance(manifest_profile, str) and manifest_profile.strip():
        info = classify_vsco_path(manifest_profile.strip())
        if info.get("kind") == "profile" and isinstance(info.get("final_url"), str):
            profile_url = info["final_url"].strip()
        elif is_vsco_url(manifest_profile):
            profile_url = manifest_profile.strip()

    if not username and profile_url:
        extracted = username_from_vsco_co(profile_url)
        if extracted:
            username = extracted
            log.debug(
                "ingest_download_results: username extracted from profile_url=%s",
                username,
            )

    if not profile_url and username:
        profile_url = f"https://vsco.co/{username}"
        log.debug(
            "ingest_download_results: profile_url synthesized=%s",
            profile_url,
        )

    if not profile_url and job.target and is_vsco_url(job.target):
        info = classify_vsco_path(job.target)
        if info.get("kind") == "profile" and isinstance(info.get("final_url"), str):
            profile_url = info["final_url"].strip()
        else:
            profile_url = job.target.strip()

    username = (username or "").strip().lstrip("@")
    profile_url = (profile_url or "").strip()

    if not username and not profile_url:
        log.info("Job #%s: skipped DB ingest — username/profile unresolved", job.id)
        return (0, False)

    conn = db_connect()
    added_items = 0
    link_added = False
    try:
        seen_urls: set[str] = set()
        for item in items_data:
            if not isinstance(item, dict):
                continue
            if "ok" in item and not item.get("ok"):
                continue
            image_url = ""
            for key in ("image_url", "responsive_url", "url"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    image_url = value.strip()
                    break
            if not image_url or is_vsco_logo_url(image_url):
                continue
            if image_url in seen_urls:
                continue
            seen_urls.add(image_url)
            if _get_item_id(conn, username, profile_url, image_url) is None:
                _insert_item(
                    conn,
                    chat_id=job.chat_id,
                    username=username,
                    profile_url=profile_url,
                    image_url=image_url,
                    latitude=None,
                    longitude=None,
                    source="download",
                    source_file=user_dir.name,
                    added_by=job.requested_by,
                )
                added_items += 1

        if username and profile_url:
            link_added = add_vsco_link_legacy(username, profile_url, job.chat_id, conn)

        conn.commit()
    finally:
        conn.close()

    return (added_items, link_added)


def _manifest_item_has_media(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("ok") is False:
        return False
    for key in ("image_url", "responsive_url", "url"):
        value = item.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and not is_vsco_logo_url(value):
                return True
    return False


def infer_media_total_from_manifest(raw: Any) -> Optional[int]:
    total: Optional[int] = None

    def _bump(candidate: Optional[int]) -> None:
        nonlocal total
        if isinstance(candidate, int) and candidate > 0:
            total = candidate if total is None else max(total, candidate)

    if isinstance(raw, dict):
        for key in ("count", "total", "items_count", "media_count"):
            _bump(raw.get(key))
        items = raw.get("items")
        if isinstance(items, list):
            count = sum(1 for item in items if _manifest_item_has_media(item))
            _bump(count)
    elif isinstance(raw, list):
        count = sum(1 for item in raw if _manifest_item_has_media(item))
        _bump(count)

    return total


def infer_media_total_from_urls_file(path: Path) -> Optional[int]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    total = 0
    for line in text.splitlines():
        url = line.strip()
        if not url or is_vsco_logo_url(url):
            continue
        total += 1
    return total or None


def infer_media_total(user_dir: Path) -> Optional[int]:
    manifest = user_dir / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            data = None
        total = infer_media_total_from_manifest(data)
        if total is not None:
            return total

    urls_file = user_dir / "urls_extracted.txt"
    if urls_file.exists():
        total = infer_media_total_from_urls_file(urls_file)
        if total is not None:
            return total

    return None

# ---------------------- Stats ----------------------
_USERNAME_KEY_SQL = "LOWER(TRIM(COALESCE(username,'')))"
_MEDIA_KEY_SQL = "COALESCE(NULLIF(TRIM(image_url),''), printf('item:%011d', id))"


def _items_where_clause(since_iso: Optional[str], chat_id: int, scope: str) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if since_iso:
        clauses.append("created_at >= ?")
        params.append(since_iso)
    if scope == "chat":
        clauses.append("chat_id = ?")
        params.append(chat_id)
    if not clauses:
        clauses.append("1=1")
    return " AND ".join(clauses), params


def _count_new_usernames(conn: sqlite3.Connection, since_iso: str, chat_id: int, scope: str) -> int:
    where_sql, params = _items_where_clause(since_iso, chat_id, scope)
    row = conn.execute(
        f"SELECT COUNT(DISTINCT {_USERNAME_KEY_SQL}) FROM items "
        f"WHERE {where_sql} AND {_USERNAME_KEY_SQL} <> ''",
        tuple(params),
    ).fetchone()
    return int(row[0] or 0)

def _count_media(conn: sqlite3.Connection, since_iso: str, chat_id: int, scope: str) -> Tuple[int, int]:
    where_sql, params = _items_where_clause(since_iso, chat_id, scope)
    row = conn.execute(
        f"""
        WITH media AS (
            SELECT
                {_MEDIA_KEY_SQL} AS media_key,
                MAX(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL THEN 1 ELSE 0 END) AS has_coords
            FROM items
            WHERE {where_sql}
            GROUP BY media_key
        )
        SELECT
            SUM(CASE WHEN has_coords = 1 THEN 1 ELSE 0 END) AS with_coords,
            SUM(CASE WHEN has_coords = 0 THEN 1 ELSE 0 END) AS without_coords
        FROM media
        """,
        tuple(params),
    ).fetchone() or (0, 0)
    with_coords = int((row[0] or 0))
    without_coords = int((row[1] or 0))
    return with_coords, without_coords

def _count_totals(conn: sqlite3.Connection, chat_id: int, scope: str) -> Tuple[int, int, int]:
    where_sql, params = _items_where_clause(None, chat_id, scope)
    username_row = conn.execute(
        f"SELECT COUNT(DISTINCT {_USERNAME_KEY_SQL}) FROM items "
        f"WHERE {where_sql} AND {_USERNAME_KEY_SQL} <> ''",
        tuple(params),
    ).fetchone()
    media_row = conn.execute(
        f"""
        WITH media AS (
            SELECT
                {_MEDIA_KEY_SQL} AS media_key,
                MAX(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL THEN 1 ELSE 0 END) AS has_coords
            FROM items
            WHERE {where_sql}
            GROUP BY media_key
        )
        SELECT
            SUM(CASE WHEN has_coords = 1 THEN 1 ELSE 0 END) AS with_coords,
            SUM(CASE WHEN has_coords = 0 THEN 1 ELSE 0 END) AS without_coords
        FROM media
        """,
        tuple(params),
    ).fetchone() or (0, 0)
    profiles = int((username_row[0] or 0) if username_row else 0)
    with_coords = int((media_row[0] or 0))
    without_coords = int((media_row[1] or 0))
    return profiles, with_coords, without_coords

def get_stats(chat_id: int, scope: str) -> Dict[str, Dict[str, int]]:
    days_map = {'day': 1, 'week': 7, 'month': 30}
    conn = db_connect()
    try:
        out: Dict[str, Dict[str, int]] = {}
        for key, days in days_map.items():
            since = _since_utc_iso(days)
            profiles = _count_new_usernames(conn, since, chat_id, scope)
            with_c, without_c = _count_media(conn, since, chat_id, scope)
            out[key] = {
                'profiles': profiles,
                'usernames': profiles,
                'media_total': with_c + without_c,
                'media_with_coords': with_c,
                'media_without_coords': without_c,
            }
        profiles_total, with_coords_total, without_coords_total = _count_totals(conn, chat_id, scope)
        out['total'] = {
            'profiles': profiles_total,
            'usernames': profiles_total,
            'media_total': with_coords_total + without_coords_total,
            'media_with_coords': with_coords_total,
            'media_without_coords': without_coords_total,
        }
        return out
    finally:
        conn.close()

def format_stats_text(stats: Dict[str, Dict[str, int]], scope: str) -> str:
    title = "📊 Статистика (область: " + ("📌 текущий чат" if scope == "chat" else "🌐 вся база") + ")"
    def blk(key: str, name: str) -> str:
        s = stats[key]
        return (
            f"<b>{name}</b>\n"
            f"— Профилей: <b>{s['profiles']}</b>\n"
            f"— Медиа в базе: <b>{s['media_total']}</b> "
            f"(с координатами: <b>{s['media_with_coords']}</b>, без координат: <b>{s['media_without_coords']}</b>)\n"
        )
    return (
        f"{title}\n\n"
        f"{blk('total','Всего')}\n"
        f"{blk('day','За день (последние 24ч)')}\n"
        f"{blk('week','За неделю (7 дней)')}\n"
        f"{blk('month','За месяц (30 дней)')}"
    )

# ---------------------- Export / Aggregations ----------------------
def fetch_gallery_users(scope: str, chat_id: int) -> List[Dict[str, Any]]:
    conn = db_connect(); conn.execute("PRAGMA read_uncommitted=1;")
    if scope == "chat":
        rows = conn.execute(
            "SELECT id,username,profile_url,latitude,longitude,image_url,added_by,source,source_file,meta_json,created_at"
            " FROM items WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,username,profile_url,latitude,longitude,image_url,added_by,source,source_file,meta_json,created_at FROM items"
        ).fetchall()
    if not rows:
        conn.close(); return []

    groups: Dict[str, Dict[str, Any]] = {}
    ids_by_user: Dict[str,List[int]] = {}
    for iid, uname, purl, lat, lon, img, added_by, source, source_file, meta_json, created_at in rows:
        uname = uname or ""
        g = groups.setdefault(
            uname,
            {
                "username": uname,
                "profile_url": purl or (f"https://vsco.co/{uname}" if uname else ""),
                "images": [],
                "image_set": set(),
                "image_meta": {},
                "image_coords": {},
                "added_by": "",
                "sources": {},
                "cities": set(),
                "first_at": None,
                "last_at": None,
                "camera_models": Counter(),
                "camera_model_labels": {},
                "geo_buckets": {},
            },
        )
        if purl and not g["profile_url"]:
            g["profile_url"] = purl
        if img and img not in g["image_set"]:
            g["images"].append(img)
            g["image_set"].add(img)
        meta_payload: Optional[Dict[str, Any]] = None
        meta_for_extract: Optional[Dict[str, Any]] = None
        if meta_json:
            try:
                decoded_meta = json.loads(meta_json)
            except Exception:
                meta_payload = {"raw": meta_json}
            else:
                if isinstance(decoded_meta, dict):
                    meta_payload = decoded_meta
                    meta_for_extract = decoded_meta
                else:
                    meta_payload = {"raw": decoded_meta}
        if img and meta_payload:
            g["image_meta"][img] = meta_payload
        elif img and meta_json and img not in g["image_meta"]:
            g["image_meta"][img] = {"raw": meta_json}

        if meta_for_extract:
            camera_counter: Counter[str] = g["camera_models"]  # type: ignore[assignment]
            labels_map: Dict[str, str] = g["camera_model_labels"]  # type: ignore[assignment]
            seen_models: set[str] = set()
            for model in extract_camera_models_from_meta(meta_for_extract):
                key = model.lower()
                if not key:
                    continue
                if key not in labels_map:
                    labels_map[key] = model
                display_label = labels_map[key]
                if key in seen_models:
                    continue
                seen_models.add(key)
                camera_counter[display_label] += 1

        coords_from_meta = extract_coordinates_from_meta(meta_for_extract) if meta_for_extract else None
        db_coords = _safe_coord_pair(lat, lon)
        resolved_coords = coords_from_meta or db_coords
        if resolved_coords:
            lat_val, lon_val = resolved_coords
            geo_buckets: Dict[Tuple[int, int], Dict[str, float]] = g["geo_buckets"]  # type: ignore[assignment]
            _update_geo_bucket(geo_buckets, lat_val, lon_val)
            city_label = resolve_city_label(lat_val, lon_val)
            if city_label:
                g["cities"].add(city_label)
            if img:
                g["image_coords"][img] = (lat_val, lon_val)
        if added_by and not g.get("added_by"):
            g["added_by"] = added_by
        for token_value, token_label in dataset_token_pairs(source, source_file):
            if token_value:
                g["sources"][token_value] = token_label
        if created_at:
            try:
                created_at = str(created_at)
                if not g["first_at"] or created_at < g["first_at"]:
                    g["first_at"] = created_at
                if not g["last_at"] or created_at > g["last_at"]:
                    g["last_at"] = created_at
            except Exception:
                pass
        ids_by_user.setdefault(uname, []).append(iid)

    all_ids = [iid for lst in ids_by_user.values() for iid in lst]
    comments_map: Dict[int,List[str]] = {}
    if all_ids:
        chunk_size = 500
        for i in range(0, len(all_ids), chunk_size):
            chunk = all_ids[i : i + chunk_size]
            q = ",".join("?" for _ in chunk)
            for iid, c in conn.execute(
                f"SELECT item_id,comment FROM comments WHERE item_id IN ({q}) ORDER BY id ASC",
                chunk,
            ):
                comments_map.setdefault(iid, []).append(c)

    tab_info_by_user: Dict[str, Dict[str, Any]] = {}
    link_query = "SELECT username, url, extra_json FROM links"
    link_params: List[Any] = []
    if scope == "chat":
        link_query += " WHERE chat_id = ?"
        link_params.append(chat_id)
    for uname, link_url, extra_json in conn.execute(link_query, tuple(link_params)).fetchall():
        key = uname or ""
        info = tab_info_by_user.setdefault(key, {"url": "", "tabs": []})
        if link_url and not info.get("url"):
            info["url"] = link_url
        tabs = parse_profile_tabs_payload(extra_json or "")
        if tabs:
            existing: List[Dict[str, str]] = info.setdefault("tabs", [])  # type: ignore[assignment]
            seen_hrefs = {tab.get("href") for tab in existing}
            for tab in tabs:
                href = tab.get("href")
                if href and href not in seen_hrefs:
                    existing.append(tab)
                    seen_hrefs.add(href)

    out = []
    for uname, g in groups.items():
        geo_buckets_raw = g.get("geo_buckets", {})  # type: ignore
        geo_buckets: Dict[Tuple[int, int], Dict[str, float]] = (
            geo_buckets_raw if isinstance(geo_buckets_raw, dict) else {}
        )
        lat, lon, location_count = _select_primary_location(geo_buckets)
        u_comments: List[str] = []
        for iid in ids_by_user[uname]:
            u_comments.extend(comments_map.get(iid, []))
        if u_comments:
            seen=set(); ded=[]
            for c in u_comments:
                if c not in seen: seen.add(c); ded.append(c)
            u_comments = ded
        raw_added = g.get("added_by", "")
        display, link = added_by_display_and_link(raw_added)
        sources_dict: Dict[str, str] = g.get("sources", {})  # type: ignore
        datasets = [
            {"value": key, "label": sources_dict[key]}
            for key in sorted(sources_dict.keys(), key=lambda k: (sources_dict[k] or "").lower())
            if key and sources_dict.get(key)
        ]
        city_list = sorted(str(city) for city in g.get("cities", []))  # type: ignore
        added_key = (display or "").strip().lower()
        image_meta_map: Dict[str, Any] = g.get("image_meta", {})  # type: ignore
        image_coords_map: Dict[str, Tuple[float, float]] = g.get("image_coords", {})  # type: ignore
        images_ordered: List[str] = g.get("images", [])  # type: ignore
        meta_entries: List[Dict[str, Any]] = []
        for img_url in images_ordered:
            payload = image_meta_map.get(img_url)
            entry: Dict[str, Any] = {"url": img_url}
            if isinstance(payload, dict):
                size_value = payload.get("size_bytes")
                if isinstance(size_value, (int, float)):
                    entry["size_bytes"] = int(size_value)
                exif_value = payload.get("exiftool")
                if isinstance(exif_value, dict):
                    entry["exiftool"] = exif_value
                extra_keys = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"size_bytes", "exiftool"}
                }
                if extra_keys:
                    entry["extra"] = extra_keys
            elif payload not in (None, ""):
                entry["extra"] = {"raw": payload}
            coords = image_coords_map.get(img_url)
            if coords:
                lat_val, lon_val = coords
                entry["lat"] = lat_val
                entry["lon"] = lon_val
                city_label = resolve_city_label(lat_val, lon_val)
                if city_label:
                    entry["city"] = city_label
            meta_entries.append(entry)

        camera_counter_raw = g.get("camera_models", Counter())  # type: ignore
        camera_counter: Counter[str] = (
            camera_counter_raw if isinstance(camera_counter_raw, Counter) else Counter(camera_counter_raw)
        )
        phone_models = [
            {
                "model": label,
                "count": int(count),
            }
            for label, count in sorted(
                camera_counter.items(),
                key=lambda item: (-int(item[1] or 0), item[0].lower()),
            )
            if label and int(count or 0) > 0
        ]

        link_info = tab_info_by_user.get(uname, {})
        link_profile_url = link_info.get("url") if isinstance(link_info, dict) else None
        profile_tabs: List[Dict[str, str]] = []
        raw_tabs = link_info.get("tabs") if isinstance(link_info, dict) else None
        if isinstance(raw_tabs, list):
            for tab in raw_tabs:
                if not isinstance(tab, dict):
                    continue
                cleaned: Dict[str, str] = {}
                for key, value in tab.items():
                    if not isinstance(key, str):
                        continue
                    if value is None:
                        continue
                    text = str(value).strip()
                    if text:
                        cleaned[key] = text
                if cleaned.get("href"):
                    profile_tabs.append(cleaned)

        out.append({
            "username": uname,
            "profile_url": link_profile_url or g["profile_url"],
            "lat": lat, "lon": lon,
            "images": images_ordered,
            "images_meta": meta_entries,
            "comments": u_comments,
            "images_count": len(images_ordered),
            "comments_count": len(u_comments),
            "added_by": display,
            "added_by_link": link or "",
            "added_by_raw": raw_added,
            "added_by_key": added_key,
            "datasets": datasets,
            "cities": city_list,
            "phone_models": phone_models,
            "location_photo_count": location_count,
            "first_created": g.get("first_at"),
            "last_created": g.get("last_at"),
            "profile_tabs": profile_tabs,
        })
    conn.close()
    return out

def fetch_items_for_map(scope: str, chat_id: int) -> List[Dict[str, Any]]:
    conn = db_connect(); conn.execute("PRAGMA read_uncommitted=1;")
    if scope == "chat":
        rows = conn.execute(
            "SELECT id,username,profile_url,image_url,latitude,longitude,added_by,source,source_file,meta_json,created_at"
            " FROM items WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,username,profile_url,image_url,latitude,longitude,added_by,source,source_file,meta_json,created_at FROM items"
        ).fetchall()
    if not rows:
        conn.close(); return []
    ids = [r[0] for r in rows]
    comments_map: Dict[int,List[str]] = {}
    if ids:
        chunk_size = 500
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            q = ",".join("?" for _ in chunk)
            for iid, c in conn.execute(
                f"SELECT item_id,comment FROM comments WHERE item_id IN ({q}) ORDER BY id ASC",
                chunk,
            ):
                comments_map.setdefault(iid, []).append(c)
    out = []
    for iid, uname, purl, img, raw_lat, raw_lon, added_by, source, source_file, meta_json, created_at in rows:
        meta_for_extract: Optional[Dict[str, Any]] = None
        if meta_json:
            try:
                decoded_meta = json.loads(meta_json)
            except Exception:
                decoded_meta = None
            if isinstance(decoded_meta, dict):
                meta_for_extract = decoded_meta

        coords_from_meta = extract_coordinates_from_meta(meta_for_extract) if meta_for_extract else None
        db_coords = _safe_coord_pair(raw_lat, raw_lon)
        resolved_coords = coords_from_meta or db_coords
        lat_val: Optional[float] = None
        lon_val: Optional[float] = None
        if resolved_coords:
            lat_val, lon_val = resolved_coords

        display, link = added_by_display_and_link(added_by or "")
        city_label = (
            resolve_city_label(lat_val, lon_val)
            if lat_val is not None and lon_val is not None
            else None
        )
        dataset_map = {}
        for token_value, token_label in dataset_token_pairs(source, source_file):
            if token_value:
                dataset_map[token_value] = token_label
        out.append({
            "id": iid,
            "username": uname or "",
            "profile_url": purl or (f"https://vsco.co/{uname}" if uname else ""),
            "image_url": img or "",
            "lat": lat_val, "lon": lon_val,
            "comments": comments_map.get(iid, []),
            "added_by": display,
            "added_by_link": link or "",
            "added_by_raw": added_by or "",
            "datasets": [
                {"value": key, "label": dataset_map[key]}
                for key in sorted(dataset_map.keys(), key=lambda k: (dataset_map[k] or "").lower())
                if key and dataset_map.get(key)
            ],
            "cities": [city_label] if city_label else [],
            "coord_source": "exif" if coords_from_meta else ("db" if db_coords else ""),
            "created_at": created_at,
        })
    conn.close()
    return out

# ---------------------- HTML builders (gallery/maps) ----------------------
def build_rich_gallery(users: List[Dict[str, Any]], title="VSCO Gallery", subtitle=""):
    data_json = json.dumps(users, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\"/>
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; background:#f5f6f8; color:#111; }}
    .wrap {{ max-width: 1400px; margin: 24px auto; padding: 0 16px; }}
    h1 {{ margin: 0 0 4px 0; }}
    .sub {{ color:#6b7280; margin-bottom: 16px; }}
    .toolbar {{
      display:grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap:12px;
      margin-bottom:16px;
      background:#fff;
      padding:16px;
      border-radius:16px;
      box-shadow:0 1px 4px rgba(15,23,42,0.08);
    }}
    .toolbar .field {{ display:flex; flex-direction:column; gap:6px; font-size:12px; color:#6b7280; }}
    .toolbar .field.inline {{ flex-direction:row; align-items:center; gap:8px; }}
    .toolbar label {{ font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:#6b7280; }}
    .toolbar input,.toolbar select,.toolbar button {{ padding:8px 10px; border:1px solid #e5e7eb; border-radius:8px; background:#fff; font-size:13px; color:#111827; }}
    .toolbar input[type="number"] {{ font-variant-numeric: tabular-nums; }}
    .toolbar .field--button {{ align-self:flex-end; display:flex; flex-direction:column; justify-content:flex-end; }}
    .toolbar .field--button button {{ width:100%; font-weight:600; cursor:pointer; transition:background .15s ease, color .15s ease; }}
    .toolbar .field--button.primary button {{ background:#111827; color:#fff; }}
    .toolbar .field--button.primary button:hover {{ background:#1f2937; }}
    .toolbar .field--button.secondary button {{ background:#f3f4f6; color:#111827; }}
    .toolbar .field--button.secondary button:hover {{ background:#e5e7eb; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }}
    .chip {{ display:inline-flex; align-items:center; padding:4px 8px; border-radius:999px; font-size:11px; background:#f3f4f6; color:#374151; }}
    .chip-city {{ background:#dbeafe; color:#1d4ed8; }}
    .chip-data {{ background:#dcfce7; color:#047857; }}
    .chip-device {{ background:#ede9fe; color:#5b21b6; }}
    .meta.created {{ color:#4b5563; }}
    .stats {{ color:#6b7280; margin: 6px 0 10px 0; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fill,minmax(300px,1fr)); gap:14px; }}
    .card {{ background:#fff; border-radius:14px; padding:12px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
    .card .head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
    .card .head .name a {{ font-weight:700; text-decoration:none; color:#111; }}
    .btn {{ display:inline-block; padding:6px 10px; border-radius:10px; background:#10b981; color:#fff; text-decoration:none; font-weight:600; }}
    .btn:visited {{ color:#fff; }}
    .btn-secondary {{ display:inline-flex; align-items:center; justify-content:center; padding:6px 12px; border-radius:10px; background:#111827; color:#fff; text-decoration:none; font-weight:600; border:none; cursor:pointer; transition:background .15s ease; }}
    .btn-secondary:hover {{ background:#374151; }}
    .card .actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
    .meta {{ font-size:12px; color:#6b7280; margin:4px 0 8px 0; }}
    .meta.added {{ color:#4b5563; margin-top:2px; }}
    .thumbs {{ display:flex; gap:6px; overflow:hidden; }}
    .thumbs img {{ width:72px; height:120px; object-fit:cover; border-radius:8px; border:1px solid #eee; }}
    .cm {{ margin-top:10px; font-size:13px; }}
    .cm ul {{ margin: 0 0 4px 18px; padding:0; }}
    .cm .empty {{ color:#9ca3af; font-size:12px; }}
    .cm .more {{ color:#6b7280; font-size:12px; }}
    .hidden {{ display:none !important; }}
    .profile-view {{ max-width: 1200px; margin: 0 auto; padding: 32px 16px 60px; }}
    .profile-wrap {{ background:#fff; border-radius:20px; padding:36px; box-shadow:0 24px 40px rgba(15,23,42,0.08); }}
    .profile-back {{ background:none; border:none; color:#2563eb; font-size:14px; font-weight:600; cursor:pointer; padding:0; margin-bottom:24px; display:inline-flex; align-items:center; gap:6px; }}
    .profile-back:hover {{ color:#1d4ed8; }}
    .profile-head {{ display:flex; gap:32px; align-items:center; margin-bottom:24px; }}
    .profile-avatar {{ width:144px; height:144px; border-radius:50%; background:linear-gradient(135deg,#e5e7eb,#d1d5db); border:6px solid #f3f4f6; display:flex; align-items:center; justify-content:center; font-size:48px; font-weight:600; color:#9ca3af; overflow:hidden; background-size:cover; background-position:center; }}
    .profile-avatar.has-image {{ border-color:#fff; color:transparent; }}
    .profile-info {{ flex:1 1 auto; }}
    .profile-username {{ font-size:32px; font-weight:300; margin:0 0 14px 0; display:flex; align-items:center; gap:14px; }}
    .profile-actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }}
    .profile-actions .follow {{ background:#111; color:#fff; border-radius:999px; padding:8px 22px; font-size:13px; letter-spacing:0.08em; text-transform:uppercase; text-decoration:none; font-weight:700; }}
    .profile-actions .follow.disabled {{ pointer-events:none; opacity:0.5; }}
    .profile-actions .open {{ font-size:13px; color:#2563eb; text-decoration:none; font-weight:600; }}
    .profile-actions .open:hover {{ text-decoration:underline; }}
    .profile-tabs {{ display:flex; flex-wrap:wrap; gap:10px; margin:12px 0 18px; }}
    .profile-tabs.hidden {{ display:none !important; }}
    .profile-tab {{ display:inline-flex; align-items:center; padding:6px 14px; border-radius:999px; background:#f3f4f6; color:#1f2937; text-decoration:none; font-weight:600; font-size:12px; letter-spacing:0.02em; transition:background .15s ease,color .15s ease; }}
    .profile-tab:hover {{ background:#e5e7eb; color:#111827; }}
    .profile-meta {{ display:flex; gap:16px; flex-wrap:wrap; font-size:13px; color:#4b5563; margin-bottom:10px; }}
    .profile-meta span {{ display:inline-flex; align-items:center; gap:6px; }}
    .profile-tags {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
    .profile-tags .tag {{ background:#f3f4f6; border-radius:999px; padding:4px 10px; font-size:12px; color:#4b5563; }}
    .profile-phones {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
    .profile-phones .tag {{ background:#ede9fe; color:#4c1d95; border-radius:999px; padding:4px 10px; font-size:12px; display:inline-flex; align-items:center; gap:6px; }}
    .profile-cities {{ display:flex; gap:8px; flex-wrap:wrap; font-size:12px; color:#1f2937; margin-bottom:18px; }}
    .profile-cities .chip {{ background:#e0f2fe; color:#1d4ed8; border-radius:999px; padding:4px 12px; }}
    .profile-stats {{ display:flex; gap:32px; margin-bottom:28px; }}
    .profile-stats .stat {{ display:flex; flex-direction:column; font-size:14px; color:#6b7280; }}
    .profile-stats .stat .value {{ font-size:20px; font-weight:600; color:#111827; }}
    .profile-grid {{ display:grid; grid-template-columns: repeat(auto-fill,minmax(220px,1fr)); gap:16px; }}
    .profile-grid .cell {{ display:flex; flex-direction:column; border-radius:18px; overflow:hidden; background:#f3f4f6; border:1px solid #e5e7eb; }}
    .profile-grid .cell-thumb {{ position:relative; width:100%; padding-bottom:100%; background:#e5e7eb; }}
    .profile-grid .cell-thumb img {{ position:absolute; top:0; left:0; width:100%; height:100%; object-fit:cover; }}
    .profile-grid .cell-city {{ font-size:12px; color:#4b5563; padding:6px 8px 10px; background:#fff; text-align:center; line-height:1.4; border-top:1px solid #e5e7eb; }}
    .profile-empty {{ text-align:center; font-size:15px; color:#6b7280; padding:40px 0; }}
    @media (max-width: 900px) {{
      .profile-wrap {{ padding:24px; }}
      .profile-head {{ flex-direction:column; align-items:flex-start; }}
      .profile-avatar {{ width:120px; height:120px; }}
      .profile-username {{ font-size:26px; }}
      .profile-grid {{ grid-template-columns: repeat(auto-fill,minmax(160px,1fr)); }}
    }}
  </style>
</head>
<body>
  <div class=\"wrap\" id=\"galleryView\">
    <h1>🎯 {escape(title)}</h1>
    <div class=\"sub\">{escape(subtitle)}</div>

    <div class=\"toolbar\">
      <div class=\"field\">
        <label for=\"q\">Поиск</label>
        <input id=\"q\" placeholder=\"Username, города, комментарии\" />
      </div>
      <div class=\"field\">
        <label for=\"datasetSelect\">Данные</label>
        <select id=\"datasetSelect\"></select>
      </div>
      <div class=\"field\">
        <label for=\"cityInput\">Город</label>
        <input id=\"cityInput\" list=\"cityOptionsList\" placeholder=\"Начните вводить город или выберите из списка\" />
        <datalist id=\"cityOptionsList\"></datalist>
      </div>
      <div class=\"field\">
        <label for=\"commentInput\">Комментарий содержит</label>
        <input id=\"commentInput\" placeholder=\"Текст комментария\" />
      </div>
      <div class=\"field\">
        <label for=\"hasComments\">Комментарии</label>
        <select id=\"hasComments\">
          <option value=\"\">Все</option>
          <option value=\"with\">Есть комментарии</option>
          <option value=\"without\">Без комментариев</option>
        </select>
      </div>
      <div class=\"field\">
        <label for=\"addedSelect\">Добавил</label>
        <select id=\"addedSelect\"></select>
      </div>
      <div class=\"field\">
        <label for=\"dateFrom\">Дата с</label>
        <input id=\"dateFrom\" type=\"date\" />
      </div>
      <div class=\"field\">
        <label for=\"dateTo\">Дата по</label>
        <input id=\"dateTo\" type=\"date\" />
      </div>
      <div class=\"field\">
        <label for=\"sort\">Сортировка</label>
        <select id=\"sort\">
          <option value=\"date_desc\" selected>Новые сначала</option>
          <option value=\"date_asc\">Старые сначала</option>
          <option value=\"img_desc\">Больше медиа</option>
          <option value=\"img_asc\">Меньше медиа</option>
          <option value=\"cm_desc\">Больше комментариев</option>
          <option value=\"cm_asc\">Меньше комментариев</option>
          <option value=\"name_asc\">Username A–Z</option>
          <option value=\"name_desc\">Username Z–A</option>
        </select>
      </div>
      <div class=\"field field--button primary\">
        <button id=\"apply\">Применить</button>
      </div>
      <div class=\"field field--button secondary\">
        <button id=\"reset\" type=\"button\">Сбросить</button>
      </div>
    </div>

    <div class=\"stats\" id=\"stats\"></div>
    <div class=\"grid\" id=\"grid\"></div>
  </div>

  <div class=\"profile-view hidden\" id=\"profileView\">
    <div class=\"profile-wrap\">
      <button class=\"profile-back\" id=\"profileBack\" type=\"button\">← Назад к галерее</button>
      <div class=\"profile-head\">
        <div class=\"profile-avatar\" id=\"profileAvatar\">@</div>
        <div class=\"profile-info\">
          <div class=\"profile-username\" id=\"profileUsername\">@username</div>
          <div class=\"profile-actions\">
            <a class=\"follow\" id=\"profileFollow\" href=\"#\" target=\"_blank\" rel=\"noopener\">FOLLOW</a>
            <a class=\"open\" id=\"profileOpen\" href=\"#\" target=\"_blank\" rel=\"noopener\">Открыть оригинал</a>
          </div>
          <div class=\"profile-tabs hidden\" id=\"profileTabs\"></div>
          <div class=\"profile-meta\" id=\"profileMeta\"></div>
          <div class=\"profile-tags\" id=\"profileDatasets\"></div>
          <div class=\"profile-phones\" id=\"profilePhones\"></div>
          <div class=\"profile-cities\" id=\"profileCities\"></div>
          <div class=\"profile-meta\" id=\"profileInfoExtra\"></div>
        </div>
      </div>
      <div class=\"profile-stats\" id=\"profileStats\"></div>
      <div class=\"profile-grid\" id=\"profileGrid\"></div>
      <div class=\"profile-empty hidden\" id=\"profileEmpty\">Нет сохранённых фотографий для этого профиля.</div>
    </div>
  </div>

  <script>

    const DATA = {data_json};
    const DATA_MAP = new Map();
    DATA.forEach(u => DATA_MAP.set((u.username || '').toLowerCase(), u));

    const galleryView = document.getElementById('galleryView');
    const profileView = document.getElementById('profileView');
    const profileBack = document.getElementById('profileBack');
    const profileAvatar = document.getElementById('profileAvatar');
    const profileUsername = document.getElementById('profileUsername');
    const profileFollow = document.getElementById('profileFollow');
    const profileOpen = document.getElementById('profileOpen');
    const profileTabs = document.getElementById('profileTabs');
    const profileMeta = document.getElementById('profileMeta');
    const profileDatasets = document.getElementById('profileDatasets');
    const profilePhones = document.getElementById('profilePhones');
    const profileCities = document.getElementById('profileCities');
    const profileInfoExtra = document.getElementById('profileInfoExtra');
    const profileStats = document.getElementById('profileStats');
    const profileGrid = document.getElementById('profileGrid');
    const profileEmpty = document.getElementById('profileEmpty');

    const searchInput = document.getElementById('q');
    const datasetSelect = document.getElementById('datasetSelect');
    const cityInput = document.getElementById('cityInput');
    const cityDatalist = document.getElementById('cityOptionsList');
    const commentInput = document.getElementById('commentInput');
    const hasCommentsSelect = document.getElementById('hasComments');
    const addedSelect = document.getElementById('addedSelect');
    const dateFromInput = document.getElementById('dateFrom');
    const dateToInput = document.getElementById('dateTo');
    const sortSelect = document.getElementById('sort');
    const applyBtn = document.getElementById('apply');
    const resetBtn = document.getElementById('reset');
    const statsEl = document.getElementById('stats');
    const gridEl = document.getElementById('grid');

    function debounce(fn, delay) {{
      let timer;
      return function() {{
        const ctx = this, args = arguments;
        clearTimeout(timer);
        timer = setTimeout(function() {{ fn.apply(ctx, args); }}, delay);
      }};
    }}

    function parseDatasetList(rawList) {{
      const result = [];
      if (!Array.isArray(rawList)) return result;
      rawList.forEach(entry => {{
        if (!entry) return;
        if (typeof entry === 'object') {{
          const value = (entry.value || '').toString();
          const label = (entry.label || value).toString();
          if (!value && !label) return;
          result.push({{ value, label, valueLower: value.toLowerCase(), labelLower: label.toLowerCase() }});
        }} else {{
          const value = entry.toString();
          if (!value) return;
          const lower = value.toLowerCase();
          result.push({{ value, label: value, valueLower: lower, labelLower: lower }});
        }}
      }});
      return result;
    }}

    function fillSelectOptions(select, placeholder, options) {{
      if (!select) return;
      const frag = document.createDocumentFragment();
      const optAll = document.createElement('option');
      optAll.value = '';
      optAll.textContent = placeholder;
      frag.appendChild(optAll);
      Object.keys(options).sort((a,b) => {{
        const labelA = (options[a] || '').toString();
        const labelB = (options[b] || '').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity: 'accent' }});
      }}).forEach(value => {{
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = options[value];
        frag.appendChild(opt);
      }});
      select.innerHTML = '';
      select.appendChild(frag);
    }}

    function fillDatalistOptions(datalist, options) {{
      if (!datalist) return;
      const frag = document.createDocumentFragment();
      Object.keys(options).sort((a,b) => {{
        const labelA = (options[a] || '').toString();
        const labelB = (options[b] || '').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity: 'accent' }});
      }}).forEach(key => {{
        const opt = document.createElement('option');
        opt.value = options[key];
        frag.appendChild(opt);
      }});
      datalist.innerHTML = '';
      datalist.appendChild(frag);
    }}

    function parseDateValue(raw) {{
      if (!raw && raw !== 0) return null;
      const str = ('' + raw).trim();
      if (!str) return null;
      const normalized = str.includes('T') ? str : str.replace(' ', 'T');
      let ts = Date.parse(normalized);
      if (!Number.isFinite(ts)) {{
        ts = Date.parse(normalized + 'Z');
      }}
      return Number.isFinite(ts) ? ts : null;
    }}

    function formatDateLabel(ts) {{
      if (ts == null || !Number.isFinite(ts)) return '';
      const d = new Date(ts);
      if (Number.isNaN(d.getTime())) return '';
      const y = d.getFullYear();
      const m = String(d.getMonth()+1).padStart(2,'0');
      const day = String(d.getDate()).padStart(2,'0');
      return `${{y}}-${{m}}-${{day}}`;
    }}

    function formatDateInput(ts) {{
      return formatDateLabel(ts);
    }}

    function toDateRangeValue(value, endOfDay) {{
      if (!value) return null;
      const str = ('' + value).trim();
      if (!str) return null;
      const base = str.length > 10 ? str : str + (endOfDay ? 'T23:59:59.999' : 'T00:00:00');
      return parseDateValue(base);
    }}

    function getNewestTs(u) {{
      if (u && u._lastTs != null) return u._lastTs;
      if (u && u._firstTs != null) return u._firstTs;
      return -Infinity;
    }}

    function getOldestTs(u) {{
      if (u && u._firstTs != null) return u._firstTs;
      if (u && u._lastTs != null) return u._lastTs;
      return Infinity;
    }}

    function compareByName(a, b) {{
      const nameA = (a && a.username ? a.username : '') || '';
      const nameB = (b && b.username ? b.username : '') || '';
      return nameA.localeCompare(nameB, undefined, {{ sensitivity: 'accent' }});
    }}

    const datasetOptions = {{}};
    const cityOptions = {{}};
    const cityLookup = {{}};
    const addedOptions = {{}};

    let globalFirstTs = null;
    let globalLastTs = null;

    DATA.forEach(u => {{
      const comments = Array.isArray(u.comments) ? u.comments.filter(c => c !== null && c !== undefined && c !== '') : [];
      u._commentText = comments.map(c => ('' + c).toLowerCase()).join(' ');
      const datasetList = parseDatasetList(u.datasets);
      u._datasetList = datasetList;
      datasetList.forEach(ds => {{
        if (ds.value && !datasetOptions[ds.value]) {{
          datasetOptions[ds.value] = ds.label || ds.value;
        }}
      }});
      const citiesRaw = Array.isArray(u.cities) ? u.cities : [];
      const preparedCities = [];
      citiesRaw.forEach(city => {{
        const label = (city || '').toString().trim();
        if (!label) return;
        const lower = label.toLowerCase();
        preparedCities.push({{ label, lower }});
        if (!cityOptions[label]) cityOptions[label] = label;
        if (lower && !cityLookup[lower]) cityLookup[lower] = label;
      }});
      u._cityList = preparedCities;
      u._cityText = preparedCities.map(entry => entry.lower).join(' ');
      const phoneRaw = Array.isArray(u.phone_models) ? u.phone_models : [];
      const phoneList = [];
      const phoneSeen = new Set();
      phoneRaw.forEach(entry => {{
        if (!entry) return;
        let model = '';
        let count = 0;
        let rawCount = null;
        if (typeof entry === 'object') {{
          model = (entry.model || entry.value || '').toString();
          rawCount = entry.count;
          if (rawCount === undefined || rawCount === null) rawCount = entry.cnt;
          if (rawCount === undefined || rawCount === null) rawCount = entry.total;
        }} else {{
          model = entry.toString();
        }}
        model = model.trim();
        if (!model) return;
        const lower = model.toLowerCase();
        if (phoneSeen.has(lower)) return;
        phoneSeen.add(lower);
        if (rawCount !== null && rawCount !== undefined) {{
          const num = Number(rawCount);
          if (Number.isFinite(num) && num > 0) {{
            count = Math.round(num);
          }}
        }}
        const display = count > 1 ? model + ' ×' + count : model;
        phoneList.push({{ label: model, lower, display, count }});
      }});
      u._phoneList = phoneList;
      u._phoneText = phoneList.map(entry => entry.lower).join(' ');
      const addedLabel = (u.added_by || '').toString();
      const addedKey = (u.added_by_key || addedLabel).toString().trim().toLowerCase();
      u._addedKey = addedKey;
      if (addedKey && !addedOptions[addedKey]) {{
        addedOptions[addedKey] = addedLabel || addedKey;
      }}
      const searchParts = [];
      const username = (u.username || '').toString();
      if (username) searchParts.push(username.toLowerCase());
      if (u._commentText) searchParts.push(u._commentText);
      if (addedLabel) searchParts.push(addedLabel.toLowerCase());
      datasetList.forEach(ds => {{
        if (ds.labelLower) searchParts.push(ds.labelLower);
        if (ds.valueLower) searchParts.push(ds.valueLower);
      }});
      preparedCities.forEach(entry => searchParts.push(entry.lower));
      phoneList.forEach(entry => searchParts.push(entry.lower));
      u._searchText = searchParts.join(' ');
      const firstTs = parseDateValue(u.first_created);
      const lastTs = parseDateValue(u.last_created);
      u._firstTs = firstTs;
      u._lastTs = lastTs;
      if (firstTs != null && (globalFirstTs == null || firstTs < globalFirstTs)) globalFirstTs = firstTs;
      if (lastTs != null && (globalLastTs == null || lastTs > globalLastTs)) globalLastTs = lastTs;
      const firstLabel = formatDateLabel(firstTs);
      const lastLabel = formatDateLabel(lastTs);
      let rangeLabel = '';
      if (firstLabel && lastLabel) {{
        rangeLabel = firstLabel === lastLabel ? firstLabel : firstLabel + ' → ' + lastLabel;
      }} else {{
        rangeLabel = firstLabel || lastLabel || '';
      }}
      u._createdRangeLabel = rangeLabel;
    }});

    fillSelectOptions(datasetSelect, 'Все данные', datasetOptions);
    fillDatalistOptions(cityDatalist, cityOptions);
    fillSelectOptions(addedSelect, 'Все добавившие', addedOptions);

    const minDateLabel = formatDateInput(globalFirstTs);
    const maxDateLabel = formatDateInput(globalLastTs);
    if (dateFromInput) {{
      if (minDateLabel) dateFromInput.min = minDateLabel;
      if (maxDateLabel) dateFromInput.max = maxDateLabel;
    }}
    if (dateToInput) {{
      if (minDateLabel) dateToInput.min = minDateLabel;
      if (maxDateLabel) dateToInput.max = maxDateLabel;
    }}

    function sortData(arr, mode) {{
      switch(mode) {{
        case 'img_desc':
          return arr.sort((a,b) => {{
            const diff = (b.images_count || 0) - (a.images_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'img_asc':
          return arr.sort((a,b) => {{
            const diff = (a.images_count || 0) - (b.images_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'cm_desc':
          return arr.sort((a,b) => {{
            const diff = (b.comments_count || 0) - (a.comments_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'cm_asc':
          return arr.sort((a,b) => {{
            const diff = (a.comments_count || 0) - (b.comments_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'name_desc':
          return arr.sort((a,b) => compareByName(b,a));
        case 'date_desc':
          return arr.sort((a,b) => {{
            const aTs = getNewestTs(a);
            const bTs = getNewestTs(b);
            if (bTs === aTs) return compareByName(a,b);
            return bTs - aTs;
          }});
        case 'date_asc':
          return arr.sort((a,b) => {{
            const aTs = getOldestTs(a);
            const bTs = getOldestTs(b);
            if (aTs === bTs) return compareByName(a,b);
            return aTs - bTs;
          }});
        case 'name_asc':
        default:
          return arr.sort((a,b) => compareByName(a,b));
      }}
    }}

    function escapeHtml(s) {{
      return (''+s).replace(/[&<>"']/g, function(m) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]; }});
    }}

    function formatBytes(bytes) {{
      if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '';
      if (bytes === 0) return '0 B';
      const units = ['B','KB','MB','GB','TB'];
      const power = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
      const value = bytes / Math.pow(1024, power);
      const fixed = value >= 100 || power === 0 ? value.toFixed(0) : value.toFixed(1);
      return fixed.replace(/\\.0$/, '') + ' ' + units[power];
    }}

    function normalizeExifEntry(entry) {{
      if (!entry || typeof entry !== 'object') return {{}};
      return entry;
    }}

    function getEntryCityLabel(entry) {{
      const data = normalizeExifEntry(entry);
      const hasLat = typeof data.lat === 'number' && isFinite(data.lat);
      const hasLon = typeof data.lon === 'number' && isFinite(data.lon);
      if (!hasLat || !hasLon) return '';
      const candidates = [];
      if (data.city !== undefined && data.city !== null) candidates.push(data.city);
      const extra = data.extra && typeof data.extra === 'object' ? data.extra : null;
      if (extra) {{
        ['city', 'City', 'town', 'Town', 'location', 'Location'].forEach(key => {{
          if (extra[key] !== undefined && extra[key] !== null) candidates.push(extra[key]);
        }});
      }}
      const exif = data.exiftool && typeof data.exiftool === 'object' ? data.exiftool : null;
      if (exif) {{
        ['City', 'Sub-location', 'Location'].forEach(key => {{
          if (exif[key] !== undefined && exif[key] !== null) candidates.push(exif[key]);
        }});
      }}
      for (const candidate of candidates) {{
        if (candidate === undefined || candidate === null) continue;
        const label = ('' + candidate).trim();
        if (label) return label;
      }}
      return '';
    }}

    function renderProfile(user, updateHash=true) {{
      if (!user) {{
        return;
      }}
      const username = user.username || '';
      const safeUsername = username ? '@' + username : 'Без username';
      profileUsername.textContent = safeUsername;

      const profileUrl = user.profile_url || '';
      if (profileUrl) {{
        profileFollow.href = profileUrl;
        profileOpen.href = profileUrl;
        profileFollow.classList.remove('disabled');
      }} else {{
        profileFollow.href = '#';
        profileOpen.href = '#';
        profileFollow.classList.add('disabled');
      }}

      if (profileTabs) {{
        const tabs = Array.isArray(user.profile_tabs) ? user.profile_tabs : [];
        const tabHtml = tabs.map(tab => {{
          if (!tab || typeof tab !== 'object') return '';
          const rawHref = tab.href != null ? String(tab.href) : '';
          const href = rawHref.trim();
          if (!href) return '';
          const label = (tab.label || tab.slug || tab.id || href || '').toString();
          return '<a class=\"profile-tab\" href=\"' + escapeHtml(href) + '\" target=\"_blank\" rel=\"noopener\">' + escapeHtml(label) + '</a>';
        }}).filter(Boolean).join('');
        profileTabs.innerHTML = tabHtml;
        profileTabs.classList.toggle('hidden', tabHtml.length === 0);
      }}

      const images = Array.isArray(user.images) ? user.images.filter(Boolean) : [];
      if (images.length) {{
        profileAvatar.classList.add('has-image');
        profileAvatar.style.backgroundImage = 'url(' + JSON.stringify(images[0]) + ')';
        profileAvatar.textContent = '';
      }} else {{
        profileAvatar.classList.remove('has-image');
        profileAvatar.style.backgroundImage = '';
        profileAvatar.textContent = username ? username[0].toUpperCase() : '@';
      }}

      const metaParts = [];
      if (user.lat != null && user.lon != null) {{
        let coordLabel = user.lat.toFixed(5) + ', ' + user.lon.toFixed(5);
        if (typeof user.location_photo_count === 'number' && user.location_photo_count > 0) {{
          coordLabel += ' • ' + user.location_photo_count + ' фото';
        }}
        metaParts.push('<span>📍 ' + coordLabel + '</span>');
      }}
      if (user.added_by) {{
        if (user.added_by_link) {{
          metaParts.push('<span>👤 <a href="' + escapeHtml(user.added_by_link) + '" target="_blank" rel="noopener">' + escapeHtml(user.added_by) + '</a></span>');
        }} else {{
          metaParts.push('<span>👤 ' + escapeHtml(user.added_by) + '</span>');
        }}
      }}
      profileMeta.innerHTML = metaParts.join('');
      profileMeta.classList.toggle('hidden', metaParts.length === 0);

      const datasetParts = (user.datasets || []).map(ds => '<span class=\"tag\">' + escapeHtml(ds.label || ds.value || '') + '</span>');
      profileDatasets.innerHTML = datasetParts.join('');
      profileDatasets.classList.toggle('hidden', datasetParts.length === 0);

      let phoneList = Array.isArray(user._phoneList) ? user._phoneList : [];
      if ((!phoneList || phoneList.length === 0) && Array.isArray(user.phone_models)) {{
        const fallback = [];
        const seen = new Set();
        user.phone_models.forEach(entry => {{
          if (!entry) return;
          let model = '';
          let count = 0;
          let rawCount = null;
          if (typeof entry === 'object') {{
            model = (entry.model || entry.value || '').toString();
            rawCount = entry.count;
            if (rawCount === undefined || rawCount === null) rawCount = entry.cnt;
            if (rawCount === undefined || rawCount === null) rawCount = entry.total;
          }} else {{
            model = entry.toString();
          }}
          model = model.trim();
          if (!model) return;
          const lower = model.toLowerCase();
          if (seen.has(lower)) return;
          seen.add(lower);
          if (rawCount !== null && rawCount !== undefined) {{
            const num = Number(rawCount);
            if (Number.isFinite(num) && num > 0) {{
              count = Math.round(num);
            }}
          }}
          fallback.push({{ label: model, display: count > 1 ? model + ' ×' + count : model }});
        }});
        phoneList = fallback;
      }}
      const phoneParts = (Array.isArray(phoneList) ? phoneList : []).map(phone => {{
        const label = (phone && (phone.display || phone.label)) ? (phone.display || phone.label) : '';
        return label ? '<span class=\"tag\">' + escapeHtml(label) + '</span>' : '';
      }}).filter(Boolean);
      if (profilePhones) {{
        profilePhones.innerHTML = phoneParts.join('');
        profilePhones.classList.toggle('hidden', phoneParts.length === 0);
      }}

      const cityParts = (user.cities || []).map(city => '<span class=\"chip\">' + escapeHtml(city) + '</span>');
      profileCities.innerHTML = cityParts.join('');
      profileCities.classList.toggle('hidden', cityParts.length === 0);

      const infoExtra = [];
      if (user.first_created) {{
        infoExtra.push('<span>🕓 Первое: ' + escapeHtml(user.first_created) + '</span>');
      }}
      if (user.last_created && user.last_created !== user.first_created) {{
        infoExtra.push('<span>🕒 Последнее: ' + escapeHtml(user.last_created) + '</span>');
      }}
      profileInfoExtra.innerHTML = infoExtra.join('');
      profileInfoExtra.classList.toggle('hidden', infoExtra.length === 0);

      profileStats.innerHTML = [
        '<div class=\"stat\"><span class=\"value\">' + images.length + '</span><span class=\"label\">posts</span></div>',
        '<div class=\"stat\"><span class=\"value\">' + (user.comments_count || 0) + '</span><span class=\"label\">comments</span></div>'
      ].join('');

      const metaList = Array.isArray(user.images_meta) ? user.images_meta : [];
      const metaByUrl = new Map();
      metaList.forEach(entry => {{
        const data = normalizeExifEntry(entry);
        const rawUrl = (data.url || entry.url || '').toString();
        if (!rawUrl) return;
        metaByUrl.set(rawUrl, data);
        const bare = rawUrl.split('?')[0];
        if (bare && bare !== rawUrl) metaByUrl.set(bare, data);
      }});

      if (images.length) {{
        const cells = images.map(src => {{
          const rawSrc = (src || '').toString();
          if (!rawSrc) return '';
          const safeSrc = escapeHtml(rawSrc);
          const bareSrc = rawSrc.split('?')[0];
          const meta = metaByUrl.get(rawSrc) || metaByUrl.get(bareSrc);
          const city = meta ? getEntryCityLabel(meta) : '';
          const cityHtml = city ? '<div class=\"cell-city\">📍 ' + escapeHtml(city) + '</div>' : '';
          return '<div class=\"cell\"><div class=\"cell-thumb\"><img src=\"' + safeSrc + '\" loading=\"lazy\" alt=\"\"></div>' + cityHtml + '</div>';
        }}).join('');
        profileGrid.innerHTML = cells;
        profileEmpty.classList.add('hidden');
      }} else {{
        profileGrid.innerHTML = '';
        profileEmpty.classList.remove('hidden');
      }}

      galleryView.classList.add('hidden');
      profileView.classList.remove('hidden');
      if (updateHash) {{
        location.hash = '#/profile/' + encodeURIComponent(username || '');
      }}
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }}

    function showGallery(updateHash=true) {{
      profileView.classList.add('hidden');
      galleryView.classList.remove('hidden');
      if (updateHash) {{
        location.hash = '#gallery';
      }}
    }}

    profileBack.addEventListener('click', () => showGallery(true));


    function render(list) {{
      if (!gridEl || !statsEl) return;
      gridEl.innerHTML='';
      let entries=0;
      list.forEach(u=>{{
        const allImages = Array.isArray(u.images) ? u.images.filter(Boolean) : [];
        const imageCount = typeof u.images_count === 'number' ? u.images_count : allImages.length;
        const previews = allImages.slice(0,4);
        const cm = Array.isArray(u.comments) ? u.comments : [];
        const commentCount = typeof u.comments_count === 'number' ? u.comments_count : cm.length;
        entries += imageCount;
        let cmHtml='';
        if (cm.length===0) cmHtml = '<div class="empty">нет комментариев</div>';
        else {{
          const head = cm.slice(0,3).map(c=>'<li>' + escapeHtml(c) + '</li>').join('');
          const more = cm.length>3 ? '<div class="more">и ещё ' + (cm.length-3) + '…</div>' : '';
          cmHtml = '<ul>' + head + '</ul>' + more;
        }}
        const thumbs = previews.map(src=>'<img src="' + escapeHtml(src) + '" loading="lazy">').join('');
        const latStr = (u.lat!=null && u.lon!=null) ? u.lat.toFixed(6) + ', ' + u.lon.toFixed(6) : '';
        const addedBy = (()=>{{
          if (!u.added_by) return '';
          const label = escapeHtml(u.added_by);
          if (u.added_by_link) {{
            return '<a href="' + escapeHtml(u.added_by_link) + '" target="_blank">' + label + '</a>';
          }}
          return label;
        }})();
        const chipParts=[];
        (u._cityList || []).slice(0,3).forEach(entry=>{{
          chipParts.push('<span class="chip chip-city">' + escapeHtml(entry.label) + '</span>');
        }});
        (u._datasetList || []).slice(0,3).forEach(ds=>{{
          const label = ds.label || ds.value;
          if (label) chipParts.push('<span class="chip chip-data">' + escapeHtml(label) + '</span>');
        }});
        (u._phoneList || []).slice(0,3).forEach(phone=>{{
          if (!phone) return;
          const label = phone.display || phone.label || '';
          if (label) chipParts.push('<span class="chip chip-device">' + escapeHtml(label) + '</span>');
        }});
        const chipsHtml = chipParts.length ? '<div class="chips">' + chipParts.join('') + '</div>' : '';
        const createdHtml = u._createdRangeLabel ? '<div class="meta created">Добавлено: ' + escapeHtml(u._createdRangeLabel) + '</div>' : '';
        const card = document.createElement('div');
        card.className = 'card';
        card.dataset.username = u.username || '';
        const profileUrl = u.profile_url || '';
        const safeProfileUrl = escapeHtml(profileUrl);
        const displayName = u.username ? '@' + escapeHtml(u.username) : 'Без username';
        card.innerHTML = `
          <div class="head">
            <div class="name"><a href="${{safeProfileUrl}}" target="_blank">${{displayName}}</a></div>
            <a class="btn" href="${{safeProfileUrl}}" target="_blank">View Profile</a>
          </div>
          <div class="meta">${{latStr ? latStr + ' • ' : ''}}${{imageCount}} item(s) • ${{commentCount}} comment(s)</div>
          ${{createdHtml}}
          ${{chipsHtml}}
          <div class="meta added">Добавил: ${{addedBy || '—'}}</div>
          <div class="thumbs">${{thumbs}}</div>
          <div class="cm">${{cmHtml}}</div>
          <div class="actions">
            <button class="btn-secondary profile-btn" type="button">Открыть галерею</button>
          </div>
        `;
        const openBtn = card.querySelector('.profile-btn');
        if (openBtn) {{
          openBtn.addEventListener('click', (ev) => {{
            ev.preventDefault();
            ev.stopPropagation();
            renderProfile(u);
          }});
        }}
        gridEl.appendChild(card);
      }});
      statsEl.textContent = `Users: ${{list.length}} • Entries: ${{entries}}`;
    }}

    function apply() {{
      const q = searchInput ? searchInput.value.trim().toLowerCase() : '';
      const datasetValue = datasetSelect ? datasetSelect.value : '';
      const datasetLower = datasetValue ? datasetValue.toLowerCase() : '';
      const cityRaw = cityInput ? cityInput.value.trim() : '';
      const cityLower = cityRaw.toLowerCase();
      const hasExactCity = cityLower && Object.prototype.hasOwnProperty.call(cityLookup, cityLower);
      const commentValue = commentInput ? commentInput.value.trim().toLowerCase() : '';
      const hasCommentsValue = hasCommentsSelect ? hasCommentsSelect.value : '';
      const addedValue = addedSelect ? addedSelect.value : '';
      const fromTs = dateFromInput ? toDateRangeValue(dateFromInput.value, false) : null;
      const toTs = dateToInput ? toDateRangeValue(dateToInput.value, true) : null;
      const sortMode = sortSelect ? (sortSelect.value || 'date_desc') : 'date_desc';

      const list = DATA.filter(u => {{
        if (q && (!u._searchText || u._searchText.indexOf(q) === -1)) return false;
        if (datasetLower) {{
          const dsList = u._datasetList || [];
          const datasetMatch = dsList.some(ds => ds.valueLower === datasetLower || ds.labelLower === datasetLower);
          if (!datasetMatch) return false;
        }}
        if (cityLower) {{
          if (hasExactCity) {{
            const matchCity = (u._cityList || []).some(entry => entry.lower === cityLower);
            if (!matchCity) return false;
          }} else {{
            if (!u._cityText || u._cityText.indexOf(cityLower) === -1) return false;
          }}
        }}
        if (commentValue) {{
          if (!u._commentText || u._commentText.indexOf(commentValue) === -1) return false;
        }}
        const commentCount = typeof u.comments_count === 'number' ? u.comments_count : (Array.isArray(u.comments) ? u.comments.length : 0);
        if (hasCommentsValue === 'with' && commentCount === 0) return false;
        if (hasCommentsValue === 'without' && commentCount > 0) return false;
        if (addedValue && u._addedKey !== addedValue) return false;
        if (fromTs != null && getNewestTs(u) < fromTs) return false;
        if (toTs != null && getOldestTs(u) > toTs) return false;
        return true;
      }});
      sortData(list, sortMode);
      render(list);
      return list;
    }}

    function reset() {{
      if (searchInput) searchInput.value='';
      if (datasetSelect) datasetSelect.value='';
      if (cityInput) cityInput.value='';
      if (commentInput) commentInput.value='';
      if (hasCommentsSelect) hasCommentsSelect.value='';
      if (addedSelect) addedSelect.value='';
      if (dateFromInput) dateFromInput.value='';
      if (dateToInput) dateToInput.value='';
      if (sortSelect) sortSelect.value='date_desc';
      apply();
    }}

    if (applyBtn) applyBtn.addEventListener('click', apply);
    if (resetBtn) resetBtn.addEventListener('click', reset);

    const debouncedApply = debounce(apply, 200);
    if (searchInput) searchInput.addEventListener('input', debouncedApply);
    if (cityInput) cityInput.addEventListener('input', debouncedApply);
    if (commentInput) commentInput.addEventListener('input', debouncedApply);

    [datasetSelect, hasCommentsSelect, addedSelect, sortSelect].forEach(el => {{
      if (el) el.addEventListener('change', apply);
    }});
    if (dateFromInput) dateFromInput.addEventListener('change', apply);
    if (dateToInput) dateToInput.addEventListener('change', apply);

    reset();

    function handleHashNavigation() {{
      const hash = location.hash || '';
      if (hash.startsWith('#/profile/')) {{
        const username = decodeURIComponent(hash.replace('#/profile/', ''));
        const user = DATA_MAP.get((username || '').toLowerCase());
        if (user) {{
          renderProfile(user, false);
          return;
        }}
      }}
      showGallery(false);
    }}

    window.addEventListener('hashchange', handleHashNavigation);
    handleHashNavigation();
  </script>
</body>
</html>"""
    return html


def _map_html(
    title: str,
    list_html: str,
    marker_js: List[str],
    stats: Optional[Dict[str, Any]] = None,
) -> str:
    # Надёжная загрузка Leaflet + MarkerCluster с fallback и инициализацией после DOMContentLoaded
    stats = stats or {}
    summary_text = stats.get("summary_text", "")
    total = int(stats.get("total", 0) or 0)
    with_coords = int(stats.get("with_coords", 0) or 0)
    without_coords = int(stats.get("without_coords", max(total - with_coords, 0)))
    summary_attrs = (
        f" data-total=\"{total}\" data-withcoords=\"{with_coords}\""
        f" data-withoutcoords=\"{without_coords}\" data-text=\"{escape(summary_text)}\""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <title>{escape(title)}</title>
  <link rel="dns-prefetch" href="https://unpkg.com"/>
  <link rel="preconnect" href="https://unpkg.com" crossorigin/>
  <link rel="dns-prefetch" href="https://a.tile.openstreetmap.org"/>
  <link rel="dns-prefetch" href="https://b.tile.openstreetmap.org"/>
  <link rel="dns-prefetch" href="https://c.tile.openstreetmap.org"/>
  <link rel="preconnect" href="https://a.tile.openstreetmap.org" crossorigin/>
  <link rel="preconnect" href="https://b.tile.openstreetmap.org" crossorigin/>
  <link rel="preconnect" href="https://c.tile.openstreetmap.org" crossorigin/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
  <style>
    html, body {{ height:100%; margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
    .layout {{ display:flex; height:100vh; }}
    #map {{ flex: 1 1 auto; min-height: 320px; }}
    .panel {{ width: 420px; max-width: 48vw; border-left:1px solid #e5e7eb; background:#fafafa; overflow:auto; display:flex; flex-direction:column; }}
    .panel .head {{ position: sticky; top:0; background:#fff; border-bottom:1px solid #e5e7eb; padding:12px 14px 10px; z-index:1; }}
    .panel .title {{ font-weight:600; margin-bottom:6px; }}
    .panel .summary {{ font-size:12px; color:#4b5563; margin-bottom:10px; }}
    .panel .loading {{ font-size:12px; color:#1f2937; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; padding:10px 12px; margin-bottom:12px; box-shadow:0 1px 2px rgba(15,23,42,0.08); display:flex; flex-direction:column; gap:6px; }}
    .panel .loading[hidden] {{ display:none !important; }}
    .panel .loading__label {{ font-weight:600; display:flex; align-items:center; gap:6px; }}
    .panel .loading__label::before {{ content:"⏳"; }}
    .panel .loading__bar {{ display:flex; align-items:center; gap:8px; }}
    .panel .loading progress {{ flex:1 1 auto; width:100%; height:8px; accent-color:#2563eb; }}
    .panel .loading__details {{ min-width:80px; text-align:right; font-variant-numeric:tabular-nums; color:#4b5563; font-size:11px; }}
    .panel .search label {{ display:block; font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px; }}
    .panel .search input {{ width:100%; padding:6px 8px; border:1px solid #d1d5db; border-radius:6px; font-size:14px; }}
    .panel .filters {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap:8px; margin-top:12px; }}
    .panel .filters .field {{ display:flex; flex-direction:column; gap:4px; font-size:12px; }}
    .panel .filters .field label {{ color:#6b7280; text-transform:uppercase; letter-spacing:0.05em; font-size:11px; }}
    .panel .filters .field input,
    .panel .filters .field select {{ padding:6px 8px; border:1px solid #d1d5db; border-radius:6px; font-size:13px; background:#fff; }}
    .panel .filters .field--button {{ align-self:end; }}
    .panel .filters .field--button button {{ padding:6px 8px; border:1px solid #bfdbfe; background:#e0f2fe; color:#1d4ed8; border-radius:6px; font-size:13px; cursor:pointer; }}
    .panel .filters .field--button button:hover {{ background:#bfdbfe; }}
    .panel .rows {{ flex:1 1 auto; }}
    .panel .row {{ padding:10px 14px; border-bottom:1px dashed #e5e7eb; display:grid; grid-template-columns:auto 80px 1fr; gap:8px; align-items:center; transition:background 0.2s ease; }}
    .panel .row.row-user {{ grid-template-columns: 1fr; }}
    .panel .row[data-key] {{ cursor:pointer; }}
    .panel .row:hover {{ background:#f3f4f6; }}
    .panel .row.active {{ background:#e0f2fe; box-shadow:inset 0 0 0 1px #bae6fd; }}
    .panel .row .u a {{ font-weight:600; color:#111; text-decoration:none; }}
    .panel .row .c {{ font-size: 13px; color:#111; grid-column:1 / -1; }}
    .panel .row .meta {{ grid-column:1 / -1; font-size:11px; color:#4b5563; display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }}
    .panel .row .meta .chip {{ display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:999px; background:#e5e7eb; color:#374151; font-size:11px; font-weight:500; }}
    .panel .row .meta .chip-city {{ background:#dbeafe; color:#1d4ed8; }}
    .panel .row .meta .chip-city::before {{ content:"📍"; }}
    .panel .row .meta .chip-data {{ background:#fef3c7; color:#92400e; }}
    .panel .row .meta .chip-data::before {{ content:"💾"; }}
    .panel .row .meta .chip-device {{ background:#ede9fe; color:#5b21b6; }}
    .panel .row .meta .chip-device::before {{ content:"📱"; }}
    .panel .row .ab {{ grid-column:1 / -1; font-size:12px; color:#4b5563; }}
    .panel .row.row-user .u {{ grid-column:1 / -1; }}
    @media (max-width: 900px) {{
      .layout {{ flex-direction: column; }}
      .panel {{ width: 100%; max-width: 100%; height: 46vh; }}
      #map {{ height: 54vh; }}
    }}
  </style>
</head>
<body>
  <div class="layout">
    <div id="map"></div>
    <div class="panel">
      <div class="head">
        <div class="title">Список / превью</div>
        <div class="summary" id="summary"{summary_attrs}>{escape(summary_text)}</div>
        <div class="loading" id="loadingIndicator" hidden>
          <div class="loading__label">Загрузка карты…</div>
          <div class="loading__bar">
            <progress id="loadingProgress" max="100" value="0"></progress>
            <span class="loading__details" id="loadingDetails">0%</span>
          </div>
        </div>
        <div class="search">
          <label for="filter">Поиск</label>
          <input id="filter" type="search" placeholder="Поиск по нику, комментариям или добавившему" autocomplete="off"/>
        </div>
        <div class="filters">
          <div class="field">
            <label for="filterData">Данные</label>
            <select id="filterData">
              <option value="">Все данные</option>
            </select>
          </div>
          <div class="field">
            <label for="filterCity">Город</label>
            <input id="filterCity" type="search" list="filterCityOptions" placeholder="Начните вводить город или выберите из списка" autocomplete="off"/>
            <datalist id="filterCityOptions"></datalist>
          </div>
          <div class="field">
            <label for="filterComment">Комментарий</label>
            <input id="filterComment" type="search" placeholder="Фильтр по тексту комментария" autocomplete="off"/>
          </div>
          <div class="field">
            <label for="filterHasComments">Наличие комментариев</label>
            <select id="filterHasComments">
              <option value="">Все</option>
              <option value="with">Только с комментариями</option>
              <option value="without">Без комментариев</option>
            </select>
          </div>
          <div class="field">
            <label for="filterAdded">Добавивший</label>
            <select id="filterAdded">
              <option value="">Все добавившие</option>
            </select>
          </div>
          <div class="field field--button">
            <label>&nbsp;</label>
            <button id="filtersReset" type="button">Сбросить</button>
          </div>
        </div>
      </div>
      <div class="rows" id="list">{list_html}</div>
    </div>
  </div>
  <script>
    function loadScript(src, onload) {{
      var s=document.createElement('script'); s.src=src; s.onload=onload; s.async=true; document.head.appendChild(s);
    }}
    function ensureLeaflet(next) {{
      if (window.L) return next();
      loadScript("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", function() {{
        if (window.L) return next();
        loadScript("https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js", next);
      }});
    }}
    function ensureCluster(next) {{
      if (window.L && L.MarkerClusterGroup) return next();
      loadScript("https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js", function() {{
        if (window.L && L.MarkerClusterGroup) return next();
        loadScript("https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js", next);
      }});
    }}
    function debounce(fn, delay) {{
      var timer; return function() {{
        var ctx=this, args=arguments; clearTimeout(timer);
        timer=setTimeout(function() {{ fn.apply(ctx,args); }}, delay);
      }};
    }}
    function parseJsonArray(raw) {{
      if (!raw) return [];
      try {{
        var parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
      }} catch (e) {{
        return [];
      }}
    }}
    function parseDatasetList(raw) {{
      var result = [];
      var arr = parseJsonArray(raw);
      arr.forEach(function(entry) {{
        if (!entry) return;
        if (typeof entry === 'string') {{
          result.push({{ value: entry, label: entry }});
        }} else if (typeof entry === 'object') {{
          var value = (entry.value || entry.label || '').toString();
          if (!value) return;
          result.push({{ value: value, label: (entry.label || value).toString() }});
        }}
      }});
      return result;
    }}
    function fillSelectOptions(select, options, placeholder) {{
      if (!select) return;
      var frag=document.createDocumentFragment();
      var optAll=document.createElement('option');
      optAll.value='';
      optAll.textContent=placeholder || 'Все';
      frag.appendChild(optAll);
      Object.keys(options).sort(function(a,b) {{
        var labelA=(options[a]||'').toString();
        var labelB=(options[b]||'').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity:'accent' }});
      }}).forEach(function(value) {{
        var opt=document.createElement('option');
        opt.value=value;
        opt.textContent=options[value];
        frag.appendChild(opt);
      }});
      select.innerHTML='';
      select.appendChild(frag);
    }}
    function fillDatalistOptions(datalist, options) {{
      if (!datalist) return;
      var frag=document.createDocumentFragment();
      Object.keys(options).sort(function(a,b) {{
        var labelA=(options[a]||'').toString();
        var labelB=(options[b]||'').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity:'accent' }});
      }}).forEach(function(value) {{
        var opt=document.createElement('option');
        opt.value=options[value] || value;
        frag.appendChild(opt);
      }});
      datalist.innerHTML='';
      datalist.appendChild(frag);
    }}
    var filteringReady=false;
    var filteringStateValue=null;
    var filteringCallbacks=[];
    function withFiltering(callback) {{
      if (typeof callback !== 'function') return;
      if (filteringReady) {{
        try {{ callback(filteringStateValue); }} catch (err) {{ if (typeof console !== 'undefined' && console.error) console.error(err); }}
      }} else {{
        filteringCallbacks.push(callback);
      }}
    }}
    function resolveFilteringState(state) {{
      if (filteringReady) return;
      filteringReady=true;
      filteringStateValue=state;
      while (filteringCallbacks.length) {{
        var cb=filteringCallbacks.shift();
        try {{ cb && cb(state); }} catch (err) {{ if (typeof console !== 'undefined' && console.error) console.error(err); }}
      }}
    }}
    function initFilteringAsync() {{
      var run=function() {{
        try {{
          resolveFilteringState(setupFiltering());
        }} catch (err) {{
          if (typeof console !== 'undefined' && console.error) console.error(err);
          resolveFilteringState({{ onChange:function() {{}}, refresh:function() {{}} }});
        }}
      }};
      if (typeof window !== 'undefined' && window.requestIdleCallback) {{
        window.requestIdleCallback(run, {{ timeout: 500 }});
      }} else {{
        setTimeout(run, 0);
      }}
    }}
    function createLoadingController() {{
      var indicator=document.getElementById('loadingIndicator');
      var bar=document.getElementById('loadingProgress');
      var details=document.getElementById('loadingDetails');
      var summary=document.getElementById('summary');
      var totalMarkers=summary ? parseInt(summary.dataset.withcoords || '0', 10) || 0 : 0;
      var displayThreshold=150;
      var active=false;
      var lastUpdate=0;
      function hideIndicator() {{
        if (!indicator) return;
        indicator.hidden=true;
        indicator.setAttribute('aria-hidden','true');
      }}
      function showIndicator() {{
        if (!indicator) return;
        indicator.hidden=false;
        indicator.setAttribute('aria-hidden','false');
      }}
      function formatDetails(processed, total) {{
        if (!details) return;
        if (!total) {{
          details.textContent=processed ? processed.toString() : '…';
          return;
        }}
        var percent=Math.min(100, Math.max(0, Math.round((processed/total)*100)));
        details.textContent=processed + ' / ' + total + ' (' + percent + '%)';
      }}
      if (!indicator || totalMarkers <= displayThreshold) {{
        hideIndicator();
        return {{
          start:function() {{}},
          update:function() {{}},
          finish:function() {{ hideIndicator(); }}
        }};
      }}
      hideIndicator();
      return {{
        start:function(total) {{
          var target=total && total>0 ? total : totalMarkers;
          active=true;
          showIndicator();
          if (bar) {{
            bar.max=100;
            bar.value=0;
          }}
          formatDetails(0, target);
          lastUpdate=Date.now();
        }},
        update:function(processed, total) {{
          if (!active) this.start(total);
          var target=total && total>0 ? total : totalMarkers;
          var percent=target ? Math.min(100, Math.max(0, Math.round((processed/target)*100))) : 0;
          if (bar) {{
            bar.max=100;
            bar.value=percent;
          }}
          formatDetails(processed, target || 0);
          lastUpdate=Date.now();
        }},
        finish:function() {{
          if (!indicator) return;
          if (bar) {{
            bar.max=100;
            bar.value=100;
          }}
          if (totalMarkers) {{
            formatDetails(totalMarkers, totalMarkers);
          }} else if (details) {{
            details.textContent='Готово';
          }}
          var delay=Math.max(0, 400 - (Date.now()-lastUpdate));
          setTimeout(hideIndicator, delay);
        }}
      }};
    }}
    function setupFiltering() {{
      var rows=Array.prototype.slice.call(document.querySelectorAll('.panel .row'));
      var summary=document.getElementById('summary');
      var input=document.getElementById('filter');
      var datasetSelect=document.getElementById('filterData');
      var cityInput=document.getElementById('filterCity');
      var cityDatalist=document.getElementById('filterCityOptions');
      var commentInput=document.getElementById('filterComment');
      var hasCommentsSelect=document.getElementById('filterHasComments');
      var addedSelect=document.getElementById('filterAdded');
      var resetBtn=document.getElementById('filtersReset');
      var baseText = summary ? (summary.dataset.text || summary.textContent || '') : '';
      var totals = summary ? {{
        total: parseInt(summary.dataset.total || rows.length, 10) || rows.length,
        withCoords: parseInt(summary.dataset.withcoords || 0, 10) || 0
      }} : {{ total: rows.length, withCoords: rows.filter(function(r) {{ return r.dataset.hasCoords==='1'; }}).length }};
      var datasetOptions={{}};
      var cityOptions={{}};
      var cityLookup={{}};
      var addedOptions={{}};
      var changeHandlers=[];
      var lastKeys=[];
      rows.forEach(function(row) {{
        row._searchText=(row.dataset.search || '').toString();
        row._hasCoords=row.dataset.hasCoords==='1';
        row._commentsText=(row.dataset.comments || '').toString();
        row._addedText=(row.dataset.added || '').toString();
        row._addedKey=(row.dataset.addedKey || '').toString();
        row._addedLabel=(row.dataset.addedLabel || '').toString();
        row._commentsCount=parseInt(row.dataset.commentsCount || '0', 10) || 0;
        row._datasets=parseDatasetList(row.dataset.datasets);
        row._cities=parseJsonArray(row.dataset.cities).map(function(city) {{ return city ? city.toString() : ''; }}).filter(function(city) {{ return !!city; }});
        row._datasets.forEach(function(ds) {{
          var value=(ds.value || '').toString();
          if (!value) return;
          var label=(ds.label || value).toString();
          if (!datasetOptions[value]) datasetOptions[value]=label;
        }});
        row._cities.forEach(function(city) {{
          if (!cityOptions[city]) cityOptions[city]=city;
          var lowerCity=city.toLowerCase();
          if (lowerCity && !cityLookup[lowerCity]) cityLookup[lowerCity]=city;
        }});
        if (row._addedKey && !addedOptions[row._addedKey]) {{
          addedOptions[row._addedKey]=row._addedLabel || row._addedKey;
        }}
      }});
      fillSelectOptions(datasetSelect, datasetOptions, 'Все данные');
      fillDatalistOptions(cityDatalist, cityOptions);
      fillSelectOptions(addedSelect, addedOptions, 'Все добавившие');
      function notify(keys) {{
        lastKeys=keys.slice();
        changeHandlers.forEach(function(fn) {{
          try {{ fn(keys.slice()); }} catch (e) {{}}
        }});
      }}
      function applyFilter() {{
        var q=(input && input.value ? input.value : '').trim().toLowerCase();
        var dsValue=datasetSelect ? datasetSelect.value : '';
        var dsValueLower=dsValue ? dsValue.toLowerCase() : '';
        var cityRaw=cityInput && cityInput.value ? cityInput.value.trim() : '';
        var cityValueLower=cityRaw.toLowerCase();
        var hasExactCity = cityValueLower && Object.prototype.hasOwnProperty.call(cityLookup, cityValueLower);
        var commentValue=(commentInput && commentInput.value ? commentInput.value : '').trim().toLowerCase();
        var hasCommentsValue=hasCommentsSelect ? hasCommentsSelect.value : '';
        var addedKey=(addedSelect && addedSelect.value ? addedSelect.value : '');
        var visible=[];
        var visibleKeys=[];
        rows.forEach(function(row) {{
          var match=true;
          if (match && q && row._searchText.indexOf(q)===-1) match=false;
          if (match && dsValue) {{
            match=row._datasets && row._datasets.some(function(ds) {{
              var val=(ds.value || '').toString().toLowerCase();
              var label=(ds.label || '').toString().toLowerCase();
              return val===dsValueLower || label===dsValueLower;
            }});
          }}
          if (match && cityValueLower) {{
            if (hasExactCity) {{
              match=row._cities && row._cities.some(function(city) {{
                return city.toLowerCase()===cityValueLower;
              }});
            }} else {{
              match=row._cities && row._cities.some(function(city) {{
                return city.toLowerCase().indexOf(cityValueLower)!==-1;
              }});
            }}
          }}
          if (match && commentValue) {{
            match=row._commentsText.indexOf(commentValue)!==-1;
          }}
          if (match && hasCommentsValue==='with') {{
            match=row._commentsCount>0;
          }} else if (match && hasCommentsValue==='without') {{
            match=row._commentsCount===0;
          }}
          if (match && addedKey) {{
            match=row._addedKey===addedKey;
          }}
          row.style.display = match ? '' : 'none';
          if (match) {{
            visible.push(row);
            if (row.dataset.key) visibleKeys.push(row.dataset.key);
          }}
        }});
        if (summary) {{
          if (!q && !dsValue && !cityValueLower && !commentValue && !addedKey && !hasCommentsValue) {{
            summary.textContent = baseText;
          }} else {{
            var coordsShown = visible.filter(function(row) {{ return row._hasCoords; }}).length;
            summary.textContent = visible.length + ' из ' + totals.total + ' записей' + ' • С координатами: ' + coordsShown;
          }}
        }}
        notify(visibleKeys);
      }}
      var debouncedApply=debounce(applyFilter, 150);
      if (input) input.addEventListener('input', debouncedApply);
      if (cityInput) cityInput.addEventListener('input', debouncedApply);
      if (commentInput) commentInput.addEventListener('input', debouncedApply);
      if (datasetSelect) datasetSelect.addEventListener('change', applyFilter);
      if (cityInput) cityInput.addEventListener('change', applyFilter);
      if (addedSelect) addedSelect.addEventListener('change', applyFilter);
      if (hasCommentsSelect) hasCommentsSelect.addEventListener('change', applyFilter);
      if (resetBtn) resetBtn.addEventListener('click', function() {{
        if (input) input.value='';
        if (datasetSelect) datasetSelect.value='';
        if (cityInput) cityInput.value='';
        if (commentInput) commentInput.value='';
        if (addedSelect) addedSelect.value='';
        if (hasCommentsSelect) hasCommentsSelect.value='';
        applyFilter();
      }});
      applyFilter();
      return {{
        onChange: function(handler) {{
          if (typeof handler === 'function') {{
            changeHandlers.push(handler);
            handler(lastKeys.slice());
          }}
        }},
        refresh: applyFilter
      }};
    }}
    function setupListInteractions(map, markerByKey, clusterGroup, filteringState) {{
      var rows=Array.prototype.slice.call(document.querySelectorAll('.panel .row'));
      var activeRow=null;
      var TARGET_ZOOM=15;
      function activate(row) {{
        if (activeRow && activeRow!==row) activeRow.classList.remove('active');
        if (row) {{
          row.classList.add('active');
          activeRow = row;
          try {{ row.scrollIntoView({{ behavior:'smooth', block:'center', inline:'nearest' }}); }} catch (e) {{ row.scrollIntoView({{ block:'center' }}); }}
        }}
      }}
      function focusMarker(marker) {{
        if (!map || !marker) return;
        var finalize=function() {{
          var latlng = marker.getLatLng && marker.getLatLng();
          if (latlng) {{
            var zoom = map.getZoom ? map.getZoom() : TARGET_ZOOM;
            if (typeof zoom !== 'number' || zoom < TARGET_ZOOM) zoom = TARGET_ZOOM;
            if (map.flyTo) map.flyTo(latlng, zoom); else map.setView(latlng, zoom);
          }}
          if (marker.openPopup) marker.openPopup();
        }};
        if (clusterGroup && clusterGroup.hasLayer && !clusterGroup.hasLayer(marker)) {{
          clusterGroup.addLayer(marker);
        }}
        if (clusterGroup && clusterGroup.zoomToShowLayer) {{
          clusterGroup.zoomToShowLayer(marker, finalize);
        }} else {{
          finalize();
        }}
      }}
      rows.forEach(function(row) {{
        var key=row.dataset.key;
        if (!key || !markerByKey || !markerByKey[key]) return;
        row.addEventListener('click', function() {{
          focusMarker(markerByKey[key]);
          activate(row);
        }});
      }});
      if (markerByKey) {{
        Object.keys(markerByKey).forEach(function(key) {{
          var marker=markerByKey[key];
          if (!marker || !marker.on) return;
          marker.on('click', function() {{
            focusMarker(marker);
            var row=document.querySelector('.panel .row[data-key="'+key+'"]');
            if (row) activate(row);
          }});
        }});
      }}
      if (filteringState && filteringState.onChange) {{
        filteringState.onChange(function() {{
          if (activeRow && activeRow.style.display==='none') {{
            activeRow.classList.remove('active');
            activeRow=null;
          }}
        }});
      }}
    }}
    function bindFilteringToMarkers(filteringState, map, clusterGroup, markerByKey) {{
      if (!filteringState || !filteringState.onChange) return;
      filteringState.onChange(function(visibleKeys) {{
        if (!markerByKey) return;
        var visibleSet={{}};
        (visibleKeys || []).forEach(function(key) {{
          if (key) visibleSet[key]=true;
        }});
        if (clusterGroup && clusterGroup.hasLayer) {{
          Object.keys(markerByKey).forEach(function(key) {{
            var marker=markerByKey[key];
            if (!marker) return;
            var shouldShow=!!visibleSet[key];
            var hasLayer=clusterGroup.hasLayer(marker);
            if (shouldShow && !hasLayer) {{
              clusterGroup.addLayer(marker);
            }} else if (!shouldShow && hasLayer) {{
              clusterGroup.removeLayer(marker);
            }}
          }});
        }} else if (map && map.addLayer && map.removeLayer) {{
          Object.keys(markerByKey).forEach(function(key) {{
            var marker=markerByKey[key];
            if (!marker) return;
            var shouldShow=!!visibleSet[key];
            var onMap=map.hasLayer ? map.hasLayer(marker) : false;
            if (shouldShow && !onMap) {{
              map.addLayer(marker);
            }} else if (!shouldShow && onMap) {{
              map.removeLayer(marker);
            }}
          }});
        }}
      }});
    }}
    document.addEventListener('DOMContentLoaded', function() {{
      initFilteringAsync();
      var loadingController=createLoadingController();
      ensureLeaflet(function() {{
        ensureCluster(function() {{
          var map=L.map('map', {{ preferCanvas:true }});
          L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'&copy; OpenStreetMap contributors', maxZoom:19, minZoom:1, updateWhenIdle:true, keepBuffer:4, reuseTiles:true, detectRetina:true}}).addTo(map);
          {chr(10).join(marker_js)}
        }});
      }});
    }});
  </script>
</body>
</html>"""
def build_map_users(users: List[Dict[str, Any]], title="VSCO Profiles (Users)"):
    def _prepare_phone_labels(value: Any) -> Tuple[List[str], List[str]]:
        raw: List[str] = []
        display: List[str] = []
        seen: set[str] = set()
        if not isinstance(value, (list, tuple)):
            return raw, display
        for entry in value:
            model = ""
            count_val = 0
            raw_count: Any = None
            if isinstance(entry, dict):
                model = str(entry.get("model") or entry.get("value") or "").strip()
                raw_count = entry.get("count")
                if raw_count is None:
                    raw_count = entry.get("cnt")
                if raw_count is None:
                    raw_count = entry.get("total")
            else:
                model = str(entry or "").strip()
            if not model:
                continue
            key = model.lower()
            if key in seen:
                continue
            seen.add(key)
            if raw_count is not None:
                try:
                    num = float(raw_count)
                except (TypeError, ValueError):
                    num = 0.0
                if math.isfinite(num) and num > 0:
                    count_val = int(round(num))
            raw.append(model)
            display.append(f"{model} ×{count_val}" if count_val > 1 else model)
        return raw, display

    meta: List[Tuple[Dict[str, Any], Optional[float], Optional[float], str, bool]] = []
    list_rows: List[str] = []
    for idx, u in enumerate(users):
        raw_lat = u.get("lat"); raw_lon = u.get("lon")
        try:
            lat = float(raw_lat) if raw_lat is not None else None
            lon = float(raw_lon) if raw_lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        has_coord = lat is not None and lon is not None
        key = f"u{idx}" if has_coord else ""
        meta.append((u, lat, lon, key, has_coord))

        uname = escape(u.get("username", ""))
        link = escape(u.get("profile_url", ""))
        cm = u.get("comments") or []
        comment_count = len(cm)
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:3])
            more = f"<div class='c'>и ещё {len(cm)-3}…</div>" if len(cm) > 3 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"

        added_raw = u.get("added_by_raw", "")
        added_display, _ = added_by_display_and_link(added_raw)
        added_html = added_by_html(added_raw)
        added_block = f"<div class='ab'>Добавил: {added_html or '—'}</div>"
        added_key = (added_display or "").strip().lower()

        raw_datasets = u.get("datasets") or []
        dataset_payload: List[Dict[str, str]] = []
        dataset_labels: List[str] = []
        seen_dataset: set[str] = set()
        for entry in raw_datasets:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "")
                label = str(entry.get("label") or value)
            else:
                value = str(entry or "")
                label = value
            if not value or value in seen_dataset:
                continue
            seen_dataset.add(value)
            dataset_labels.append(label)
            dataset_payload.append({"value": value, "label": label})

        raw_cities = [str(city) for city in (u.get("cities") or []) if city]
        phone_raw_labels, phone_display_labels = _prepare_phone_labels(u.get("phone_models"))
        u["_phone_display_cache"] = phone_display_labels

        search_parts = [str(u.get("username") or "")]
        search_parts.extend(str(x or "") for x in cm)
        if added_display:
            search_parts.append(added_display)
        search_parts.extend(dataset_labels)
        search_parts.extend(raw_cities)
        search_parts.extend(phone_raw_labels)
        search_text = " ".join(p.strip() for p in search_parts if p).lower()

        comment_filter_text = " ".join(str(x or "") for x in cm).lower()
        added_filter_text = (added_display or "").lower()

        attrs = [
            f"data-has-coords=\"{1 if has_coord else 0}\"",
            f"data-search=\"{escape(search_text, quote=True)}\"",
            f"data-comments=\"{escape(comment_filter_text, quote=True)}\"",
            f"data-added=\"{escape(added_filter_text, quote=True)}\"",
            f"data-cities=\"{escape(json.dumps(raw_cities, ensure_ascii=False), quote=True)}\"",
            f"data-datasets=\"{escape(json.dumps(dataset_payload, ensure_ascii=False), quote=True)}\"",
            f"data-phones=\"{escape(json.dumps(phone_raw_labels, ensure_ascii=False), quote=True)}\"",
            f"data-comments-count=\"{comment_count}\"",
            f"data-added-key=\"{escape(added_key, quote=True)}\"",
            f"data-added-label=\"{escape(added_display or '', quote=True)}\"",
        ]
        if has_coord:
            attrs.extend(
                [
                    f"data-key=\"{key}\"",
                    f"data-lat=\"{lat:.6f}\"",
                    f"data-lon=\"{lon:.6f}\"",
                ]
            )
        meta_chips: List[str] = []
        for city in raw_cities[:3]:
            meta_chips.append(f"<span class='chip chip-city'>{escape(city)}</span>")
        for label in dataset_labels[:3]:
            meta_chips.append(f"<span class='chip chip-data'>{escape(label)}</span>")
        for label in phone_display_labels[:3]:
            meta_chips.append(f"<span class='chip chip-device'>{escape(label)}</span>")
        meta_block = f"<div class='meta'>{''.join(meta_chips)}</div>" if meta_chips else ""

        attr_html = " " + " ".join(attrs)
        list_rows.append(
            f"""
          <div class=\"row row-user\"{attr_html}>
            <div class=\"u\"><a href=\"{link}\" target=\"_blank\">@{uname}</a></div>
            {meta_block}
            {cm_txt}
            {added_block}
          </div>"""
        )

    list_html = "".join(list_rows)
    total = len(users)
    with_coords = sum(1 for _, _, _, _, hc in meta if hc)
    summary_text = (
        "Нет данных"
        if total == 0
        else f"Пользователи: {total} • С координатами: {with_coords} • Без координат: {total - with_coords}"
    )

    marker_js: List[str] = []
    if with_coords:
        marker_js += [
            f"if(loadingController && loadingController.start){{ loadingController.start({with_coords}); }}",
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup({chunkedLoading:true,chunkDelay:20,chunkInterval:200,removeOutsideVisibleBounds:true,spiderfyDistanceMultiplier:1.1,chunkProgress:function(processed,total){ if(loadingController && loadingController.update){ loadingController.update(processed,total); } }});",
            "var markerByKey={};",
            "if(markers.on){ markers.on('chunkedLoadingEnd', function(){ if(loadingController && loadingController.finish){ loadingController.finish(); }}); } else if(loadingController && loadingController.finish){ loadingController.finish(); }",
        ]
        for u, lat, lon, key, has_coord in meta:
            if not has_coord:
                continue
            uname = escape(str(u.get("username") or ""))
            prof = escape(str(u.get("profile_url") or ""))
            added_html = added_by_html(u.get("added_by_raw", ""))
            city_values: List[str] = []
            for city in u.get("cities") or []:
                text = str(city)
                if text:
                    city_values.append(escape(text))
            dataset_labels_marker: List[str] = []
            seen_dataset_labels: set[str] = set()
            for entry in u.get("datasets") or []:
                if isinstance(entry, dict):
                    label = str(entry.get("label") or entry.get("value") or "")
                else:
                    label = str(entry or "")
                if not label or label in seen_dataset_labels:
                    continue
                seen_dataset_labels.add(label)
                dataset_labels_marker.append(escape(label))
            parts = [f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a>"]
            if city_values:
                parts.append("<br/>📍 " + ", ".join(city_values[:3]))
            if dataset_labels_marker:
                parts.append("<br/>💾 " + ", ".join(dataset_labels_marker[:3]))
            phone_display_marker = u.get("_phone_display_cache")
            if not phone_display_marker:
                _, phone_display_marker = _prepare_phone_labels(u.get("phone_models"))
            if phone_display_marker:
                parts.append("<br/>📱 " + ", ".join(escape(label) for label in phone_display_marker[:3]))
            if added_html:
                parts.append(f"<br/>Добавил: {added_html}")
            parts.append("</div>")
            popup = "".join(parts)
            marker_js.append(
                f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); "
                f"markers.addLayer(m); bounds.extend([{lat},{lon}]); markerByKey[{key!r}]=m;",
            )
        marker_js += [
            "map.addLayer(markers);",
            "if(bounds.isValid()){map.fitBounds(bounds.pad(0.1));}else{map.setView([20,0],2);}",
            "withFiltering(function(filteringState){",
            "  setupListInteractions(map, markerByKey, markers, filteringState);",
            "  bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
            "});",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("if(loadingController && loadingController.finish){ loadingController.finish(); }")
        marker_js.append("withFiltering(function(filteringState){")
        marker_js.append("  setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("  bindFilteringToMarkers(filteringState, map, null, {});")
        marker_js.append("});")

    return _map_html(
        title,
        list_html,
        marker_js,
        stats={
            "summary_text": summary_text,
            "total": total,
            "with_coords": with_coords,
            "without_coords": total - with_coords,
        },
    )

def build_map_images(items: List[Dict[str, Any]], title="VSCO Profiles (Images)"):
    meta: List[Tuple[Dict[str, Any], Optional[float], Optional[float], str, bool]] = []
    rows: List[str] = []
    for idx, r in enumerate(items):
        raw_lat = r.get("lat"); raw_lon = r.get("lon")
        try:
            lat = float(raw_lat) if raw_lat is not None else None
            lon = float(raw_lon) if raw_lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        has_coord = lat is not None and lon is not None
        key = f"i{idx}" if has_coord else ""
        meta.append((r, lat, lon, key, has_coord))

        uname = escape(r.get("username", ""))
        link = escape(r.get("profile_url", ""))
        img = r.get("image_url") or ""
        thumb_html = (
            f"<img src='{escape(img)}' loading='lazy' style='width:68px;height:68px;object-fit:cover;border-radius:8px;border:1px solid #eee;'/>"
            if img
            else ""
        )
        cm = r.get("comments") or []
        comment_count = len(cm)
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:2])
            more = f"<div class='c'>и ещё {len(cm)-2}…</div>" if len(cm) > 2 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"

        added_raw = r.get("added_by_raw", "")
        added_display, _ = added_by_display_and_link(added_raw)
        added_html = added_by_html(added_raw)
        added_block = f"<div class='ab'>Добавил: {added_html or '—'}</div>"
        added_key = (added_display or "").strip().lower()

        raw_datasets = r.get("datasets") or []
        dataset_payload: List[Dict[str, str]] = []
        dataset_labels: List[str] = []
        seen_dataset: set[str] = set()
        for entry in raw_datasets:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "")
                label = str(entry.get("label") or value)
            else:
                value = str(entry or "")
                label = value
            if not value or value in seen_dataset:
                continue
            seen_dataset.add(value)
            dataset_labels.append(label)
            dataset_payload.append({"value": value, "label": label})

        raw_cities = [str(city) for city in (r.get("cities") or []) if city]

        search_parts = [str(r.get("username") or ""), str(img or "")]
        search_parts.extend(str(x or "") for x in cm)
        if added_display:
            search_parts.append(added_display)
        search_parts.extend(dataset_labels)
        search_parts.extend(raw_cities)
        search_text = " ".join(p.strip() for p in search_parts if p).lower()

        comment_filter_text = " ".join(str(x or "") for x in cm).lower()
        added_filter_text = (added_display or "").lower()

        attrs = [
            f"data-has-coords=\"{1 if has_coord else 0}\"",
            f"data-search=\"{escape(search_text, quote=True)}\"",
            f"data-comments=\"{escape(comment_filter_text, quote=True)}\"",
            f"data-added=\"{escape(added_filter_text, quote=True)}\"",
            f"data-cities=\"{escape(json.dumps(raw_cities, ensure_ascii=False), quote=True)}\"",
            f"data-datasets=\"{escape(json.dumps(dataset_payload, ensure_ascii=False), quote=True)}\"",
            f"data-comments-count=\"{comment_count}\"",
            f"data-added-key=\"{escape(added_key, quote=True)}\"",
            f"data-added-label=\"{escape(added_display or '', quote=True)}\"",
        ]
        if has_coord:
            attrs.extend(
                [
                    f"data-key=\"{key}\"",
                    f"data-lat=\"{lat:.6f}\"",
                    f"data-lon=\"{lon:.6f}\"",
                ]
            )
        meta_chips: List[str] = []
        for city in raw_cities[:3]:
            meta_chips.append(f"<span class='chip chip-city'>{escape(city)}</span>")
        for label in dataset_labels[:3]:
            meta_chips.append(f"<span class='chip chip-data'>{escape(label)}</span>")
        meta_block = f"<div class='meta'>{''.join(meta_chips)}</div>" if meta_chips else ""

        attr_html = " " + " ".join(attrs)
        rows.append(
            f"""
          <div class=\"row row-image\"{attr_html}>
            <div class=\"u\"><a href=\"{link}\" target=\"_blank\">@{uname}</a></div>
            <div class=\"t\">{thumb_html}</div>
            {meta_block}
            {cm_txt}
            {added_block}
          </div>"""
        )

    list_html = "".join(rows)
    total = len(items)
    with_coords = sum(1 for _, _, _, _, hc in meta if hc)
    unique_users = len({str(r.get("username") or "") for r in items if r.get("username")})
    summary_text = (
        "Нет данных"
        if total == 0
        else f"Фотографии: {total} • Пользователи: {unique_users} • С координатами: {with_coords}"
    )

    marker_js: List[str] = []
    if with_coords:
        marker_js += [
            f"if(loadingController && loadingController.start){{ loadingController.start({with_coords}); }}",
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup({chunkedLoading:true,chunkDelay:20,chunkInterval:200,removeOutsideVisibleBounds:true,spiderfyDistanceMultiplier:1.1,chunkProgress:function(processed,total){ if(loadingController && loadingController.update){ loadingController.update(processed,total); } }});",
            "var markerByKey={};",
            "if(markers.on){ markers.on('chunkedLoadingEnd', function(){ if(loadingController && loadingController.finish){ loadingController.finish(); }}); } else if(loadingController && loadingController.finish){ loadingController.finish(); }",
        ]
        for r, lat, lon, key, has_coord in meta:
            if not has_coord:
                continue
            uname = escape(str(r.get("username") or ""))
            prof = escape(str(r.get("profile_url") or ""))
            img = r.get("image_url") or ""
            img_html = (
                f"<img src='{escape(img)}' loading='lazy' style='width:140px;height:140px;object-fit:cover;border-radius:10px;border:1px solid #eee;'/>"
                if img
                else ""
            )
            added_html = added_by_html(r.get("added_by_raw", ""))
            city_values: List[str] = []
            for city in r.get("cities") or []:
                text = str(city)
                if text:
                    city_values.append(escape(text))
            dataset_labels_marker: List[str] = []
            seen_dataset_labels: set[str] = set()
            for entry in r.get("datasets") or []:
                if isinstance(entry, dict):
                    label = str(entry.get("label") or entry.get("value") or "")
                else:
                    label = str(entry or "")
                if not label or label in seen_dataset_labels:
                    continue
                seen_dataset_labels.add(label)
                dataset_labels_marker.append(escape(label))
            parts = [f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a>"]
            if city_values:
                parts.append("<br/>📍 " + ", ".join(city_values[:3]))
            if dataset_labels_marker:
                parts.append("<br/>💾 " + ", ".join(dataset_labels_marker[:3]))
            if img_html:
                parts.append("<br/>" + img_html)
            if added_html:
                parts.append(f"<br/>Добавил: {added_html}")
            parts.append("</div>")
            popup = "".join(parts)
            marker_js.append(
                f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); "
                f"markers.addLayer(m); bounds.extend([{lat},{lon}]); markerByKey[{key!r}]=m;",
            )
        marker_js += [
            "map.addLayer(markers);",
            "if(bounds.isValid()){map.fitBounds(bounds.pad(0.1));}else{map.setView([20,0],2);}",
            "withFiltering(function(filteringState){",
            "  setupListInteractions(map, markerByKey, markers, filteringState);",
            "  bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
            "});",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("if(loadingController && loadingController.finish){ loadingController.finish(); }")
        marker_js.append("withFiltering(function(filteringState){")
        marker_js.append("  setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("  bindFilteringToMarkers(filteringState, map, null, {});")
        marker_js.append("});")

    return _map_html(
        title,
        list_html,
        marker_js,
        stats={
            "summary_text": summary_text,
            "total": total,
            "with_coords": with_coords,
            "without_coords": total - with_coords,
        },
    )

# ---------------------- Bot ----------------------
if not TOKEN: raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(zip_router)
@dataclass
class Session:
    chat_id: int
    dir: Path
    uploaded_html: List[Path] = field(default_factory=list)
    uploaded_csv: List[Path] = field(default_factory=list)
    export_scope: str = "chat"  # 'chat' | 'all'
    pending_action: Optional[str] = None
_sessions: dict[int, Session] = {}

def get_session(chat_id: int) -> Session:
    if chat_id not in _sessions:
        p = WORKDIR / f"chat_{chat_id}"; p.mkdir(parents=True, exist_ok=True)
        _sessions[chat_id] = Session(chat_id=chat_id, dir=p)
    return _sessions[chat_id]


def _format_export_initiator(user: Optional[User]) -> Optional[str]:
    if user is None:
        return None
    name = (user.full_name or user.first_name or user.username or "") or "Без имени"
    safe_name = escape(name)
    mention = f"<a href=\"tg://user?id={user.id}\">{safe_name}</a>"
    if user.username:
        mention += f" (@{escape(user.username)})"
    else:
        mention += f" (ID <code>{user.id}</code>)"
    return mention


async def mirror_export_to_admin(
    path: Path,
    caption: str,
    chat_id: int,
    scope: str,
    export_format: str,
    zipped: bool,
    chat_title: Optional[str],
    user: Optional[User],
) -> None:
    if not ARCHIVE_ADMIN_CHANNEL_ID:
        return

    if not path.exists():
        log.warning("Export mirror skipped, file missing: %s", path)
        return

    scope_label = "вся база" if scope == "all" else "текущий чат"
    fmt_labels = {
        "csv": "CSV",
        "gallery": "Галерея",
        "map_users": "Карта (польз.)",
        "map_images": "Карта (фото)",
    }
    fmt_label = fmt_labels.get(export_format, export_format)
    user_label = _format_export_initiator(user)
    chat_label = escape(chat_title) if chat_title else None
    lines = [
        "📤 <b>Экспорт данных</b>",
        f"• Формат: {fmt_label}{' (ZIP)' if zipped else ''}",
        f"• Область: {scope_label}",
    ]
    if chat_label:
        lines.append(f"• Чат: {chat_label} (<code>{chat_id}</code>)")
    else:
        lines.append(f"• Чат ID: <code>{chat_id}</code>")
    if user_label:
        lines.append(f"• Пользователь: {user_label}")

    try:
        await bot.send_message(
            ARCHIVE_ADMIN_CHANNEL_ID,
            "\n".join(lines),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Failed to send export notice to admin channel")

    try:
        await bot.send_document(
            ARCHIVE_ADMIN_CHANNEL_ID,
            FSInputFile(path, filename=path.name),
            caption=caption,
            request_timeout=SEND_TIMEOUT,
        )
    except TelegramBadRequest as err:
        if "file is too big" in str(err).lower():
            log.warning("Export mirror: file too big for Telegram: %s", path)
        else:
            log.exception("Export mirror failed for %s", path)
    except Exception:
        log.exception("Export mirror failed for %s", path)


export_manager = ExportManager(
    ExportDependencies(
        send_timeout=SEND_TIMEOUT,
        ensure_user_has_access=ensure_user_has_access,
        ensure_callback_access=ensure_callback_access,
        has_daily_data_access=has_daily_data_access,
        get_session=get_session,
        fetch_gallery_users=fetch_gallery_users,
        fetch_items_for_map=fetch_items_for_map,
        build_rich_gallery=build_rich_gallery,
        build_map_users=build_map_users,
        build_map_images=build_map_images,
        mirror_export=mirror_export_to_admin,
    )
)
dp.include_router(export_manager.router)

@dp.message(F.document)
async def on_document(msg: Message):
    doc = msg.document
    low_name = (doc.file_name or "").lower()
    is_html_upload = low_name.endswith((".html", ".htm"))

    if not is_html_upload and not await ensure_user_has_access(msg):
        return

    ses = get_session(msg.chat.id)
    found = added_items = added_comments = 0
    new_links: List[str] = []
    added_by = resolve_added_by(msg.from_user)
    caption_profile_files: List[Path] = []

    if msg.caption:
        pairs = await normalize_vsco_pairs(parse_vsco_pairs_from_message(msg.caption, msg.caption_entities))
        caption_profile_files = persist_profile_media_urls(pairs, ses.dir)
        found += len(pairs)
        ai, ac, links = upsert_items_with_comments(
            msg.chat.id,
            pairs,
            source="text",
            source_file="caption",
            added_by=added_by,
        )
        added_items += ai; added_comments += ac; new_links.extend(links)
        await _maybe_schedule_profile_scans(
            msg.chat.id,
            pairs,
            links,
            added_by=added_by,
            source="text",
        )

    p = ses.dir / (doc.file_name or "file.bin")
    await msg.bot.download(doc, destination=p)
    low = (p.name or "").lower()

    if low.endswith(".csv"):
        ses.uploaded_csv.append(p)
        try:
            encodings = ("utf-8-sig", "utf-8", "cp1251")
            pairs: List[Dict[str, str]] = []

            for encoding in encodings:
                try:
                    attempt_pairs: List[Dict[str, str]] = []
                    with p.open("r", encoding=encoding, newline="") as fh:
                        reader = csv.reader(fh)
                        for row in reader:
                            for cell in row:
                                if cell is None:
                                    continue
                                attempt_pairs.extend(parse_vsco_pairs_from_cell(str(cell)))
                    pairs = attempt_pairs
                    break
                except UnicodeDecodeError:
                    continue
            else:
                with p.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
                    reader = csv.reader(fh)
                    for row in reader:
                        for cell in row:
                            if cell is None:
                                continue
                            pairs.extend(parse_vsco_pairs_from_cell(str(cell)))

            pairs = await normalize_vsco_pairs(pairs)
            found += len(pairs)
            ai, ac, links = upsert_items_with_comments(
                msg.chat.id,
                pairs,
                source="csv",
                source_file=p.name,
                added_by=added_by,
            )
            added_items += ai
            added_comments += ac
            new_links.extend(links)
            await _maybe_schedule_profile_scans(
                msg.chat.id,
                pairs,
                links,
                added_by=added_by,
                source="csv",
            )
            links_block = format_new_links_block(new_links)
            notice_block = format_profile_urls_notice(caption_profile_files, ses.dir)
            await msg.answer(
                f"CSV загружен: <code>{escape(p.name)}</code>\n"
                f"Найдено VSCO-ссылок: {found}, добавлено ссылок/медиа: {added_items}, добавлено комментариев: {added_comments}"
                f"{links_block}{notice_block}"
            )
        except Exception as e:
            log.exception("CSV processing failed")
            await msg.answer(
                f"CSV загружен: <code>{escape(p.name)}</code>, но не удалось обработать: {escape(str(e))}"
            )
        return

    if low.endswith(".html") or low.endswith(".htm"):
        ses.uploaded_html.append(p)
        try:
            rows = dedupe_rows(parse_html_file(p), mode="safe")
            added_full, html_links = insert_full_rows_from_html(
                msg.chat.id,
                rows,
                source_file=p.name,
                added_by=added_by,
            )
            await _maybe_schedule_profile_scans(
                msg.chat.id,
                rows,
                html_links,
                added_by=added_by,
                source="html",
            )
            all_links: List[str] = []
            if msg.caption:
                all_links.extend(new_links)
            all_links.extend(html_links)
            links_block = format_new_links_block(all_links)
            extra = ""
            if msg.caption:
                extra = (
                    f"\n+ из подписи: добавлено {added_items} записей, комментариев {added_comments}"
                )
            notice_block = format_profile_urls_notice(caption_profile_files, ses.dir)
            await msg.answer(
                f"HTML загружен: <code>{escape(p.name)}</code>\n"
                f"Сохранено элементов: {added_full}"
                f"{extra}"
                f"{links_block}{notice_block}"
            )
        except Exception as e:
            log.exception("HTML processing failed")
            await msg.answer(f"HTML загружен: <code>{escape(p.name)}</code>, но не удалось обработать: {escape(str(e))}")
        return

    links_block = format_new_links_block(new_links) if msg.caption else ""
    notice_block = format_profile_urls_notice(caption_profile_files, ses.dir)
    await msg.answer(
        "Файл сохранён. Нужны .html/.csv. Ссылки из подписи учтены, если были."
        f"{links_block}{notice_block}"
    )

# ---------- plain text ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message):
    text = (msg.text or "").strip()

    if not text:
        return

    ses = get_session(msg.chat.id)

    if ses.pending_action == "download":
        if not await ensure_user_has_access(msg):
            return
        parts = text.split()
        target = parts[0]
        extra_flags = [p for p in parts[1:] if p.startswith("--")]
        success = await _enqueue_download_request(
            msg,
            target,
            extra_flags,
            getattr(msg.from_user, "id", None),
        )
        ses.pending_action = None
        return

    if text == "📥 Скачать профиль":
        if not await ensure_user_has_access(msg):
            return
        if msg.chat.type in ("group", "supergroup"):
            await msg.answer("Скачивание доступно только в личных сообщениях. Напишите мне в ЛС.")
            return
        ses.pending_action = "download"
        await msg.answer(
            "Отправьте username или ссылку профиля VSCO, чтобы поставить скачивание в очередь."
            " Можно добавить флаги, например: <code>username --max 100</code>."
        )
        return

    if text == "📤 Экспорт":
        if not await ensure_user_has_access(msg):
            return
        if msg.chat.type in ("group", "supergroup"):
            await msg.answer("Экспорт доступен только в личных сообщениях. Напишите мне в ЛС.")
            return
        await export_manager.open_menu(msg, user_id=getattr(msg.from_user, "id", None))
        return

    if text == "🔗 Ссылки за 24ч":
        if not await ensure_user_has_access(msg):
            return
        await cmd_links(msg, user_id=getattr(msg.from_user, "id", None))
        return

    if text == "📈 Статистика":
        if not await ensure_user_has_access(msg):
            return
        await cmd_stats(msg, user_id=getattr(msg.from_user, "id", None))
        return

    if text == "📊 Очередь":
        if not await ensure_user_has_access(msg):
            return
        await cmd_qstat(msg, user_id=getattr(msg.from_user, "id", None))
        return

    if text == "📚 Туториал":
        if not await ensure_user_has_access(msg):
            return
        await cmd_tutorial(msg, user_id=getattr(msg.from_user, "id", None))
        return

    pairs = await normalize_vsco_pairs(parse_vsco_pairs_from_message(text, msg.entities))
    if not pairs:
        await ensure_user_has_access(msg)
        return  # без ответа
    profile_files = persist_profile_media_urls(pairs, ses.dir)
    added_by = resolve_added_by(msg.from_user)
    ai, ac, links = upsert_items_with_comments(
        msg.chat.id,
        pairs,
        source="text",
        source_file="message",
        added_by=added_by,
    )
    await _maybe_schedule_profile_scans(
        msg.chat.id,
        pairs,
        links,
        added_by=added_by,
        source="text",
    )
    links_block = format_new_links_block(links)
    notice_block = format_profile_urls_notice(profile_files, ses.dir)
    await msg.answer(
        f"Найдено VSCO-ссылок: {len(pairs)}, добавлено записей: {ai}, комментариев: {ac}{links_block}{notice_block}"
    )

# ---------- stats ----------
def stats_scope_keyboard(ses: Session) -> InlineKeyboardMarkup:
    scope = [
        InlineKeyboardButton(text=("✅ 📌 Текущий чат" if ses.export_scope=="chat" else "📌 Текущий чат"), callback_data="stats:scope:chat"),
        InlineKeyboardButton(text=("✅ 🌐 Вся база" if ses.export_scope=="all" else "🌐 Вся база"), callback_data="stats:scope:all"),
    ]
    actions = [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats:refresh")]
    return InlineKeyboardMarkup(inline_keyboard=[scope, actions])

@dp.message(Command("stats"))
async def cmd_stats(msg: Message, user_id: Optional[int] = None):
    if not await ensure_user_has_access(msg, user_id=user_id):
        return

    ses = get_session(msg.chat.id)
    s = get_stats(msg.chat.id, ses.export_scope)
    txt = format_stats_text(s, ses.export_scope)
    await msg.answer(txt, reply_markup=stats_scope_keyboard(ses))

@dp.callback_query(F.data.startswith("stats:"))
async def on_stats_click(cq: CallbackQuery):
    if not await ensure_callback_access(cq):
        return

    chat_id = cq.message.chat.id
    ses = get_session(chat_id)
    parts = cq.data.split(":")
    if len(parts) >= 3 and parts[1] == "scope":
        scope = parts[2]
        if scope in ("chat","all"):
            ses.export_scope = scope
        else:
            await cq.answer("Неизвестная область", show_alert=True)
            return
    s = get_stats(chat_id, ses.export_scope)
    txt = format_stats_text(s, ses.export_scope)
    try:
        await cq.message.edit_text(txt, reply_markup=stats_scope_keyboard(ses))
    except Exception:
        await cq.message.answer(txt, reply_markup=stats_scope_keyboard(ses))
    await cq.answer("Готово")

# ---------- links (за день) ----------
LINKS_PAGE = 50

def _links_since_query(chat_id: int, scope: str, since_iso: str, limit: int, offset: int):
    conn = db_connect()
    try:
        params: List[Any] = [since_iso]
        where = "created_at >= ?"
        if scope == "chat":
            where += " AND chat_id = ?"
            params.append(chat_id)

        total = conn.execute(f"SELECT COUNT(*) FROM links WHERE {where}", tuple(params)).fetchone()[0]
        order = "ORDER BY datetime(created_at) DESC, username ASC"
        rows = conn.execute(
            f"""
            SELECT username, url, created_at, chat_id,
                   (
                       SELECT added_by FROM items
                       WHERE chat_id = links.chat_id AND username = links.username
                       ORDER BY datetime(created_at) ASC
                       LIMIT 1
                   ) AS added_by
            FROM links
            WHERE {where} {order} LIMIT ? OFFSET ?
            """,
            tuple(params + [limit, offset])
        ).fetchall()
        return int(total or 0), rows
    finally:
        conn.close()

def _fmt_links_block(rows: List[Tuple[str,str,str,int,Optional[str]]]) -> str:
    out = []
    for uname, url, created_at, _chat, added_by in rows:
        t = created_at
        try:
            dt = datetime.fromisoformat(created_at.replace("Z","+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            else:
                dt = dt.astimezone(LOCAL_TZ)
            t = dt.strftime("%H:%M")
        except Exception:
            pass
        u = escape(uname or "")
        href = escape(url or "")
        # ВАЖНО: не используем <span>; Telegram не поддерживает. Берём <i>.
        added_html = added_by_html(added_by or "")
        by_part = f" — добавил {added_html}" if added_html else ""
        out.append(f"• <a href=\"{href}\">@{u}</a>{by_part} <i>({LOCAL_TZ_LABEL} {t})</i>")
    return "\n".join(out) if out else "— нет ссылок"

def _links_scope_keyboard(ses: Session, page: int, total: int) -> InlineKeyboardMarkup:
    pages = max(1, (total + LINKS_PAGE - 1)//LINKS_PAGE)
    prev_btn = InlineKeyboardButton(text="◀️", callback_data=f"links:page:{max(1,page-1)}")
    next_btn = InlineKeyboardButton(text="▶️", callback_data=f"links:page:{min(pages,page+1)}")
    page_btn = InlineKeyboardButton(text=f"{page}/{pages}", callback_data="links:nop")

    scope = [
        InlineKeyboardButton(text=("✅ 📌 Текущий чат" if ses.export_scope=="chat" else "📌 Текущий чат"), callback_data="links:scope:chat"),
        InlineKeyboardButton(text=("✅ 🌐 Вся база" if ses.export_scope=="all" else "🌐 Вся база"), callback_data="links:scope:all"),
    ]
    act = [
        prev_btn, page_btn, next_btn,
        InlineKeyboardButton(text="📄 CSV", callback_data="links:csv"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="links:refresh"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[scope, act])

def _render_links_text(chat_id: int, scope: str, page: int) -> Tuple[str, int]:
    since = _since_utc_iso(1)
    total, rows = _links_since_query(chat_id, scope, since, LINKS_PAGE, (page-1)*LINKS_PAGE)
    head = f"🔗 Ссылки за день (последние 24ч) • область: " + ("📌 текущий чат" if scope=="chat" else "🌐 вся база")
    sub = f"Показано {len(rows)} из {total}"
    body = _fmt_links_block(rows)
    txt = f"{head}\n{sub}\n\n{body}"
    return txt, total

@dp.message(Command("links"))
async def cmd_links(msg: Message, user_id: Optional[int] = None):
    if not await ensure_user_has_access(msg, user_id=user_id):
        return

    ses = get_session(msg.chat.id)
    page = 1
    txt, total = _render_links_text(msg.chat.id, ses.export_scope, page)
    await msg.answer(txt, reply_markup=_links_scope_keyboard(ses, page, total))

@dp.callback_query(F.data.startswith("links:"))
async def on_links_click(cq: CallbackQuery):
    if not await ensure_callback_access(cq):
        return

    chat_id = cq.message.chat.id
    ses = get_session(chat_id)
    parts = cq.data.split(":")

    if len(parts) >= 3 and parts[1] == "scope":
        scope = parts[2]
        if scope in ("chat","all"):
            ses.export_scope = scope
        else:
            await cq.answer("Неизвестная область", show_alert=True)
            return
        page = 1
    elif len(parts) >= 3 and parts[1] == "page":
        try:
            page = max(1, int(parts[2]))
        except Exception:
            page = 1
    elif parts[1] == "refresh":
        page = 1
    elif parts[1] == "csv":
        since = _since_utc_iso(1)
        total, rows = _links_since_query(chat_id, ses.export_scope, since, limit=10_000, offset=0)
        if not rows:
            await cq.answer("За день нет ссылок", show_alert=True)
            return
        out = get_session(chat_id).dir / f"links_day_{ses.export_scope}.csv"
        fieldnames = ["username", "url", "created_at", "chat_id", "added_by"]
        with out.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(
                    {
                        "username": r[0],
                        "url": r[1],
                        "created_at": r[2],
                        "chat_id": r[3],
                        "added_by": r[4],
                    }
                )
        await cq.message.answer_document(
            FSInputFile(out),
            caption=f"Ссылки за день — {('вся база' if ses.export_scope=='all' else 'текущий чат')}: {total} шт.",
            request_timeout=SEND_TIMEOUT,
        )
        await cq.answer("CSV готово")
        return
    else:
        await cq.answer()
        return

    txt, total = _render_links_text(chat_id, ses.export_scope, page)
    try:
        await cq.message.edit_text(txt, reply_markup=_links_scope_keyboard(ses, page, total))
    except Exception:
        await cq.message.answer(txt, reply_markup=_links_scope_keyboard(ses, page, total))
    await cq.answer("Готово")

# ---------- reset ----------
@dp.message(Command("reset"))
async def cmd_reset(msg: Message, user_id: Optional[int] = None):
    if not await ensure_user_has_access(msg, user_id=user_id):
        return

    ses = get_session(msg.chat.id)
    for p in ses.dir.glob("*"):
        try: p.unlink()
        except Exception: pass
    ses.uploaded_html.clear(); ses.uploaded_csv.clear(); ses.export_scope = "chat"
    await msg.answer("Сессия очищена.")

# ---------- boot ----------

# ---------- Download Queue (Variant C) ----------
_DL_QUEUE: asyncio.Queue | None = None
_DL_WORKER_TASK: asyncio.Task | None = None
_DL_COUNTER = 0  # монотонный ID джоб
_CURRENT_JOB: Dict[str, Any] | None = None

_PROFILE_SCAN_QUEUE: asyncio.Queue | None = None
_PROFILE_SCAN_TASK: asyncio.Task | None = None
_PROFILE_SCAN_PENDING: set[tuple[int, str]] = set()


async def _enqueue_download_request(
    msg: Message,
    target: str,
    extra_flags: Sequence[str],
    request_user_id: Optional[int],
) -> bool:
    if msg.chat.type in ("group", "supergroup"):
        await msg.answer("🚫 Архив можно скачать только в личных сообщениях. Напишите мне в ЛС.")
        return False

    clean_target = target.strip()
    if not clean_target:
        await msg.answer("Отправьте username или ссылку профиля VSCO для скачивания.")
        return False

    username_hint = _extract_username_from_target(clean_target)
    if username_hint is None:
        if clean_target.startswith("--"):
            await msg.answer(
                "Сначала укажите username или ссылку профиля, затем дополнительные флаги. "
                "Скачивание отменено. Нажмите «Скачать профиль» и попробуйте снова."
            )
        else:
            await msg.answer(
                "Не распознал ссылку или username VSCO. Скачивание отменено. "
                "Нажмите «Скачать профиль» и отправьте корректную ссылку."
            )
        return False

    maybe_url = clean_target if "://" in clean_target else f"https://{clean_target}"
    slug = vsco_short_slug(maybe_url)
    if slug:
        try:
            async with aiohttp.ClientSession() as session:
                resolved = await resolve_vsco_short(maybe_url, session)
        except Exception:
            resolved = None
        if resolved:
            clean_target = normalize_vsco_profile_url(resolved) or resolved
        else:
            clean_target = build_perception_gallery_url(slug)

    allowed, info = has_daily_data_access(msg.chat.id, request_user_id)
    if not allowed:
        await msg.answer(info)
        return False

    ses = get_session(msg.chat.id)
    out_base = ses.dir / "downloads"
    out_base.mkdir(parents=True, exist_ok=True)

    global _DL_QUEUE, _DL_COUNTER
    if _DL_QUEUE is None:
        _DL_QUEUE = asyncio.Queue()

    _DL_COUNTER += 1
    safe_flags = [f for f in extra_flags if f.startswith("--")]
    requested_by = resolve_added_by(msg.from_user)
    job = DLJob(
        id=_DL_COUNTER,
        chat_id=msg.chat.id,
        target=clean_target,
        extra_flags=list(safe_flags),
        out_base=out_base,
        requested_by=requested_by,
    )
    await _DL_QUEUE.put(job)

    pos = _DL_QUEUE.qsize()  # позиция «после put»: 1 — значит выполнится следующим
    await msg.answer(f"🗂️ Задание #{job.id} поставлено в очередь. Позиция: {pos}.")
    return True


@dataclass
class DLJob:
    id: int
    chat_id: int
    target: str           # username или полный профильный URL
    extra_flags: list     # список флагов вида ["--max","100","--no-zip",...]
    out_base: Path        # базовая папка для выдачи
    cancelled: bool = False
    requested_by: str = ""


@dataclass
class ProfileScanJob:
    chat_id: int
    username: str
    profile_url: str
    source: str = "bot"
    added_by: str = ""


async def _enqueue_profile_scan(job: ProfileScanJob) -> bool:
    normalized_url = normalize_vsco_profile_url(job.profile_url) or job.profile_url
    username = (job.username or "").lstrip("@")
    if not normalized_url or not username:
        return False

    job.profile_url = normalized_url
    job.username = username

    key = (job.chat_id, normalized_url.lower())
    if key in _PROFILE_SCAN_PENDING:
        log.debug(
            "Profile scan already scheduled: chat_id=%s profile_url=%s",
            job.chat_id,
            normalized_url,
        )
        return False

    global _PROFILE_SCAN_QUEUE
    if _PROFILE_SCAN_QUEUE is None:
        _PROFILE_SCAN_QUEUE = asyncio.Queue()

    await _PROFILE_SCAN_QUEUE.put(job)
    _PROFILE_SCAN_PENDING.add(key)
    log.info(
        "Profile scan scheduled: chat_id=%s profile_url=%s added_by=%s",
        job.chat_id,
        normalized_url,
        job.added_by,
    )
    return True


async def _profile_scan_worker() -> None:
    global _PROFILE_SCAN_QUEUE
    if _PROFILE_SCAN_QUEUE is None:
        _PROFILE_SCAN_QUEUE = asyncio.Queue()

    log.info("Profile scan worker started")
    while True:
        job: ProfileScanJob = await _PROFILE_SCAN_QUEUE.get()
        key = (job.chat_id, job.profile_url.lower())
        try:
            log.info(
                "Profile scan worker: start chat_id=%s profile_url=%s",
                job.chat_id,
                job.profile_url,
            )
            collected = await collect_profile_media(
                job.profile_url,
                max_width=MEDIA_PAGE_MAX_WIDTH,
                include_details=True,
            )
            if isinstance(collected, ProfileMediaCollection):
                media_urls = collected.media_urls
                profile_tabs = collected.profile_tabs
            else:
                media_urls = collected
                profile_tabs = []
            result: ScanResult = store_profile_media(
                Path(DB_PATH),
                job.chat_id,
                job.username,
                job.profile_url,
                media_urls,
                source=job.source,
                added_by=job.added_by,
                profile_tabs=profile_tabs,
            )
            if result.added_items > 0 or not result.media_urls:
                total = len(result.media_urls)
                text = (
                    f"🔍 Профиль <code>@{escape(job.username)}</code> — найдено ссылок: <b>{total}</b>. "
                    f"Новых: <b>{result.added_items}</b>."
                )
                if not result.media_urls:
                    text += "\n⚠️ Не удалось обнаружить медиа у этого профиля."
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        job.chat_id,
                        text,
                        parse_mode="HTML",
                    )
        except asyncio.CancelledError:
            log.info("Profile scan worker cancelled")
            raise
        except Exception:
            log.exception(
                "Profile scan worker failed for chat_id=%s profile_url=%s",
                job.chat_id,
                job.profile_url,
            )
            with contextlib.suppress(Exception):
                await bot.send_message(
                    job.chat_id,
                    (
                        "❌ Не удалось сканировать профиль "
                        f"<code>{escape(job.profile_url)}</code>."
                    ),
                    parse_mode="HTML",
                )
        finally:
            _PROFILE_SCAN_PENDING.discard(key)
            if _PROFILE_SCAN_QUEUE is not None:
                _PROFILE_SCAN_QUEUE.task_done()

def _dl_script_path() -> Path:
    # vsco_downloader.py должен лежать рядом с текущим файлом
    return Path(__file__).with_name("vsco_downloader.py")


@dp.message(Command("dl"))
async def cmd_dl_enqueue(msg: Message, user_id: Optional[int] = None):
    """
    /dl <vsco_username | profile_url> [--flags ...]
    Кладёт задание в очередь. Выполняет воркер по одному.
    """
    if not await ensure_user_has_access(msg, user_id=user_id):
        return

    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.answer("Usage: <code>/dl &lt;username|profile_url&gt; [--flags...]</code>\n"
                         "Например: <code>/dl johndoe --max 120 --split-zip-size-mb 45</code>")
        return

    target = parts[1]
    extra_flags = [p for p in parts[2:] if p.startswith("--")]  # простой whitelist
    await _enqueue_download_request(
        msg,
        target,
        extra_flags,
        getattr(msg.from_user, "id", None),
    )


@dp.message(Command("qstat"))
async def cmd_qstat(msg: Message, user_id: Optional[int] = None):
    if not await ensure_user_has_access(msg, user_id=user_id):
        return

    q = _DL_QUEUE
    size = q.qsize() if q else 0
    await msg.answer(f"📊 В очереди заданий: {size}. Один воркер обрабатывает по одному.")


def _format_admin_queue_report() -> str:
    lines = ["📋 <b>Очередь загрузок</b>"]

    cur = _CURRENT_JOB
    job = cur.get("job") if isinstance(cur, dict) else None
    if isinstance(job, DLJob):
        flag_display = " ".join(job.extra_flags) if job.extra_flags else "—"
        lines.append(
            f"▶️ Выполняется #{job.id} — <code>{escape(job.target)}</code> (чат {job.chat_id})"
        )
        lines.append(f"   Флаги: <code>{escape(flag_display)}</code>")
    else:
        lines.append("▶️ Активных заданий нет.")

    pending_jobs: List[DLJob] = []
    q = _DL_QUEUE
    if q:
        try:
            raw = list(q._queue)  # type: ignore[attr-defined]
        except Exception:
            raw = []
        for item in raw:
            if isinstance(item, DLJob):
                pending_jobs.append(item)

    if pending_jobs:
        lines.append(f"⏳ В ожидании: {len(pending_jobs)}")
        for idx, pending in enumerate(pending_jobs, start=1):
            flag_display = " ".join(pending.extra_flags) if pending.extra_flags else "—"
            lines.append(
                f"{idx}. #{pending.id} — <code>{escape(pending.target)}</code> "
                f"(чат {pending.chat_id}, флаги: <code>{escape(flag_display)}</code>)"
            )
    else:
        lines.append("⏳ Очередь пуста.")

    return "\n".join(lines)


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Очередь", callback_data="admin:queue")],
            [InlineKeyboardButton(text="🧮 Лимиты", callback_data="admin:limits")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        ]
    )


@dp.message(Command("admin"))
async def cmd_admin(msg: Message, user_id: Optional[int] = None):
    if not await ensure_user_has_access(msg, user_id=user_id):
        return

    if not require_admin(msg):
        await msg.answer("🚫 Команда доступна только администраторам.")
        return
    await msg.answer(
        "🛠️ <b>Панель администратора</b>\nВыберите действие:",
        reply_markup=_admin_keyboard(),
    )


@dp.callback_query(F.data.startswith("admin:"))
async def on_admin_click(cq: CallbackQuery):
    if not await ensure_callback_access(cq):
        return

    if not is_admin_id(getattr(cq.from_user, "id", None)):
        await cq.answer("🚫 Недостаточно прав", show_alert=True)
        return

    action = (cq.data or "").split(":", 1)[1] if ":" in (cq.data or "") else ""
    chat_id = cq.message.chat.id if cq.message else None

    if action == "queue":
        text = _format_admin_queue_report()
        if cq.message:
            await cq.message.answer(text)
        await cq.answer("Готово")
        return

    if action == "limits" and chat_id is not None:
        _, info = has_daily_data_access(chat_id, getattr(cq.from_user, "id", None))
        if cq.message:
            await cq.message.answer(info)
        await cq.answer("Готово")
        return

    if action == "stats" and chat_id is not None:
        ses = get_session(chat_id)
        stats = get_stats(chat_id, ses.export_scope)
        text = format_stats_text(stats, ses.export_scope)
        if cq.message:
            await cq.message.answer(text)
        await cq.answer("Готово")
        return

    await cq.answer("Неизвестная команда", show_alert=True)


@dp.callback_query(F.data.startswith("cancel:"))
async def cq_cancel_job(cq: CallbackQuery):
    if not await ensure_callback_access(cq):
        return

    global _CURRENT_JOB
    try:
        job_id = int(cq.data.split(":", 1)[1])
    except Exception:
        await cq.answer()
        return
    cur = _CURRENT_JOB
    if cur and cur.get("job") and cur["job"].id == job_id:
        cur["job"].cancelled = True
        proc = cur.get("proc")
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                log.warning("Job #%s: process did not terminate, killing", job_id)
                proc.kill()
        evt = cur.get("stop_evt")
        if evt:
            evt.set()
        await cq.answer("Останавливаю…")
    else:
        await cq.answer("Задание не выполняется", show_alert=True)


def rebuild_urls_extracted(user_dir: Path) -> None:
    """Recreate urls_extracted.txt from manifest.json using full image URLs.

    Some external downloaders deduplicate entries by filename which causes
    distinct VSCO links with the same basename to be lost. Here we rebuild the
    list so every unique image URL is written out regardless of name
    collisions.
    """
    man = user_dir / "manifest.json"
    if not man.exists():
        return
    try:
        data = json.loads(man.read_text(encoding="utf-8"))
    except Exception:
        return
    items = data.get("items", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return
    urls: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url: Optional[str] = None
        ok_flag = item.get("ok")
        for key in ("image_url", "responsive_url", "url"):
            candidate = item.get(key)
            if not isinstance(candidate, str) or not candidate:
                continue
            if key == "url" and ok_flag is False:
                continue
            url = candidate
            break
        if not isinstance(url, str):
            continue
        if is_vsco_logo_url(url) or url in seen:
            continue
        urls.append(url)
        seen.add(url)
    if not urls:
        return
    try:
        out = user_dir / "urls_extracted.txt"
        out.write_text("\n".join(urls) + "\n", encoding="utf-8")
    except Exception:
        pass


def _extract_username_from_target(target: str) -> Optional[str]:
    text = (target or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        text = text[1:]
    if USERNAME_RE.match(text):
        return text
    normalized = text if "://" in text else f"https://{text}"
    info = classify_vsco_path(text)
    if not info:
        info = classify_vsco_path(normalized)
    username = None
    if isinstance(info, dict):
        username = info.get("username") or info.get("slug")
    if isinstance(username, str) and username.strip():
        return username.strip()
    if is_vsco_url(text) or is_vsco_url(normalized):
        candidate_url = text if is_vsco_url(text) else normalized
        extracted = username_from_vsco_co(candidate_url)
        if extracted:
            return extracted
        slug = vsco_short_slug(candidate_url)
        if slug:
            return slug
    else:
        slug = vsco_short_slug(normalized)
        if slug:
            return slug
    return None


def _read_manifest_summary(user_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    man = user_dir / "manifest.json"
    if not man.exists():
        return result
    try:
        data = json.loads(man.read_text(encoding="utf-8"))
    except Exception:
        return result

    if isinstance(data, dict):
        for key in ("username", "profile_url", "display_name", "full_name", "bio"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                result[key] = value.strip()
        for key in ("media_count", "total", "count", "items_count"):
            value = data.get(key)
            if isinstance(value, int) and value > 0:
                result[key] = value
        items = data.get("items")
        if isinstance(items, list) and "items_count" not in result:
            result["items_count"] = len(items)
    return result


def _build_archive_summary(
    job: "DLJob",
    username: Optional[str],
    info: Optional[ProfileArchiveInfo],
    manifest_meta: Dict[str, Any],
    total_found: Optional[int],
) -> ArchiveSummaryData:
    summary = ArchiveSummaryData()

    resolved_username = (username or "").strip()
    if not resolved_username:
        candidate = manifest_meta.get("username")
        if isinstance(candidate, str) and candidate.strip():
            resolved_username = candidate.strip()
    summary.username = resolved_username or None

    display_name = manifest_meta.get("display_name") or manifest_meta.get("full_name")
    if isinstance(display_name, str) and display_name.strip():
        summary.display_name = display_name.strip()

    profile_url = ""
    if info and info.profile_url:
        profile_url = info.profile_url
    elif isinstance(manifest_meta.get("profile_url"), str):
        profile_url = manifest_meta["profile_url"]
    elif resolved_username:
        profile_url = f"https://vsco.co/{resolved_username}"
    elif job.target and is_vsco_url(job.target):
        profile_url = job.target
    summary.profile_url = profile_url or None

    total_media: Optional[int] = None
    if isinstance(total_found, int) and total_found > 0:
        total_media = total_found
    else:
        for key in ("media_count", "total", "count", "items_count"):
            value = manifest_meta.get(key)
            if isinstance(value, int) and value > 0:
                total_media = value
                break
    if total_media is None and info and info.items_count:
        total_media = info.items_count
    summary.total_media = total_media

    if info:
        summary.comments = list(info.comments)
        summary.last_created_at = info.last_created_at
        if info.added_by_raw:
            summary.added_by_html = added_by_html(info.added_by_raw)

    return summary


def _format_archive_admin_message(
    job: "DLJob",
    *,
    summary: ArchiveSummaryData,
    zip_count: int,
) -> str:
    lines: List[str] = ["📦 <b>Выгрузка профиля VSCO</b>"]
    lines.append(f"🆔 Задание: <code>#{job.id}</code> • Чат: <code>{job.chat_id}</code>")

    requested = (job.target or "").strip()
    if requested:
        lines.append(f"🎯 Запрос: <code>{escape(requested)}</code>")

    if summary.username:
        lines.append(f"👤 Профиль: <code>{escape(summary.username)}</code>")

    if summary.display_name:
        lines.append(f"📛 Имя в профиле: <b>{escape(summary.display_name)}</b>")

    if summary.profile_url:
        lines.append(f"🔗 <a href=\"{escape(summary.profile_url)}\">{escape(summary.profile_url)}</a>")

    if summary.total_media is not None:
        lines.append(f"📸 Медиа: <b>{summary.total_media}</b>")

    if summary.last_created_at:
        lines.append(f"🕒 Последняя запись в базе: <code>{escape(summary.last_created_at)}</code>")

    if summary.added_by_html:
        lines.append(f"📝 В базу добавил: {summary.added_by_html}")

    if job.requested_by:
        lines.append(f"🙋 Скачивание запросил: {escape(job.requested_by)}")

    lines.append(f"📁 Архивов: <b>{zip_count}</b>")

    if summary.comments:
        lines.append("💬 Комментарии:")
        preview = summary.comments[:5]
        for comment in preview:
            sanitized = escape(comment).replace("\n", " ")
            lines.append(f"• {sanitized}")
        if len(summary.comments) > len(preview):
            lines.append(f"… и ещё {len(summary.comments) - len(preview)}")
    else:
        lines.append("💬 Комментариев в базе не найдено")

    return "\n".join(lines)


def _format_archive_summary_message(
    job: "DLJob",
    *,
    summary: ArchiveSummaryData,
) -> str:
    lines: List[str] = ["📦 <b>Новая выгрузка VSCO</b>"]

    if summary.username:
        lines.append(f"👤 Профиль: <code>{escape(summary.username)}</code>")

    if summary.display_name:
        lines.append(f"📛 Имя: <b>{escape(summary.display_name)}</b>")

    if summary.total_media is not None:
        lines.append(f"📸 Медиа: <b>{summary.total_media}</b>")

    comments_count = len(summary.comments)
    if comments_count:
        lines.append(f"💬 Комментарии ({comments_count}):")
        preview = summary.comments[:3]
        for comment in preview:
            sanitized = escape(comment).replace("\n", " ")
            lines.append(f"• {sanitized}")
        if comments_count > len(preview):
            lines.append(f"… и ещё {comments_count - len(preview)}")
    else:
        lines.append("💬 Комментариев нет")

    if job.requested_by:
        lines.append(f"🙋 Запросил: {escape(job.requested_by)}")

    return "\n".join(lines)


def _create_single_archive(
    job: "DLJob",
    zips: Sequence[Path],
    user_dir: Path,
    summary: ArchiveSummaryData,
) -> Optional[Path]:
    if not zips:
        return None
    if len(zips) == 1:
        return zips[0]

    slug_source = summary.username or user_dir.name or f"profile_{job.id}"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", slug_source).strip("_ ")
    if not slug:
        slug = f"profile_{job.id}"

    dest = user_dir / f"{slug}_full.zip"
    counter = 1
    while dest.exists():
        counter += 1
        dest = user_dir / f"{slug}_full_{counter}.zip"

    extracted_any = False
    with tempfile.TemporaryDirectory(dir=user_dir) as tmp_dir:
        tmp_path = Path(tmp_dir)
        for part in zips:
            try:
                with zipfile.ZipFile(part) as src:
                    src.extractall(tmp_path)
                    extracted_any = True
            except Exception as err:
                log.warning("Job #%s: failed to extract %s for combined archive: %s", job.id, part, err)
        if not extracted_any:
            return None

        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as dst:
            for file_path in sorted(tmp_path.rglob("*")):
                if file_path.is_file():
                    dst.write(file_path, file_path.relative_to(tmp_path))

    if dest.exists():
        log.info("Job #%s: combined %d archives into %s", job.id, len(zips), dest)
        return dest
    return None


async def _send_archives_to_channels(
    job: "DLJob",
    zips: Sequence[Path],
    user_dir: Path,
    total_found: Optional[int],
) -> None:
    if not ARCHIVE_ADMIN_CHANNEL_ID and not ARCHIVE_SUMMARY_CHANNEL_ID:
        return

    manifest_meta = _read_manifest_summary(user_dir)
    username = _extract_username_from_target(job.target)
    info = fetch_profile_archive_info(username)
    summary = _build_archive_summary(job, username, info, manifest_meta, total_found)

    if ARCHIVE_ADMIN_CHANNEL_ID:
        admin_text = _format_archive_admin_message(
            job,
            summary=summary,
            zip_count=len(zips),
        )
        await bot.send_message(ARCHIVE_ADMIN_CHANNEL_ID, admin_text, disable_web_page_preview=True)
        for z in zips:
            await bot.send_document(
                ARCHIVE_ADMIN_CHANNEL_ID,
                FSInputFile(z, filename=z.name),
                caption=f"📦 {z.name}",
                request_timeout=SEND_TIMEOUT,
            )

    if ARCHIVE_SUMMARY_CHANNEL_ID:
        summary_text = _format_archive_summary_message(job, summary=summary)
        await bot.send_message(ARCHIVE_SUMMARY_CHANNEL_ID, summary_text, disable_web_page_preview=True)

        combined_path: Optional[Path] = None
        try:
            combined_path = await asyncio.to_thread(_create_single_archive, job, zips, user_dir, summary)
        except Exception:
            log.exception("Job #%s: failed to build single archive for summary channel", job.id)

        if combined_path:
            try:
                await bot.send_document(
                    ARCHIVE_SUMMARY_CHANNEL_ID,
                    FSInputFile(combined_path, filename=combined_path.name),
                    caption=f"📦 {combined_path.name}",
                    request_timeout=SEND_TIMEOUT,
                )
            finally:
                if combined_path not in zips:
                    with contextlib.suppress(Exception):
                        combined_path.unlink(missing_ok=True)


class Stage(Enum):
    SCAN = "Сканирование"
    DOWNLOAD = "Загрузка"
    ARCHIVE = "Архив"


async def _dl_worker():
    """Единственный воркер: берёт задания по одному и запускает downloader как отдельный процесс.
    Добавлено: во время сканирования показывает «Найдено медиа: N» по manifest/urls_extracted или stdout-подсказкам.
    """
    global _DL_QUEUE, _CURRENT_JOB
    if _DL_QUEUE is None:
        _DL_QUEUE = asyncio.Queue()

    def build_progress_text(stage: Stage, target: str, total_found: int | None, downloaded: int, zip_parts: int | None) -> str:
        icon = {Stage.SCAN: "🔄", Stage.DOWNLOAD: "📥", Stage.ARCHIVE: "🗜️"}[stage]
        txt = f"{icon} <b>{stage.value}</b>: <code>{target}</code>"
        if total_found is not None:
            txt += f"\n🔎 Найдено медиа: <b>{total_found}</b>"
        elif stage in {Stage.SCAN, Stage.DOWNLOAD}:
            txt += "\n🔎 Найдено медиа: <b>0</b>"
        if stage is Stage.DOWNLOAD and total_found is not None:
            txt += f"\n📥 Загрузка: <b>{downloaded}/{total_found}</b>"
        if stage is Stage.ARCHIVE and zip_parts is not None:
            txt += f"\n🗜️ Архив: будет {zip_parts} томов"
        return txt

    def bump_total(candidate: Optional[int], origin: str) -> bool:
        nonlocal total_found
        if candidate is None:
            return False
        try:
            candidate_int = int(candidate)
        except (TypeError, ValueError):
            return False
        if candidate_int <= 0:
            return False
        if total_found is None or candidate_int > total_found:
            total_found = candidate_int
            log.info("Job #%s: media total updated to %d via %s", job.id, total_found, origin)
            return True
        return False

    async def _safe_edit(chat_id: int, message_id: int, text: str, reply_markup=None):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            raise

    while True:
        job: DLJob = await _DL_QUEUE.get()
        try:
            job_started = time.time()
            log.info("Job #%s: start for %s", job.id, job.target)
            cancel_kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{job.id}")]]
            )
            stage = Stage.SCAN
            total_found: int | None = None
            downloaded = 0
            zip_parts: int | None = None
            progress = await bot.send_message(
                job.chat_id,
                build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                reply_markup=cancel_kb,
            )
            global _CURRENT_JOB
            _CURRENT_JOB = {"job": job, "progress": progress}

            script = _dl_script_path()
            if not script.exists():
                log.error("Job #%s: downloader script not found at %s", job.id, script)
                await bot.send_message(job.chat_id, "❌ Не найден vsco_downloader.py рядом с ботом.")
                continue

            # Формируем аргументы CLI
            args = [sys.executable, "-u", str(script)]
            if job.target.startswith("http://") or job.target.startswith("https://"):
                args += ["--profile-url", job.target]
            else:
                args += ["--username", job.target]

            # Базовые опции: каталог вывода и безопасный размер ZIP для Telegram
            args += ["--out", str(job.out_base), "--split-zip-size-mb", "45"]

            # Пользовательские флаги
            if job.extra_flags:
                args += job.extra_flags

            cmd_display = " ".join(shlex.quote(str(a)) for a in args)
            log.info("Job #%s: running downloader: %s", job.id, cmd_display)

            user_dir: Path | None = None
            log.debug("Job #%s: initial stage %s", job.id, stage.value)

            stop_evt = asyncio.Event()

            async def fs_probe_loop():
                nonlocal user_dir, total_found, stage, downloaded, zip_parts
                last_edit = 0.0
                cutoff = max(job_started - 1.0, 0.0)
                last_manifest_mtime = cutoff
                last_urls_mtime = cutoff
                loop = asyncio.get_running_loop()
                while not stop_evt.is_set():
                    try:
                        if user_dir is None:
                            cand = [p for p in job.out_base.glob("*") if p.is_dir()]
                            if cand:
                                user_dir = max(cand, key=lambda p: p.stat().st_mtime)
                                log.debug("Job #%s: working directory %s", job.id, user_dir)
                        progress_dirty = False
                        if user_dir:
                            man = user_dir / "manifest.json"
                            if man.exists():
                                mtime = man.stat().st_mtime
                                if mtime >= cutoff and mtime > last_manifest_mtime:
                                    last_manifest_mtime = mtime
                                    try:
                                        data = json.loads(man.read_text(encoding="utf-8"))
                                    except Exception:
                                        data = None
                                    if bump_total(infer_media_total_from_manifest(data), "manifest"):
                                        progress_dirty = True
                            urls = user_dir / "urls_extracted.txt"
                            if urls.exists():
                                mtime = urls.stat().st_mtime
                                if mtime >= cutoff and mtime > last_urls_mtime:
                                    last_urls_mtime = mtime
                                    if bump_total(infer_media_total_from_urls_file(urls), "urls_extracted"):
                                        progress_dirty = True
                        txt = build_progress_text(stage, job.target, total_found, downloaded, zip_parts)
                        now = loop.time()
                        if progress_dirty or now - last_edit >= 2.0:
                            await _safe_edit(job.chat_id, progress.message_id, txt, reply_markup=cancel_kb)
                            last_edit = now
                    except Exception:
                        pass
                    await asyncio.sleep(2.0)

            # Запускаем процесс и фоновый опрос файловой системы
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            _CURRENT_JOB.update({"proc": proc, "stop_evt": stop_evt})
            fs_task = asyncio.create_task(fs_probe_loop())
            log.debug("Job #%s: subprocess started", job.id)

            # Разбираем stdout для подсказок по стадиям и числу найденных
            last_ping = 0.0
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    txt = line.decode("utf-8", "ignore").rstrip()
                    low = txt.lower()

                    m = re.search(r"\bscan_progress\s+(\d+)", txt)
                    if not m and txt.startswith("scan_progress"):
                        m = re.search(r"(\d+)", txt)
                    if m and bump_total(int(m.group(1)), "stdout:scan_progress"):
                        await _safe_edit(
                            job.chat_id,
                            progress.message_id,
                            build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                            reply_markup=cancel_kb,
                        )
                        continue

                    if any(k in low for k in ("download", "загрузка", "скачива")) and stage is not Stage.DOWNLOAD:
                        stage = Stage.DOWNLOAD
                        downloaded = 0
                        log.info("Job #%s: stage -> %s", job.id, stage.value)
                        await _safe_edit(
                            job.chat_id,
                            progress.message_id,
                            build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                            reply_markup=cancel_kb,
                        )
                    if any(k in low for k in ("zip", "архив")) and stage is not Stage.ARCHIVE:
                        stage = Stage.ARCHIVE
                        log.info("Job #%s: stage -> %s", job.id, stage.value)
                        await _safe_edit(
                            job.chat_id,
                            progress.message_id,
                            build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                            reply_markup=cancel_kb,
                        )

                    if re.match(r"^\[\d+\]\s+ok", low):
                        downloaded += 1
                        if stage is Stage.DOWNLOAD and total_found is not None:
                            await _safe_edit(
                                job.chat_id,
                                progress.message_id,
                                build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                                reply_markup=cancel_kb,
                            )
                        continue

                    if "создано zip-томов" in low:
                        m = re.search(r"(\d+)", low)
                        if m:
                            zip_parts = int(m.group(1))
                            if stage is Stage.ARCHIVE:
                                await _safe_edit(
                                    job.chat_id,
                                    progress.message_id,
                                    build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                                    reply_markup=cancel_kb,
                                )
                        continue

                    m = re.search(r"(?:found|найден[оа])\D+(\d+)\D+(?:media|items|files|медиа|ссыл)", low)
                    if m and bump_total(int(m.group(1)), "stdout:found"):
                        await _safe_edit(
                            job.chat_id,
                            progress.message_id,
                            build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                            reply_markup=cancel_kb,
                        )
                        continue

                    now = asyncio.get_running_loop().time()
                    if now - last_ping > 10.0:
                        try:
                            await _safe_edit(
                                job.chat_id,
                                progress.message_id,
                                build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                                reply_markup=cancel_kb,
                            )
                        except Exception:
                            pass
                        last_ping = now

                rc = await proc.wait()
                log.info("Job #%s: downloader exited with code %s", job.id, rc)
            finally:
                stop_evt.set()
                with contextlib.suppress(Exception):
                    await fs_task

            if job.cancelled:
                await _safe_edit(
                    job.chat_id,
                    progress.message_id,
                    "🚫 Задание отменено пользователем.",
                    reply_markup=None,
                )
                await bot.send_message(job.chat_id, f"❌ Задание #{job.id} отменено.")
                log.info("Job #%s: cancelled", job.id)
                _CURRENT_JOB = None
                continue

            # Отправка результатов
            user_dirs = [p for p in job.out_base.glob("*") if p.is_dir()]
            user_dir = max(user_dirs, key=lambda p: p.stat().st_mtime, default=None)

            db_items_added = 0
            db_link_added = False

            if user_dir is not None:
                rebuild_urls_extracted(user_dir)
                try:
                    db_items_added, db_link_added = ingest_download_results(job, user_dir)
                    if db_items_added or db_link_added:
                        log.info(
                            "Job #%s: ingested %d items%s into DB",
                            job.id,
                            db_items_added,
                            " + profile link" if db_link_added else "",
                        )
                except Exception:
                    log.exception("Job #%s: failed to ingest download results", job.id)

                if bump_total(infer_media_total(user_dir), "final_artifacts"):
                    await _safe_edit(
                        job.chat_id,
                        progress.message_id,
                        build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                        reply_markup=cancel_kb,
                    )

            if user_dir is None:
                log.warning("Job #%s: completed but no results found", job.id)
                await _safe_edit(job.chat_id, progress.message_id, f"⚠️ Завершено, но результирующих файлов не найдено.")
            else:
                zips = sorted(
                    p for p in user_dir.glob("*.zip") if p.stat().st_mtime >= job_started
                )
                if zips:
                    log.info("Job #%s: %d zip(s) ready", job.id, len(zips))
                    stage = Stage.ARCHIVE
                    zip_parts = len(zips)
                    await _safe_edit(
                        job.chat_id,
                        progress.message_id,
                        build_progress_text(stage, job.target, total_found, downloaded, zip_parts) + "\n📦 Архивы готовы — отправляю…",
                        reply_markup=None,
                    )
                    for z in zips:
                        try:
                            log.info("Job #%s: sending archive %s", job.id, z)
                            await bot.send_document(
                                job.chat_id,
                                FSInputFile(z, filename=z.name),
                                caption=f"📦 {z.name}",
                                request_timeout=SEND_TIMEOUT,
                            )
                        except Exception as e:
                            log.warning("Job #%s: failed to send %s: %s", job.id, z, e)
                            await bot.send_message(job.chat_id, f"Не удалось отправить {z.name}: {e}")
                    if ARCHIVE_ADMIN_CHANNEL_ID or ARCHIVE_SUMMARY_CHANNEL_ID:
                        try:
                            await _send_archives_to_channels(job, zips, user_dir, total_found)
                        except Exception as e:
                            log.warning(
                                "Job #%s: failed to mirror archives to channel: %s", job.id, e
                            )
                else:
                    man = user_dir / "manifest.json"
                    urls = user_dir / "urls_extracted.txt"
                    lines = [f"✅ Готово."]
                    if total_found is not None:
                        lines.append(f"🔎 Медиа найдено: <b>{total_found}</b>")
                    await _safe_edit(job.chat_id, progress.message_id, "\n".join(lines))
                    if man.exists():
                        log.info("Job #%s: sending manifest", job.id)
                        await bot.send_document(
                            job.chat_id,
                            BufferedInputFile(man.read_bytes(), filename=man.name),
                            caption="manifest.json",
                            request_timeout=SEND_TIMEOUT,
                        )
                    if urls.exists():
                        log.info("Job #%s: sending urls_extracted", job.id)
                        await bot.send_document(
                            job.chat_id,
                            BufferedInputFile(urls.read_bytes(), filename=urls.name),
                            caption="urls_extracted.txt",
                            request_timeout=SEND_TIMEOUT,
                        )

            await bot.send_message(job.chat_id, f"✅ Задание #{job.id} завершено.")
            log.info("Job #%s: finished", job.id)
            _CURRENT_JOB = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                await bot.send_message(job.chat_id, f"❌ Ошибка в задании #{job.id}: {e}")
            except Exception:
                pass
            log.exception("Job #%s: failed", job.id)
        finally:
            _DL_QUEUE.task_done()
            _CURRENT_JOB = None
            log.debug("Job #%s: task done", job.id)


@dp.message(Command("tutorial"))
async def cmd_tutorial(msg: Message, user_id: Optional[int] = None):
    if not await ensure_user_has_access(msg, user_id=user_id):
        return

    text = (
        "❗ <b>Tutorial:</b>\n"
        "<b>Как получать ссылки профилей и ссылки фоток с координатами.</b>\n\n"

        "В <a href='https://t.me/VSCoord_bot'>@VSCoord_bot</a> даётся 0.6 бесплатных кредитов.\n"
        "Их можно использовать для получения профилей на радиус 3.4 км, фоток с координатами на радиус 1.8 км.\n\n"

        "Бот <a href='https://t.me/vscoleak_bot'>@vscoleak_bot</a> создан для сбора всех ссылок в одном месте и скачивания профиля.\n"
        "Также формируется карта с координатами.\n\n"

        "❗ Для доступа к командам <code>/dl</code> (скачивание профиля) и <code>/export</code> "
        "(экспорт базы в формате csv, карты и галереи) нужно выполнить одно из условий:\n"
        "• Отправить в бота или 15 уникальных ссылок профиля, или 10 уникальных ссылок фоток с координатами.\n"
        "Можно добавлять комментарий к ссылке, который будет добавлен в базу.\n"
        "Доступ к командам сбрасывается каждые сутки.\n\n"

        "🔶 Все результаты <a href='https://t.me/VSCoord_bot'>@VSCoord_bot</a> воспринимаются "
        "<a href='https://t.me/vscoleak_bot'>@vscoleak_bot</a>.\n"
        "Для добавления можно переслать результат.\n\n"

        "✅ Основная цель бота — заполнить полностью карту координатами фоток.\n"
        "В дальнейшем будут обновления, если есть предложения по функционалу — пишите в личные сообщения или в группу.\n"
    )
    await msg.answer(text)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать профиль", callback_data="menu:download")],
            [InlineKeyboardButton(text="📤 Экспорт", callback_data="menu:export")],
            [
                InlineKeyboardButton(text="🔗 Ссылки за 24ч", callback_data="menu:links"),
                InlineKeyboardButton(text="📈 Статистика", callback_data="menu:stats"),
            ],
            [InlineKeyboardButton(text="📊 Очередь", callback_data="menu:qstat")],
            [InlineKeyboardButton(text="📚 Туториал", callback_data="menu:tutorial")],
        ]
    )


def functions_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📥 Скачать профиль"),
                KeyboardButton(text="📤 Экспорт"),
            ],
            [
                KeyboardButton(text="🔗 Ссылки за 24ч"),
                KeyboardButton(text="📈 Статистика"),
            ],
            [
                KeyboardButton(text="📊 Очередь"),
                KeyboardButton(text="📚 Туториал"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите функцию",
    )


@dp.message(Command("start", "help"))
async def cmd_help(msg: Message):
    if not await ensure_user_has_access(msg):
        return

    text = (
        "🆘 <b>Справка</b>\n\n"
        "Этот бот скачивает медиа из VSCO через очередь заданий, собирает статистику\n"
        "и позволяет экспортировать ссылки. Во время сканирования профиля я показываю,\n"
        "<b>сколько медиа найдено</b>. Можно загружать CSV/HTML со ссылками.\n\n"
        "📋 <b>Команды</b>:\n"
        "• <b>/start</b> — приветствие и справка\n"
        "• <b>/dl &lt;username|profile_url&gt; [--flags]</b> — поставить профиль на скачивание\n"
        "• <b>/qstat</b> — показать размер очереди\n"
        "• <b>/links</b> — ссылки за последние 24 часа\n"
        f"• <b>/export</b> — экспорт CSV/галереи или карты (после {DAILY_PROFILE_LIMIT} новых профилей за сутки)\n"
        "• <b>/stats</b> — статистика по скачанным данным\n"
        "• <b>/reset</b> — очистить текущую сессию\n"
        "• <b>/tutorial</b> — пошаговый гайд по использованию\n"
        "• <b>/help</b> — эта справка\n\n"
        f"ℹ️ Экспорт и скачивание доступны, если за последние 24 часа добавлено {DAILY_PROFILE_LIMIT} новых профилей. "
        "Лимит обнуляется ежедневно.\n\n"
        "💡 <b>Примеры</b>:\n"
        "• <code>/dl johndoe</code>\n"
        "• <code>/dl https://vsco.co/johndoe </code>\n\n"
        "👇 Быстрые действия доступны на кнопках ниже."
    )
    await msg.answer(text, reply_markup=main_menu_keyboard())
    await msg.answer(
        "👇 Быстрый доступ к функциям также доступен через клавиатуру.",
        reply_markup=functions_reply_keyboard(),
    )


@dp.callback_query(F.data.startswith("menu:"))
async def on_menu_click(cq: CallbackQuery):
    if not await ensure_callback_access(cq):
        return

    if not cq.data:
        await cq.answer()
        return

    action = cq.data.split(":", 1)[1] if ":" in cq.data else ""
    chat_id = cq.message.chat.id if cq.message else cq.from_user.id
    ses = get_session(chat_id)

    if action == "download":
        if cq.message and cq.message.chat.type in ("group", "supergroup"):
            await cq.answer("Скачивание доступно только в личных сообщениях. Напишите мне в ЛС.", show_alert=True)
            return
        ses.pending_action = "download"
        await cq.message.answer(
            "Отправьте username или ссылку профиля VSCO, чтобы поставить скачивание в очередь."
            " Можно добавить флаги, например: <code>username --max 100</code>."
        )
        await cq.answer("Ожидаю ввод")
        return

    if action == "links":
        await cmd_links(cq.message, user_id=getattr(cq.from_user, "id", None))
        await cq.answer("Готово")
        return

    if action == "stats":
        await cmd_stats(cq.message, user_id=getattr(cq.from_user, "id", None))
        await cq.answer("Готово")
        return

    if action == "export":
        if cq.message and cq.message.chat.type in ("group", "supergroup"):
            await cq.answer("Экспорт доступен только в личных сообщениях. Напишите мне в ЛС.", show_alert=True)
            return
        if cq.message is not None:
            await export_manager.open_menu(cq.message, user_id=getattr(cq.from_user, "id", None))
        await cq.answer("Открываю экспорт")
        return

    if action == "qstat":
        await cmd_qstat(cq.message, user_id=getattr(cq.from_user, "id", None))
        await cq.answer("Готово")
        return

    if action == "tutorial":
        await cmd_tutorial(cq.message, user_id=getattr(cq.from_user, "id", None))
        await cq.answer()
        return

    await cq.answer()


async def _start_polling_with_retries(*, max_attempts: Optional[int] = None) -> None:
    """Start polling and recover from transient Telegram network errors."""
    delay = 1
    attempt = 0
    while True:
        attempt += 1
        try:
            await dp.start_polling(bot)
            log.info("Polling finished (attempt %s)", attempt)
            break
        except asyncio.CancelledError:
            raise
        except TelegramNetworkError as err:
            if max_attempts is not None and attempt >= max_attempts:
                log.error(
                    "Polling aborted after %s attempts due to network error", attempt, exc_info=err
                )
                raise
            log.error(
                "Polling failed due to network error (attempt %s), retrying in %s seconds",
                attempt,
                delay,
                exc_info=err,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception:
            log.exception("Unhandled error during polling")
            raise


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    # --- start download worker ---
    global _DL_WORKER_TASK, _DL_QUEUE
    if _DL_QUEUE is None:
        _DL_QUEUE = asyncio.Queue()
    if _DL_WORKER_TASK is None or _DL_WORKER_TASK.done():
        _DL_WORKER_TASK = asyncio.create_task(_dl_worker())
    # -----------------------------
    global _PROFILE_SCAN_TASK, _PROFILE_SCAN_QUEUE
    if _PROFILE_SCAN_QUEUE is None:
        _PROFILE_SCAN_QUEUE = asyncio.Queue()
    if _PROFILE_SCAN_TASK is None or _PROFILE_SCAN_TASK.done():
        _PROFILE_SCAN_TASK = asyncio.create_task(_profile_scan_worker())
    log.info("Bot is starting polling…")
    try:
        await _start_polling_with_retries()
    finally:
        if _PROFILE_SCAN_TASK:
            _PROFILE_SCAN_TASK.cancel()
            with contextlib.suppress(Exception):
                await _PROFILE_SCAN_TASK
        if _DL_WORKER_TASK:
            _DL_WORKER_TASK.cancel()
            with contextlib.suppress(Exception):
                await _DL_WORKER_TASK
        with contextlib.suppress(Exception):
            await bot.session.close()
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
