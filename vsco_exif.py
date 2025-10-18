"""Helpers for retrieving EXIF metadata from direct media links."""
from __future__ import annotations

import sys

from _vsco_exif_core import *  # noqa: F401,F403 - re-exported for public API
from _vsco_exif_core import __all__ as _CORE_ALL
from _vsco_exif_core import main as _core_main

__all__ = list(_CORE_ALL)

# Ensure ``main`` points to the implementation while remaining import-safe.
main = _core_main
del _CORE_ALL, _core_main

if __name__ == "__main__":  # pragma: no cover - manual invocation
    sys.exit(main())
