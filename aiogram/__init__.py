"""Lightweight test stub for the aiogram package."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional


class _SessionStub:
    async def close(self) -> None:  # pragma: no cover - trivial
        return None


class Bot:
    """Minimal Bot stub used by tests."""

    def __init__(self, token: str, *, default: Any | None = None) -> None:
        self.token = token
        self.default = default
        self.session = _SessionStub()

    async def get_chat_member(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def send_document(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def delete_webhook(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def download(self, *args: Any, **kwargs: Any) -> Any:
        return None


class _HandlerRegistry:
    def __init__(self) -> None:
        self.handlers: list[Callable[..., Any]] = []

    def register(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        self.handlers.append(handler)
        return handler


class Dispatcher:
    """Simplified dispatcher which only stores handlers."""

    def __init__(self) -> None:
        self.message_handlers = _HandlerRegistry()
        self.callback_query_handlers = _HandlerRegistry()
        self.routers: list[Router] = []

    def message(self, *filters: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            return self.message_handlers.register(handler)

        return decorator

    def callback_query(self, *filters: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            return self.callback_query_handlers.register(handler)

        return decorator

    async def start_polling(self, *args: Any, **kwargs: Any) -> None:
        return None

    def include_router(self, router: "Router") -> None:
        self.routers.append(router)


class Router:
    """Router stub mirroring dispatcher behaviour."""

    def __init__(self, *, name: str | None = None) -> None:
        self.name = name
        self.message_handlers = _HandlerRegistry()
        self.callback_query_handlers = _HandlerRegistry()
        self.children: list["Router"] = []

    def message(self, *filters: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            return self.message_handlers.register(handler)

        return decorator

    def callback_query(self, *filters: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            return self.callback_query_handlers.register(handler)

        return decorator

    def include_router(self, router: "Router") -> None:
        self.children.append(router)


class _FilterExpression:
    def __init__(self, path: tuple[str, ...] = ()) -> None:
        self.path = path

    def __getattr__(self, item: str) -> "_FilterExpression":
        return _FilterExpression(self.path + (item,))

    def __call__(self, *args: Any, **kwargs: Any) -> "_FilterExpression":
        return self

    def __and__(self, other: Any) -> "_FilterExpression":
        return self

    def __or__(self, other: Any) -> "_FilterExpression":
        return self

    def __invert__(self) -> "_FilterExpression":
        return self

    def startswith(self, *args: Any, **kwargs: Any) -> "_FilterExpression":
        return self

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"F{self.path!r}"


F = _FilterExpression()


__all__ = [
    "Bot",
    "Dispatcher",
    "F",
    "Router",
]
