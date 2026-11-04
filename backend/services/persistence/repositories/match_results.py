"""用户匹配结果的长期记录。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..sqlalchemy import get_engine, platform_match_results


class PostgresMatchResultStore:
    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        import os
        self.engine = engine or get_engine(dsn or os.getenv("DATABASE_URL", ""))

    def save(self, *, result_id: str, user_id: str, session_id: str | None,
             query: str, draft_id: str | None, result: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        values = {"result_id": result_id, "user_id": user_id, "session_id": session_id,
                  "query": query, "draft_id": draft_id, "result": result, "created_at": now}
        with self.engine.begin() as conn:
            existing = conn.execute(platform_match_results.select().where(
                platform_match_results.c.result_id == result_id,
            )).first()
            if existing is None:
                conn.execute(platform_match_results.insert().values(**values))
            else:
                conn.execute(platform_match_results.update().where(
                    platform_match_results.c.result_id == result_id,
                ).values(result=result, query=query, draft_id=draft_id))
        return {**values, "created_at": now.isoformat()}

    def list(self, *, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self.engine.connect() as conn:
            rows = conn.execute(platform_match_results.select().where(
                platform_match_results.c.user_id == user_id,
            ).order_by(platform_match_results.c.created_at.desc()).limit(limit)).mappings().all()
        return [dict(row) for row in rows]

    def list_by_session(self, *, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self.engine.connect() as conn:
            rows = conn.execute(platform_match_results.select().where(
                platform_match_results.c.session_id == session_id,
            ).order_by(platform_match_results.c.created_at.desc()).limit(limit)).mappings().all()
        return [dict(row) for row in rows]
