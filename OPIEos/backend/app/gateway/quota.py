"""Shared Redis-backed admission quotas for active agent runs."""

from __future__ import annotations

import os
from dataclasses import dataclass


class QuotaExceeded(RuntimeError):
    """Raised when a user or global active-run quota is exhausted."""

    def __init__(self, scope: str, limit: int):
        self.scope = scope
        self.limit = limit
        super().__init__(f"{scope} active-run quota ({limit}) exceeded")


class DailyTokenQuotaExceeded(RuntimeError):
    """Raised when a user's UTC-day token budget is exhausted."""

    def __init__(self, used: int, limit: int):
        self.used = used
        self.limit = limit
        super().__init__(f"daily token quota ({limit}) exceeded; already used {used}")


@dataclass(slots=True)
class RedisQuotaService:
    client: object
    key_prefix: str = "deerflow:quota:runs"
    lease_seconds: int = 3600

    async def acquire(self, *, user_id: str, reservation_id: str, per_user: int, global_limit: int) -> None:
        """Atomically reserve one active run for a user and the deployment."""
        script = """
        local user_key = KEYS[1]
        local global_key = KEYS[2]
        local member = ARGV[1]
        local per_user = tonumber(ARGV[2])
        local global_limit = tonumber(ARGV[3])
        local ttl = tonumber(ARGV[4])
        if redis.call('SISMEMBER', user_key, member) == 1 then return 1 end
        if redis.call('SCARD', user_key) >= per_user then return -1 end
        if redis.call('SCARD', global_key) >= global_limit then return -2 end
        redis.call('SADD', user_key, member)
        redis.call('SADD', global_key, member)
        redis.call('EXPIRE', user_key, ttl)
        redis.call('EXPIRE', global_key, ttl)
        return 1
        """
        result = int(
            await self.client.eval(
                script,
                2,
                f"{self.key_prefix}:user:{user_id}",
                f"{self.key_prefix}:global",
                reservation_id,
                max(1, per_user),
                max(1, global_limit),
                max(60, self.lease_seconds),
            )
        )
        if result == -1:
            raise QuotaExceeded("user", per_user)
        if result == -2:
            raise QuotaExceeded("global", global_limit)

    async def release(self, *, user_id: str, reservation_id: str) -> None:
        """Release a reservation from both sets; missing members are harmless."""
        script = """
        local user_key = KEYS[1]
        local global_key = KEYS[2]
        local member = ARGV[1]
        redis.call('SREM', user_key, member)
        redis.call('SREM', global_key, member)
        if redis.call('SCARD', user_key) == 0 then redis.call('DEL', user_key) end
        if redis.call('SCARD', global_key) == 0 then redis.call('DEL', global_key) end
        return 1
        """
        await self.client.eval(
            script,
            2,
            f"{self.key_prefix}:user:{user_id}",
            f"{self.key_prefix}:global",
            reservation_id,
        )

    async def close(self) -> None:
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


def build_redis_quota_service(url: str | None = None) -> RedisQuotaService | None:
    redis_url = url or os.getenv("DEER_FLOW_STREAM_BRIDGE_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        from redis.asyncio import Redis
    except ImportError as exc:
        raise RuntimeError("Redis run quotas require the redis package") from exc
    lease_seconds = int(os.getenv("DEER_FLOW_RUN_QUOTA_LEASE_SECONDS", "3600"))
    return RedisQuotaService(Redis.from_url(redis_url, decode_responses=True), lease_seconds=lease_seconds)


def configured_run_limits() -> tuple[int, int]:
    """Return (per-user, global) active-run limits from deployment env."""
    return (
        max(1, int(os.getenv("DEER_FLOW_MAX_CONCURRENT_RUNS_PER_USER", "3"))),
        max(1, int(os.getenv("DEER_FLOW_MAX_CONCURRENT_RUNS", "10"))),
    )


def configured_daily_token_limit() -> int:
    """Return the per-user UTC-day token limit; zero disables enforcement."""
    return max(0, int(os.getenv("DEER_FLOW_DAILY_TOKEN_LIMIT", "0")))


def enforce_daily_token_limit(used: int, limit: int) -> None:
    """Raise when a completed/progress token total has reached the limit."""
    if limit > 0 and max(0, used) >= limit:
        raise DailyTokenQuotaExceeded(max(0, used), limit)
