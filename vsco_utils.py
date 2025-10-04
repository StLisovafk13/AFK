"""Shared VSCO helpers."""
from __future__ import annotations

from typing import Optional

VSCO_LOGO_MARKERS = ("vsco-logo-white",)


def is_vsco_logo_url(url: Optional[str]) -> bool:
    """Return True when the URL points to a known VSCO logo asset."""
    if not url:
        return False
    low = url.lower()
    return any(marker in low for marker in VSCO_LOGO_MARKERS)
