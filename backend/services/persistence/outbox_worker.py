"""Outbox 补偿处理器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .repositories.outbox_repository import OutboxRepository


class OutboxProcessor:
    """一次处理一批事件；可挂到本地循环或生产 Worker。"""

    def __init__(self, repository: OutboxRepository,
                 handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]],
                 *, max_attempts: int = 10) -> None:
        self.repository = repository
        self.handlers = handlers
        self.max_attempts = max(1, max_attempts)

    async def process_once(self, *, limit: int = 100) -> int:
        processed = 0
        for event in self.repository.pending(limit=limit, max_attempts=self.max_attempts):
            claimed = self.repository.claim(event["event_id"], max_attempts=self.max_attempts)
            if claimed is None:
                continue
            handler = self.handlers.get(claimed["event_type"])
            try:
                if handler is None:
                    raise RuntimeError(f"未注册 Outbox 事件: {claimed['event_type']}")
                await handler(claimed)
            except Exception as exc:  # noqa: BLE001 - 补偿边界记录后重试
                self.repository.mark_failed(claimed["event_id"], str(exc))
            else:
                self.repository.mark_processed(claimed["event_id"])
                processed += 1
        return processed

    async def run(self, *, poll_seconds: float = 2.0) -> None:
        while True:
            await self.process_once()
            await asyncio.sleep(max(0.1, poll_seconds))
