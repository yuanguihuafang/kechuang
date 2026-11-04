"""开发环境本地任务队列；不作为跨实例事实来源。"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from .base import TaskEnvelope


class LocalTaskQueue:
    """进程内队列，用于开发和契约测试。"""

    def __init__(self) -> None:
        self._pending: deque[TaskEnvelope] = deque()
        self._leased: dict[str, TaskEnvelope] = {}
        self._done: set[str] = set()
        self._lock = Lock()

    def enqueue(self, task_id: str, *, operation: str = "", payload: dict | None = None) -> None:
        with self._lock:
            if task_id in self._done or task_id in self._leased:
                return
            if any(item.task_id == task_id for item in self._pending):
                return
            self._pending.append(TaskEnvelope(task_id, operation, dict(payload or {})))

    def claim(self, consumer_id: str, *, lease_seconds: int = 120) -> TaskEnvelope | None:
        del consumer_id
        with self._lock:
            self.reclaim_expired()
            while self._pending:
                item = self._pending.popleft()
                if item.task_id in self._done:
                    continue
                item.attempts += 1
                item.lease_until = time.time() + max(1, lease_seconds)
                self._leased[item.task_id] = item
                return item
        return None

    def ack(self, task_id: str) -> None:
        with self._lock:
            self._leased.pop(task_id, None)
            self._done.add(task_id)

    def retry(self, task_id: str, reason: str) -> None:
        with self._lock:
            item = self._leased.pop(task_id, None)
            if item is None or task_id in self._done:
                return
            item.payload["last_error"] = reason
            item.lease_until = None
            self._pending.append(item)

    def reclaim_expired(self) -> int:
        now = time.time()
        expired = [item for item in self._leased.values()
                   if item.lease_until is not None and item.lease_until < now]
        for item in expired:
            self._leased.pop(item.task_id, None)
            item.lease_until = None
            self._pending.append(item)
        return len(expired)

    def renew(self, task_id: str, *, lease_seconds: int = 120) -> bool:
        with self._lock:
            item = self._leased.get(task_id)
            if item is None:
                return False
            item.lease_until = time.time() + max(1, lease_seconds)
            return True
