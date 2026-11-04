from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.audit.model import AuditLogRow


class AuditLogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sf = session_factory

    async def record(self, *, actor_user_id: str | None, action: str, resource_type: str, resource_id: str | None, metadata: dict | None = None) -> None:
        async with self._sf() as session:
            session.add(AuditLogRow(id=str(uuid.uuid4()), actor_user_id=actor_user_id, action=action, resource_type=resource_type, resource_id=resource_id, metadata_json=metadata or {}))
            await session.commit()
