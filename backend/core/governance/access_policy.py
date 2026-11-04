"""Retrieval 测试阶段的手动身份与知识范围策略。

当前不接入 JWT。前端只选择测试身份，Retrieval 仍在服务端根据身份计算可访问
scope，避免把客户端传入的 scope 直接当成权限依据。
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class UserType(StrEnum):
    """平台用户身份；业务方向由问答意图决定。"""

    ADMIN = "admin"
    INTERNAL = "internal"
    EXTERNAL = "external"


class ManualIdentity(BaseModel):
    """测试身份；正式接入时由 JWT claims 替换。"""

    user_role: UserType
    user_id: str = Field(min_length=1, max_length=64)


class AccessPolicy:
    """根据测试身份计算 Retrieval 的可检索 scope。"""

    @staticmethod
    def allowed_scopes(identity: ManualIdentity) -> list[str]:
        if identity.user_role in (UserType.ADMIN, UserType.INTERNAL):
            return ["public", "internal"]
        return ["public"]

    @staticmethod
    def can_write_private(identity: ManualIdentity) -> bool:
        """内部补充和用户私有块只能由内部测试身份写入。"""
        return identity.user_role in (UserType.ADMIN, UserType.INTERNAL)

    @classmethod
    def resolve_scopes(
        cls,
        identity: ManualIdentity,
        requested_scope: str | None = None,
    ) -> list[str]:
        allowed = cls.allowed_scopes(identity)
        if requested_scope is None or not requested_scope.strip():
            return allowed
        if requested_scope.strip() == "accessible":
            return allowed
        return [requested_scope] if requested_scope in allowed else []

    @classmethod
    def resolve_scope(cls, identity: ManualIdentity, requested_scope: str | None) -> str | None:
        scopes = cls.resolve_scopes(identity, requested_scope)
        return scopes[0] if len(scopes) == 1 else None
