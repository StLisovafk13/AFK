# zip_profile.py
# VSCO profile downloader (no DB) with detailed logging and API fallback
# Requires: aiogram v3, aiohttp
from __future__ import annotations

import asyncio
import io
import re
import time
import zipfile
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Iterable, List, Optional

import aiohttp
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BufferedInputFile

from vsco_utils import (
    build_perception_gallery_url,
    extract_vsco_media_urls,
    normalize_vsco_profile_url,
    resolve_vsco_short_link,
    vsco_short_slug,
)

zip_router = Router(name="zip_profile")

# ------------ logging config (module-level) ------------
log = logging.getLogger("zip_profile")
# Tips:
#   In your main file set basics via logging.basicConfig(level=logging.INFO, ...)
#   To see DEBUG from this module only: logging.getLogger("zip_profile").setLevel(logging.DEBUG)

# ------------ constants ------------
MAX_ZIP_SIZE = 45 * 1024 * 1024  # ~45MB, safe for Telegram limits
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)  # slightly stricter than default
CONNECTION_LIMIT = 10  # download concurrency

# ------------ helpers ------------
@dataclass
class ZipStats:
    req_id: str
    start_ts: float
    normalized_url: Optional[str] = None
    html_bytes: int = 0
    urls_found: int = 0
    urls_after_dedup: int = 0
    urls_fetched_ok: int = 0
    urls_fetched_fail: int = 0
    parts: int = 0
    total_zip_bytes: int = 0

    def asdict(self):
        return {
            "req_id": self.req_id,
            "elapsed_ms": int((time.time() - self.start_ts) * 1000),
            "normalized_url": self.normalized_url,
            "html_bytes": self.html_bytes,
            "urls_found": self.urls_found,
            "urls_after_dedup": self.urls_after_dedup,
            "urls_fetched_ok": self.urls_fetched_ok,
            "urls_fetched_fail": self.urls_fetched_fail,
            "parts": self.parts,
            "total_zip_bytes": self.total_zip_bytes,
        }

def _gen_req_id(chat_id: int) -> str:
    # Generate a readable request id
    return f"ZIP{chat_id}-{int(time.time()*1000)%1_000_000:06d}"

def _dedup_preserve_order(seq: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _safe_img_name(url: str, idx: int) -> str:
    name = url.split("/")[-1].split("?")[0].strip()
    if not name or "." not in name or name.rsplit(".", 1)[-1].lower() not in ("jpg", "jpeg", "png", "webp", "gif"):
        name = f"vsco_{idx:05d}.jpg"
    return name

@asynccontextmanager
async def _http_simple(headers: Optional[dict] = None):
    base_headers = {"User-Agent": "VSCO-ZipBot/1.0 (+aiogram)"}
    if headers:
        base_headers.update(headers)
    # ssl=False can help on some hostings; change if you require strict SSL
    conn = aiohttp.TCPConnector(limit=CONNECTION_LIMIT, ssl=False)
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=base_headers, connector=conn) as s:
        yield s

# ------------ VSCO parsing ------------
# NEW: site_id finders
_SITE_ID_RE_LIST = [
    re.compile(r'"site_id"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'data-site-id=["\'](\d+)["\']', re.IGNORECASE),
    re.compile(r'\bsiteId\s*:\s*(\d+)\b', re.IGNORECASE),
]


async def resolve_vsco_short_or_profile(u: str, stats: ZipStats) -> Optional[str]:
    """Return a normalized VSCO profile URL for vs.co/vsco.co inputs."""

    raw = (u or "").strip()
    if not raw:
        return None

    norm = normalize_vsco_profile_url(raw)
    if norm and not norm.lower().startswith("https://vs.co"):
        stats.normalized_url = norm
        return norm

    candidate = norm or raw
    slug = vsco_short_slug(candidate)

    async with _http_simple() as s:
        try:
            resolved = await resolve_vsco_short_link(candidate, s)
        except Exception:
            log.exception("resolve_vsco_short: exception", extra=stats.asdict())
            resolved = None

    normalized = normalize_vsco_profile_url(resolved) if resolved else None
    if normalized:
        stats.normalized_url = normalized
        return normalized

    if resolved:
        stats.normalized_url = resolved
        return resolved

    if slug:
        fallback = build_perception_gallery_url(slug)
        stats.normalized_url = fallback
        return fallback

    log.warning("resolve_vsco_short: redirect resolution failed", extra=stats.asdict())
    return None

def _extract_site_id(html: str) -> Optional[str]:
    for rx in _SITE_ID_RE_LIST:
        m = rx.search(html)
        if m:
            return m.group(1)
    return None

async def fetch_vsco_api_image_urls(site_id: str, stats: ZipStats, max_urls: int = 2000) -> List[str]:
    """
    Call VSCO public API:
      https://vsco.co/api/2.0/medias?site_id=<id>&page=<n>&size=100
    Collect direct image links from various fields.
    """
    if not site_id:
        return []
    page = 1
    size = 100
    urls: List[str] = []
    api_pages = 0
    api_items = 0

    async with _http_simple() as s:
        while True:
            if max_urls and len(urls) >= max_urls:
                break
            api_url = f"https://vsco.co/api/2.0/medias?site_id={site_id}&page={page}&size={size}"
            try:
                t0 = time.time()
                async with s.get(api_url, allow_redirects=True) as r:
                    if r.status != 200:
                        log.debug("api_non_200 page=%d status=%s", page, r.status, extra=stats.asdict())
                        break
                    data = await r.json(content_type=None)
                api_pages += 1
                log.debug("api_page_ok page=%d elapsed_ms=%d", page, int((time.time()-t0)*1000), extra=stats.asdict())
            except Exception:
                log.exception("api_request_exception page=%d", page, extra=stats.asdict())
                break

            items = (data or {}).get("medias") or (data or {}).get("media") or []
            if not items:
                break

            api_items += len(items)
            for it in items:
                u = (
                    it.get("responsive_url")
                    or it.get("image", {}).get("cdn_url")
                    or it.get("image", {}).get("url")
                    or it.get("image", {}).get("path")
                    or it.get("url")
                )
                if isinstance(u, str) and u.startswith("http"):
                    urls.append(u)

                variants = it.get("images") or it.get("variants") or []
                if isinstance(variants, list):
                    for v in variants:
                        vu = v.get("url") or v.get("cdn_url")
                        if isinstance(vu, str) and vu.startswith("http"):
                            urls.append(vu)

            page += 1

    urls = _dedup_preserve_order(urls)
    if max_urls and len(urls) > max_urls:
        urls = urls[:max_urls]

    log.info("api_collect_done pages=%d items=%d urls=%d site_id=%s",
             api_pages, api_items, len(urls), site_id, extra=stats.asdict())
    return urls

async def fetch_vsco_profile_image_urls(profile_url: str, stats: ZipStats, max_urls: int = 2000) -> List[str]:
    """Fetch profile HTML and extract image links. If none found — fallback to API by site_id."""
    html = ""
    async with _http_simple() as s:
        try:
            t0 = time.time()
            async with s.get(profile_url, allow_redirects=True) as r:
                html = await r.text(errors="ignore")
                stats.html_bytes = len(html.encode("utf-8", "ignore"))
                log.debug("profile_fetch: status=%s bytes=%s elapsed_ms=%d",
                          r.status, stats.html_bytes, int((time.time()-t0)*1000), extra=stats.asdict())
                if r.status != 200:
                    return []
        except Exception:
            log.exception("profile_fetch: exception", extra=stats.asdict())
            return []

    urls = extract_vsco_media_urls(html, sources=("responsive", "twitter", "inline"))

    stats.urls_found = len(urls)
    urls = _dedup_preserve_order(urls)
    stats.urls_after_dedup = len(urls)

    if not urls:
        # ---- Fallback: try API with site_id ----
        site_id = _extract_site_id(html)
        if not site_id:
            log.warning("no_images_and_no_site_id", extra=stats.asdict())
            return []
        log.info("fallback_api_try site_id=%s", site_id, extra=stats.asdict())
        api_urls = await fetch_vsco_api_image_urls(site_id, stats, max_urls=max_urls)
        stats.urls_found = len(api_urls)
        stats.urls_after_dedup = len(api_urls)
        return api_urls

    if max_urls and len(urls) > max_urls:
        urls = urls[:max_urls]
    log.info("profile_parse: urls_found=%d dedup=%d", stats.urls_found, stats.urls_after_dedup, extra=stats.asdict())
    return urls

# ------------ ZIP building (to memory; send as document) ------------
async def _fetch_one(session: aiohttp.ClientSession, url: str, idx: int, stats: ZipStats) -> Optional[tuple[str, bytes]]:
    for attempt in range(2):
        try:
            async with session.get(url, allow_redirects=True) as r:
                if r.status == 200:
                    data = await r.read()
                    return _safe_img_name(url, idx), data
                else:
                    log.debug("fetch_one: non-200 url=%s status=%s", url, r.status, extra=stats.asdict())
        except Exception:
            if attempt == 1:
                log.exception("fetch_one: exception url=%s", url, extra=stats.asdict())
    return None

async def build_zip_parts_from_urls(urls: List[str], stats: ZipStats) -> List[bytes]:
    """
    Return a list of ZIP-part blobs (bytes) kept in memory.
    """
    if not urls:
        return []

    parts: List[bytes] = []
    part_idx = 1
    current_buf = io.BytesIO()
    zf = zipfile.ZipFile(current_buf, "w", compression=zipfile.ZIP_DEFLATED)
    current_size = 0
    written_in_part = 0

    sem = asyncio.Semaphore(CONNECTION_LIMIT)

    async def fetch_and_add(i: int, u: str):
        nonlocal zf, current_buf, current_size, written_in_part, part_idx, parts
        async with sem:
            res = await _fetch_one(session, u, i, stats)
        if not res:
            stats.urls_fetched_fail += 1
            return
        fname, data = res
        # split parts if needed
        if current_size + len(data) + 2048 > MAX_ZIP_SIZE and written_in_part > 0:
            zf.close()
            parts.append(current_buf.getvalue())
            stats.parts += 1
            stats.total_zip_bytes += len(parts[-1])

            # new part
            current_buf = io.BytesIO()
            zf = zipfile.ZipFile(current_buf, "w", compression=zipfile.ZIP_DEFLATED)
            current_size = 0
            written_in_part = 0

        zf.writestr(fname, data)
        current_size += len(data)
        written_in_part += 1
        stats.urls_fetched_ok += 1

    async with _http_simple() as session:
        await asyncio.gather(*(fetch_and_add(i, u) for i, u in enumerate(urls, 1)))

    # finalize last part
    try:
        zf.close()
    except Exception:
        pass
    if current_buf.getbuffer().nbytes > 0 and written_in_part > 0:
        parts.append(current_buf.getvalue())
        stats.parts += 1
        stats.total_zip_bytes += len(parts[-1])

    log.info("zip_build_done", extra=stats.asdict())
    return parts

# ------------ Command handler ------------
@zip_router.message(Command("zipurl"))
async def cmd_zipurl(message: Message, command: CommandObject):
    """
    /zipurl <VSCO-profile-URL-or-vs.co>
    Downloads images directly from the internet (no DB), packs into ZIP(s), sends to chat.
    """
    chat_id = message.chat.id
    req_id = _gen_req_id(chat_id)
    stats = ZipStats(req_id=req_id, start_ts=time.time())

    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Использование:\n"
            "<code>/zipurl https://vsco.co/&lt;username&gt;</code>\n"
            "или короткая: <code>/zipurl https://vs.co/xxxxx</code>"
        )
        log.info("zipurl_no_args", extra=stats.asdict())
        return

    await message.answer("🔎 Проверяю ссылку…")
    log.info("zipurl_start args=%s", args, extra=stats.asdict())

    norm = await resolve_vsco_short_or_profile(args, stats)
    if not norm:
        await message.answer("Не удалось распознать профиль VSCO. Проверь ссылку.")
        log.warning("normalize_failed", extra=stats.asdict())
        return

    await message.answer("🌐 Загружаю профиль и собираю изображения…")
    urls = await fetch_vsco_profile_image_urls(norm, stats, max_urls=2000)
    if not urls:
        await message.answer("На странице не нашлось изображений. Попробуй другой профиль.")
        log.warning("no_images_found", extra=stats.asdict())
        return

    # Simple heuristic — longer URLs are often originals
    urls.sort(key=len, reverse=True)
    await message.answer(f"🗜️ Собрано ссылок: <b>{len(urls)}</b>. Пакую ZIP…")

    try:
        parts = await build_zip_parts_from_urls(urls, stats)
    except Exception:
        log.exception("zip_build_exception", extra=stats.asdict())
        await message.answer("Ошибка при сборке ZIP.")
        return

    if not parts:
        await message.answer("Не удалось собрать ZIP.")
        log.error("zip_empty_parts", extra=stats.asdict())
        return

    for idx, blob in enumerate(parts, 1):
        fname = f"vsco_profile_part_{idx:02d}.zip"
        await message.answer_document(
            BufferedInputFile(blob, filename=fname),
            caption=f"ZIP из профиля — {fname}"
        )
        log.info("zip_part_sent idx=%d size=%d", idx, len(blob), extra=stats.asdict())

    log.info("zipurl_done", extra=stats.asdict())
