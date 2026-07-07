import pytest

from sentinel.data.rate_limit import BUDGETS, RateLimiter, RateLimitExceeded


@pytest.fixture()
def limiter(monkeypatch):
    # Point at a dead redis so the local fallback is exercised deterministically.
    lim = RateLimiter(redis_url="redis://127.0.0.1:1/0")
    # fresh local window per test
    import sentinel.data.rate_limit as rl

    monkeypatch.setattr(rl, "_local", rl._LocalWindow())
    return lim


def test_acquire_within_budget(limiter):
    for _ in range(BUDGETS["edgar"][0]):
        limiter.acquire("edgar")


def test_acquire_exceeds_budget(limiter):
    limit, _ = BUDGETS["edgar"]
    for _ in range(limit):
        limiter.acquire("edgar")
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.acquire("edgar")
    assert exc.value.provider == "edgar"
    assert exc.value.retry_after > 0


def test_unknown_provider_gets_default_budget(limiter):
    limiter.acquire("whatever")
