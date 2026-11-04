from __future__ import annotations

import pytest

from app.gateway.quota import DailyTokenQuotaExceeded, QuotaExceeded, enforce_daily_token_limit


class FakeQuota:
    def __init__(self) -> None:
        self.members: dict[str, set[str]] = {"global": set()}

    async def acquire(self, *, user_id: str, reservation_id: str, per_user: int, global_limit: int) -> None:
        user = self.members.setdefault(user_id, set())
        if reservation_id in user:
            return
        if len(user) >= per_user:
            raise QuotaExceeded("user", per_user)
        if len(self.members["global"]) >= global_limit:
            raise QuotaExceeded("global", global_limit)
        user.add(reservation_id)
        self.members["global"].add(reservation_id)

    async def release(self, *, user_id: str, reservation_id: str) -> None:
        self.members.setdefault(user_id, set()).discard(reservation_id)
        self.members["global"].discard(reservation_id)


@pytest.mark.asyncio
async def test_run_quota_limits_per_user_and_releases() -> None:
    quota = FakeQuota()
    for index in range(3):
        await quota.acquire(user_id="alice", reservation_id=f"r{index}", per_user=3, global_limit=10)
    with pytest.raises(QuotaExceeded, match="user"):
        await quota.acquire(user_id="alice", reservation_id="r3", per_user=3, global_limit=10)
    await quota.release(user_id="alice", reservation_id="r1")
    await quota.acquire(user_id="alice", reservation_id="r3", per_user=3, global_limit=10)


@pytest.mark.asyncio
async def test_run_quota_limits_global_runs_across_users() -> None:
    quota = FakeQuota()
    for index in range(3):
        await quota.acquire(user_id=f"user-{index}", reservation_id=f"r{index}", per_user=3, global_limit=3)
    with pytest.raises(QuotaExceeded, match="global"):
        await quota.acquire(user_id="new-user", reservation_id="r3", per_user=3, global_limit=3)


def test_daily_token_quota_allows_under_limit_and_rejects_at_limit() -> None:
    enforce_daily_token_limit(999, 1000)
    with pytest.raises(DailyTokenQuotaExceeded):
        enforce_daily_token_limit(1000, 1000)
    enforce_daily_token_limit(1000, 0)
