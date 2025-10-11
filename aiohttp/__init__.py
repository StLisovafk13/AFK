"""Minimal aiohttp stub for unit tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ClientTimeout:
    total: Optional[float] = None


@dataclass
class TCPConnector:
    limit: Optional[int] = None
    ssl: bool = True

    async def close(self) -> None:
        return None


class ClientSession:
    def __init__(self, *args: Any, timeout: ClientTimeout | None = None, headers: Optional[dict[str, str]] = None, connector: TCPConnector | None = None, **kwargs: Any) -> None:
        self.timeout = timeout
        self.headers = headers or {}
        self.connector = connector
        self._closed = False

    async def __aenter__(self) -> "ClientSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.close()
        return False

    async def close(self) -> None:
        self._closed = True
        if self.connector is not None:
            await self.connector.close()

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ClientSession.get is not implemented in the test stub")

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ClientSession.post is not implemented in the test stub")


__all__ = ["ClientSession", "ClientTimeout", "TCPConnector"]
