"""Rate limiting, retry/backoff and circuit breaking."""

import asyncio
import time

import httpx
import pytest

from app.core.exceptions import ProviderError
from app.providers.cache import InProcessCache
from app.providers.http import (
    CircuitBreaker,
    CircuitOpenError,
    ProviderClient,
    RateLimitedError,
    TokenBucket,
    _backoff,
)


class TestTokenBucket:
    async def test_burst_up_to_capacity_is_immediate(self) -> None:
        bucket = TokenBucket(rate=5, period=60.0)
        start = time.monotonic()
        for _ in range(5):
            await bucket.acquire()
        assert time.monotonic() - start < 0.1

    async def test_exhausted_bucket_makes_the_caller_wait(self) -> None:
        # 10 tokens per second: the 3rd call must wait ~0.1s.
        bucket = TokenBucket(rate=10, period=1.0, capacity=2)
        await bucket.acquire()
        await bucket.acquire()
        start = time.monotonic()
        await bucket.acquire()
        assert time.monotonic() - start >= 0.05

    async def test_penalise_drains_the_bucket(self) -> None:
        bucket = TokenBucket(rate=100, period=1.0)
        bucket.penalise(0.2)
        start = time.monotonic()
        await bucket.acquire()
        assert time.monotonic() - start > 0.0

    async def test_concurrent_acquires_are_serialised(self) -> None:
        bucket = TokenBucket(rate=20, period=1.0, capacity=4)
        await asyncio.gather(*(bucket.acquire() for _ in range(4)))
        assert bucket._tokens < 1.0


class TestCircuitBreaker:
    def test_opens_after_threshold_consecutive_failures(self) -> None:
        breaker = CircuitBreaker(threshold=3)
        for _ in range(2):
            breaker.record_failure()
        assert not breaker.is_open
        breaker.record_failure()
        assert breaker.is_open

    def test_success_closes_and_resets_the_count(self) -> None:
        breaker = CircuitBreaker(threshold=2)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open
        breaker.record_success()
        assert not breaker.is_open
        assert breaker.failures == 0

    def test_half_opens_after_the_recovery_window(self) -> None:
        breaker = CircuitBreaker(threshold=1, recovery_seconds=0.05)
        breaker.record_failure()
        assert breaker.is_open
        time.sleep(0.06)
        assert not breaker.is_open  # a probe is allowed through


def test_backoff_is_bounded_and_jittered() -> None:
    values = [_backoff(attempt=5, base=0.5, cap=8.0) for _ in range(50)]
    assert all(0 <= v <= 8.0 for v in values)
    assert len(set(values)) > 1  # jitter, not a fixed delay


def _client(handler: httpx.MockTransport, **kwargs: object) -> ProviderClient:
    client = ProviderClient(provider="test", base_url="https://example.test", **kwargs)  # type: ignore[arg-type]
    client._client = httpx.AsyncClient(transport=handler, base_url="https://example.test")
    return client


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = InProcessCache()
    monkeypatch.setattr("app.providers.http.get_cache", lambda: cache)


class TestProviderClient:
    async def test_successful_json_is_returned_and_cached(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"ok": True})

        client = _client(httpx.MockTransport(handler))
        assert await client.get_json("/x") == {"ok": True}
        assert await client.get_json("/x") == {"ok": True}
        assert calls == 1, "second call must be served from cache"

    async def test_transient_500_is_retried_then_succeeds(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"recovered": True})

        client = _client(httpx.MockTransport(handler), max_retries=2)
        assert await client.get_json("/x") == {"recovered": True}
        assert calls == 2

    async def test_persistent_429_raises_rate_limited(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "0"})

        client = _client(httpx.MockTransport(handler), max_retries=1)
        with pytest.raises(RateLimitedError):
            await client.get_json("/x")

    async def test_client_error_is_not_retried(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(403)

        client = _client(httpx.MockTransport(handler), max_retries=3)
        with pytest.raises(ProviderError):
            await client.get_json("/x")
        assert calls == 1, "a 403 is our fault, not a blip — do not retry"

    async def test_breaker_opens_and_short_circuits(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        client = _client(httpx.MockTransport(handler), max_retries=0)
        client.breaker.threshold = 2
        for i in range(2):
            with pytest.raises(ProviderError):
                await client.get_json(f"/x{i}")

        before = calls
        with pytest.raises(CircuitOpenError):
            await client.get_json("/x-after-open")
        assert calls == before, "an open circuit must not hit the network"

    async def test_quota_headers_are_recorded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[], headers={"x-requests-remaining": "37"})

        client = _client(httpx.MockTransport(handler))
        await client.get_json("/sports")
        assert client.quota_remaining == 37

    async def test_api_key_never_reaches_the_cache_key(self) -> None:
        client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        key = client.cache_key("/odds", {"apiKey": "secret-value", "regions": "uk"})
        assert "secret-value" not in key
        # Different keys, same query -> same cache entry.
        other = client.cache_key("/odds", {"apiKey": "another", "regions": "uk"})
        assert key == other
