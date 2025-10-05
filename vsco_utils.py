"""Shared VSCO helpers."""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional, Sequence, Union, Any
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
_SITE_ID_RE_LIST = (
    re.compile(r'"site_id"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'data-site-id=["\'](\d+)["\']', re.IGNORECASE),
    re.compile(r'\bsiteId\s*:\s*(\d+)\b', re.IGNORECASE),
)

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


def extract_site_id_from_html(html: str) -> Optional[str]:
    """Extract a numeric ``site_id`` from VSCO profile HTML snippets."""

    if not html:
        return None
    for rx in _SITE_ID_RE_LIST:
        match = rx.search(html)
        if match:
            return match.group(1)
    return None


async def fetch_vsco_api_media_urls(
    session,
    site_id: str,
    *,
    max_width: int = 2048,
    max_items: int = 0,
    logger: Optional[logging.Logger] = None,
    request_kwargs: Optional[dict] = None,
) -> list[str]:
    """Fetch profile media via the public VSCO API using a ``site_id``."""

    if not site_id:
        return []

    urls: list[str] = []
    page = 1
    page_size = 100
    api_kwargs = {"allow_redirects": True}
    if request_kwargs:
        api_kwargs.update(request_kwargs)

    def push(candidate: Optional[str]) -> None:
        if not candidate:
            return
        normalized = normalize_media_url(candidate)
        if not normalized:
            return
        final_url = upscale_w_param(normalized, max_width)
        if is_media_url(final_url) and not is_vsco_logo_url(final_url):
            urls.append(final_url)

    while True:
        if max_items and len(urls) >= max_items:
            break

        api_url = f"https://vsco.co/api/2.0/medias?site_id={site_id}&page={page}&size={page_size}"
        try:
            async with session.get(api_url, **api_kwargs) as resp:
                if resp.status != 200:
                    if logger is not None:
                        logger.debug(
                            "fetch_vsco_api_media_urls: non-200 status=%s page=%s", resp.status, page
                        )
                    break
                data: Any = await resp.json(content_type=None)
        except Exception as exc:
            if logger is not None:
                logger.warning("fetch_vsco_api_media_urls: request failed for %s: %s", api_url, exc)
            break

        items = []
        if isinstance(data, dict):
            maybe_items: Any = data.get("medias") or data.get("media")
            if isinstance(maybe_items, list):
                items = maybe_items

        if not items:
            break

        for item in items:
            stack: list[Union[dict, list, str]] = [item] if isinstance(item, (dict, list)) else []
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    for value in current.values():
                        if isinstance(value, (dict, list)):
                            stack.append(value)
                        elif isinstance(value, str):
                            push(value)
                elif isinstance(current, list):
                    for value in current:
                        if isinstance(value, (dict, list)):
                            stack.append(value)
                        elif isinstance(value, str):
                            push(value)
            if isinstance(item, str):
                push(item)

        urls = dedupe_keep_order(urls)
        if max_items and len(urls) >= max_items:
            break
        if len(items) < page_size:
            break
        page += 1

    if max_items and len(urls) > max_items:
        urls = urls[:max_items]

    return urls


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


async def scan_profile_media(
    session,
    profile_url: str,
    *,
    max_width: int = 2048,
    limit: int = 0,
    logger: Optional[logging.Logger] = None,
    request_kwargs: Optional[dict] = None,
) -> list[str]:
    """Fetch a VSCO profile page and return direct media asset URLs.

    The helper performs a single HTTP GET (with optional ``request_kwargs``)
    and extracts media links via :func:`extract_media_urls_from_html`. If no
    direct ``img``/``video`` tags are present, a fallback scan through OG /
    Twitter / responsive meta tags is attempted.
    """

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
        return []

    urls = extract_media_urls_from_html(html, max_width=max_width, root=profile_url)

    fallback: list[str] = []
    if not urls:
        for candidate in extract_vsco_media_urls(html, sources=("og", "twitter", "responsive", "inline")):
            normalized = normalize_media_url(candidate, root=profile_url)
            if not normalized:
                continue
            final_url = upscale_w_param(normalized, max_width)
            if is_media_url(final_url) and not is_vsco_logo_url(final_url):
                fallback.append(final_url)
        urls = dedupe_keep_order(fallback)

    site_id = extract_site_id_from_html(html)
    if site_id:
        api_urls = await fetch_vsco_api_media_urls(
            session,
            site_id,
            max_width=max_width,
            max_items=limit,
            logger=logger,
            request_kwargs=request_kwargs,
        )
        if api_urls:
            combined = urls + [u for u in api_urls if u not in urls]
            urls = combined if combined else api_urls

    if limit and len(urls) > limit:
        urls = urls[:limit]

    return urls

