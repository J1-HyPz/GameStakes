"""Shared HTTP machinery for provider adapters.

Every upstream call goes through ProviderClient, which layers:
  cache -> circuit breaker -> rate limiter -> request -> retry/backoff

Free tiers are small and unforgiving (The Odds API bills 500 credits a month,
football-data.org allows 10 requests a minute), so the limiter is a real token
bucket rather than a sleep, and 429s feed back into it. A provider that keeps
failing is short-circuited instead of being hammered — the breaker surfaces as
degraded status in the UI rather than as stalled ingestion.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.providers.cache import get_cache

log = get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimitedError(ProviderError):
    """Upstream returned 429 and the retry budget is spent."""


class CircuitOpenError(ProviderError):
    """The breaker is open: recent calls failed, so this one is not attempted."""


class TokenBucket:
    """Classic token bucket. `rate` tokens per `period` seconds, burst `capacity`."""

    def __init__(self, rate: float, period: float = 60.0, capacity: float | None = None):
        self.rate = rate
        self.period = period
        self.capacity = capacity if capacity is not None else rate
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until `tokens` are available. Returns seconds spent waiting."""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._updated) * (self.rate / self.period),
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit * (self.period / self.rate)
                waited += delay
                await asyncio.sleep(delay)

    def penalise(self, seconds: float) -> None:
        """Drain the bucket after a 429 so the next call genuinely waits."""
        self._tokens = 0.0
        self._updated = time.monotonic() + seconds


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive failures; probes again after
    `recovery_seconds`. One success closes it."""

    threshold: int = 5
    recovery_seconds: float = 120.0
    failures: int = 0
    opened_at: float | None = field(default=None)

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        # Past the recovery window the breaker is half-open: allow one probe.
        return time.monotonic() - self.opened_at < self.recovery_seconds

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    def retry_in(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.recovery_seconds - (time.monotonic() - self.opened_at))


class ProviderClient:
    """HTTP client scoped to one provider."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        rate: float = 60,
        period: float = 60.0,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
    ):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout
        self.max_retries = max_retries
        self.bucket = TokenBucket(rate=rate, period=period)
        self.breaker = CircuitBreaker()
        self.last_error: str | None = None
        self.quota_remaining: int | None = None
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.headers,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def cache_key(self, path: str, params: dict[str, Any] | None) -> str:
        # The API key travels in params for some providers — hash, never store.
        safe = {k: v for k, v in (params or {}).items() if k.lower() not in {"apikey", "api_key"}}
        blob = json.dumps({"p": path, "q": safe}, sort_keys=True)
        digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
        return f"gs:provider:{self.provider}:{digest}"

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        ttl: int = 300,
        cost: float = 1.0,
        use_cache: bool = True,
    ) -> Any:
        """GET returning parsed JSON, cached and rate-limited.

        `cost` is how many tokens the call consumes — endpoints that bill more
        than one credit should say so.
        """
        key = self.cache_key(path, params)
        cache = get_cache()
        if use_cache:
            cached = await cache.get(key)
            if cached is not None:
                return cached

        if self.breaker.is_open:
            raise CircuitOpenError(
                f"{self.provider} circuit open after {self.breaker.failures} failures; "
                f"retrying in {self.breaker.retry_in():.0f}s"
            )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.bucket.acquire(cost)
            try:
                response = await self._http().get(path, params=params)
                self._read_quota_headers(response)

                if response.status_code in RETRYABLE_STATUS:
                    if response.status_code == 429:
                        retry_after = _retry_after_seconds(response)
                        self.bucket.penalise(retry_after)
                        log.warning(
                            "provider.rate_limited",
                            provider=self.provider,
                            path=path,
                            retry_after=retry_after,
                        )
                    if attempt < self.max_retries:
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    self.breaker.record_failure()
                    self.last_error = f"HTTP {response.status_code}"
                    if response.status_code == 429:
                        raise RateLimitedError(f"{self.provider}: rate limited")
                    raise ProviderError(f"{self.provider}: HTTP {response.status_code}")

                response.raise_for_status()
                payload = response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                self.breaker.record_failure()
                self.last_error = str(exc)
                raise ProviderError(f"{self.provider}: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                # 4xx other than 429: no retry, the request itself is wrong.
                self.breaker.record_failure()
                self.last_error = f"HTTP {exc.response.status_code}"
                raise ProviderError(
                    f"{self.provider}: HTTP {exc.response.status_code} for {path}"
                ) from exc

            self.breaker.record_success()
            self.last_error = None
            if use_cache:
                await cache.set(key, payload, ttl)
            return payload

        raise ProviderError(f"{self.provider}: exhausted retries ({last_exc})")

    def _read_quota_headers(self, response: httpx.Response) -> None:
        for header in ("x-requests-remaining", "X-Requests-Remaining"):
            if header in response.headers:
                with contextlib.suppress(ValueError):
                    self.quota_remaining = int(response.headers[header])
                return


def _backoff(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * (2**attempt)))  # noqa: S311 — not cryptographic


def _retry_after_seconds(response: httpx.Response, default: float = 30.0) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
