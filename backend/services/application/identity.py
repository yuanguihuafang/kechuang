"""共用业务身份解析；沿用当前认证规则，不依赖 HTTP 路由。"""

from __future__ import annotations

from collections.abc import Collection
from typing import Callable, Protocol
from fastapi import HTTPException
from services.mysql.user_identity import UserIdentity


class IdentityReader(Protocol):
    def get(self, user_id: str) -> UserIdentity | None: ...


IdentityReaderSource = IdentityReader | Callable[[], IdentityReader | None] | None


class TestIdentityResolver:
    """根据用户表的内部标记映射为唯一的业务身份。"""

    __test__ = False

    def __init__(self, reader: IdentityReaderSource) -> None:
        self.reader = reader

    def _reader(self) -> IdentityReader | None:
        return self.reader() if callable(self.reader) else self.reader

    def resolve(self, user_id: str) -> tuple[str, str, UserIdentity | None]:
        normalized_id = str(user_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=422, detail="user_id 不能为空")

        reader = self._reader()
        if reader is None:
            raise HTTPException(status_code=503, detail="用户身份服务未配置")
        profile = reader.get(normalized_id) if reader is not None else None
        if profile is None:
            raise HTTPException(status_code=401, detail="用户不存在或身份已失效")
        role = "internal" if profile.is_inner_user else "external"
        return normalized_id, role, profile

    def resolve_required(self, user_id: str) -> tuple[str, str, UserIdentity]:
        """登录鉴权入口：必须在 MySQL 用户表中找到身份。"""

        normalized_id, role, profile = self.resolve(user_id)
        if profile is None:  # pragma: no cover - resolve 已拒绝此分支
            raise HTTPException(status_code=401, detail="用户不存在或身份已失效")
        return normalized_id, role, profile

    @staticmethod
    def require_roles(
        role: str,
        allowed_roles: Collection[str],
        detail: str,
    ) -> str:
        if role not in allowed_roles:
            raise HTTPException(status_code=403, detail=detail)
        return role
