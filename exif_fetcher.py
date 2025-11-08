"""Utilities for extracting EXIF metadata from remote image URLs."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from os import PathLike
from typing import Any, Dict, Optional, Union

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


def extract_exif_from_url(
    url: str,
    *,
    referer: Optional[str] = "https://vsco.co/",
    timeout: int = 20,
    headers: Optional[Dict[str, str]] = None,
    keep_file: bool = False,
    download_to: Optional[Union[str, PathLike[str]]] = None,
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

    keep_file:
        If ``True``, the downloaded file is not removed and the response
        contains the key ``download_path`` with its location. Defaults to
        ``False``.
    download_to:
        Optional path where the file should be stored. When provided the
        file is always kept and the path is returned via
        ``download_path`` regardless of ``keep_file``.

    Returns
    -------
    dict
        A dictionary with extracted metadata. Always contains the key
        ``size_bytes`` with the downloaded file size if the download
        succeeds. When ``exiftool`` is available, the dictionary also
        includes the key ``exiftool`` with the parsed metadata tree. When
        the downloaded file is preserved, the dictionary also includes
        ``download_path`` with the filesystem location of the file.
    """

    combined_headers = dict(_DEFAULT_HEADERS)
    if headers:
        combined_headers.update(headers)

    if download_to is not None:
        temp_path = os.fspath(download_to)
        os.makedirs(os.path.dirname(temp_path) or ".", exist_ok=True)
        remove_after = False
    else:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        temp_path = tmp.name
        remove_after = not keep_file
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
        with contextlib.suppress(OSError):
            os.remove(temp_path)
        raise ExifExtractionError(f"curl failed for {url}: {exc}") from exc

    try:
        size_bytes = os.path.getsize(temp_path)
        meta: Dict[str, Any] = {"size_bytes": size_bytes}
        exif = _call_exiftool(temp_path)
        if exif:
            meta["exiftool"] = exif
        if not remove_after:
            meta["download_path"] = temp_path
        return meta
    finally:
        if remove_after:
            with contextlib.suppress(OSError):
                os.remove(temp_path)
