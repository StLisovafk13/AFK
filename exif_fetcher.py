"""Utilities for extracting EXIF metadata from remote image URLs."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

__all__ = ["extract_exif_from_url"]

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
    timeout: Optional[int],
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
        "--http1.1",
    ]

    if timeout is not None:
        cmd.extend(["--max-time", str(timeout)])

    for header, value in headers.items():
        if header.lower() == "user-agent":
            cmd.extend(["-A", value])
        else:
            cmd.extend(["-H", f"{header}: {value}"])

    if referer:
        cmd.extend(["-e", referer])

    cmd.append(url)
    return cmd


def _download_with_python(
    url: str,
    *,
    referer: Optional[str],
    timeout: Optional[int],
    headers: Dict[str, str],
    out_path: str,
) -> None:
    request_headers = dict(headers)
    if referer and not any(key.lower() == "referer" for key in request_headers):
        request_headers["Referer"] = referer

    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout or None) as response, open(
            out_path, "wb"
        ) as file_obj:
            shutil.copyfileobj(response, file_obj)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network failure
        raise ExifExtractionError(f"HTTP error while downloading {url}: {exc.code}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network failure
        raise ExifExtractionError(f"Failed to download {url}: {exc.reason}") from exc


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


def extract_exif_from_url(
    url: str,
    *,
    referer: Optional[str] = "https://vsco.co/",
    timeout: Optional[int] = 20,
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
        Maximum time for the HTTP download, in seconds. Pass ``None`` to disable
        the limit for the Python fallback downloader.
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
    try:
        cmd = _build_curl_command(
            url,
            referer=referer,
            timeout=timeout,
            headers=combined_headers,
        )
    except ExifExtractionError:
        cmd = None

    if cmd:
        cmd.extend(["-o", temp_path])
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as exc:
            raise ExifExtractionError(f"curl failed for {url}: {exc}") from exc
    else:
        _download_with_python(
            url,
            referer=referer,
            timeout=timeout,
            headers=combined_headers,
            out_path=temp_path,
        )

    try:
        size_bytes = os.path.getsize(temp_path)
        meta: Dict[str, Any] = {"size_bytes": size_bytes}
        exif = _call_exiftool(temp_path)
        if exif:
            meta["exiftool"] = exif
        return meta
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
