"""Minimal filters used in tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


class Command:
    def __init__(self, *commands: str) -> None:
        self.commands = commands


@dataclass
class CommandObject:
    args: Optional[str] = None


__all__ = ["Command", "CommandObject"]
