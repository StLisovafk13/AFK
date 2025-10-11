"""Helpers for formatting added_by metadata."""

from __future__ import annotations

import re
from html import escape
from typing import Optional, Tuple

TG_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

__all__ = ["TG_USERNAME_RE", "added_by_display_and_link", "added_by_html"]

def added_by_display_and_link(value: str) -> Tuple[str, Optional[str]]:
    raw = (value or "").strip()
    if not raw:
        return "", None
    handle = raw[1:] if raw.startswith("@") else raw
    if TG_USERNAME_RE.match(handle):
        return f"@{handle}", f"https://t.me/{handle}"
    return raw, None


def added_by_html(value: str) -> str:
    display, link = added_by_display_and_link(value)
    if not display:
        return ""
    if link:
        return f"<a href=\"{escape(link)}\">{escape(display)}</a>"
    return escape(display)

