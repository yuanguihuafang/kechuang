"""Small shared Redis rate limiter used by auth entry points."""

from __future__ import annotations

import os
from dataclasses import dataclass


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(f"rate limit exceeded; retry in {self.retry_after}s")


@dataclass(slots=True)
class RedisRateLimiter:
    client: object

    async def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        """Atomically-ish increment a fixed window counter and reject overflow.

        Redis INCR is atomic. Expiry is set only for the first writer, so the
        counter cannot be extended by subsequent requests.
        """
        count = int(await self.client.incr(key))  # type: ignore[attr-defined]
        if count == 1:
            await self.client.expire(key, window_seconds)  # type: ignore[attr-defined]
        if count > limit:
            ttl = await self.client.ttl(key)  # type: ignore[attr-defined]
            raise RateLimitExceeded(int(ttl) if int(ttl) > 0 else window_seconds)

    async def close(self) -> None:
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


def build_redis_rate_limiter(url: str | None = None) -> RedisRateLimiter | None:
    redis_url = url or os.getenv("DEER_FLOW_STREAM_BRIDGE_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - deployment dependency check
        raise RuntimeError("Redis rate limiting requires the redis package") from exc
    return RedisRateLimiter(Redis.from_url(redis_url, decode_responses=True))
