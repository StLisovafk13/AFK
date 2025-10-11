"""Default client properties stub."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class DefaultBotProperties:
    parse_mode: Optional[str] = None


__all__ = ["DefaultBotProperties"]
