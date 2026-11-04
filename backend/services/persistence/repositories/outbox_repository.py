"""跨存储补偿事件 Outbox。"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from ..sqlalchemy import get_engine, platform_outbox_events


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OutboxRepository:
    """以 operation_id + event_type 幂等保存可重试补偿事件。"""

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        target = dsn or os.getenv("DATABASE_URL", "")
        if not target and engine is None:
            raise ValueError("DATABASE_URL 未配置")
        self.engine = engine or get_engine(target)

    def append(self, operation_id: str, event_type: str,
               payload: dict[str, Any], *, connection: Any = None) -> dict[str, Any]:
        now = _now()
        values = {
            "operation_id": operation_id,
            "event_type": event_type,
            "payload": payload,
            "status": "PENDING",
            "attempts": 0,
            "created_at": now,
        }
        try:
            if connection is not None:
                connection.execute(platform_outbox_events.insert().values(**values))
            else:
                with self.engine.begin() as conn:
                    conn.execute(platform_outbox_events.insert().values(**values))
        except IntegrityError:
            pass
        return self.get(operation_id, event_type) or {
            "event_id": f"outbox_{uuid.uuid4().hex}", **values
        }

    def get(self, operation_id: str, event_type: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(platform_outbox_events.select().where(
                platform_outbox_events.c.operation_id == operation_id,
                platform_outbox_events.c.event_type == event_type,
            )).mappings().first()
        return self._dump(row) if row else None

    def pending(self, *, limit: int = 100, max_attempts: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        expired = _now()
        with self.engine.connect() as conn:
            rows = conn.execute(platform_outbox_events.select().where(
                ((platform_outbox_events.c.status.in_(("PENDING", "FAILED"))) |
                 ((platform_outbox_events.c.status == "PROCESSING") &
                  (platform_outbox_events.c.lease_until < expired))),
                platform_outbox_events.c.attempts < max(1, max_attempts),
            ).order_by(platform_outbox_events.c.created_at.asc()).limit(limit)).mappings().all()
        return [self._dump(row) for row in rows]

    def claim(self, event_id: int, *, max_attempts: int = 10,
              lease_seconds: int = 120) -> dict[str, Any] | None:
        """原子领取 Outbox 事件，避免多个 Worker 重复补偿。"""
        with self.engine.begin() as conn:
            now = _now()
            row = conn.execute(platform_outbox_events.select().where(
                platform_outbox_events.c.event_id == event_id,
                ((platform_outbox_events.c.status.in_(("PENDING", "FAILED"))) |
                 ((platform_outbox_events.c.status == "PROCESSING") &
                  (platform_outbox_events.c.lease_until < now))),
                platform_outbox_events.c.attempts < max(1, max_attempts),
            ).with_for_update()).mappings().first()
            if row is None:
                return None
            conn.execute(platform_outbox_events.update().where(
                platform_outbox_events.c.event_id == event_id,
            ).values(status="PROCESSING", lease_until=now + timedelta(seconds=max(1, lease_seconds))))
        return self._dump({**dict(row), "status": "PROCESSING"})

    def mark_processed(self, event_id: int) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(platform_outbox_events.update().where(
                platform_outbox_events.c.event_id == event_id,
                platform_outbox_events.c.status.in_(("PENDING", "FAILED", "PROCESSING")),
            ).values(status="PROCESSED", processed_at=_now(), lease_until=None))
        return bool(result.rowcount)

    def mark_failed(self, event_id: int, error: str) -> bool:
        with self.engine.begin() as conn:
            row = conn.execute(platform_outbox_events.select().where(
                platform_outbox_events.c.event_id == event_id,
            )).mappings().first()
            if row is None:
                return False
            payload = dict(row["payload"] or {})
            payload["last_error"] = error
            result = conn.execute(platform_outbox_events.update().where(
                platform_outbox_events.c.event_id == event_id,
            ).values(status="FAILED",
                     attempts=platform_outbox_events.c.attempts + 1,
                     payload=payload,
                     lease_until=None,
                     processed_at=None))
        return bool(result.rowcount)

    @staticmethod
    def _dump(row: Any) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "operation_id": row["operation_id"],
            "event_type": row["event_type"],
            "payload": row["payload"] or {},
            "status": row["status"],
            "attempts": row["attempts"],
            "created_at": row["created_at"].isoformat(),
            "processed_at": row["processed_at"].isoformat() if row["processed_at"] else None,
            "lease_until": row["lease_until"].isoformat() if row.get("lease_until") else None,
        }
