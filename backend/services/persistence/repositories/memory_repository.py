"""PostgreSQL 长期记忆仓储：负责记忆读写、去重、过期清理和合并。"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

from ..postgres import _iso, _now, memory_content_hash, memory_content_similarity
from ..sqlalchemy import get_engine, user_memories


class PostgresMemoryStore:
    """长期用户记忆仓储；只按 user_id 访问，不与 Milvus 知识库混用。"""

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        self.engine = engine or get_engine(self.dsn)

    @staticmethod
    def _record(row: Any) -> dict[str, Any]:
        return {
            "memory_id": row["memory_id"], "user_id": row["user_id"], "memory_type": row["memory_type"],
            "content": row["content"], "source": row["source"], "importance": row["importance"],
            "confirmed": row["confirmed"], "status": row["status"],
            "expires_at": _iso(row["expires_at"]) if row["expires_at"] else None,
            "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
        }

    def create(self, *, user_id: str, memory_type: str, content: str,
               source: str = "user_declared", importance: float = 0.5,
               confirmed: bool = True, status: str = "ACTIVE",
               expires_at: datetime | None = None) -> dict[str, Any]:
        confirmed = bool(confirmed)
        status = "ACTIVE" if confirmed else "PENDING"
        memory_id = f"memory_{uuid.uuid4().hex}"
        now = _now()
        content_hash = memory_content_hash(content)
        with self.engine.begin() as conn:
            existing = conn.execute(user_memories.select().where(
                user_memories.c.user_id == user_id, user_memories.c.content_hash == content_hash,
                user_memories.c.status.in_(["PENDING", "ACTIVE"]),
            ).order_by(user_memories.c.updated_at.desc()).limit(1)).mappings().first()
            if existing:
                existing_id = existing["memory_id"]
                conn.execute(user_memories.update().where(user_memories.c.memory_id == existing_id).values(
                    importance=max(float(existing["importance"] or 0), importance), confirmed=bool(existing["confirmed"] or confirmed),
                    status="ACTIVE" if confirmed else existing["status"], expires_at=expires_at or existing["expires_at"], updated_at=now,
                ))
                merged = conn.execute(user_memories.select().where(user_memories.c.memory_id == existing_id)).mappings().one()
                return self._record(merged)
            conn.execute(user_memories.insert().values(memory_id=memory_id, user_id=user_id, memory_type=memory_type,
                content=content, content_hash=content_hash, source=source, importance=importance,
                confirmed=confirmed, status=status, expires_at=expires_at, created_at=now, updated_at=now))
        return self.get(memory_id, user_id=user_id)  # type: ignore[return-value]

    def list(self, *, user_id: str, memory_type: str | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        query = user_memories.select().where(user_memories.c.user_id == user_id,
            user_memories.c.status == "ACTIVE", user_memories.c.confirmed.is_(True),
            (user_memories.c.expires_at.is_(None) | (user_memories.c.expires_at > _now())))
        if memory_type:
            query = query.where(user_memories.c.memory_type == memory_type)
        with self.engine.connect() as conn:
            rows = conn.execute(query.order_by(user_memories.c.importance.desc(), user_memories.c.updated_at.desc()).limit(limit)).mappings().all()
        return [self._record(row) for row in rows]

    def list_candidates(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(user_memories.select().where(
                user_memories.c.user_id == user_id, user_memories.c.status == "PENDING",
            ).order_by(user_memories.c.created_at.desc()).limit(limit)).mappings().all()
        return [self._record(row) for row in rows]

    def get(self, memory_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(user_memories.select().where(
                user_memories.c.memory_id == memory_id, user_memories.c.user_id == user_id,
            )).mappings().first()
        return self._record(row) if row else None

    def update(self, memory_id: str, *, user_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"content", "importance", "confirmed", "status", "expires_at"}
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return self.get(memory_id, user_id=user_id)
        if "content" in changes:
            changes["content_hash"] = memory_content_hash(str(changes["content"]))
        changes["updated_at"] = _now()
        with self.engine.begin() as conn:
            conn.execute(user_memories.update().where(
                user_memories.c.memory_id == memory_id, user_memories.c.user_id == user_id,
            ).values(**changes))
        return self.get(memory_id, user_id=user_id)

    def delete(self, memory_id: str, *, user_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(user_memories.delete().where(
                user_memories.c.memory_id == memory_id, user_memories.c.user_id == user_id,
            ))
        return result.rowcount > 0

    def cleanup_expired(self, *, user_id: str | None = None) -> int:
        query = user_memories.delete().where(
            user_memories.c.expires_at.is_not(None), user_memories.c.expires_at <= _now(),
        )
        if user_id:
            query = query.where(user_memories.c.user_id == user_id)
        with self.engine.begin() as conn:
            return int(conn.execute(query).rowcount)

    def consolidate(self, *, user_id: str, memory_type: str | None = None,
                    similarity_threshold: float = 0.92) -> dict[str, int]:
        threshold = min(0.99, max(0.80, float(similarity_threshold)))
        query = user_memories.select().where(
            user_memories.c.user_id == user_id,
            user_memories.c.status.in_(["PENDING", "ACTIVE"]),
        )
        if memory_type:
            query = query.where(user_memories.c.memory_type == memory_type)
        with self.engine.begin() as conn:
            rows = conn.execute(query.order_by(user_memories.c.memory_type,
                user_memories.c.confirmed.desc(), user_memories.c.importance.desc(),
                user_memories.c.updated_at.desc())).mappings().all()
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                item = self._record(row)
                groups.setdefault(item["memory_type"], []).append(item)
            merged = 0
            conflicts = 0
            for items in groups.values():
                kept: list[dict[str, Any]] = []
                for item in items:
                    duplicate = next(
                        (candidate for candidate in kept
                         if memory_content_similarity(candidate["content"], item["content"]) >= threshold),
                        None,
                    )
                    if duplicate is None:
                        kept.append(item)
                        continue
                    winner, loser = sorted(
                        (duplicate, item),
                        key=lambda value: (
                            bool(value.get("confirmed")),
                            float(value.get("importance") or 0),
                            str(value.get("updated_at") or ""),
                        ),
                        reverse=True,
                    )
                    conn.execute(user_memories.update().where(
                        user_memories.c.memory_id == winner["memory_id"],
                        user_memories.c.user_id == user_id,
                    ).values(importance=max(float(winner["importance"] or 0), float(loser["importance"] or 0)),
                             confirmed=bool(winner["confirmed"] or loser["confirmed"]),
                             status="ACTIVE" if loser["confirmed"] else winner["status"],
                             expires_at=winner["expires_at"] or loser["expires_at"], updated_at=_now()))
                    conn.execute(user_memories.update().where(
                        user_memories.c.memory_id == loser["memory_id"],
                        user_memories.c.user_id == user_id,
                    ).values(status="ARCHIVED", updated_at=_now()))
                    if winner is not duplicate:
                        kept.remove(duplicate)
                        kept.append(winner)
                    merged += 1
            return {"merged_count": merged, "conflict_count": conflicts}
