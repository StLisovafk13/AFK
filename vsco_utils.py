"""Shared VSCO helpers."""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunparse,
    urlunsplit,
)

try:  # pragma: no cover - optional dependency
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    BeautifulSoup = None  # type: ignore


VSCO_LOGO_MARKERS = ("vsco-logo-white",)
PROFILE_TAB_ID_RE = re.compile(r"^profileTab-([A-Za-z0-9_-]+)$")

# --- Domains & regexes shared between the bot and the ZIP helper ---
VSCO_HOSTS = {"vsco.co", "www.vsco.co"}
VSCO_SHORT_HOSTS = {"vs.co", "www.vs.co"}
VSCO_PERCEPTION_HOSTS = {"perception.vsco.co", "www.perception.vsco.co"}

SHORT_SLUG_RE = re.compile(r"^/([A-Za-z0-9]+)(?:/.*)?$")

MEDIA_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp|mp4|webm|mov)(\?|$)", re.IGNORECASE)

OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TWITTER_IMAGE_RE = re.compile(
    r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
RESPONSIVE_URL_RE = re.compile(r'"responsive_url"\s*:\s*"([^"]+)"', re.IGNORECASE)
INLINE_IMG_RE = re.compile(
    r'https://[^"\']+\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?',
    re.IGNORECASE,
)


def is_vsco_logo_url(url: Optional[str]) -> bool:
    """Return ``True`` when the URL points to a known VSCO logo asset."""

    if not url:
        return False
    low = url.lower()
    return any(marker in low for marker in VSCO_LOGO_MARKERS)


# --- Media helpers --------------------------------------------------------
def select_best_from_srcset(srcset: str) -> Optional[str]:
    """Return the highest-resolution candidate from an HTML ``srcset`` string."""

    try:
        candidates = []
        for chunk in srcset.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if " " in chunk:
                url_part, size_part = chunk.rsplit(" ", 1)
                try:
                    width = int(size_part.rstrip("w")) if size_part.endswith("w") else int(size_part)
                except ValueError:
                    width = 0
            else:
                url_part, width = chunk, 0
            candidates.append((width, url_part))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]
    except Exception:
        return None


def normalize_media_url(url: Optional[str], *, root: str = "https://vsco.co/") -> Optional[str]:
    """Normalise VSCO CDN/relative URLs to absolute HTTPS links."""

    if not url:
        return None
    candidate = url.strip()
    if candidate.startswith("//"):
        return "https:" + candidate
    if candidate.startswith("/"):
        return urljoin(root, candidate)
    return candidate


def is_media_url(url: str) -> bool:
    """Return ``True`` when the link looks like an image/video asset."""

    if not url:
        return False
    return bool(MEDIA_EXT_RE.search(url))


def upscale_w_param(url: str, max_width: int) -> str:
    """Bump VSCO ``?w=`` query parameters up to ``max_width`` for images."""

    if not url or not MEDIA_EXT_RE.search(url):
        return url
    if re.search(r"\.(mp4|webm|mov)(\?|$)", url, re.IGNORECASE):
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "w" in query:
        try:
            current = int(query["w"])
        except (ValueError, TypeError):
            return url
        if current < max_width:
            query["w"] = str(max_width)
            new_query = urlencode(query, doseq=True)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return url


def dedupe_keep_order(items: Iterable[str]) -> list[str]:
    """Return unique items preserving the first-seen order."""

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def generate_media_filename(url: str, idx: int, *, default_ext: str = "jpg") -> str:
    """Derive a collision-resistant filename for VSCO CDN URLs."""

    path_parts = urlsplit(url).path.rstrip("/").split("/")
    base = path_parts[-1] if path_parts else ""
    if base and "." in base:
        if len(path_parts) >= 2 and path_parts[-2]:
            return f"{path_parts[-2]}_{base}"
        return base
    path = urlsplit(url).path.lower()
    if path.endswith(".mp4"):
        return f"vsco_{idx:05d}.mp4"
    if path.endswith(".webm"):
        return f"vsco_{idx:05d}.webm"
    if path.endswith(".mov"):
        return f"vsco_{idx:05d}.mov"
    return f"vsco_{idx:05d}.{default_ext.strip('.')}"
# --- URL helpers ---------------------------------------------------------
def build_perception_gallery_url(slug: str) -> str:
    return f"https://perception.vsco.co/{slug}/gallery"


def vsco_short_slug(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if host not in VSCO_SHORT_HOSTS:
        return None
    match = SHORT_SLUG_RE.match(parsed.path or "")
    if match:
        return match.group(1)
    return None


def normalize_vsco_profile_url(url: Optional[str]) -> Optional[str]:
    """Normalise VSCO links to ``https://vsco.co/<username>/gallery``."""

    if not url:
        return None
    candidate = url.strip()
    if candidate.startswith("http://"):
        candidate = "https://" + candidate[len("http://") :]

    try:
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    path_segments = [segment for segment in (parsed.path or "").split("/") if segment]

    if host in VSCO_HOSTS:
        if not path_segments:
            return None
        username = path_segments[0]
        new_path = f"/{username}/gallery"
        return urlunparse(("https", "vsco.co", new_path, "", "", ""))

    if host in VSCO_SHORT_HOSTS:
        # defer resolution to the short-link resolver
        return candidate

    if host in VSCO_PERCEPTION_HOSTS and path_segments:
        slug = path_segments[0]
        return build_perception_gallery_url(slug)

    return None


async def resolve_vsco_short_link(
    url: str,
    session,
    *,
    headers: Optional[dict] = None,
    max_hops: int = 5,
    request_kwargs: Optional[dict] = None,
) -> str:
    """Follow redirects for ``vs.co`` short-links and return a best-effort URL."""

    slug = vsco_short_slug(url)
    current = url
    kwargs = {"allow_redirects": False}
    if headers:
        kwargs["headers"] = headers
    if request_kwargs:
        kwargs.update(request_kwargs)

    try:
        for _ in range(max_hops):
            async with session.get(current, **kwargs) as resp:
                final_url = str(resp.url)
                status = resp.status
                if status in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("Location")
                    if not location:
                        break
                    next_url = urljoin(final_url, location)
                    try:
                        parsed = urlparse(next_url)
                    except Exception:
                        parsed = None
                    host = (parsed.netloc or "").lower() if parsed else ""
                    if slug and (host in VSCO_PERCEPTION_HOSTS or host == "apps.apple.com"):
                        return build_perception_gallery_url(slug)
                    current = next_url
                    continue

                resp_host = (resp.url.host or "").lower() if resp.url else ""
                if slug and (resp_host in VSCO_PERCEPTION_HOSTS or resp_host == "apps.apple.com"):
                    return build_perception_gallery_url(slug)
                if slug and resp_host in VSCO_SHORT_HOSTS:
                    break
                return final_url
    except Exception:
        pass

    if slug:
        return build_perception_gallery_url(slug)
    return url


# --- HTML helpers --------------------------------------------------------
def extract_vsco_media_urls(
    html: str,
    *,
    sources: Sequence[str] = ("og", "twitter", "responsive", "inline"),
) -> list[str]:
    """Collect VSCO media URLs from HTML in the provided priority order."""

    order: Iterable[str] = sources or ()
    urls: list[str] = []
    for source in order:
        if source == "og":
            urls.extend(match.group(1) for match in OG_IMAGE_RE.finditer(html))
        elif source == "twitter":
            urls.extend(match.group(1) for match in TWITTER_IMAGE_RE.finditer(html))
        elif source == "responsive":
            urls.extend(match.group(1) for match in RESPONSIVE_URL_RE.finditer(html))
        elif source == "inline":
            urls.extend(INLINE_IMG_RE.findall(html))
    return urls


def extract_media_urls_from_html(
    html: str,
    *,
    max_width: int,
    root: Optional[str] = None,
) -> list[str]:
    """Collect direct media URLs from a VSCO HTML snippet.

    The function prefers high-resolution candidates, applies ``?w=`` upscaling
    and filters out known VSCO logo assets. Relative URLs are resolved against
    ``root`` when provided.
    """

    if not html or not BeautifulSoup:
        return []

    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    base_root = root or "https://vsco.co/"

    def push(candidate: Optional[str]):
        if not candidate:
            return
        normalized = normalize_media_url(candidate, root=base_root)
        if not normalized or not is_media_url(normalized):
            return
        final_url = upscale_w_param(normalized, max_width)
        if not is_vsco_logo_url(final_url):
            urls.append(final_url)

    for img in soup.find_all("img"):
        srcset = img.get("srcset")
        candidate = select_best_from_srcset(srcset) if srcset else None
        if not candidate:
            candidate = img.get("src")
        push(candidate)

    for picture in soup.find_all("picture"):
        for source in picture.find_all("source"):
            candidate = select_best_from_srcset(source.get("srcset") or "")
            push(candidate or source.get("src"))

    for video in soup.find_all("video"):
        push(video.get("src"))
        for source in video.find_all("source"):
            push(source.get("src"))
            srcset = source.get("srcset")
            if srcset:
                push(select_best_from_srcset(srcset))

    return dedupe_keep_order(urls)


def extract_profile_tab_links(html: str, *, root: Optional[str] = None) -> list[dict[str, str]]:
    """Return structured navigation tab links from a VSCO profile page."""

    if not html or not BeautifulSoup:
        return []

    soup = BeautifulSoup(html, "html.parser")
    tabs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    base_root = root or "https://vsco.co/"

    for anchor in soup.select("a[id^='profileTab-'][href]"):
        href_raw = anchor.get("href") or ""
        if not href_raw:
            continue
        href_abs = urljoin(base_root, href_raw)
        tab_id = (anchor.get("id") or "").strip()
        key = (tab_id.lower(), href_abs)
        if key in seen:
            continue
        seen.add(key)

        label = (anchor.get_text(strip=True) or "").strip()
        slug_match = PROFILE_TAB_ID_RE.match(tab_id)
        slug = slug_match.group(1) if slug_match else ""

        entry: dict[str, str] = {
            "id": tab_id,
            "href": href_abs,
        }
        if label:
            entry["label"] = label
        elif slug:
            entry["label"] = slug

        if slug:
            entry["slug"] = slug

        aria_current = (anchor.get("aria-current") or "").strip()
        if aria_current:
            entry["active"] = aria_current

        tabs.append(entry)

    return tabs


async def scan_profile_media(
    session,
    profile_url: str,
    *,
    max_width: int = 2048,
    limit: int = 0,
    logger: Optional[logging.Logger] = None,
    request_kwargs: Optional[dict] = None,
) -> tuple[list[str], str]:
    """Fetch a VSCO profile page and return media URLs along with HTML."""

    html = ""
    kwargs = {"allow_redirects": True}
    if request_kwargs:
        kwargs.update(request_kwargs)

    try:
        async with session.get(profile_url, **kwargs) as resp:
            html = await resp.text(errors="ignore")
    except Exception as exc:
        if logger is not None:
            logger.warning("scan_profile_media: fetch failed for %s: %s", profile_url, exc)
        return [], html

    urls = extract_media_urls_from_html(html, max_width=max_width, root=profile_url)

    if not urls:
        fallback: list[str] = []
        for candidate in extract_vsco_media_urls(html, sources=("og", "twitter", "responsive", "inline")):
            normalized = normalize_media_url(candidate, root=profile_url)
            if not normalized:
                continue
            final_url = upscale_w_param(normalized, max_width)
            if is_media_url(final_url) and not is_vsco_logo_url(final_url):
                fallback.append(final_url)
        urls = dedupe_keep_order(fallback)

    if limit and len(urls) > limit:
        urls = urls[:limit]

    return urls, html


async def playwright_scan_profile(
    profile_url: str,
    *,
    max_width: int = 2048,
    session: Any = None,
    logger: Optional[logging.Logger] = None,
    delay: float = 0.4,
    target_count: int = 0,
) -> list[str]:
    """Extract profile media via Playwright with graceful HTTP fallback."""

    try:
        from playwright.async_api import async_playwright
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except Exception as exc:  # pragma: no cover - optional dependency
        if logger is not None:
            logger.info(
                "playwright_scan_profile: Playwright unavailable, falling back: %s",
                exc,
            )
        if session is not None:
            urls, _html = await scan_profile_media(
                session,
                profile_url,
                max_width=max_width,
                logger=logger,
            )
            return urls
        return []

    gallery_url = profile_url.rstrip("/")
    if not gallery_url.endswith("/gallery"):
        gallery_url = f"{gallery_url}/gallery"

    browser = context = page = None

    def _log(level: str, message: str, *args: object) -> None:
        if logger is None:
            return
        log_method = getattr(logger, level, None)
        if log_method is not None:
            log_method(message, *args)

    _log("debug", "playwright_scan_profile: preparing to open %s", gallery_url)

    async def _extract_current_urls() -> list[str]:
        html = await page.content()
        root = getattr(page, "url", None) or gallery_url
        return extract_media_urls_from_html(html, max_width=max_width, root=root)

    try:
        async with async_playwright() as playwright:
            _log("debug", "playwright_scan_profile: launching Firefox")
            browser = await playwright.firefox.launch(headless=True)
            _log("debug", "playwright_scan_profile: creating new browser context")
            context = await browser.new_context()
            _log("debug", "playwright_scan_profile: creating new page")
            page = await context.new_page()
            _log("info", "playwright_scan_profile: navigating to %s", gallery_url)
            try:
                await page.goto(
                    gallery_url,
                    wait_until="networkidle",
                    timeout=60000,
                )
            except PlaywrightTimeoutError as exc:
                _log(
                    "warning",
                    "playwright_scan_profile: networkidle timeout, continuing with DOMContentLoaded: %s",
                    exc,
                )
                try:
                    await page.wait_for_load_state("domcontentloaded")
                except Exception as wait_exc:
                    _log(
                        "debug",
                        "playwright_scan_profile: waiting for DOMContentLoaded failed: %s",
                        wait_exc,
                    )
            _log(
                "debug",
                "playwright_scan_profile: navigation finished, current URL %s",
                getattr(page, "url", None),
            )

            urls = dedupe_keep_order(await _extract_current_urls())
            if logger is not None and urls:
                logger.info("playwright_scan_profile: initial %d asset(s)", len(urls))

            max_scrolls = 120
            stagnation_limit = 5
            no_growth_click_limit = 3
            max_clicks = 500
            load_clicks = 0
            stagnation = 0
            prev_count = len(urls)

            for scroll_idx in range(max_scrolls):
                btn = page.locator("#loadMore-Button").first
                try:
                    btn_count = await btn.count()
                    btn_exists = btn_count > 0
                    btn_visible = btn_exists and await btn.is_visible()
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

                _log(
                    "debug",
                    (
                        "playwright_scan_profile: scroll %d - btn_exists=%s "
                        "btn_visible=%s btn_disabled=%s load_clicks=%d"
                    ),
                    scroll_idx,
                    btn_exists,
                    btn_visible,
                    btn_disabled,
                    load_clicks,
                )

                clicked = False
                if (
                    btn_exists
                    and btn_visible
                    and not btn_disabled
                    and load_clicks < max_clicks
                ):
                    try:
                        await btn.scroll_into_view_if_needed()
                        _log(
                            "debug",
                            "playwright_scan_profile: button scrolled into view",
                        )
                    except Exception:
                        pass
                    try:
                        _log(
                            "debug",
                            "playwright_scan_profile: attempting to click load more",
                        )
                        await btn.click()
                        load_clicks += 1
                        clicked = True
                        _log(
                            "debug",
                            "playwright_scan_profile: load more clicked (%d)",
                            load_clicks,
                        )
                        try:
                            await page.wait_for_load_state("networkidle", timeout=2000)
                            _log(
                                "debug",
                                "playwright_scan_profile: network idle after click",
                            )
                        except Exception:
                            await page.wait_for_timeout(int(max(0.1, delay) * 1000))
                            _log(
                                "debug",
                                "playwright_scan_profile: network idle timeout, "
                                "waited for %.2f seconds",
                                max(0.1, delay),
                            )
                    except Exception as exc:
                        _log(
                            "warning",
                            "playwright_scan_profile: load more click failed: %s",
                            exc,
                        )

                await page.evaluate(
                    "() => { window.scrollBy(0, Math.floor(window.innerHeight * 0.9)); }"
                )
                await page.wait_for_timeout(int(max(0.1, delay) * 1000))
                _log(
                    "debug",
                    "playwright_scan_profile: performed scroll %d and waited %.2f seconds",
                    scroll_idx,
                    max(0.1, delay),
                )

                extracted = dedupe_keep_order(await _extract_current_urls())
                combined = dedupe_keep_order(urls + extracted)
                new_count = len(combined)
                if logger is not None and new_count > prev_count:
                    logger.info("playwright_scan_profile: progress %d", new_count)
                elif logger is not None:
                    logger.debug(
                        "playwright_scan_profile: no new assets after scroll %d (total %d)",
                        scroll_idx,
                        new_count,
                    )
                urls = combined

                if target_count and new_count >= target_count:
                    _log(
                        "info",
                        "playwright_scan_profile: reached target %d assets",
                        target_count,
                    )
                    break

                if new_count > prev_count:
                    stagnation = 0
                    prev_count = new_count
                    continue

                stagnation += 1
                if (not btn_exists or not btn_visible or btn_disabled) and stagnation >= stagnation_limit:
                    break
                if clicked and stagnation >= no_growth_click_limit:
                    _log(
                        "debug",
                        "playwright_scan_profile: stopping after %d scrolls due to "
                        "no growth post-click",
                        scroll_idx + 1,
                    )
                    break
                if not clicked and stagnation >= stagnation_limit:
                    _log(
                        "debug",
                        "playwright_scan_profile: stopping after %d scrolls due to "
                        "stagnation",
                        scroll_idx + 1,
                    )
                    break

            return urls
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "playwright_scan_profile: failed to scan via Playwright, falling back: %s",
                exc,
            )
    finally:
        for handle in (page, context, browser):
            if handle is None:
                continue
            _log(
                "debug",
                "playwright_scan_profile: closing %s",
                handle.__class__.__name__,
            )
            try:
                await handle.close()  # type: ignore[func-returns-value]
            except Exception:  # pragma: no cover - cleanup best-effort
                pass

    if session is not None:
        urls, _html = await scan_profile_media(
            session,
            gallery_url,
            max_width=max_width,
            logger=logger,
        )
        return urls
    return []

