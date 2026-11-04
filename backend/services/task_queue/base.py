"""生产任务队列的最小契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class TaskEnvelope:
    task_id: str
    operation: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    lease_until: float | None = None


class TaskQueue(Protocol):
    def enqueue(self, task_id: str, *, operation: str = "", payload: dict[str, Any] | None = None) -> None: ...
    def claim(self, consumer_id: str, *, lease_seconds: int = 120) -> TaskEnvelope | None: ...
    def ack(self, task_id: str) -> None: ...
    def retry(self, task_id: str, reason: str) -> None: ...
    def reclaim_expired(self) -> int: ...
    def renew(self, task_id: str, *, lease_seconds: int = 120) -> bool: ...
