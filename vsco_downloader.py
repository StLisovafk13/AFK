#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VSCO Media Downloader (Playwright-session) + Auto-upscale + Load More + Multi-ZIP (NO EXIF)

- Извлекает URL фото/видео с профиля VSCO (img/picture + video/source)
- Кликает "Load More" (#loadMore-Button) до исчерпания ленты + мягкий скролл
- Авто-апскейлит параметр ?w= у картинок до --max-width (если есть)
- Скачивает файлы через Playwright context.request (обходит 403 CDN)
- Пишет подробные логи, manifest.json и urls_extracted.txt
- Ассоциирует постер-картинки с видео (поле thumbnail_of); можно исключить постеры флагом --skip-video-thumbs
- Собирает ZIP-архив(ы) с результатами ПО УМОЛЧАНИЮ:
    * моно-архив, или
    * мульти-ZIP при --split-zip-size-mb N (N МБ на том)
- EXIF полностью удалён

Usage:
  python vsco_downloader.py --username <vsco_name> [--out ./downloads] [--max 0] \
    [--concurrency 4] [--delay 0.4] [--timeout 30] [--max-width 2048] \
    [--no-zip] [--zip-name <file_or_base.zip>] [--skip-video-thumbs] \
    [--split-zip-size-mb 45]

Requirements:
  Python 3.10+
  pip install playwright beautifulsoup4
  python -m playwright install chromium
"""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

# -----------------------------
# ЛОГИ
# -----------------------------
def setup_logger(username: str) -> Tuple[logging.Logger, Path, str]:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs_dir = Path("logs"); logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = logs_dir / f"vsco_{username}_{ts}.log"
    logger = logging.getLogger(f"vsco.{username}"); logger.setLevel(logging.DEBUG); logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout); sh.setLevel(logging.INFO); sh.setFormatter(fmt)
    fh = logging.FileHandler(logfile, encoding="utf-8"); fh.setLevel(logging.DEBUG); fh.setFormatter(fmt)
    logger.addHandler(sh); logger.addHandler(fh)
    return logger, logfile, ts

def short_ok(logger: logging.Logger, msg: str): logger.info(f"✅ {msg}")
def short_fail(logger: logging.Logger, msg: str): logger.error(f"❌ {msg}")

# -----------------------------
# АРГУМЕНТЫ
# -----------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download VSCO media with validation & logs (Playwright session)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--username", type=str, help="VSCO username (e.g., johndoe)")
    g.add_argument("--profile-url", type=str, help="Full VSCO profile URL")
    p.add_argument("--out", type=str, default="./downloads", help="Output base dir")
    p.add_argument("--max", type=int, default=0, help="Max media to download (0 = all)")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel downloads via Playwright context")
    p.add_argument("--delay", type=float, default=0.4, help="Delay between scroll steps (sec)")
    p.add_argument("--timeout", type=float, default=30.0, help="Network timeout seconds")
    p.add_argument("--max-width", type=int, default=2048, help="Auto-upscale ?w= for images when possible")
    # ZIP options: по умолчанию архив создаётся; можно выключить флагом --no-zip
    p.add_argument("--no-zip", action="store_true", help="Do NOT create ZIP (by default ZIP is created)")
    p.add_argument("--zip-name", type=str, default="", help="ZIP filename (default: <username>_<timestamp>.zip). Для мульти-ZIP это будет базой имени.")
    # Постеры видео
    p.add_argument("--skip-video-thumbs", action="store_true",
                   help="Не сохранять .jpg/.png постеры, которые являются миниатюрами для видео")
    # Мульти-ZIP
    p.add_argument("--split-zip-size-mb", type=int, default=0,
                   help="Макс. размер одного ZIP (МБ). 0 = один архив. Пример: 45 для Telegram.")
    return p.parse_args()

# -----------------------------
# ВАЛИДАЦИЯ ОКРУЖЕНИЯ
# -----------------------------
def validate_environment(logger: logging.Logger) -> bool:
    ok = True
    try:
        import playwright  # noqa
        from playwright.async_api import async_playwright  # noqa
    except Exception as e:
        ok = False; short_fail(logger, f"Playwright не найден: {e}. Установите: pip install playwright && python -m playwright install chromium")
    try:
        import bs4  # noqa
    except Exception as e:
        ok = False; short_fail(logger, f"Библиотека bs4 не найдена: {e}. Установите зависимости.")
    if ok:
        short_ok(logger, "Этап 1 (инициализация): библиотеки найдены")
    return ok

def normalize_profile(username: Optional[str], profile_url: Optional[str]) -> Tuple[str, str]:
    if profile_url:
        m = re.search(r"vsco\.co/([^/]+)/", profile_url) or re.search(r"vsco\.co/([^/]+)", profile_url)
        user = m.group(1) if m else "unknown"
        url = profile_url if profile_url.endswith("/gallery") else profile_url.rstrip("/") + "/gallery"
        return user, url
    assert username
    return username, f"https://vsco.co/{username}/gallery"

# -----------------------------
# URL / МЕДИА УТИЛИТЫ
# -----------------------------
MEDIA_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp|mp4|webm|mov)(\?|$)", re.I)
POSTER_HINT_RE = re.compile(r"(?i)(poster|thumb|thumbnail|cover|preview|frame)")

def select_best_from_srcset(srcset: str) -> Optional[str]:
    try:
        cand = []
        for part in srcset.split(","):
            part = part.strip()
            if not part: continue
            if " " in part:
                u, sz = part.rsplit(" ", 1)
                w = int(sz.replace("w", "")) if sz.endswith("w") else 0
            else:
                u, w = part, 0
            cand.append((w, u))
        if not cand: return None
        cand.sort(key=lambda x: x[0], reverse=True)
        return cand[0][1]
    except Exception:
        return None

def normalize_url(u: Optional[str], base: str = "https://") -> Optional[str]:
    if not u: return None
    u = u.strip()
    if u.startswith("//"): return base + u.lstrip("/")
    if u.startswith("/"):  return urljoin("https://vsco.co/", u)
    return u

def is_media_url(u: str) -> bool:
    if not u: return False
    return bool(MEDIA_EXT_RE.search(u))

def upscale_w_param(u: str, max_w: int) -> str:
    """
    Апскейлим только картинки по параметру ?w=. Для видео возвращаем исходный URL.
    """
    if not u or not MEDIA_EXT_RE.search(u):
        return u
    if re.search(r"\.(mp4|webm|mov)(\?|$)", u, re.I):
        return u
    parts = urlsplit(u)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "w" in q:
        try:
            cur = int(q["w"])
            if cur < max_w:
                q["w"] = str(max_w)
                new_query = urlencode(q, doseq=True)
                return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
        except ValueError:
            return u
    return u

def guess_filename(url: str, idx: int) -> str:
    """Return a deterministic file name derived from URL.

    Some VSCO CDN URLs use the same media file name while placing a
    unique identifier in the path right before it. For example:

        .../6340fee78c264f3f15c8be6c/vsco_100722.jpg
        .../6340fe288c264f3f15c8be68/vsco_100722.jpg

    Without accounting for the parent directory, both would save as
    ``vsco_100722.jpg`` and overwrite each other.  To avoid this we
    prepend the immediate parent folder when available.
    """

    path_parts = urlsplit(url).path.rstrip("/").split("/")
    base = path_parts[-1] if path_parts else ""
    if base and "." in base:
        # Include the segment preceding the filename to keep URLs unique
        if len(path_parts) >= 2 and path_parts[-2]:
            return f"{path_parts[-2]}_{base}"
        return base
    path = urlsplit(url).path.lower()
    if path.endswith(".mp4"):  return f"vsco_{idx:05d}.mp4"
    if path.endswith(".webm"): return f"vsco_{idx:05d}.webm"
    if path.endswith(".mov"):  return f"vsco_{idx:05d}.mov"
    return f"vsco_{idx:05d}.jpg"

# -----------------------------
# ПОСТЕРЫ ВИДЕО: ЭВРИСТИКИ И ПАРИНГ
# -----------------------------
def _stem_no_ext(path: str) -> str:
    name = Path(path).name
    stem = Path(name).stem
    return stem

def looks_like_video_poster(img_url: str, video_url: str) -> bool:
    """Эвристика: img — постер для video (одинаковый stem или явные суффиксы)."""
    ip = urlsplit(img_url).path
    vp = urlsplit(video_url).path
    ist, vst = _stem_no_ext(ip), _stem_no_ext(vp)
    if ist == vst:
        return True
    if ist.replace("-", "_").startswith(vst.replace("-", "_")) and POSTER_HINT_RE.search(ist):
        return True
    if vst.replace("-", "_").startswith(ist.replace("-", "_")) and POSTER_HINT_RE.search(vst):
        return True
    return False

def pair_thumbnails_with_videos(urls: List[str], skip_thumbs: bool) -> Tuple[List[str], Dict[str, str]]:
    """
    Возвращает:
      - финальный список URL к скачиванию (с учётом skip_thumbs),
      - словарь {img_url: video_url} для постеров.
    """
    videos = [u for u in urls if re.search(r"\.(mp4|webm|mov)(\?|$)", u, re.I)]
    images = [u for u in urls if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", u, re.I)]
    pairs: Dict[str, str] = {}
    for img in images:
        for vid in videos:
            if looks_like_video_poster(img, vid):
                pairs[img] = vid
                break
    if skip_thumbs:
        filtered = [u for u in urls if u not in pairs]
        return filtered, pairs
    return urls, pairs

# -----------------------------
# СБОР URL (кнопка Load More + скролл) + UPSCALE
# -----------------------------
async def collect_image_urls(
    page,
    logger: logging.Logger,
    delay: float,
    timeout: float,
    target_count: int,
    max_width: int,
) -> List[str]:
    """
    Открыта страница профиля.
    Извлекаем URL из img/src и picture/source[srcset], а также video/source.
    Кликаем 'Load More' (#loadMore-Button) до исчерпания ленты.
    Параллельно используем мягкий скролл как запасной механизм.
    """

    def extract_from_html(html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        urls: List[str] = []

        # IMG (картинки)
        for img in soup.find_all("img"):
            src = img.get("src"); srcset = img.get("srcset")
            cand = select_best_from_srcset(srcset) if srcset else None
            if not cand and src: cand = src
            if cand:
                cand = normalize_url(cand, base="https://")
                if cand and is_media_url(cand):
                    urls.append(upscale_w_param(cand, max_width))

        # PICTURE/SOURCE (картинки)
        for pic in soup.find_all("picture"):
            for s in pic.find_all("source"):
                ss = s.get("srcset") or ""
                best = select_best_from_srcset(ss)
                best = normalize_url(best, base="https://") if best else None
                if best and is_media_url(best):
                    urls.append(upscale_w_param(best, max_width))

        # VIDEO (src) + SOURCE (видео)
        for vid in soup.find_all("video"):
            vsrc = normalize_url(vid.get("src"), base="https://")
            if vsrc and is_media_url(vsrc):
                urls.append(vsrc)
            for s in vid.find_all("source"):
                vurl = normalize_url(s.get("src"), base="https://")
                if vurl and is_media_url(vurl):
                    urls.append(vurl)
                vset = s.get("srcset")
                if vset:
                    bestv = select_best_from_srcset(vset)
                    bestv = normalize_url(bestv, base="https://") if bestv else None
                    if bestv and is_media_url(bestv):
                        urls.append(bestv)

        return urls

    def dedup_keep_order(seq: List[str]) -> List[str]:
        seen, out = set(), []
        for u in seq:
            if u not in seen:
                seen.add(u); out.append(u)
        return out

    html = await page.content()
    urls = dedup_keep_order(extract_from_html(html))
    if urls:
        logger.info(f"scan_progress {len(urls)}")
    else:
        logger.warning("На первом экране не нашли img/picture. Пробуем прокрутку и кнопку Load More…")

    max_scrolls = 999999 if target_count == 0 else max(30, min(999999, target_count // 2 + 20))
    max_clicks = 500
    stagnation_limit = 5
    no_growth_click_limit = 3

    last_height = await page.evaluate("() => document.body.scrollHeight")
    stagnation = 0
    load_clicks = 0
    clicks_without_growth = 0
    prev_count = len(urls)

    for _ in range(max_scrolls):
        # Кнопка Load More
        btn = page.locator("#loadMore-Button").first
        try:
            btn_exists = (await btn.count()) > 0
            btn_visible = btn_exists and (await btn.is_visible())
            disabled_attr = await btn.get_attribute("disabled") if btn_exists else None
            aria_disabled = await btn.get_attribute("aria-disabled") if btn_exists else None
            btn_disabled = (disabled_attr is not None) or (aria_disabled is not None and aria_disabled.lower() in ("true", "1"))
        except Exception:
            btn_exists = btn_visible = False
            btn_disabled = True

        clicked = False
        if btn_exists and btn_visible and (not btn_disabled) and load_clicks < max_clicks:
            try:
                await btn.scroll_into_view_if_needed()
                await btn.click()
                load_clicks += 1
                clicked = True
                logger.debug(f"Load More: клик #{load_clicks}")
                try:
                    await page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    await page.wait_for_timeout(int(max(1, delay) * 1000))
            except Exception as e:
                logger.debug(f"Load More: клик не удался: {e}")

        # Мягкий скролл
        await page.evaluate("""() => { window.scrollBy(0, Math.floor(window.innerHeight * 0.9)); }""")
        await page.wait_for_timeout(int(delay * 1000))

        # Повторное извлечение
        html = await page.content()
        urls = dedup_keep_order(urls + extract_from_html(html))
        if len(urls) > prev_count:
            logger.info(f"scan_progress {len(urls)}")

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

        # Выходы
        if target_count and len(urls) >= target_count:
            logger.debug("Достигли целевого количества ссылок (по --max).")
            break
        if (not btn_exists or not btn_visible or btn_disabled) and stagnation >= stagnation_limit:
            logger.debug("Похоже, достигнут конец ленты (нет роста и нет активной кнопки).")
            break
        if clicked and clicks_without_growth >= no_growth_click_limit:
            logger.debug("Несколько кликов Load More подряд не дали новых медиа — выходим.")
            break

    short_ok(logger, f"Этап 2–3: извлекли {len(urls)} ссылок (до лимита/конца ленты).")
    return urls

# -----------------------------
# ПРОБА (через context.request)
# -----------------------------
async def probe_via_context(logger: logging.Logger, ctx_request, urls: List[str], referer: str, sample: int = 5) -> List[str]:
    probe = [u for u in urls if is_media_url(u)][:max(1, sample)]
    good: List[str] = []
    headers = {
        "Referer": referer,
        "Accept": "video/*;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }

    async def one(u: str):
        try:
            r = await ctx_request.get(u, headers=headers, timeout=15000)
            ctype = (r.headers.get("content-type") or "").lower()
            if r.ok and (ctype.startswith("image/") or ctype.startswith("video/")):
                good.append(u); logger.debug(f"CTX PROBE OK {r.status} {ctype} {u}")
            else:
                logger.debug(f"CTX PROBE BAD {r.status} {ctype} {u}")
        except Exception as e:
            logger.debug(f"CTX PROBE EX {u} :: {e}")

    await asyncio.gather(*[one(u) for u in probe])
    if good:
        short_ok(logger, f"Этап 4: пробная валидация пройдена контекстом ({len(good)}/{len(probe)}).")
    else:
        logger.warning("Этап 4: проба контекстом не подтвердилась — продолжаем к реальной загрузке (фейловер).")
    return good

# -----------------------------
# ЗАГРУЗКА (через context.request)
# -----------------------------
async def download_all_via_context(
    logger: logging.Logger,
    ctx_request,
    urls: List[str],
    out_dir: Path,
    referer: str,
    concurrency: int,
    timeout: float,
    max_items: int,
    thumb_pairs: Dict[str, str],
) -> List[Dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(max(1, concurrency))
    headers = {
        "Referer": referer,
        "Accept": "video/*;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }

    async def fetch(idx: int, url: str):
        name = guess_filename(url, idx)
        dest = out_dir / name
        meta: Dict[str, Any] = {
            "index": idx, "url": url, "file": str(dest),
            "ok": False, "status": None, "sha256": None, "bytes": 0
        }

        # пометка: является ли этот элемент постером для какого-то видео
        if url in thumb_pairs:
            meta["thumbnail_of"] = thumb_pairs[url]
            # Подправим имя, чтобы лежало рядом: foo.mp4 -> foo.poster.jpg
            parent_name = guess_filename(thumb_pairs[url], idx)
            parent_stem = Path(parent_name).stem
            ext = Path(name).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            name = f"{parent_stem}.poster{ext}"
            dest = out_dir / name
            meta["file"] = str(dest)

        async with sem:
            for attempt in range(1, 3 + 1):
                try:
                    r = await ctx_request.get(url, headers=headers, timeout=int(timeout * 1000))
                    meta["status"] = r.status
                    if r.status >= 400:
                        raise RuntimeError(f"HTTP {r.status}")
                    body = await r.body()
                    if len(body) < 256:
                        raise RuntimeError("Слишком малый размер ответа")
                    with open(dest, "wb") as f:
                        f.write(body)
                    meta["sha256"] = hashlib.sha256(body).hexdigest()
                    meta["bytes"] = len(body)
                    meta["ok"] = True
                    logger.info(f"[{idx}] OK {name} ({len(body)} bytes)")
                    break
                except Exception as e:
                    logger.warning(f"[{idx}] Попытка {attempt}/3 не удалась: {e}")
                    await asyncio.sleep(0.8 * attempt)
            results.append(meta)

    take = urls if max_items == 0 else urls[:max_items]
    logger.info(f"scan_progress {len(take)}")
    await asyncio.gather(*[fetch(i + 1, u) for i, u in enumerate(take)])
    return results

# -----------------------------
# МАНИФЕСТ и ПОСТ-ВАЛИДАЦИЯ
# -----------------------------
def write_manifest(manifest_path: Path, items: List[Dict[str, Any]], profile_url: str):
    data = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "profile_url": profile_url,
        "total": len(items),
        "ok": sum(1 for x in items if x.get("ok")),
        "items": items,
    }
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def post_validate(logger: logging.Logger, items: List[Dict[str, Any]]) -> bool:
    total = len(items); ok = sum(1 for x in items if x.get("ok"))
    if total == 0 or ok == 0:
        short_fail(logger, f"Этап 6 (пост-валидация): скачано {ok}/{total}. Провал.")
        return False
    if ok < total:
        logger.warning(f"Этап 6: часть загрузок не удалась ({ok}/{total}). Повторите позже.")
    short_ok(logger, f"Этап 6: итоговая валидация пройдена ({ok}/{total}).")
    return True

# -----------------------------
# ZIP-АРХИВАЦИЯ (моно и мульти)
# -----------------------------
def _estimate_zip_footprint(files: List[Path]) -> int:
    """
    Грубая оценка веса ZIP (без сжатия для уже сжатых форматов).
    Для JPEG/WEBP/MP4/WEBM/MOV считаем, что выигрыш небольшой,
    поэтому используем сырой вес + накладные ~2КБ/файл.
    """
    total = 0
    for f in files:
        try:
            total += f.stat().st_size + 2048
        except Exception:
            pass
    return total

def _split_into_parts(candidates: List[Path], limit_bytes: int) -> List[List[Path]]:
    """
    Делит файлы на части по оценке суммарного веса (жадно).
    manifest.json и urls_extracted.txt будут добавляться в КАЖДЫЙ том позже.
    """
    parts: List[List[Path]] = []
    cur: List[Path] = []
    cur_bytes = 0
    for f in candidates:
        try:
            w = f.stat().st_size + 2048
        except Exception:
            w = 2048
        if cur and (cur_bytes + w > limit_bytes):
            parts.append(cur); cur = [f]; cur_bytes = w
        else:
            cur.append(f); cur_bytes += w
    if cur:
        parts.append(cur)
    return parts

def build_zip_multi(
    logger: logging.Logger,
    out_dir: Path,
    manifest_path: Path,
    urls_file: Path,
    items: List[Dict[str, Any]],
    zip_base_name: str,
    split_mb: int,
) -> List[Path]:
    """
    Создаёт несколько ZIP-томов так, чтобы каждый был НЕБОЛЕЕ split_mb МБ (по оценке).
    Имя: <base_stem>_part01.zip, _part02.zip ...
    В каждый том добавляется:
      - свой набор успешных медиа
      - копия manifest.json
      - urls_extracted.txt
    """
    ok_files = [Path(x["file"]) for x in items if x.get("ok") and x.get("file") and Path(x["file"]).exists()]
    if not ok_files:
        raise RuntimeError("Нет файлов для архивации.")
    limit_bytes = max(1, split_mb) * 1024 * 1024

    # Разбиваем кандидатов на части
    parts = _split_into_parts(ok_files, limit_bytes)
    zip_paths: List[Path] = []

    base_stem = Path(zip_base_name).stem  # игнорируем исходный .zip
    for i, files_part in enumerate(parts, 1):
        zip_name = f"{base_stem}_part{i:02d}.zip"
        zip_path = (out_dir / zip_name).resolve()
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for src in files_part:
                try:
                    zf.write(src, arcname=src.name)
                except Exception as e:
                    logger.warning(f"Не удалось добавить в ZIP: {src} :: {e}")
            # служебные (в каждом томе)
            if manifest_path.exists():
                zf.write(manifest_path, arcname="manifest.json")
            if urls_file.exists():
                zf.write(urls_file, arcname=urls_file.name)

        # валидация
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                bad = z.testzip()
                if bad is not None:
                    raise RuntimeError(f"ZIP повреждён на файле: {bad}")
        except Exception as e:
            raise RuntimeError(f"Проверка ZIP провалилась ({zip_name}): {e}")

        logger.info(f"ZIP-том #{i}: {zip_path.name} ({zip_path.stat().st_size} bytes)")
        zip_paths.append(zip_path)

    short_ok(logger, f"Создано ZIP-томов: {len(zip_paths)}")
    return zip_paths

def build_zip_single(
    logger: logging.Logger,
    out_dir: Path,
    manifest_path: Path,
    urls_file: Path,
    items: List[Dict[str, Any]],
    zip_name: str,
) -> Path:
    zip_path = (out_dir / zip_name).resolve()
    members: List[Tuple[Path, str]] = []
    ok_files = [Path(x["file"]) for x in items if x.get("ok") and x.get("file")]
    for f in ok_files:
        if f.exists():
            members.append((f, f.name))
    if manifest_path.exists():
        members.append((manifest_path, "manifest.json"))
    if urls_file.exists():
        members.append((urls_file, urls_file.name))
    if not members:
        raise RuntimeError("Нет файлов для архивации.")

    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for src, arcname in members:
            try:
                zf.write(src, arcname=arcname)
            except Exception as e:
                logger.warning(f"Не удалось добавить в ZIP: {src} :: {e}")

    # Валидация
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            test = z.testzip()
            if test is not None:
                raise RuntimeError(f"ZIP повреждён на файле: {test}")
            has_media = any(n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mov")) for n in z.namelist())
            if not has_media:
                logger.warning("ZIP создан, но в нём нет медиа-файлов (возможно, все загрузки провалились).")
    except Exception as e:
        raise RuntimeError(f"Проверка ZIP провалилась: {e}")

    short_ok(logger, f"ZIP создан: {zip_path.name} ({zip_path.stat().st_size} bytes)")
    return zip_path

# -----------------------------
# MAIN
# -----------------------------
async def main_async(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    user, profile_url = normalize_profile(args.username, args.profile_url)
    logger, logpath, ts = setup_logger(user)
    logger.info("== Этап 1: Инициализация и мини-тест окружения ==")
    if not validate_environment(logger): return 2

    out_root = Path(args.out); out_dir = out_root / user; out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("== Этап 2–3: Доступ к профилю, сбор URL, Load More + скролл ==")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0 Safari/537.36")
        )
        page = await context.new_page()
        logger.info(f"Открываю профиль: {profile_url}")
        resp = await page.goto(profile_url, wait_until="domcontentloaded", timeout=int(args.timeout * 1000))
        if not resp or not resp.ok:
            short_fail(logger, f"Страница не загрузилась (status={getattr(resp,'status',None)}). Провал шага.")
            await context.close(); await browser.close(); return 3

        urls = await collect_image_urls(page, logger, delay=args.delay, timeout=args.timeout,
                                        target_count=args.max, max_width=args.max_width)
        if not urls:
            short_fail(logger, "Не удалось извлечь ссылки на медиа."); await context.close(); await browser.close(); return 3

        # Ассоциация постеров с видео (опциональное исключение постеров)
        final_urls, thumb_pairs = pair_thumbnails_with_videos(urls, skip_thumbs=args.skip_video_thumbs)
        if len(final_urls) != len(urls):
            logger.info(f"Убрали {len(urls) - len(final_urls)} миниатюр (постеры видео) из скачивания по флагу --skip-video-thumbs")
        urls = final_urls
        logger.info(f"scan_progress {len(urls)}")

        short_ok(logger, f"Извлекли {len(urls)} ссылок (до лимита/конца ленты).")
        urls_file = out_dir / "urls_extracted.txt"
        urls_file.write_text("\n".join(urls), encoding="utf-8")
        logger.info(f"Найдено уникальных ссылок: {len(urls)}")

        logger.info("== Этап 4: Пробная валидация (через Playwright context) ==")
        good_sample = await probe_via_context(logger, context.request, urls, referer=profile_url, sample=min(5, len(urls)))
        if not good_sample:
            logger.warning("Продолжаем загрузку несмотря на пустую пробу (CDN может блокировать пробные запросы).")

        logger.info("== Этап 5: Загрузка файлов с ретраями (через Playwright context) ==")
        items = await download_all_via_context(
            logger=logger,
            ctx_request=context.request,
            urls=urls,
            out_dir=out_dir,
            referer=profile_url,
            concurrency=args.concurrency,
            timeout=args.timeout,
            max_items=args.max,
            thumb_pairs=thumb_pairs,
        )

        logger.info("== Этап 6: Пост-валидация и манифест ==")
        manifest_path = out_dir / "manifest.json"
        write_manifest(manifest_path, items, profile_url)
        ok = post_validate(logger, items)

        # -------- ZIP (по умолчанию включён; можно отключить --no-zip)
        zip_created = None
        zip_created_parts: List[Path] = []
        if not args.no_zip:
            logger.info("== Этап 7: Архивация (моно или мульти ZIP) ==")
            try:
                base_zip_name = args.zip_name.strip() or f"{user}_{ts}.zip"
                if args.split_zip_size_mb and args.split_zip_size_mb > 0:
                    # мульти-ZIP
                    zip_created_parts = build_zip_multi(
                        logger=logger,
                        out_dir=out_dir,
                        manifest_path=manifest_path,
                        urls_file=urls_file,
                        items=items,
                        zip_base_name=base_zip_name,
                        split_mb=args.split_zip_size_mb,
                    )
                else:
                    # одиночный ZIP
                    zip_created = build_zip_single(
                        logger=logger,
                        out_dir=out_dir,
                        manifest_path=manifest_path,
                        urls_file=urls_file,
                        items=items,
                        zip_name=base_zip_name,
                    )
            except Exception as e:
                short_fail(logger, f"ZIP не создан: {e}")

        logger.info("== Этап 8: Итоговый отчёт ==")
        logger.info(f"Лог-файл: {logpath}")
        logger.info(f"Каталог загрузки: {out_dir}")
        logger.info(f"Манифест: {manifest_path}")
        if zip_created:
            logger.info(f"ZIP: {zip_created}")
        if zip_created_parts:
            for p in zip_created_parts:
                logger.info(f"ZIP часть: {p}")

        await context.close(); await browser.close()
        return 0 if ok else 5

def main():
    args = parse_args()
    if args.profile_url and not re.search(r"^https?://", args.profile_url):
        print("❌ Некорректный --profile-url", file=sys.stderr); sys.exit(2)
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nОстановлено пользователем."); rc = 130
    sys.exit(rc)

if __name__ == "__main__":
    main()
