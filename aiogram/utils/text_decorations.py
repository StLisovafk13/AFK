"""Text decoration helpers used in tests."""
from __future__ import annotations


def add_surrogates(text: str) -> str:
    """Encode text to a UTF-16 surrogate representation."""

    return text.encode("utf-16-le", "surrogatepass").decode("latin-1")


def remove_surrogates(text: str) -> str:
    """Restore original text from surrogate representation."""

    return text.encode("latin-1", "surrogatepass").decode("utf-16-le", "surrogatepass")


__all__ = ["add_surrogates", "remove_surrogates"]
