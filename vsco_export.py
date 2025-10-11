"""Compatibility wrapper for VSCO export helpers."""
from __future__ import annotations

from typing import TYPE_CHECKING

from features.export.manager import ExportDependencies, ExportManager

__all__ = ("ExportDependencies", "ExportManager")

if TYPE_CHECKING:  # pragma: no cover - keep type checkers aware of the public API
    # Re-export for static type checkers without triggering runtime side effects.
    from features.export.manager import SessionLike
