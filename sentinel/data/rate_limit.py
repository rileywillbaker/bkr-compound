"""Per-provider rate-limit budgets.

Primary implementation is a Redis fixed-window counter shared across
processes (api + worker). When Redis is unreachable (unit tests, local dev
without docker) it degrades to an in-process window so callers never crash.
"""

import threading
import time

import redis as redis_lib

from sentinel.config import get_settings

# provider -> (max calls, window seconds). Free-tier limits with headroom.
# Finnhub is paced at 1/s rather than 55/60s: a fixed 60s window releases a
# ~50-call burst at each reset, which trips Finnhub's own short-term burst
# limit (observed as upstream 429s during full-universe sweeps). 1/s gives
# the same worst-case throughput (60/min) with no bursts.
BUDGETS: dict[str, tuple[int, int]] = {
    "alpaca": (190, 60),
    "finnhub": (1, 1),
    "fred": (100, 60),
    "edgar": (8, 1),
    "telegram": (25, 60),
}


class RateLimitExceeded(Exception):
    def __init__(self, provider: str, retry_after: float):
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"{provider} rate limit hit; retry in {retry_after:.1f}s")


class _LocalWindow:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[int, float]] = {}  # provider -> (count, window_start)

    def acquire(self, provider: str, limit: int, window: int) -> None:
        now = time.monotonic()
        with self._lock:
            count, start = self._counts.get(provider, (0, now))
            if now - start >= window:
                count, start = 0, now
            if count >= limit:
                raise RateLimitExceeded(provider, window - (now - start))
            self._counts[provider] = (count + 1, start)


_local = _LocalWindow()


class RateLimiter:
    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or get_settings().redis_url
        self._redis: redis_lib.Redis | None = None
        self._redis_failed = False

    def _client(self) -> redis_lib.Redis | None:
        if self._redis_failed:
            return None
        if self._redis is None:
            try:
                self._redis = redis_lib.Redis.from_url(
                    self._redis_url, socket_connect_timeout=1, socket_timeout=2
                )
                self._redis.ping()
            except Exception:
                self._redis_failed = True
                self._redis = None
        return self._redis

    def acquire(self, provider: str) -> None:
        """Consume one call from the provider budget or raise RateLimitExceeded."""
        limit, window = BUDGETS.get(provider, (60, 60))
        client = self._client()
        if client is None:
            _local.acquire(provider, limit, window)
            return
        key = f"ratelimit:{provider}:{int(time.time() // window)}"
        try:
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, window)
            count, _ = pipe.execute()
        except Exception:
            self._redis_failed = True
            _local.acquire(provider, limit, window)
            return
        if int(count) > limit:
            retry = window - (time.time() % window)
            raise RateLimitExceeded(provider, retry)

    def wait_and_acquire(self, provider: str, max_wait: float = 120.0) -> None:
        """Blocking acquire for batch ingestion jobs.

        max_wait must exceed the provider's window (60s for most budgets):
        full-universe sweeps exhaust a window every ~55 calls, and the next
        one can be a full window away — a shorter deadline aborts the batch
        partway through instead of riding out the boundary."""
        deadline = time.monotonic() + max_wait
        while True:
            try:
                self.acquire(provider)
                return
            except RateLimitExceeded as exc:
                if time.monotonic() + exc.retry_after > deadline:
                    raise
                time.sleep(min(exc.retry_after, 1.0))


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
