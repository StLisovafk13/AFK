"""Exception stubs for aiogram."""
from __future__ import annotations


class TelegramError(Exception):
    pass


class TelegramBadRequest(TelegramError):
    pass


class TelegramNetworkError(TelegramError):
    def __init__(self, *, method: str | None = None, message: str | None = None) -> None:
        self.method = method
        self.message = message or ""
        super().__init__(self.message)


__all__ = ["TelegramError", "TelegramBadRequest", "TelegramNetworkError"]
