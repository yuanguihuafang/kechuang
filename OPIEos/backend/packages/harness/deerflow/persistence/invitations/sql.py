from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.invitations.model import InvitationRow


class InvitationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sf = session_factory

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create(self, *, email: str, created_by: str, ttl_hours: int = 72) -> tuple[dict, str]:
        raw = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        row = InvitationRow(id=str(uuid.uuid4()), email=email.lower(), token_hash=self.hash_token(raw), created_by=created_by, status="pending", expires_at=now + timedelta(hours=ttl_hours), created_at=now)
        async with self._sf() as session:
            session.add(row)
            await session.commit()
        return self._to_dict(row), raw

    async def get_by_token(self, token: str) -> dict | None:
        async with self._sf() as session:
            row = (await session.execute(select(InvitationRow).where(InvitationRow.token_hash == self.hash_token(token)))).scalar_one_or_none()
            return self._to_dict(row) if row else None

    async def consume(self, token: str, *, used_by: str, now: datetime | None = None) -> dict | None:
        now = now or datetime.now(UTC)
        async with self._sf() as session:
            result = await session.execute(
                update(InvitationRow).where(InvitationRow.token_hash == self.hash_token(token), InvitationRow.status == "pending", InvitationRow.expires_at > now).values(status="used", used_at=now, used_by=used_by).returning(InvitationRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                await session.rollback()
                return None
            await session.commit()
            return self._to_dict(row)

    async def revoke(self, invitation_id: str) -> bool:
        async with self._sf() as session:
            result = await session.execute(update(InvitationRow).where(InvitationRow.id == invitation_id, InvitationRow.status == "pending").values(status="revoked", revoked_at=datetime.now(UTC)))
            await session.commit()
            return result.rowcount == 1

    async def list(self, limit: int = 100) -> list[dict]:
        async with self._sf() as session:
            rows = (await session.execute(select(InvitationRow).order_by(InvitationRow.created_at.desc()).limit(limit))).scalars()
            return [self._to_dict(row) for row in rows]

    @staticmethod
    def _to_dict(row: InvitationRow) -> dict:
        return {"id": row.id, "email": row.email, "created_by": row.created_by, "status": row.status, "expires_at": row.expires_at, "created_at": row.created_at, "used_at": row.used_at, "used_by": row.used_by, "revoked_at": row.revoked_at}
