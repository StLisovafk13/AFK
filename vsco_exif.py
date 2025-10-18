"""Helpers for retrieving EXIF metadata from direct media links."""
from __future__ import annotations

import argparse
import json
import sys
import io
import logging
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image, ExifTags


LOG = logging.getLogger(__name__)

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


def _merge_headers(extra: dict[str, str] | None) -> dict[str, str]:
    """Return headers combined with defaults, allowing overrides."""

    headers = dict(DEFAULT_HEADERS)
    if extra:
        for key, value in extra.items():
            if not key:
                raise ValueError("Header names must be non-empty")
            headers[key] = value
    return headers


def fetch_image_bytes(
    url: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Return the raw bytes from an image URL.

    A VSCO-style user agent is supplied to avoid CDN blocks.
    """

    if not url:
        raise ValueError("URL is required to download image bytes")
    request = Request(url, headers=_merge_headers(headers))
    LOG.debug("Fetching image bytes from %s", url)
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec: B310 - validated URL
            data = response.read()
    except HTTPError as exc:  # pragma: no cover - network edge cases mocked in tests
        if exc.code == 403:
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
) -> dict[str, Any]:
    """Download an image and return its EXIF metadata."""

    data = fetch_image_bytes(url, timeout=timeout, headers=headers)
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
    value = value.strip()
    if not name:
        raise argparse.ArgumentTypeError("Header name cannot be empty")
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
    return parser


def _normalise_cookie(cookie_value: str) -> str:
    """Return a cookie string cleaned for HTTP header usage."""

    if cookie_value is None:
        raise ValueError("Cookie value cannot be None")

    cleaned = cookie_value.strip()
    lower_cleaned = cleaned.lower()

    if lower_cleaned.startswith("cookie"):
        remainder = cleaned[6:]
        if remainder[:1] in {":", " ", "\t", "\r", "\n"}:
            if remainder.startswith(":"):
                cleaned = remainder[1:].lstrip()
            else:
                cleaned = remainder.lstrip()

    cleaned = cleaned.replace("\r", " ")
    cleaned = " ".join(cleaned.replace("\n", " ").split())

    if not cleaned:
        raise ValueError("Cookie header cannot be empty")

    return cleaned


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


if __name__ == "__main__":  # pragma: no cover - manual invocation
    sys.exit(main())
