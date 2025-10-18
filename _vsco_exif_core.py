"""Internal implementation for retrieving EXIF metadata from direct media links."""
from __future__ import annotations

import argparse
import json
import sys
import io
import logging
import unicodedata
from pathlib import Path
from typing import Any, Sequence, Callable, Awaitable
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
    quote,
)

import asyncio
import threading

from PIL import Image, ExifTags


LOG = logging.getLogger("vsco_exif")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://vsco.co/",
}

PLAYWRIGHT_DEFAULT_HEADERS = {
    "Accept": "video/*;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

__all__ = [
    "DEFAULT_USER_AGENT",
    "DEFAULT_HEADERS",
    "PLAYWRIGHT_DEFAULT_HEADERS",
    "_merge_headers",
    "_merge_playwright_headers",
    "_normalise_vsco_cdn_url",
    "_normalise_request_url",
    "_run_coroutine",
    "_async_playwright_fetch",
    "_playwright_fetch",
    "fetch_image_bytes",
    "extract_exif_from_bytes",
    "get_exif_from_url",
    "configure_logging",
    "_parse_header",
    "_build_parser",
    "_normalise_cookie",
    "main",
    "urlopen",
]

_ASCII_CONFUSABLES = str.maketrans(
    {
        "Α": "A",
        "А": "A",
        "В": "B",
        "Е": "E",
        "Н": "H",
        "Κ": "K",
        "М": "M",
        "Ο": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Χ": "X",
        "а": "a",
        "е": "e",
        "н": "h",
        "к": "k",
        "м": "m",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "х": "x",
    }
)


def _ascii_header_name(name: str) -> str:
    """Return a header name guaranteed to be ASCII."""

    normalised = unicodedata.normalize("NFKC", name)
    translated = normalised.translate(_ASCII_CONFUSABLES)
    try:
        encoded = translated.encode("ascii")
    except UnicodeEncodeError as exc:  # pragma: no cover - defensive
        raise ValueError("Header names must contain only ASCII characters") from exc
    # Preserve the original casing for readability while ensuring ASCII
    # by mapping characters individually.
    return encoded.decode("ascii")


def _merge_headers(extra: dict[str, str] | None) -> dict[str, str]:
    """Return headers combined with defaults, allowing overrides."""

    headers = dict(DEFAULT_HEADERS)
    if extra:
        for key, value in extra.items():
            if not key:
                raise ValueError("Header names must be non-empty")
            headers[_ascii_header_name(key)] = value
    return headers


def _merge_playwright_headers(extra: dict[str, str] | None) -> dict[str, str]:
    """Merge headers for Playwright requests with sensible defaults."""

    merged = dict(DEFAULT_HEADERS)
    merged.update(PLAYWRIGHT_DEFAULT_HEADERS)
    if extra:
        for key, value in extra.items():
            merged[_ascii_header_name(key)] = value
    return merged


def _normalise_vsco_cdn_url(url: str) -> str:
    """Normalise alternate VSCO CDN hostnames to direct ``img.vsco.co`` links."""

    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    netloc = parts.netloc.lower()
    if netloc != "im.vsco.co":
        return url

    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and segments[0].startswith("aws-"):
        segments = segments[1:]
    if not segments:
        return url
    path = "/" + "/".join(segments)

    query_items = parse_qsl(parts.query, keep_blank_values=True)
    filtered_query = [(key, value) for key, value in query_items if key.lower() != "w"]
    query = urlencode(filtered_query, doseq=True) if filtered_query else ""

    normalised = urlunsplit((parts.scheme or "https", "img.vsco.co", path, query, parts.fragment))
    return normalised


def _normalise_request_url(url: str) -> str:
    """Percent-encode non-ASCII characters in URL components for requests."""

    if not url:
        return url

    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    path = parts.path
    if path and any(ord(ch) >= 128 for ch in path):
        encoded_path = quote(path, safe="/%:@!$&'()*+,;=")
        if encoded_path != path:
            LOG.debug("Percent-encoded non-ASCII characters in path: %s -> %s", path, encoded_path)
        path = encoded_path

    query = parts.query
    if query and any(ord(ch) >= 128 for ch in query):
        encoded_query = urlencode(parse_qsl(query, keep_blank_values=True), doseq=True)
        if encoded_query != query:
            LOG.debug("Percent-encoded non-ASCII characters in query: %s -> %s", query, encoded_query)
        query = encoded_query

    fragment = parts.fragment
    if fragment and any(ord(ch) >= 128 for ch in fragment):
        encoded_fragment = quote(fragment, safe="/%:@!$&'()*+,;=")
        if encoded_fragment != fragment:
            LOG.debug(
                "Percent-encoded non-ASCII characters in fragment: %s -> %s",
                fragment,
                encoded_fragment,
            )
        fragment = encoded_fragment

    return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))


def _run_coroutine(factory: Callable[[], Awaitable[bytes]]) -> bytes:
    """Run a coroutine factory even when an event loop is already running."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            result["value"] = new_loop.run_until_complete(factory())
        except BaseException as exc:  # pragma: no cover - exceptional path
            error["exc"] = exc
        finally:
            asyncio.set_event_loop(None)
            new_loop.close()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()

    if error:
        raise error["exc"]
    return result["value"]


async def _async_playwright_fetch(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None,
) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - optional dependency missing
        raise RuntimeError("Playwright is not installed. Run 'pip install playwright' and 'playwright install'.") from exc

    merged_headers = _merge_playwright_headers(headers)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        try:
            response = await context.request.get(
                url,
                headers=merged_headers,
                timeout=int(max(timeout, 0) * 1000),
            )
            status = getattr(response, "status", None)
            status_text = getattr(response, "status_text", "")
            if not getattr(response, "ok", False) or (status is not None and status >= 400):
                raise HTTPError(url, status or 0, status_text or "Forbidden", hdrs=None, fp=None)
            body = await response.body()
            if not body:
                raise ValueError(f"No data returned when fetching image bytes from {url}")
            return body
        finally:
            await context.close()
            await browser.close()


def _playwright_fetch(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None,
) -> bytes:
    """Synchronously fetch bytes using Playwright."""

    return _run_coroutine(lambda: _async_playwright_fetch(url, timeout=timeout, headers=headers))


def fetch_image_bytes(
    url: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    use_playwright: bool = False,
    playwright_timeout: float | None = None,
) -> bytes:
    """Return the raw bytes from an image URL.

    A VSCO-style user agent is supplied to avoid CDN blocks.
    """

    if not url:
        raise ValueError("URL is required to download image bytes")
    normalised_url = _normalise_request_url(_normalise_vsco_cdn_url(url))
    if normalised_url != url:
        LOG.debug("Normalised request URL %s -> %s", url, normalised_url)
    request_headers = _merge_headers(headers)
    request = Request(normalised_url, headers=request_headers)
    LOG.debug("Fetching image bytes from %s", url)
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec: B310 - validated URL
            data = response.read()
    except HTTPError as exc:  # pragma: no cover - network edge cases mocked in tests
        if exc.code == 403:
            if use_playwright:
                LOG.info("Primary request forbidden, attempting Playwright fallback")
                try:
                    return _playwright_fetch(
                        normalised_url,
                        timeout=playwright_timeout or timeout,
                        headers=request_headers,
                    )
                except Exception as play_exc:
                    LOG.error("Playwright fallback failed: %s", play_exc, exc_info=True)
                    raise PermissionError(
                        "Access to the image was forbidden (HTTP 403). Playwright fallback failed: "
                        f"{play_exc}"
                    ) from play_exc
            raise PermissionError(
                "Access to the image was forbidden (HTTP 403). VSCO may require "
                "authenticated access for this link."
            ) from exc
        raise
    if not data:
        raise ValueError(f"No data returned when fetching image bytes from {url}")
    return data


def extract_exif_from_bytes(data: bytes) -> dict[str, Any]:
    """Return a dictionary of EXIF tags decoded from raw image bytes."""

    if not data:
        return {}
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif_data = image.getexif()
            if not exif_data:
                return {}
            result: dict[str, Any] = {}
            for tag_id, value in exif_data.items():
                tag_name = ExifTags.TAGS.get(tag_id, f"Tag_{tag_id}")
                if isinstance(value, bytes):
                    try:
                        value = value.decode("utf-8", "replace").rstrip("\x00")
                    except Exception:  # pragma: no cover - defensive
                        value = value.hex()
                result[tag_name] = value
            return result
    except Exception as exc:  # pragma: no cover - Pillow raises specific errors
        LOG.warning("Failed to extract EXIF metadata: %s", exc, exc_info=True)
        return {}


def get_exif_from_url(
    url: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    use_playwright: bool = False,
    playwright_timeout: float | None = None,
) -> dict[str, Any]:
    """Download an image and return its EXIF metadata."""

    data = fetch_image_bytes(
        url,
        timeout=timeout,
        headers=headers,
        use_playwright=use_playwright,
        playwright_timeout=playwright_timeout,
    )
    exif = extract_exif_from_bytes(data)
    LOG.debug("Extracted %d EXIF tags from %s", len(exif), url)
    return exif


def configure_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure application-wide logging.

    Parameters
    ----------
    level:
        Logging level name (e.g. ``"INFO"`` or ``"DEBUG"``).
    log_file:
        Optional path to a file where log messages will be duplicated.
    """

    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level}")

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def _parse_header(argument: str) -> tuple[str, str]:
    """Parse a ``KEY:VALUE`` header CLI argument."""

    if ":" not in argument:
        raise argparse.ArgumentTypeError(
            "Headers must use the format 'Name: Value'"
        )
    name, value = argument.split(":", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("Header name cannot be empty")
    try:
        name = _ascii_header_name(name)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    value = value.strip()
    return name, value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download an image by direct URL and print its EXIF metadata as JSON",
    )
    parser.add_argument("url", help="Direct link to an image (e.g. VSCO CDN URL)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout for the download request in seconds (default: 10)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Format JSON output with indentation for readability",
    )
    parser.add_argument(
        "--output",
        help="Optional path to save the EXIF JSON response",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging verbosity (e.g. INFO, DEBUG, WARNING)",
    )
    parser.add_argument(
        "--log-file",
        help="Write logs to the specified file in addition to the console",
    )
    parser.add_argument(
        "--header",
        action="append",
        type=_parse_header,
        metavar="NAME:VALUE",
        help=(
            "Additional request headers to send when downloading the image. "
            "May be provided multiple times."
        ),
    )
    parser.add_argument(
        "--cookie",
        help=(
            "Convenience shortcut for supplying a Cookie header. "
            "Equivalent to --header 'Cookie: <value>'."
        ),
    )
    parser.add_argument(
        "--playwright",
        action="store_true",
        help=(
            "Attempt a Playwright-powered fallback when the CDN returns 403 Forbidden. "
            "Requires 'playwright' to be installed."
        ),
    )
    parser.add_argument(
        "--playwright-timeout",
        type=float,
        help=(
            "Custom timeout (seconds) for Playwright fallback requests. "
            "Defaults to the --timeout value."
        ),
    )
    return parser


def _normalise_cookie(cookie_value: str) -> str:
    """Return a cookie string cleaned for HTTP header usage."""

    if cookie_value is None:
        raise ValueError("Cookie value cannot be None")

    cleaned = cookie_value.strip()
    if not cleaned:
        raise ValueError("Cookie header cannot be empty")

    normalised = unicodedata.normalize("NFKC", cleaned)
    flattened = normalised.replace("\r", " ").replace("\n", " ")
    flattened = " ".join(flattened.split())

    colon_index = flattened.find(":")
    if colon_index != -1:
        prefix = flattened[:colon_index]
        try:
            ascii_prefix = _ascii_header_name(prefix.strip())
        except ValueError:
            ascii_prefix = ""
        if ascii_prefix.lower() == "cookie":
            flattened = flattened[colon_index + 1 :].lstrip()
    else:
        first, _, rest = flattened.partition(" ")
        if rest:
            try:
                ascii_prefix = _ascii_header_name(first.strip())
            except ValueError:
                ascii_prefix = ""
            if ascii_prefix.lower() == "cookie":
                flattened = rest.lstrip()

    if not flattened:
        raise ValueError("Cookie header cannot be empty")

    return flattened


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for fetching EXIF metadata from a direct URL."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        configure_logging(args.log_level, args.log_file)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Error configuring logging: {exc}", file=sys.stderr)
        return 1

    LOG.info("Starting EXIF extraction from %s", args.url)

    request_headers: dict[str, str] = {}
    if args.header:
        request_headers.update(dict(args.header))
    if args.cookie:
        try:
            request_headers["Cookie"] = _normalise_cookie(args.cookie)
        except Exception as exc:
            LOG.error("Invalid cookie string supplied: %s", exc, exc_info=True)
            print(f"Error parsing cookie value: {exc}", file=sys.stderr)
            return 1

    try:
        exif = get_exif_from_url(
            args.url,
            timeout=args.timeout,
            headers=request_headers or None,
            use_playwright=args.playwright,
            playwright_timeout=args.playwright_timeout,
        )
    except Exception as exc:  # pragma: no cover - defensive, logged and reported
        LOG.error("Failed to retrieve EXIF data: %s", exc, exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    json_kwargs = {"ensure_ascii": False}
    if args.pretty:
        json_kwargs.update(indent=2, sort_keys=True)
    json_payload = json.dumps(exif, **json_kwargs)

    if args.output:
        try:
            output_path = Path(args.output)
            output_path.write_text(json_payload + "\n", encoding="utf-8")
            LOG.info("EXIF metadata saved to %s", output_path)
        except Exception as exc:
            LOG.error("Failed to write EXIF JSON to %s: %s", args.output, exc, exc_info=True)
            print(f"Error writing JSON output: {exc}", file=sys.stderr)
            return 1

    print(json_payload)
    return 0


