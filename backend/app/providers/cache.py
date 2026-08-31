"""Response cache: Redis when configured, in-process otherwise.

Slim mode runs without Redis, so the in-process fallback is a first-class path
rather than a degraded one — it just does not survive a restart or span
workers. TTLs are per-endpoint (fixtures 1h, live scores 30s, historical stats
24h, odds 5m) and chosen by the caller.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl: int) -> None: ...
    async def close(self) -> None: ...


class InProcessCache:
    """TTL dict. Bounded so a long-running process cannot grow without limit."""

    def __init__(self, max_entries: int = 2048):
        self._data: dict[str, tuple[float, Any]] = {}
        self._max_entries = max_entries

    async def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if len(self._data) >= self._max_entries:
            # Drop the entry closest to expiry — cheap and good enough here.
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic() + ttl, value)

    async def close(self) -> None:
        self._data.clear()


class RedisCache:
    def __init__(self, url: str):
        self._client = aioredis.from_url(url, socket_connect_timeout=3)

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(key)
        except Exception as exc:  # noqa: BLE001 — a cache miss beats an outage
            log.warning("cache.get_failed", key=key, error=str(exc))
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self._client.set(key, json.dumps(value), ex=ttl)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache.set_failed", key=key, error=str(exc))

    async def close(self) -> None:
        await self._client.aclose()


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        settings = get_settings()
        if settings.redis_url:
            _cache = RedisCache(settings.redis_url)
            log.info("cache.backend", backend="redis")
        else:
            _cache = InProcessCache()
            log.info("cache.backend", backend="in-process")
    return _cache


async def reset_cache() -> None:
    """Drop the cache (shutdown, and between tests)."""
    global _cache
    if _cache is not None:
        await _cache.close()
    _cache = None
