# Version: 18.4.3 — 2025-10-02
# Python: 3.11
# Telegram VSCO Toolkit Bot — v18.4.3
# Изменения (18.4.3):
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

import os
import re
import sqlite3
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
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

try:
    import pandas as pd  # type: ignore
except Exception:  # pandas is optional
    pd = None  # type: ignore

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
)
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.utils.text_decorations import add_surrogates, remove_surrogates
import aiohttp
import sys
import contextlib
import shlex

# ---- external utils (optional HTML export parser) ----
from vsco_parser3 import parse_html_file, dedupe_rows

from zip_profile import zip_router

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


def _parse_channel_id_value(raw: str, *, env_name: str) -> Optional[int | str]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        return text
    try:
        return int(text)
    except ValueError:
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

if pd is None:
    log.warning("pandas is not installed; CSV features are disabled")

# ---------------------- VSCO constants ----------------------
VSCO_HOSTS = {"vsco.co", "www.vsco.co"}
VSCO_SHORT_HOSTS = {"vs.co", "www.vs.co"}
VSCO_RESERVED = {
    "", "discover", "search", "images", "image", "media", "terms", "privacy",
    "about", "gallery", "videos", "press", "company", "legal", "pricing",
    "signin", "login", "signup", "api"
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TG_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
URL_RE = re.compile(r'(https?://[^\s<>"\'\]\)]+)', re.IGNORECASE)
MEDIA_PATH_RE = re.compile(r"^/([^/]+)/media/([A-Za-z0-9]+)")

OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
TW_IMAGE_RE = re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
RESP_URL_RE = re.compile(r'responsive_url"\s*:\s*"([^"]+)"')

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

DAILY_COORDS_LIMIT = 0
DAILY_NO_COORDS_LIMIT = 0


def _daily_item_counts(chat_id: int) -> Tuple[int, int]:
    since = _since_utc_iso(1)
    conn = db_connect()
    try:
        with_coords, without_coords = conn.execute(
            """
            SELECT
                SUM(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL THEN 1 ELSE 0 END) AS with_coords,
                SUM(CASE WHEN latitude IS NULL OR longitude IS NULL THEN 1 ELSE 0 END) AS without_coords
            FROM items
            WHERE chat_id = ? AND created_at >= ?
            """,
            (chat_id, since),
        ).fetchone()
    finally:
        conn.close()

    return int(with_coords or 0), int(without_coords or 0)


def has_daily_data_access(chat_id: int, user_id: Optional[int] = None) -> Tuple[bool, str]:
    """Check whether a chat accumulated enough fresh items for export/download."""
    with_coords, without_coords = _daily_item_counts(chat_id)

    counters = (
        f"с координатами — {with_coords}/{DAILY_COORDS_LIMIT}, "
        f"без координат — {without_coords}/{DAILY_NO_COORDS_LIMIT}"
    )

    if is_admin_id(user_id):
        text = (
            "👑 Администратор: лимиты отключены. Доступ к экспорту и скачиванию всегда открыт.\n"
            f"За последние 24 часа: {counters}."
        )
        return True, text

    allowed = with_coords >= DAILY_COORDS_LIMIT or without_coords >= DAILY_NO_COORDS_LIMIT

    if allowed:
        text = (
            "✅ Доступ к экспорту и скачиванию активен на текущие сутки.\n"
            f"За последние 24 часа: {counters}. Лимит обновляется ежедневно."
        )
    else:
        text = (
            "🚫 Нужно накопить за последние 24 часа минимум "
            f"{DAILY_COORDS_LIMIT} элементов с координатами или {DAILY_NO_COORDS_LIMIT} без координат.\n"
            f"Сейчас: {counters}. Лимит обнуляется каждый день."
        )

    return allowed, text

def is_vsco_url(u: str) -> bool:
    try:
        p = urlparse(u); host = (p.netloc or "").lower()
        return host in VSCO_HOSTS or host in VSCO_SHORT_HOSTS
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
    try:
        async with session.get(url, allow_redirects=True, timeout=10, headers=HEADERS) as resp:
            return str(resp.url)
    except Exception:
        return url

async def fetch_media_image_url(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """
    Пытается достать прямой URL картинки по HTML:
    - <meta property="og:image">, <meta name="twitter:image">,
    - responsive_url во встроенном JSON.
    """
    try:
        async with session.get(url, allow_redirects=True, timeout=12, headers=HEADERS) as resp:
            html = await resp.text(errors="ignore")
    except Exception as e:
        log.warning("fetch_media_image_url failed: %s", e)
        return None

    for rx in (OG_IMAGE_RE, TW_IMAGE_RE, RESP_URL_RE):
        m = rx.search(html)
        if m:
            return m.group(1)
    return None

# === Парсер "ссылка, комментарий до следующей ссылки" ========================
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
    expanded = _expand_text_with_entities(text, entities)
    return _parse_vsco_pairs(expanded)


def _parse_vsco_pairs(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not text:
        return out
    matches = [m for m in URL_RE.finditer(text) if is_vsco_url(m.group(0))]
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
    return out

def parse_vsco_pairs_from_text(text: str) -> List[Dict[str, str]]:
    return _parse_vsco_pairs(text or "")

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
    if not pairs: return []
    res: List[Dict[str, str]] = []
    async with aiohttp.ClientSession() as s:
        for pair in pairs:
            u = pair.get("url", ""); c = (pair.get("comment") or "").strip()
            if not is_vsco_url(u):
                continue

            p = urlparse(u); host = (p.netloc or "").lower()
            final = u
            if host in VSCO_SHORT_HOSTS:
                final = await resolve_vsco_short(u, s)

            info = classify_vsco_path(final)
            if not info:
                usr = username_from_vsco_co(final)
                if usr:
                    res.append({"username": usr, "url": f"https://vsco.co/{usr}", "comment": c, "image_url": ""})
                continue

            if info["kind"] == "profile":
                res.append({"username": info["username"], "url": info["final_url"], "comment": c, "image_url": ""})
            elif info["kind"] == "media":
                usr = info["username"]
                profile_url = f"https://vsco.co/{usr}"
                image_url = await fetch_media_image_url(final, s) or final  # fallback: страница медиа
                res.append({"username": usr, "url": profile_url, "comment": c, "image_url": image_url})
    uniq = {(r["username"], r["url"], r["comment"], r.get("image_url","")): r for r in res}
    return list(uniq.values())

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
        """INSERT INTO items(chat_id,username,latitude,longitude,profile_url,image_url,source,source_file,added_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
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
            utc_now_iso(),
        )
    )
    return cur.lastrowid

def upsert_items_with_comments(chat_id: int, pairs: List[Dict[str,str]], source: str, source_file: Optional[str], added_by: str) -> Tuple[int,int,List[str]]:
    if not pairs: return (0,0,[])
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

            item_id = _get_item_id(conn, username, profile_url, image_url)
            if item_id is None:
                item_id = _insert_item(conn, chat_id, username, profile_url, image_url, None, None, source, source_file, added_by)
                added_items += 1

            if comment:
                conn.execute(
                    "INSERT OR IGNORE INTO comments(item_id,chat_id,comment,created_at) VALUES(?,?,?,?)",
                    (item_id, chat_id, comment, utc_now_iso())
                )
                if conn.total_changes > 0:
                    added_comments += 1

            if add_vsco_link_legacy(username, profile_url, chat_id, conn):
                new_links.append(profile_url)

        conn.commit()
        return (added_items, added_comments, new_links)
    finally:
        conn.close()

def insert_full_rows_from_html(chat_id: int, rows: List[Dict[str,Any]], source_file: str, added_by: str) -> Tuple[int, List[str]]:
    if not rows:
        return (0, [])
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
                _insert_item(conn, chat_id, username, profile_url, image_url, lat, lon, "html", source_file, added_by)
                added += 1

            if add_vsco_link_legacy(username, profile_url, chat_id, conn):
                new_links.append(profile_url)
        conn.commit()
        return (added, new_links)
    finally:
        conn.close()

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
            "SELECT id,username,profile_url,latitude,longitude,image_url,added_by,source,source_file,created_at"
            " FROM items WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,username,profile_url,latitude,longitude,image_url,added_by,source,source_file,created_at FROM items"
        ).fetchall()
    if not rows:
        conn.close(); return []

    groups: Dict[str, Dict[str, Any]] = {}
    ids_by_user: Dict[str,List[int]] = {}
    for iid, uname, purl, lat, lon, img, added_by, source, source_file, created_at in rows:
        uname = uname or ""
        g = groups.setdefault(uname, {
            "username": uname, "profile_url": purl or (f"https://vsco.co/{uname}" if uname else ""),
            "lat_sum":0.0, "lon_sum":0.0, "lat_n":0, "lon_n":0,
            "images": set(), "added_by": "",
            "sources": {}, "cities": set(), "first_at": None, "last_at": None,
        })
        if purl and not g["profile_url"]:
            g["profile_url"] = purl
        if img: g["images"].add(img)
        if lat is not None and lon is not None:
            try:
                lat_f = float(lat); lon_f = float(lon)
                g["lat_sum"] += lat_f; g["lon_sum"] += lon_f
                g["lat_n"] += 1; g["lon_n"] += 1
                city_label = resolve_city_label(lat_f, lon_f)
                if city_label:
                    g["cities"].add(city_label)
            except Exception: pass
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
        q = ",".join("?" for _ in all_ids)
        for iid, c in conn.execute(f"SELECT item_id,comment FROM comments WHERE item_id IN ({q}) ORDER BY id ASC", all_ids):
            comments_map.setdefault(iid, []).append(c)

    out = []
    for uname, g in groups.items():
        lat = (g["lat_sum"]/g["lat_n"]) if g["lat_n"] else None
        lon = (g["lon_sum"]/g["lon_n"]) if g["lon_n"] else None
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
        city_list = sorted(g.get("cities", []))  # type: ignore
        out.append({
            "username": uname,
            "profile_url": g["profile_url"],
            "lat": lat, "lon": lon,
            "images": list(g["images"]),
            "comments": u_comments,
            "images_count": len(g["images"]),
            "comments_count": len(u_comments),
            "added_by": display,
            "added_by_link": link or "",
            "added_by_raw": raw_added,
            "datasets": datasets,
            "cities": city_list,
            "first_created": g.get("first_at"),
            "last_created": g.get("last_at"),
        })
    conn.close()
    return out

def fetch_items_for_map(scope: str, chat_id: int) -> List[Dict[str, Any]]:
    conn = db_connect(); conn.execute("PRAGMA read_uncommitted=1;")
    if scope == "chat":
        rows = conn.execute(
            "SELECT id,username,profile_url,image_url,latitude,longitude,added_by,source,source_file,created_at"
            " FROM items WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,username,profile_url,image_url,latitude,longitude,added_by,source,source_file,created_at FROM items"
        ).fetchall()
    if not rows:
        conn.close(); return []
    ids = [r[0] for r in rows]
    comments_map: Dict[int,List[str]] = {}
    q = ",".join("?" for _ in ids)
    for iid, c in conn.execute(f"SELECT item_id,comment FROM comments WHERE item_id IN ({q}) ORDER BY id ASC", ids):
        comments_map.setdefault(iid, []).append(c)
    out = []
    for iid, uname, purl, img, lat, lon, added_by, source, source_file, created_at in rows:
        try: lat = float(lat) if lat not in ("", None, "None") else None
        except Exception: lat = None
        try: lon = float(lon) if lon not in ("", None, "None") else None
        except Exception: lon = None
        display, link = added_by_display_and_link(added_by or "")
        city_label = resolve_city_label(lat, lon) if lat is not None and lon is not None else None
        dataset_map = {}
        for token_value, token_label in dataset_token_pairs(source, source_file):
            if token_value:
                dataset_map[token_value] = token_label
        out.append({
            "id": iid,
            "username": uname or "",
            "profile_url": purl or (f"https://vsco.co/{uname}" if uname else ""),
            "image_url": img or "",
            "lat": lat, "lon": lon,
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
            "created_at": created_at,
        })
    conn.close()
    return out

# ---------------------- HTML builders (gallery/maps) ----------------------
def _comments_html_preview(comments: List[str], limit: int = 3, max_len: int = 160) -> str:
    if not comments:
        return "<div class='cm-empty'>нет комментариев</div>"
    parts = []
    for c in comments[:limit]:
        txt = escape(c)
        if len(txt) > max_len: txt = txt[:max_len-1] + "…"
        parts.append(f"<li>{txt}</li>")
    more = f"<div class='cm-more'>и ещё {len(comments)-limit}…</div>" if len(comments) > limit else ""
    return "<ul class='cm-list'>" + "".join(parts) + "</ul>" + more

def _thumbs_html_preview(images: List[str], limit: int = 4) -> str:
    if not images: return ""
    thumbs = "".join([f"<img src='{escape(src)}' loading='lazy'/>" for src in images[:limit]])
    return f"<div class='pop-thumbs'>{thumbs}</div>"

def build_rich_gallery(users: List[Dict[str, Any]], title="VSCO Gallery", subtitle=""):
    data_json = json.dumps(users, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; background:#f5f6f8; color:#111; }}
    .wrap {{ max-width: 1400px; margin: 24px auto; padding: 0 16px; }}
    h1 {{ margin: 0 0 4px 0; }}
    .sub {{ color:#6b7280; margin-bottom: 16px; }}
    .toolbar {{ display:grid; grid-template-columns: 1.5fr 0.8fr 0.8fr 0.8fr 0.8fr 1.1fr auto auto; gap:10px; margin-bottom:14px; }}
    .toolbar input,.toolbar select,.toolbar button {{ padding:8px 10px; border:1px solid #e5e7eb; border-radius:8px; background:#fff; }}
    .stats {{ color:#6b7280; margin: 6px 0 10px 0; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fill,minmax(300px,1fr)); gap:14px; }}
    .card {{ background:#fff; border-radius:14px; padding:12px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
    .card .head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
    .card .head .name a {{ font-weight:700; text-decoration:none; color:#111; }}
    .btn {{ display:inline-block; padding:6px 10px; border-radius:10px; background:#10b981; color:#fff; text-decoration:none; font-weight:600; }}
    .meta {{ font-size:12px; color:#6b7280; margin:4px 0 8px 0; }}
    .meta.added {{ color:#4b5563; margin-top:2px; }}
    .thumbs {{ display:flex; gap:6px; overflow:hidden; }}
    .thumbs img {{ width:72px; height:120px; object-fit:cover; border-radius:8px; border:1px solid #eee; }}
    .cm {{ margin-top:10px; font-size:13px; }}
    .cm ul {{ margin: 0 0 4px 18px; padding:0; }}
    .cm .empty {{ color:#9ca3af; font-size:12px; }}
    .cm .more {{ color:#6b7280; font-size:12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🎯 {escape(title)}</h1>
    <div class="sub">{escape(subtitle)}</div>

    <div class="toolbar">
      <input id="q" placeholder="Filter by username" />
      <input id="latMin" placeholder="Lat min" type="number" step="0.0001"/>
      <input id="latMax" placeholder="Lat max" type="number" step="0.0001"/>
      <input id="lonMin" placeholder="Lon min" type="number" step="0.0001"/>
      <input id="lonMax" placeholder="Lon max" type="number" step="0.0001"/>
      <select id="sort">
        <option value="img_desc">More images first</option>
        <option value="img_asc">Fewer images first</option>
        <option value="cm_desc">More comments first</option>
        <option value="cm_asc">Fewer comments first</option>
        <option value="name_asc">Username A–Z</option>
        <option value="name_desc">Username Z–A</option>
      </select>
      <button id="apply">Apply</button>
      <button id="reset" type="button">Reset</button>
    </div>

    <div class="stats" id="stats"></div>
    <div class="grid" id="grid"></div>
  </div>

  <script>
    const DATA = {data_json};

    function sortData(arr, mode) {{
      switch(mode) {{
        case 'img_desc': return arr.sort((a,b)=> (b.images_count-a.images_count)||a.username.localeCompare(b.username));
        case 'img_asc':  return arr.sort((a,b)=> (a.images_count-b.images_count)||a.username.localeCompare(b.username));
        case 'cm_desc':  return arr.sort((a,b)=> (b.comments_count-a.comments_count)||a.username.localeCompare(b.username));
        case 'cm_asc':   return arr.sort((a,b)=> (a.comments_count-b.comments_count)||a.username.localeCompare(b.username));
        case 'name_desc':return arr.sort((a,b)=> b.username.localeCompare(a.username));
        default:         return arr.sort((a,b)=> a.username.localeCompare(b.username));
      }}
    }}

    function inBbox(u, latMin, latMax, lonMin, lonMax) {{
      if (latMin===''&&latMax===''&&lonMin===''&&lonMax==='') return true;
      const lat=u.lat, lon=u.lon;
      if (lat==null || lon==null) return false;
      if (latMin!=='' && lat<parseFloat(latMin)) return false;
      if (latMax!=='' && lat>parseFloat(latMax)) return false;
      if (lonMin!=='' && lon<parseFloat(lonMin)) return false;
      if (lonMax!=='' && lon>parseFloat(lonMax)) return false;
      return true;
    }}

    function escapeHtml(s) {{
      return (''+s).replace(/[&<>\"']/g, function(m) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[m]; }});
    }}

    function render(list) {{
      const grid=document.getElementById('grid'), stats=document.getElementById('stats');
      grid.innerHTML='';
      let entries=0; list.forEach(u=>{{ entries += (u.images?u.images.length:0); }});
      stats.textContent = `Users: ${{list.length}} • Entries: ${{entries}}`;

      list.forEach(u=>{{
        const images=(u.images||[]).slice(0,4);
        const cm=(u.comments||[]);
        let cmHtml='';
        if (cm.length===0) cmHtml = '<div class="empty">нет комментариев</div>';
        else {{
          const head = cm.slice(0,3).map(c=>`<li>${{escapeHtml(c)}}</li>`).join('');
          const more = cm.length>3 ? `<div class="more">и ещё ${{cm.length-3}}…</div>` : '';
          cmHtml = `<ul>${{head}}</ul>` + more;
        }}
        const thumbs = images.map(src=>`<img src="${{src}}" loading="lazy">`).join('');
        const latStr = (u.lat!=null && u.lon!=null) ? `${{u.lat.toFixed(6)}}, ${{u.lon.toFixed(6)}}` : '';
        const addedBy = (()=>{{
          if (!u.added_by) return '';
          const label = escapeHtml(u.added_by);
          if (u.added_by_link) {{
            return `<a href="${{escapeHtml(u.added_by_link)}}" target="_blank">${{label}}</a>`;
          }}
          return label;
        }})();
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="head">
            <div class="name"><a href="${{u.profile_url}}" target="_blank">@${{escapeHtml(u.username)}}</a></div>
            <a class="btn" href="${{u.profile_url}}" target="_blank">View Profile</a>
          </div>
          <div class="meta">${{latStr ? latStr + ' • ' : ''}}${{u.images_count}} item(s) • ${{u.comments_count}} comment(s)</div>
          <div class="meta added">Добавил: ${{addedBy || '—'}}</div>
          <div class="thumbs">${{thumbs}}</div>
          <div class="cm">${{cmHtml}}</div>
        `;
        grid.appendChild(card);
      }});
    }}

    function apply() {{
      const q=document.getElementById('q').value.trim().toLowerCase();
      const latMin=document.getElementById('latMin').value;
      const latMax=document.getElementById('latMax').value;
      const lonMin=document.getElementById('lonMin').value;
      const lonMax=document.getElementById('lonMax').value;
      const sort=document.getElementById('sort').value;

      let list = DATA.filter(u => (!q || u.username.toLowerCase().includes(q)) && inBbox(u,latMin,latMax,lonMin,lonMax));
      sortData(list, sort);
      render(list);
    }}

    function reset() {{
      document.getElementById('q').value='';
      ['latMin','latMax','lonMin','lonMax'].forEach(id=>document.getElementById(id).value='');
      document.getElementById('sort').value='img_desc';
      apply();
    }}

    document.getElementById('apply').addEventListener('click', apply);
    document.getElementById('reset').addEventListener('click', reset);
    reset();
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
  <title>{escape(title)}</title>
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
            <select id="filterCity">
              <option value="">Все города</option>
            </select>
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
    function setupFiltering() {{
      var rows=Array.prototype.slice.call(document.querySelectorAll('.panel .row'));
      var summary=document.getElementById('summary');
      var input=document.getElementById('filter');
      var datasetSelect=document.getElementById('filterData');
      var citySelect=document.getElementById('filterCity');
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
        }});
        if (row._addedKey && !addedOptions[row._addedKey]) {{
          addedOptions[row._addedKey]=row._addedLabel || row._addedKey;
        }}
      }});
      fillSelectOptions(datasetSelect, datasetOptions, 'Все данные');
      fillSelectOptions(citySelect, cityOptions, 'Все города');
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
        var cityValue=citySelect ? citySelect.value : '';
        var cityValueLower=cityValue ? cityValue.toLowerCase() : '';
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
          if (match && cityValue) {{
            match=row._cities && row._cities.some(function(city) {{
              return city.toLowerCase()===cityValueLower;
            }});
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
          if (!q && !dsValue && !cityValue && !commentValue && !addedKey && !hasCommentsValue) {{
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
      if (commentInput) commentInput.addEventListener('input', debouncedApply);
      if (datasetSelect) datasetSelect.addEventListener('change', applyFilter);
      if (citySelect) citySelect.addEventListener('change', applyFilter);
      if (addedSelect) addedSelect.addEventListener('change', applyFilter);
      if (hasCommentsSelect) hasCommentsSelect.addEventListener('change', applyFilter);
      if (resetBtn) resetBtn.addEventListener('click', function() {{
        if (input) input.value='';
        if (datasetSelect) datasetSelect.value='';
        if (citySelect) citySelect.value='';
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
      var filteringState = setupFiltering();
      ensureLeaflet(function() {{
        ensureCluster(function() {{
          var map=L.map('map');
          L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
          {chr(10).join(marker_js)}
        }});
      }});
    }});
  </script>
</body>
</html>"""
def build_map_users(users: List[Dict[str, Any]], title="VSCO Profiles (Users)"):
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

        search_parts = [str(u.get("username") or "")]
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
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup();",
            "var markerByKey={};",
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
            "setupListInteractions(map, markerByKey, markers, filteringState);",
            "bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("bindFilteringToMarkers(filteringState, map, null, {});")

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
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup();",
            "var markerByKey={};",
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
            "setupListInteractions(map, markerByKey, markers, filteringState);",
            "bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("bindFilteringToMarkers(filteringState, map, null, {});")

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

async def send_file(msg: Message, path: Path, caption: str = ""):
    await msg.answer_document(BufferedInputFile(path.read_bytes(), filename=path.name), caption=caption)


@dp.message(F.document)
async def on_document(msg: Message):
    ses = get_session(msg.chat.id)
    found = added_items = added_comments = 0
    new_links: List[str] = []
    added_by = resolve_added_by(msg.from_user)

    if msg.caption:
        pairs = await normalize_vsco_pairs(parse_vsco_pairs_from_message(msg.caption, msg.caption_entities))
        found += len(pairs)
        ai, ac, links = upsert_items_with_comments(
            msg.chat.id,
            pairs,
            source="text",
            source_file="caption",
            added_by=added_by,
        )
        added_items += ai; added_comments += ac; new_links.extend(links)

    p = ses.dir / (msg.document.file_name or "file.bin")
    await msg.bot.download(msg.document, destination=p)
    low = (p.name or "").lower()

    if low.endswith(".csv"):
        if pd is None:
            await msg.answer("Обработка CSV недоступна: не установлен pandas")
            return
        ses.uploaded_csv.append(p)
        try:
            df = pd.read_csv(p)
            pairs: List[Dict[str,str]] = []
            for c in [c for c in df.columns if isinstance(c, str)]:
                for v in df[c].astype(str).tolist():
                    pairs.extend(parse_vsco_pairs_from_cell(v))
            pairs = await normalize_vsco_pairs(pairs)
            found += len(pairs)
            ai, ac, links = upsert_items_with_comments(
                msg.chat.id,
                pairs,
                source="csv",
                source_file=p.name,
                added_by=added_by,
            )
            added_items += ai; added_comments += ac; new_links.extend(links)
            links_block = format_new_links_block(new_links)
            await msg.answer(
                f"CSV загружен: <code>{escape(p.name)}</code>\n"
                f"Найдено VSCO-ссылок: {found}, добавлено ссылок/медиа: {added_items}, добавлено комментариев: {added_comments}"
                f"{links_block}"
            )
        except Exception as e:
            log.exception("CSV processing failed")
            await msg.answer(f"CSV загружен: <code>{escape(p.name)}</code>, но не удалось обработать: {escape(str(e))}")
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
            await msg.answer(
                f"HTML загружен: <code>{escape(p.name)}</code>\n"
                f"Сохранено элементов: {added_full}"
                f"{extra}"
                f"{links_block}"
            )
        except Exception as e:
            log.exception("HTML processing failed")
            await msg.answer(f"HTML загружен: <code>{escape(p.name)}</code>, но не удалось обработать: {escape(str(e))}")
        return

    links_block = format_new_links_block(new_links) if msg.caption else ""
    await msg.answer(
        "Файл сохранён. Нужны .html/.csv. Ссылки из подписи учтены, если были."
        f"{links_block}"
    )

# ---------- plain text ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message):
    ses = get_session(msg.chat.id)
    if ses.pending_action == "download":
        text = (msg.text or "").strip()
        if not text:
            await msg.answer("Отправьте username или ссылку профиля VSCO для скачивания.")
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
        if not success:
            await msg.answer("Если нужно попробовать снова, нажмите кнопку «Скачать профиль» ещё раз.")
        return

    pairs = await normalize_vsco_pairs(parse_vsco_pairs_from_message(msg.text, msg.entities))
    if not pairs:
        return  # без ответа
    added_by = resolve_added_by(msg.from_user)
    ai, ac, links = upsert_items_with_comments(
        msg.chat.id,
        pairs,
        source="text",
        source_file="message",
        added_by=added_by,
    )
    links_block = format_new_links_block(links)
    await msg.answer(
        f"Найдено VSCO-ссылок: {len(pairs)}, добавлено записей: {ai}, комментариев: {ac}{links_block}"
    )

# ---------- export ----------
def export_scope_keyboard(ses: Session) -> InlineKeyboardMarkup:
    scope = [
        InlineKeyboardButton(text=("✅ 📌 Текущий чат" if ses.export_scope=="chat" else "📌 Текущий чат"), callback_data="export:scope:chat"),
        InlineKeyboardButton(text=("✅ 🌐 Вся база" if ses.export_scope=="all" else "🌐 Вся база"), callback_data="export:scope:all"),
    ]
    types = [
        InlineKeyboardButton(text="📄 CSV", callback_data="export:format:csv"),
        InlineKeyboardButton(text="🖼️ Галерея", callback_data="export:format:gallery"),
    ]
    maps = [
        InlineKeyboardButton(text="🗺️ Карта (польз.)", callback_data="export:format:map_users"),
        InlineKeyboardButton(text="🗺️ Карта (фото)", callback_data="export:format:map_images"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[scope, types, maps])


def kb_struct(kb: InlineKeyboardMarkup | None):
    """Normalize keyboard for safe equality check."""
    if kb is None:
        return None
    return tuple(
        tuple(
            (btn.text, getattr(btn, "callback_data", None), getattr(btn, "url", None))
            for btn in row
        )
        for row in kb.inline_keyboard
    )

@dp.message(Command("export"))
async def cmd_export(msg: Message):
    if msg.chat.type in ("group", "supergroup"):
        await msg.answer("🚫 Экспорт доступен только в личных сообщениях. Напишите мне в ЛС.")
        return
    allowed, info = has_daily_data_access(msg.chat.id, getattr(msg.from_user, "id", None))
    if not allowed:
        await msg.answer(info)
        return
    ses = get_session(msg.chat.id)
    await msg.answer(
        "Экспорт VSCO:\n• CSV / Галерея\n• Карта: по пользователям или по фото",
        reply_markup=export_scope_keyboard(ses)
    )

@dp.callback_query(F.data.startswith("export:"))
async def on_export_click(cq: CallbackQuery):
    chat_id = cq.message.chat.id
    if cq.message.chat.type in ("group", "supergroup"):
        await cq.answer("Экспорт доступен только в личных сообщениях. Напишите мне в ЛС.", show_alert=True)
        return
    allowed, info = has_daily_data_access(chat_id, getattr(cq.from_user, "id", None))
    if not allowed:
        await cq.answer(info, show_alert=True)
        try:
            await cq.message.answer(info)
        except Exception:
            pass
        return
    ses = get_session(chat_id)
    parts = cq.data.split(":")
    if len(parts)>=3 and parts[1]=="scope":
        scope = parts[2]
        if scope in ("chat","all"):
            ses.export_scope = scope
            new_kb = export_scope_keyboard(ses)
            # safe edit: only if changed
            if kb_struct(cq.message.reply_markup) != kb_struct(new_kb):
                try:
                    await cq.message.edit_reply_markup(reply_markup=new_kb)
                except TelegramBadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        raise
            await cq.answer("Область обновлена")
        else:
            await cq.answer("Неизвестная область", show_alert=True)
        return

    if len(parts)>=3 and parts[1]=="format":
        fmt = parts[2]
        if fmt == "csv":
            if pd is None:
                await cq.answer("Экспорт CSV недоступен: не установлен pandas", show_alert=True)
                return
            users = fetch_gallery_users(ses.export_scope, chat_id)
            if not users:
                await cq.answer("Нет данных", show_alert=True)
                return
            await cq.answer("Готовлю экспорт…", cache_time=0)
            flat = [{
                "username": u["username"], "profile_url": u["profile_url"],
                "lat": u["lat"], "lon": u["lon"],
                "images_count": u["images_count"], "comments_count": u["comments_count"],
                "comments": " | ".join(u["comments"]),
                "added_by": u.get("added_by_raw", ""),
                "added_by_display": u.get("added_by", ""),
                "added_by_link": u.get("added_by_link", ""),
            } for u in users]
            out = ses.dir / f"export_{ses.export_scope}.csv"
            pd.DataFrame(flat).to_csv(out, index=False, encoding="utf-8")
            await cq.message.answer_document(BufferedInputFile(out.read_bytes(), filename=out.name),
                caption=f"CSV ({'вся база' if ses.export_scope=='all' else 'текущий чат'})")
            return

        if fmt == "gallery":
            users = fetch_gallery_users(ses.export_scope, chat_id)
            if not users:
                await cq.answer("Нет данных", show_alert=True)
                return
            await cq.answer("Готовлю экспорт…", cache_time=0)
            html = build_rich_gallery(users, title="VSCO Gallery",
                subtitle=("All DB" if ses.export_scope=='all' else "Current Chat"))
            out = ses.dir / f"export_gallery_{ses.export_scope}.html"
            out.write_text(html, encoding="utf-8")
            await cq.message.answer_document(BufferedInputFile(out.read_bytes(), filename=out.name),
                caption=f"Галерея ({'вся база' if ses.export_scope=='all' else 'текущий чат'})")
            return

        if fmt in ("map_users","map","map_images"):
            if fmt in ("map","map_users"):
                users = fetch_gallery_users(ses.export_scope, chat_id)
                if not users:
                    await cq.answer("Нет данных", show_alert=True)
                    return
                await cq.answer("Готовлю экспорт…", cache_time=0)
                html = build_map_users(users, title=f"VSCO Profiles — {'Users' if fmt!='map_images' else 'Images'}")
                out = ses.dir / f"export_map_users_{ses.export_scope}.html"
            else:
                items = fetch_items_for_map(ses.export_scope, chat_id)
                if not items:
                    await cq.answer("Нет данных", show_alert=True)
                    return
                await cq.answer("Готовлю экспорт…", cache_time=0)
                html = build_map_images(items, title="VSCO Profiles — Images")
                out = ses.dir / f"export_map_images_{ses.export_scope}.html"

            out.write_text(html, encoding="utf-8")
            await cq.message.answer_document(BufferedInputFile(out.read_bytes(), filename=out.name),
                caption=f"Карта ({'вся база' if ses.export_scope=='all' else 'текущий чат'})")
            return

        await cq.answer("Неизвестный формат", show_alert=True); return

    await cq.answer("Неизвестное действие", show_alert=True)

# ---------- stats ----------
def stats_scope_keyboard(ses: Session) -> InlineKeyboardMarkup:
    scope = [
        InlineKeyboardButton(text=("✅ 📌 Текущий чат" if ses.export_scope=="chat" else "📌 Текущий чат"), callback_data="stats:scope:chat"),
        InlineKeyboardButton(text=("✅ 🌐 Вся база" if ses.export_scope=="all" else "🌐 Вся база"), callback_data="stats:scope:all"),
    ]
    actions = [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats:refresh")]
    return InlineKeyboardMarkup(inline_keyboard=[scope, actions])

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    ses = get_session(msg.chat.id)
    s = get_stats(msg.chat.id, ses.export_scope)
    txt = format_stats_text(s, ses.export_scope)
    await msg.answer(txt, reply_markup=stats_scope_keyboard(ses))

@dp.callback_query(F.data.startswith("stats:"))
async def on_stats_click(cq: CallbackQuery):
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
async def cmd_links(msg: Message):
    ses = get_session(msg.chat.id)
    page = 1
    txt, total = _render_links_text(msg.chat.id, ses.export_scope, page)
    await msg.answer(txt, reply_markup=_links_scope_keyboard(ses, page, total))

@dp.callback_query(F.data.startswith("links:"))
async def on_links_click(cq: CallbackQuery):
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
        if pd is None:
            await cq.answer("Экспорт CSV недоступен: не установлен pandas", show_alert=True)
            return
        since = _since_utc_iso(1)
        total, rows = _links_since_query(chat_id, ses.export_scope, since, limit=10_000, offset=0)
        if not rows:
            await cq.answer("За день нет ссылок", show_alert=True)
            return
        df = pd.DataFrame([
            {
                "username": r[0],
                "url": r[1],
                "created_at": r[2],
                "chat_id": r[3],
                "added_by": r[4],
            }
            for r in rows
        ])
        out = get_session(chat_id).dir / f"links_day_{ses.export_scope}.csv"
        df.to_csv(out, index=False, encoding="utf-8")
        await cq.message.answer_document(BufferedInputFile(out.read_bytes(), filename=out.name),
                                         caption=f"Ссылки за день — {('вся база' if ses.export_scope=='all' else 'текущий чат')}: {total} шт.")
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
async def cmd_reset(msg: Message):
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

def _dl_script_path() -> Path:
    # vsco_downloader.py должен лежать рядом с текущим файлом
    return Path(__file__).with_name("vsco_downloader.py")


@dp.message(Command("dl"))
async def cmd_dl_enqueue(msg: Message):
    """
    /dl <vsco_username | profile_url> [--flags ...]
    Кладёт задание в очередь. Выполняет воркер по одному.
    """
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
async def cmd_qstat(msg: Message):
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
async def cmd_admin(msg: Message):
    if not require_admin(msg):
        await msg.answer("🚫 Команда доступна только администраторам.")
        return
    await msg.answer(
        "🛠️ <b>Панель администратора</b>\nВыберите действие:",
        reply_markup=_admin_keyboard(),
    )


@dp.callback_query(F.data.startswith("admin:"))
async def on_admin_click(cq: CallbackQuery):
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
        url = item.get("image_url") or item.get("responsive_url")
        if isinstance(url, str) and url not in seen:
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
    info = classify_vsco_path(text)
    username = info.get("username") if isinstance(info, dict) else None
    if isinstance(username, str) and username.strip():
        return username.strip()
    if is_vsco_url(text):
        extracted = username_from_vsco_co(text)
        if extracted:
            return extracted
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
        if stage is Stage.DOWNLOAD and total_found is not None:
            txt += f"\n📥 Загрузка: <b>{downloaded}/{total_found}</b>"
        if stage is Stage.ARCHIVE and zip_parts is not None:
            txt += f"\n🗜️ Архив: будет {zip_parts} томов"
        return txt

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
                last_manifest_mtime = 0.0
                last_urls_mtime = 0.0
                loop = asyncio.get_running_loop()
                while not stop_evt.is_set():
                    try:
                        if user_dir is None:
                            cand = [p for p in job.out_base.glob("*") if p.is_dir()]
                            if cand:
                                user_dir = max(cand, key=lambda p: p.stat().st_mtime)
                                log.debug("Job #%s: working directory %s", job.id, user_dir)
                        if user_dir and total_found is None:
                            man = user_dir / "manifest.json"
                            if man.exists():
                                mtime = man.stat().st_mtime
                                if mtime > last_manifest_mtime:
                                    last_manifest_mtime = mtime
                                    try:
                                        data = json.loads(man.read_text(encoding="utf-8"))
                                        if isinstance(data, dict):
                                            for k in ("count","total","items_count","media_count"):
                                                v = data.get(k)
                                                if isinstance(v, int):
                                                    total_found = v; break
                                            if total_found is None and isinstance(data.get("items"), list):
                                                total_found = len(data["items"])
                                        elif isinstance(data, list):
                                            total_found = len(data)
                                    except Exception:
                                        pass
                            if total_found is not None:
                                log.info("Job #%s: media count determined: %d", job.id, total_found)
                            if total_found is None:
                                urls = user_dir / "urls_extracted.txt"
                                if urls.exists():
                                    mtime = urls.stat().st_mtime
                                    if mtime > last_urls_mtime:
                                        last_urls_mtime = mtime
                                        try:
                                            n = sum(1 for ln in urls.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip())
                                            if n > 0:
                                                total_found = n
                                        except Exception:
                                            pass
                            if total_found is not None and stage is Stage.SCAN:
                                log.info("Job #%s: media found so far %d", job.id, total_found)
                        txt = build_progress_text(stage, job.target, total_found, downloaded, zip_parts)
                        now = loop.time()
                        if now - last_edit >= 2.0:
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

                    if txt.startswith("scan_progress"):
                        parts = txt.split()
                        if len(parts) >= 2:
                            try:
                                total_found = int(parts[1])
                                log.info("Job #%s: scan progress %d", job.id, total_found)
                                if stage is Stage.SCAN:
                                    await _safe_edit(
                                        job.chat_id,
                                        progress.message_id,
                                        build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                                        reply_markup=cancel_kb,
                                    )
                            except Exception:
                                pass
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
                        continue
                    elif any(k in low for k in ("zip", "архив")) and stage is not Stage.ARCHIVE:
                        stage = Stage.ARCHIVE
                        log.info("Job #%s: stage -> %s", job.id, stage.value)
                        await _safe_edit(
                            job.chat_id,
                            progress.message_id,
                            build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                            reply_markup=cancel_kb,
                        )
                        continue

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

                    if total_found is None:
                        m = re.search(r"(?:found|найден[оа])\D+(\d+)\D+(?:media|items|files|медиа|ссыл)", low)
                        if m:
                            try:
                                total_found = int(m.group(1))
                                log.info("Job #%s: media count from stdout %d", job.id, total_found)
                                await _safe_edit(
                                    job.chat_id,
                                    progress.message_id,
                                    build_progress_text(stage, job.target, total_found, downloaded, zip_parts),
                                    reply_markup=cancel_kb,
                                )
                            except Exception:
                                pass
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

            if user_dir is not None:
                rebuild_urls_extracted(user_dir)

            if user_dir is None:
                log.warning("Job #%s: completed but no results found", job.id)
                await _safe_edit(job.chat_id, progress.message_id, f"⚠️ Завершено, но результирующих файлов не найдено.")
            else:
                zips = sorted(
                    p for p in user_dir.glob("*.zip") if p.stat().st_mtime >= job_started
                )
                if zips:
                    log.info("Job #%s: %d zip(s) ready", job.id, len(zips))
                    await _safe_edit(job.chat_id, progress.message_id, "📦 Архив(ы) готовы — отправляю…")
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
async def cmd_tutorial(msg: Message):
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


@dp.message(Command("start", "help"))
async def cmd_help(msg: Message):
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
        f"• <b>/export</b> — экспорт CSV/галереи или карты (после {DAILY_COORDS_LIMIT} элементов с координатами или {DAILY_NO_COORDS_LIMIT} без координат за сутки)\n"
        "• <b>/stats</b> — статистика по скачанным данным\n"
        "• <b>/reset</b> — очистить текущую сессию\n"
        "• <b>/tutorial</b> — пошаговый гайд по использованию\n"
        "• <b>/help</b> — эта справка\n\n"
        f"ℹ️ Экспорт и скачивание доступны, если за последние 24 часа собрано {DAILY_COORDS_LIMIT} элементов с координатами "
        f"или {DAILY_NO_COORDS_LIMIT} без координат. Лимит обнуляется ежедневно.\n\n"
        "💡 <b>Примеры</b>:\n"
        "• <code>/dl johndoe</code>\n"
        "• <code>/dl https://vsco.co/johndoe </code>\n\n"
        "👇 Быстрые действия доступны на кнопках ниже."
    )
    await msg.answer(text, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data.startswith("menu:"))
async def on_menu_click(cq: CallbackQuery):
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
        await cmd_links(cq.message)
        await cq.answer("Готово")
        return

    if action == "stats":
        await cmd_stats(cq.message)
        await cq.answer("Готово")
        return

    if action == "export":
        if cq.message and cq.message.chat.type in ("group", "supergroup"):
            await cq.answer("Экспорт доступен только в личных сообщениях. Напишите мне в ЛС.", show_alert=True)
            return
        await cmd_export(cq.message)
        await cq.answer("Открываю экспорт")
        return

    if action == "qstat":
        await cmd_qstat(cq.message)
        await cq.answer("Готово")
        return

    if action == "tutorial":
        await cmd_tutorial(cq.message)
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
    log.info("Bot is starting polling…")
    try:
        await _start_polling_with_retries()
    finally:
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
