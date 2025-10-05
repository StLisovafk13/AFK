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
from typing import List, Optional

import aiohttp
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BufferedInputFile

from vsco_utils import (
    build_perception_gallery_url,
    dedupe_keep_order,
    extract_media_urls_from_html,
    extract_site_id_from_html,
    extract_vsco_media_urls,
    fetch_vsco_api_media_urls,
    generate_media_filename,
    is_media_url,
    is_vsco_logo_url,
    normalize_media_url,
    normalize_vsco_profile_url,
    upscale_w_param,
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

@asynccontextmanager
async def _http_simple(headers: Optional[dict] = None):
    base_headers = {"User-Agent": "VSCO-ZipBot/1.0 (+aiogram)"}
    if headers:
        base_headers.update(headers)
    # ssl=False can help on some hostings; change if you require strict SSL
    conn = aiohttp.TCPConnector(limit=CONNECTION_LIMIT, ssl=False)
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=base_headers, connector=conn) as s:
        yield s

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

async def fetch_vsco_profile_image_urls(profile_url: str, stats: ZipStats, max_urls: int = 2000) -> List[str]:
    """Fetch profile HTML and extract image links. Fallback to the VSCO API when needed."""

    html = ""
    async with _http_simple() as s:
        try:
            t0 = time.time()
            async with s.get(profile_url, allow_redirects=True) as r:
                html = await r.text(errors="ignore")
                stats.html_bytes = len(html.encode("utf-8", "ignore"))
                log.debug(
                    "profile_fetch: status=%s bytes=%s elapsed_ms=%d",
                    r.status,
                    stats.html_bytes,
                    int((time.time() - t0) * 1000),
                    extra=stats.asdict(),
                )
                if r.status != 200:
                    return []
        except Exception:
            log.exception("profile_fetch: exception", extra=stats.asdict())
            return []

        direct_urls = extract_media_urls_from_html(html, max_width=2048, root=profile_url)

        fallback_candidates: List[str] = []
        if not direct_urls:
            for candidate in extract_vsco_media_urls(html, sources=("responsive", "twitter", "inline")):
                normalized = normalize_media_url(candidate, root=profile_url)
                if not normalized:
                    continue
                final_url = upscale_w_param(normalized, 2048)
                if is_media_url(final_url) and not is_vsco_logo_url(final_url):
                    fallback_candidates.append(final_url)

        urls = dedupe_keep_order([*direct_urls, *fallback_candidates])
        stats.urls_found = len(urls)
        stats.urls_after_dedup = len(urls)

        site_id = extract_site_id_from_html(html)
        api_urls: List[str] = []
        if site_id:
            log.info("api_profile_scan site_id=%s", site_id, extra=stats.asdict())
            api_urls = await fetch_vsco_api_media_urls(
                s,
                site_id,
                max_width=2048,
                max_items=max_urls,
                logger=log,
            )
        elif not urls:
            log.warning("no_images_and_no_site_id", extra=stats.asdict())

        if api_urls:
            merged = dedupe_keep_order([*urls, *api_urls]) if urls else api_urls
            stats.urls_found = len(merged)
            stats.urls_after_dedup = len(merged)
            urls = merged

        if not urls:
            return []

        if max_urls and len(urls) > max_urls:
            urls = urls[:max_urls]
            stats.urls_after_dedup = len(urls)

        log.info(
            "profile_parse: urls_found=%d dedup=%d",
            stats.urls_found,
            stats.urls_after_dedup,
            extra=stats.asdict(),
        )
        return urls

# ------------ ZIP building (to memory; send as document) ------------
async def _fetch_one(session: aiohttp.ClientSession, url: str, idx: int, stats: ZipStats) -> Optional[tuple[str, bytes]]:
    for attempt in range(2):
        try:
            async with session.get(url, allow_redirects=True) as r:
                if r.status == 200:
                    data = await r.read()
                    return generate_media_filename(url, idx, default_ext="jpg"), data
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
