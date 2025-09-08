# Version: 18.4.2 — 2025-09-04
# Python: 3.11
# Telegram VSCO Toolkit Bot — v18.4.2
# Изменения (18.4.2):
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
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import json
import time
from html import escape

try:
    import pandas as pd  # type: ignore
except Exception:  # pandas is optional
    pd = None  # type: ignore

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
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
WORKDIR = Path(os.getenv("BOT_WORKDIR", "./work")); WORKDIR.mkdir(parents=True, exist_ok=True)
LOGDIR = Path(os.getenv("BOT_LOGDIR", "./logs")); LOGDIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv("BOT_DB_PATH", "vsco_links.db")
SEND_TIMEOUT = int(os.getenv("BOT_SEND_TIMEOUT", "600"))

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
    conn.commit(); conn.close()
    log.info("DB initialized at %s", DB_PATH)

# ---------------------- helpers ----------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=timezone.utc).isoformat()

def _since_utc_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=timezone.utc).isoformat()

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
        if j < len(text) and text[j] == ',':
            k = j + 1
            while k < len(text) and text[k].isspace():
                k += 1
            next_start = matches[i+1].start() if i + 1 < len(matches) else len(text)
            comment = text[k:next_start].strip()
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
def add_vsco_link_legacy(username: str, url: str, chat_id: int, conn: sqlite3.Connection):
    conn.execute(
        "INSERT OR IGNORE INTO links(chat_id,username,url,created_at) VALUES(?,?,?,?)",
        (chat_id, username, url, utc_now_iso())
    )

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
                 source: str, source_file: Optional[str]) -> int:
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO items(chat_id,username,latitude,longitude,profile_url,image_url,source,source_file,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (chat_id, username, latitude, longitude, profile_url, image_url, source or "", source_file or "",
         utc_now_iso())
    )
    return cur.lastrowid

def upsert_items_with_comments(chat_id: int, pairs: List[Dict[str,str]], source: str, source_file: Optional[str]) -> Tuple[int,int]:
    if not pairs: return (0,0)
    conn = db_connect()
    added_items = added_comments = 0
    try:
        for r in pairs:
            username = (r.get("username") or "").lstrip("@")
            profile_url = r.get("url") or ""
            image_url = (r.get("image_url") or "").strip()
            comment = (r.get("comment") or "").strip()
            if not username or not profile_url: continue

            item_id = _get_item_id(conn, username, profile_url, image_url)
            if item_id is None:
                item_id = _insert_item(conn, chat_id, username, profile_url, image_url, None, None, source, source_file)
                added_items += 1

            if comment:
                conn.execute(
                    "INSERT OR IGNORE INTO comments(item_id,chat_id,comment,created_at) VALUES(?,?,?,?)",
                    (item_id, chat_id, comment, utc_now_iso())
                )
                if conn.total_changes > 0:
                    added_comments += 1

            add_vsco_link_legacy(username, profile_url, chat_id, conn)

        conn.commit()
        return (added_items, added_comments)
    finally:
        conn.close()

def insert_full_rows_from_html(chat_id: int, rows: List[Dict[str,Any]], source_file: str) -> int:
    if not rows: return 0
    conn = db_connect(); added = 0
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
            if not username or not profile_url: continue

            if _get_item_id(conn, username, profile_url, image_url) is None:
                _insert_item(conn, chat_id, username, profile_url, image_url, lat, lon, "html", source_file)
                added += 1
                add_vsco_link_legacy(username, profile_url, chat_id, conn)
        conn.commit()
        return added
    finally:
        conn.close()

# ---------------------- Stats ----------------------
def _count_new_usernames(conn: sqlite3.Connection, since_iso: str, chat_id: int, scope: str) -> int:
    if scope == "chat":
        row = conn.execute(
            "SELECT COUNT(DISTINCT username) FROM links WHERE created_at >= ? AND chat_id = ?",
            (since_iso, chat_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(DISTINCT username) FROM links WHERE created_at >= ?",
            (since_iso,)
        ).fetchone()
    return int(row[0] or 0)

def _count_media(conn: sqlite3.Connection, since_iso: str, chat_id: int, scope: str) -> Tuple[int, int]:
    if scope == "chat":
        with_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE created_at >= ? AND chat_id = ?
                 AND image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND latitude IS NOT NULL AND longitude IS NOT NULL""",
            (since_iso, chat_id)
        ).fetchone()[0]
        without_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE created_at >= ? AND chat_id = ?
                 AND image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND (latitude IS NULL OR longitude IS NULL)""",
            (since_iso, chat_id)
        ).fetchone()[0]
    else:
        with_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE created_at >= ?
                 AND image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND latitude IS NOT NULL AND longitude IS NOT NULL""",
            (since_iso,)
        ).fetchone()[0]
        without_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE created_at >= ?
                 AND image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND (latitude IS NULL OR longitude IS NULL)""",
            (since_iso,)
        ).fetchone()[0]
    return int(with_c or 0), int(without_c or 0)

def _count_totals(conn: sqlite3.Connection, chat_id: int, scope: str) -> Tuple[int,int,int]:
    if scope == "chat":
        u = conn.execute("SELECT COUNT(DISTINCT username) FROM links WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        with_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE chat_id = ?
                 AND image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND latitude IS NOT NULL AND longitude IS NOT NULL""",
            (chat_id,)
        ).fetchone()[0]
        without_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE chat_id = ?
                 AND image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND (latitude IS NULL OR longitude IS NULL)""",
            (chat_id,)
        ).fetchone()[0]
    else:
        u = conn.execute("SELECT COUNT(DISTINCT username) FROM links").fetchone()[0]
        with_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND latitude IS NOT NULL AND longitude IS NOT NULL"""
        ).fetchone()[0]
        without_c = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE image_url IS NOT NULL AND TRIM(image_url) <> ''
                 AND (latitude IS NULL OR longitude IS NULL)"""
        ).fetchone()[0]
    return int(u or 0), int(with_c or 0), int(without_c or 0)

def get_stats(chat_id: int, scope: str) -> Dict[str, Dict[str, int]]:
    days_map = {'day': 1, 'week': 7, 'month': 30}
    conn = db_connect()
    try:
        out: Dict[str, Dict[str, int]] = {}
        for key, days in days_map.items():
            since = _since_utc_iso(days)
            unames = _count_new_usernames(conn, since, chat_id, scope)
            with_c, without_c = _count_media(conn, since, chat_id, scope)
            out[key] = {
                'usernames': unames,
                'media_with_coords': with_c,
                'media_without_coords': without_c,
            }
        tu, twc, two = _count_totals(conn, chat_id, scope)
        out['total'] = {
            'usernames': tu,
            'media_with_coords': twc,
            'media_without_coords': two,
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
            f"— Уникальные username: <b>{s['usernames']}</b>\n"
            f"— Media с координатами: <b>{s['media_with_coords']}</b>\n"
            f"— Media без координат: <b>{s['media_without_coords']}</b>\n"
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
        rows = conn.execute("SELECT id,username,profile_url,latitude,longitude,image_url FROM items WHERE chat_id=?", (chat_id,)).fetchall()
    else:
        rows = conn.execute("SELECT id,username,profile_url,latitude,longitude,image_url FROM items").fetchall()
    if not rows:
        conn.close(); return []

    groups: Dict[str, Dict[str, Any]] = {}
    ids_by_user: Dict[str,List[int]] = {}
    for iid, uname, purl, lat, lon, img in rows:
        uname = uname or ""
        g = groups.setdefault(uname, {
            "username": uname, "profile_url": purl or (f"https://vsco.co/{uname}" if uname else ""),
            "lat_sum":0.0, "lon_sum":0.0, "lat_n":0, "lon_n":0,
            "images": set()
        })
        if purl and not g["profile_url"]:
            g["profile_url"] = purl
        if img: g["images"].add(img)
        if lat is not None and lon is not None:
            try:
                g["lat_sum"] += float(lat); g["lon_sum"] += float(lon)
                g["lat_n"] += 1; g["lon_n"] += 1
            except Exception: pass
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
        out.append({
            "username": uname,
            "profile_url": g["profile_url"],
            "lat": lat, "lon": lon,
            "images": list(g["images"]),
            "comments": u_comments,
            "images_count": len(g["images"]),
            "comments_count": len(u_comments),
        })
    conn.close()
    return out

def fetch_items_for_map(scope: str, chat_id: int) -> List[Dict[str, Any]]:
    conn = db_connect(); conn.execute("PRAGMA read_uncommitted=1;")
    if scope == "chat":
        rows = conn.execute("SELECT id,username,profile_url,image_url,latitude,longitude FROM items WHERE chat_id=?", (chat_id,)).fetchall()
    else:
        rows = conn.execute("SELECT id,username,profile_url,image_url,latitude,longitude FROM items").fetchall()
    if not rows:
        conn.close(); return []
    ids = [r[0] for r in rows]
    comments_map: Dict[int,List[str]] = {}
    q = ",".join("?" for _ in ids)
    for iid, c in conn.execute(f"SELECT item_id,comment FROM comments WHERE item_id IN ({q}) ORDER BY id ASC", ids):
        comments_map.setdefault(iid, []).append(c)
    out = []
    for iid, uname, purl, img, lat, lon in rows:
        try: lat = float(lat) if lat not in ("", None, "None") else None
        except Exception: lat = None
        try: lon = float(lon) if lon not in ("", None, "None") else None
        except Exception: lon = None
        out.append({
            "id": iid,
            "username": uname or "",
            "profile_url": purl or (f"https://vsco.co/{uname}" if uname else ""),
            "image_url": img or "",
            "lat": lat, "lon": lon,
            "comments": comments_map.get(iid, [])
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
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="head">
            <div class="name"><a href="${{u.profile_url}}" target="_blank">@${{escapeHtml(u.username)}}</a></div>
            <a class="btn" href="${{u.profile_url}}" target="_blank">View Profile</a>
          </div>
          <div class="meta">${{latStr}} • ${{u.images_count}} item(s) • ${{u.comments_count}} comment(s)</div>
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

def _map_html(title: str, list_html: str, marker_js: List[str]) -> str:
    # Надёжная загрузка Leaflet + MarkerCluster с fallback и инициализацией после DOMContentLoaded
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
  <style>
    html, body {{ height:100%; margin:0; }}
    .layout {{ display:flex; height:100vh; }}
    #map {{ flex: 1 1 auto; min-height: 320px; }}
    .panel {{ width: 420px; max-width: 48vw; border-left:1px solid #e5e7eb; background:#fafafa; overflow:auto; }}
    .panel .head {{ position: sticky; top:0; background:#fff; padding:12px 14px; border-bottom:1px solid #e5e7eb; font-weight:600; }}
    .panel .row {{ padding:10px 14px; border-bottom:1px dashed #e5e7eb; display:grid; grid-template-columns:auto 80px 1fr; gap:8px; align-items:center; }}
    .panel .row .u a {{ font-weight:600; color:#111; text-decoration:none; }}
    .panel .row .c {{ font-size: 13px; color:#111; }}
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
      <div class="head">Список / превью</div>
      {list_html}
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
    document.addEventListener('DOMContentLoaded', function() {{
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
    has_coords = any((u.get("lat") is not None and u.get("lon") is not None) for u in users)
    list_rows = []
    for u in users:
        uname = escape(u.get("username","")); link = escape(u.get("profile_url",""))
        cm = u.get("comments") or []
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:3])
            more = f"<div class='c'>и ещё {len(cm)-3}…</div>" if len(cm) > 3 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"
        list_rows.append(f"""
          <div class="row">
            <div class="u"><a href="{link}" target="_blank">@{uname}</a></div>
            {cm_txt}
          </div>""")
    list_html = "".join(list_rows)

    marker_js = []
    if has_coords:
        marker_js += ["var bounds=L.latLngBounds();","var markers=L.markerClusterGroup();"]
        for u in users:
            lat=u.get("lat"); lon=u.get("lon")
            if lat is None or lon is None: continue
            uname=escape(str(u.get("username") or "")); prof=escape(str(u.get("profile_url") or ""))
            popup=f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a></div>"
            marker_js.append(f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); markers.addLayer(m); bounds.extend([{lat},{lon}]);")
        marker_js += ["map.addLayer(markers);","if(bounds.isValid()){{map.fitBounds(bounds.pad(0.1));}}else{{map.setView([20,0],2);}}"]
    else:
        marker_js.append("map.setView([20,0],2);")

    return _map_html(title, list_html, marker_js)

def build_map_images(items: List[Dict[str, Any]], title="VSCO Profiles (Images)"):
    has_coords = any((r.get("lat") is not None and r.get("lon") is not None) for r in items)
    rows=[]
    for r in items:
        uname=escape(r.get("username","")); link=escape(r.get("profile_url",""))
        img=r.get("image_url") or ""
        img_html = f"<img src='{escape(img)}' loading='lazy' style='width:68px;height:68px;object-fit:cover;border-radius:8px;border:1px solid #eee;'/>" if img else ""
        cm = r.get("comments") or []
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:2])
            more = f"<div class='c'>и ещё {len(cm)-2}…</div>" if len(cm) > 2 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"
        rows.append(f"""
          <div class="row">
            <div class="u"><a href="{link}" target="_blank">@{uname}</a></div>
            <div class="t">{img_html}</div>
            {cm_txt}
          </div>""")
    list_html="".join(rows)

    marker_js=[]
    if has_coords:
        marker_js += ["var bounds=L.latLngBounds();","var markers=L.markerClusterGroup();"]
        for r in items:
            lat=r.get("lat"); lon=r.get("lon")
            if lat is None or lon is None: continue
            uname=escape(str(r.get("username") or "")); prof=escape(str(r.get("profile_url") or ""))
            img=r.get("image_url") or ""
            img_html = f"<img src='{escape(img)}' loading='lazy' style='width:140px;height:140px;object-fit:cover;border-radius:10px;border:1px solid #eee;'/>" if img else ""
            popup=f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a><br/>{img_html}</div>"
            marker_js.append(f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); markers.addLayer(m); bounds.extend([{lat},{lon}]);")
        marker_js += ["map.addLayer(markers);","if(bounds.isValid()){{map.fitBounds(bounds.pad(0.1));}}else{{map.setView([20,0],2);}}"]
    else:
        marker_js.append("map.setView([20,0],2);")

    return _map_html(title, list_html, marker_js)

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

    if msg.caption:
        pairs = await normalize_vsco_pairs(parse_vsco_pairs_from_text(msg.caption))
        found += len(pairs)
        ai, ac = upsert_items_with_comments(msg.chat.id, pairs, source="text", source_file="caption")
        added_items += ai; added_comments += ac

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
            ai, ac = upsert_items_with_comments(msg.chat.id, pairs, source="csv", source_file=p.name)
            added_items += ai; added_comments += ac
            await msg.answer(
                f"CSV загружен: <code>{escape(p.name)}</code>\n"
                f"Найдено VSCO-ссылок: {found}, добавлено ссылок/медиа: {added_items}, добавлено комментариев: {added_comments}"
            )
        except Exception as e:
            log.exception("CSV processing failed")
            await msg.answer(f"CSV загружен: <code>{escape(p.name)}</code>, но не удалось обработать: {escape(str(e))}")
        return

    if low.endswith(".html") or low.endswith(".htm"):
        ses.uploaded_html.append(p)
        try:
            rows = dedupe_rows(parse_html_file(p), mode="safe")
            added_full = insert_full_rows_from_html(msg.chat.id, rows, source_file=p.name)
            extra = f"\n+ из подписи: добавлено {added_items} записей, комментариев {added_comments}" if msg.caption else ""
            await msg.answer(f"HTML загружен: <code>{escape(p.name)}</code>\nСохранено элементов: {added_full}{extra}")
        except Exception as e:
            log.exception("HTML processing failed")
            await msg.answer(f"HTML загружен: <code>{escape(p.name)}</code>, но не удалось обработать: {escape(str(e))}")
        return

    await msg.answer("Файл сохранён. Нужны .html/.csv. Ссылки из подписи учтены, если были.")

# ---------- plain text ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message):
    pairs = await normalize_vsco_pairs(parse_vsco_pairs_from_text(msg.text))
    if not pairs:
        return  # без ответа
    ai, ac = upsert_items_with_comments(msg.chat.id, pairs, source="text", source_file="message")
    await msg.answer(f"Найдено VSCO-ссылок: {len(pairs)}, добавлено записей: {ai}, комментариев: {ac}")

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
    ses = get_session(msg.chat.id)
    await msg.answer(
        "Экспорт VSCO:\n• CSV / Галерея\n• Карта: по пользователям или по фото",
        reply_markup=export_scope_keyboard(ses)
    )

@dp.callback_query(F.data.startswith("export:"))
async def on_export_click(cq: CallbackQuery):
    chat_id = cq.message.chat.id
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
            if not users: await cq.answer("Нет данных", show_alert=True); return
            flat = [{
                "username": u["username"], "profile_url": u["profile_url"],
                "lat": u["lat"], "lon": u["lon"],
                "images_count": u["images_count"], "comments_count": u["comments_count"],
                "comments": " | ".join(u["comments"])
            } for u in users]
            out = ses.dir / f"export_{ses.export_scope}.csv"
            pd.DataFrame(flat).to_csv(out, index=False, encoding="utf-8")
            await cq.message.answer_document(BufferedInputFile(out.read_bytes(), filename=out.name),
                caption=f"CSV ({'вся база' if ses.export_scope=='all' else 'текущий чат'})")
            await cq.answer(); return

        if fmt == "gallery":
            users = fetch_gallery_users(ses.export_scope, chat_id)
            if not users: await cq.answer("Нет данных", show_alert=True); return
            html = build_rich_gallery(users, title="VSCO Gallery",
                subtitle=("All DB" if ses.export_scope=='all' else "Current Chat"))
            out = ses.dir / f"export_gallery_{ses.export_scope}.html"
            out.write_text(html, encoding="utf-8")
            await cq.message.answer_document(BufferedInputFile(out.read_bytes(), filename=out.name),
                caption=f"Галерея ({'вся база' if ses.export_scope=='all' else 'текущий чат'})")
            await cq.answer(); return

        if fmt in ("map_users","map","map_images"):
            if fmt in ("map","map_users"):
                users = fetch_gallery_users(ses.export_scope, chat_id)
                if not users: await cq.answer("Нет данных", show_alert=True); return
                html = build_map_users(users, title=f"VSCO Profiles — {'Users' if fmt!='map_images' else 'Images'}")
                out = ses.dir / f"export_map_users_{ses.export_scope}.html"
            else:
                items = fetch_items_for_map(ses.export_scope, chat_id)
                if not items: await cq.answer("Нет данных", show_alert=True); return
                html = build_map_images(items, title="VSCO Profiles — Images")
                out = ses.dir / f"export_map_images_{ses.export_scope}.html"

            out.write_text(html, encoding="utf-8")
            await cq.message.answer_document(BufferedInputFile(out.read_bytes(), filename=out.name),
                caption=f"Карта ({'вся база' if ses.export_scope=='all' else 'текущий чат'})")
            await cq.answer(); return

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
            f"SELECT username, url, created_at, chat_id FROM links WHERE {where} {order} LIMIT ? OFFSET ?",
            tuple(params + [limit, offset])
        ).fetchall()
        return int(total or 0), rows
    finally:
        conn.close()

def _fmt_links_block(rows: List[Tuple[str,str,str,int]]) -> str:
    out = []
    for uname, url, created_at, _chat in rows:
        t = created_at
        try:
            dt = datetime.fromisoformat(created_at.replace("Z","+00:00"))
            t = dt.strftime("%H:%M")
        except Exception:
            pass
        u = escape(uname or "")
        href = escape(url or "")
        # ВАЖНО: не используем <span>; Telegram не поддерживает. Берём <i>.
        out.append(f"• <a href=\"{href}\">@{u}</a> <i>(UTC {t})</i>")
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
        df = pd.DataFrame([{ "username": r[0], "url": r[1], "created_at": r[2], "chat_id": r[3]} for r in rows])
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

@dataclass
class DLJob:
    id: int
    chat_id: int
    target: str           # username или полный профильный URL
    extra_flags: list     # список флагов вида ["--max","100","--no-zip",...]
    out_base: Path        # базовая папка для выдачи

def _dl_script_path() -> Path:
    # vsco_downloader.py должен лежать рядом с текущим файлом
    return Path(__file__).with_name("vsco_downloader.py")


@dp.message(Command("dl"))
async def cmd_dl_enqueue(msg: Message):
    # Полный запрет команды /dl в групповых чатах
    if msg.chat.type in ("group", "supergroup"):
        await msg.answer("🚫 Архив можно скачать только в личных сообщениях. Напишите мне в ЛС.")
        return
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

    ses = get_session(msg.chat.id)
    out_base = ses.dir / "downloads"
    out_base.mkdir(parents=True, exist_ok=True)

    global _DL_QUEUE, _DL_COUNTER
    if _DL_QUEUE is None:
        _DL_QUEUE = asyncio.Queue()

    _DL_COUNTER += 1
    job = DLJob(
        id=_DL_COUNTER,
        chat_id=msg.chat.id,
        target=target,
        extra_flags=extra_flags,
        out_base=out_base,
    )
    await _DL_QUEUE.put(job)

    pos = _DL_QUEUE.qsize()  # позиция «после put»: 1 — значит выполнится следующим
    await msg.answer(f"🗂️ Задание #{job.id} поставлено в очередь. Позиция: {pos}.")


@dp.message(Command("qstat"))
async def cmd_qstat(msg: Message):
    q = _DL_QUEUE
    size = q.qsize() if q else 0
    await msg.answer(f"📊 В очереди заданий: {size}. Один воркер обрабатывает по одному.")


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




async def _dl_worker():
    """Единственный воркер: берёт задания по одному и запускает downloader как отдельный процесс.
    Добавлено: во время сканирования показывает «Найдено медиа: N» по manifest/urls_extracted или stdout-подсказкам.
    """
    global _DL_QUEUE
    if _DL_QUEUE is None:
        _DL_QUEUE = asyncio.Queue()

    async def _safe_edit(chat_id: int, message_id: int, text: str):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            raise

    while True:
        job: DLJob = await _DL_QUEUE.get()
        try:
            job_started = time.time()
            log.info("Job #%s: start for %s", job.id, job.target)
            progress = await bot.send_message(job.chat_id, f"⏳ Сканирование профиля: <code>{job.target}</code>")

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

            total_found: int | None = None
            stage = "Сканирование"
            user_dir: Path | None = None
            log.debug("Job #%s: initial stage %s", job.id, stage)

            stop_evt = asyncio.Event()

            async def fs_probe_loop():
                nonlocal user_dir, total_found, stage
                last_edit = 0.0
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
                                    try:
                                        n = sum(1 for ln in urls.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip())
                                        if n > 0:
                                            total_found = n
                                    except Exception:
                                        pass
                            if total_found is not None and stage == "Сканирование":
                                log.info("Job #%s: media found so far %d", job.id, total_found)
                        txt = f"🔄 <b>{stage}</b>: <code>{job.target}</code>"
                        if total_found is not None:
                            txt += f"\n🔎 Найдено медиа: <b>{total_found}</b>"
                        now = loop.time()
                        if now - last_edit >= 2.0:
                            await _safe_edit(job.chat_id, progress.message_id, txt)
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
                    if any(k in low for k in ("download", "загрузка", "скачива")) and stage != "Загрузка":
                        stage = "Загрузка"
                        log.info("Job #%s: stage -> %s", job.id, stage)
                        await _safe_edit(job.chat_id, progress.message_id, f"📥 <b>{stage}</b>: <code>{job.target}</code>")
                    elif any(k in low for k in ("zip", "архив")) and stage != "Архив":
                        stage = "Архив"
                        log.info("Job #%s: stage -> %s", job.id, stage)
                        await _safe_edit(job.chat_id, progress.message_id, f"🗜️ <b>{stage}</b>: <code>{job.target}</code>")

                    if total_found is None:
                        m = re.search(r"(?:found|найден[оа])\\D+(\\d+)\\D+(?:media|items|files|медиа|ссыл)", low)
                        if m:
                            try:
                                total_found = int(m.group(1))
                                log.info("Job #%s: media count from stdout %d", job.id, total_found)
                                await _safe_edit(job.chat_id, progress.message_id, f"🔎 Найдено медиа: <b>{total_found}</b>")
                            except Exception:
                                pass

                    now = asyncio.get_running_loop().time()
                    if now - last_ping > 10.0:
                        try:
                            base = f"🔄 <b>{stage}</b>: <code>{job.target}</code>"
                            if total_found is not None:
                                base += f"\n🔎 Найдено медиа: <b>{total_found}</b>"
                            await _safe_edit(job.chat_id, progress.message_id, base)
                        except Exception:
                            pass
                        last_ping = now

                rc = await proc.wait()
                log.info("Job #%s: downloader exited with code %s", job.id, rc)
            finally:
                stop_evt.set()
                with contextlib.suppress(Exception):
                    await fs_task

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
            log.debug("Job #%s: task done", job.id)


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
        "• <b>/export</b> — экспорт CSV/галереи или карты\n"
        "• <b>/stats</b> — статистика по скачанным данным\n"
        "• <b>/reset</b> — очистить текущую сессию\n"
        "• <b>/help</b> — эта справка\n\n"
        "🔧 <b>Полезные флаги</b>:\n"
        "• <code>--max N</code> — лимит медиа (0 = все)\n"
        "• <code>--split-zip-size-mb N</code> — размер части архива (если включена упаковка)\n"
        "• <code>--no-zip</code> — не упаковывать в ZIP\n"
        "• <code>--concurrency 4</code>, <code>--delay 0.4</code>, <code>--timeout 30</code> — тонкая настройка\n\n"
        "💡 <b>Примеры</b>:\n"
        "• <code>/dl johndoe</code>\n"
        "• <code>/dl https://vsco.co/johndoe --max 100 --split-zip-size-mb 45</code>\n\n"
        "ℹ️ Если не видите архив — проверьте, что задание завершено, или запросите <code>manifest.json</code> / <code>urls_extracted.txt</code>."
    )
    await msg.answer(text)


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    # --- start download worker ---
    global _DL_WORKER_TASK, _DL_QUEUE
    if _DL_QUEUE is None:
        _DL_QUEUE = asyncio.Queue()
    _DL_WORKER_TASK = asyncio.create_task(_dl_worker())
    # -----------------------------
    log.info("Bot is starting polling…")
    try:
        await dp.start_polling(bot)
    finally:
        if _DL_WORKER_TASK:
            _DL_WORKER_TASK.cancel()
            with contextlib.suppress(Exception):
                await _DL_WORKER_TASK
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")



