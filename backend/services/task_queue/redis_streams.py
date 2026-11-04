"""Redis Streams 适配器；Redis 是投递层，不保存业务任务事实。"""

from __future__ import annotations

import json
from typing import Any

from .base import TaskEnvelope


class RedisStreamsTaskQueue:
    """依赖注入 Redis 客户端，避免开发环境强制安装或连接 Redis。"""

    def __init__(self, client: Any, *, stream: str = "kc:tasks", group: str = "kc-workers") -> None:
        self.client, self.stream, self.group = client, stream, group
        self._message_ids: dict[str, str] = {}
        self._consumer_id = ""

    def enqueue(self, task_id: str, *, operation: str = "", payload: dict | None = None) -> None:
        self.client.xadd(self.stream, {"task_id": task_id, "operation": operation,
                                       "payload": json.dumps(payload or {}, ensure_ascii=False)}, id="*")

    def claim(self, consumer_id: str, *, lease_seconds: int = 120) -> TaskEnvelope | None:
        del lease_seconds
        self._consumer_id = consumer_id
        self.ensure_group()
        rows = self.client.xreadgroup(self.group, consumer_id, {self.stream: ">"}, count=1, block=1)
        if not rows:
            return None
        _, messages = rows[0]
        message_id, values = messages[0]
        def value(name: str) -> Any:
            return values.get(name) if name in values else values.get(name.encode())
        task_id = str(value("task_id"))
        self._message_ids[task_id] = str(message_id)
        raw_payload = value("payload") or "{}"
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8")
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else {}
        except json.JSONDecodeError:
            payload = {}
        return TaskEnvelope(task_id, str(value("operation") or ""),
                            payload if isinstance(payload, dict) else {})

    def ack(self, task_id: str) -> None:
        self.client.xack(self.stream, self.group, self._message_ids.pop(task_id, task_id))

    def retry(self, task_id: str, reason: str) -> None:
        self.enqueue(task_id, payload={"last_error": reason})

    def reclaim_expired(self) -> int:
        # Redis 负责 PEL 租约，具体恢复由消费组在下一次 claim 时读取 pending 消息。
        return 0

    def renew(self, task_id: str, *, lease_seconds: int = 120) -> bool:
        message_id = self._message_ids.get(task_id)
        if message_id is None:
            return False
        self.client.xclaim(self.stream, self.group, self._consumer_id, max(1, lease_seconds) * 1000,
                           [message_id])
        return True

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
