"""通用任务 Worker：队列负责投递，任务仓储负责业务状态。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .base import TaskEnvelope, TaskQueue


class TaskWorker:
    """单 Worker 消费循环；多实例时由队列消费组分配任务。"""

    def __init__(self, queue: TaskQueue, handlers: dict[str, Callable[[TaskEnvelope], Awaitable[Any]]],
                 *, consumer_id: str, max_attempts: int = 3, task_store: Any | None = None) -> None:
        self.queue, self.handlers, self.consumer_id = queue, handlers, consumer_id
        self.max_attempts = max(1, max_attempts)
        self.task_store = task_store
        self.dead_letters: list[TaskEnvelope] = []

    async def run_once(self) -> bool:
        item = self.queue.claim(self.consumer_id)
        if item is None:
            return False
        handler = self.handlers.get(item.operation)
        if handler is None:
            reason = f"未知任务类型: {item.operation}"
            if item.attempts >= self.max_attempts:
                self.dead_letters.append(item)
                if self.task_store is not None:
                    self.task_store.update(item.task_id, status="DEAD_LETTER",
                                           error_code="TASK_UNKNOWN_OPERATION", error=reason)
                self.queue.ack(item.task_id)
            else:
                self.queue.retry(item.task_id, reason)
            return True
        heartbeat = asyncio.create_task(self._heartbeat(item.task_id))
        try:
            await handler(item)
        except Exception as exc:  # noqa: BLE001 - Worker 边界统一重试
            if item.attempts >= self.max_attempts:
                self.dead_letters.append(item)
                if self.task_store is not None:
                    self.task_store.update(
                        item.task_id, status="DEAD_LETTER", error_code="TASK_MAX_ATTEMPTS",
                        error=str(exc), operation=item.operation,
                    )
                self.queue.ack(item.task_id)
            else:
                self.queue.retry(item.task_id, str(exc))
        else:
            self.queue.ack(item.task_id)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def _heartbeat(self, task_id: str) -> None:
        renew = getattr(self.queue, "renew", None)
        if renew is None:
            return
        while True:
            await asyncio.sleep(30)
            renew(task_id, lease_seconds=120)

    async def run(self, *, poll_seconds: float = 1.0) -> None:
        while True:
            if not await self.run_once():
                await asyncio.sleep(max(0.05, poll_seconds))
