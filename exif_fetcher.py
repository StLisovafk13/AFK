"""Utilities for extracting EXIF metadata from remote image URLs."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["extract_exif_from_url", "extract_exif_with_playwright"]

_LOGGER = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.8",
    "User-Agent": "Mozilla/5.0",
}


class ExifExtractionError(RuntimeError):
    """Raised when EXIF data cannot be extracted."""


def _build_curl_command(
    url: str,
    *,
    referer: Optional[str],
    timeout: int,
    headers: Dict[str, str],
) -> list[str]:
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        raise ExifExtractionError("curl binary is not available in PATH")

    cmd = [
        curl,
        "-sS",
        "--fail",
        "--location",
        "--max-time",
        str(timeout),
        "--http1.1",
    ]

    for header, value in headers.items():
        if header.lower() == "user-agent":
            cmd.extend(["-A", value])
        else:
            cmd.extend(["-H", f"{header}: {value}"])

    if referer:
        cmd.extend(["-e", referer])

    cmd.append(url)
    return cmd


def _call_exiftool(path: str) -> Optional[Dict[str, Any]]:
    exiftool = shutil.which("exiftool")
    if not exiftool:
        return None

    try:
        output = subprocess.check_output(
            [exiftool, "-j", "-a", "-u", "-g1", "-n", path],
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        _LOGGER.debug("exiftool failed for %s: %s", path, exc)
        return None

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        _LOGGER.debug("Failed to decode exiftool output for %s", path)
        return None

    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, dict):
            return first
    elif isinstance(parsed, dict):
        return parsed

    return None


def _extract_metadata_from_file(path: str) -> Dict[str, Any]:
    size_bytes = os.path.getsize(path)
    meta: Dict[str, Any] = {"size_bytes": size_bytes}
    exif = _call_exiftool(path)
    if exif:
        meta["exiftool"] = exif
    return meta


def extract_exif_from_url(
    url: str,
    *,
    referer: Optional[str] = "https://vsco.co/",
    timeout: int = 20,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Download an image and extract EXIF metadata.

    Parameters
    ----------
    url:
        Direct link to the image.
    referer:
        Optional HTTP referer to send when downloading the image.
    timeout:
        Maximum time for the HTTP download, in seconds.
    headers:
        Extra HTTP headers to merge with the defaults.

    Returns
    -------
    dict
        A dictionary with extracted metadata. Always contains the key
        ``size_bytes`` with the downloaded file size if the download
        succeeds. When ``exiftool`` is available, the dictionary also
        includes the key ``exiftool`` with the parsed metadata tree.
    """

    combined_headers = dict(_DEFAULT_HEADERS)
    if headers:
        combined_headers.update(headers)

    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    temp_path = tmp.name
    cmd = _build_curl_command(
        url,
        referer=referer,
        timeout=timeout,
        headers=combined_headers,
    )
    cmd.extend(["-o", temp_path])

    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        raise ExifExtractionError(f"curl failed for {url}: {exc}") from exc

    try:
        return _extract_metadata_from_file(temp_path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def extract_exif_with_playwright(
    url: str,
    *,
    referer: Optional[str] = "https://vsco.co/",
    timeout: int = 20,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Download an image using Playwright and extract EXIF metadata.

    This helper intentionally opens the referer page first to pick up any
    session cookies that the CDN might require. It mirrors the return shape of
    :func:`extract_exif_from_url`.
    """

    from playwright.sync_api import sync_playwright

    temp_file = tempfile.NamedTemporaryFile(delete=False)
    temp_file.close()
    temp_path = Path(temp_file.name)

    headers = {"Referer": referer} if referer else {}
    ua = user_agent or _DEFAULT_HEADERS.get("User-Agent") or "Mozilla/5.0"

    try:
        with sync_playwright() as pw:
            browser = pw.firefox.launch(headless=True)
            context = browser.new_context(user_agent=ua)
            try:
                if referer:
                    page = context.new_page()
                    try:
                        page.goto(
                            referer,
                            wait_until="domcontentloaded",
                            timeout=timeout * 1000,
                        )
                    except Exception:
                        # Even if the page fails to load completely we still try
                        # to reuse whatever cookies were set.
                        pass
                response = context.request.get(
                    url,
                    timeout=timeout * 1000,
                    headers=headers,
                )
                if not response.ok:
                    raise ExifExtractionError(
                        f"playwright failed for {url}: status={response.status}"
                    )
                content = response.body()
                temp_path.write_bytes(content)
            finally:
                context.close()
                browser.close()

        return _extract_metadata_from_file(str(temp_path))
    except ExifExtractionError:
        raise
    except Exception as exc:  # pragma: no cover - network/browser failures
        raise ExifExtractionError(f"playwright failed for {url}: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            temp_path.unlink()
