import pytest

from app.gateway.rate_limit import RateLimitExceeded, RedisRateLimiter


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}

    async def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        self.expiries[key] = seconds

    async def ttl(self, key):
        return self.expiries.get(key, 1)


@pytest.mark.asyncio
async def test_fixed_window_rejects_after_limit():
    limiter = RedisRateLimiter(FakeRedis())
    await limiter.check("login:ip", limit=2, window_seconds=60)
    await limiter.check("login:ip", limit=2, window_seconds=60)
    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.check("login:ip", limit=2, window_seconds=60)
    assert exc.value.retry_after == 60


@pytest.mark.asyncio
async def test_counters_are_scoped_by_key():
    limiter = RedisRateLimiter(FakeRedis())
    await limiter.check("login:a", limit=1, window_seconds=30)
    await limiter.check("login:b", limit=1, window_seconds=30)
