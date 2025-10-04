"""Shared VSCO helpers."""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence
from urllib.parse import urljoin, urlparse, urlunparse


VSCO_LOGO_MARKERS = ("vsco-logo-white",)

# --- Domains & regexes shared between the bot and the ZIP helper ---
VSCO_HOSTS = {"vsco.co", "www.vsco.co"}
VSCO_SHORT_HOSTS = {"vs.co", "www.vs.co"}
VSCO_PERCEPTION_HOSTS = {"perception.vsco.co", "www.perception.vsco.co"}

SHORT_SLUG_RE = re.compile(r"^/([A-Za-z0-9]+)(?:/.*)?$")

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

