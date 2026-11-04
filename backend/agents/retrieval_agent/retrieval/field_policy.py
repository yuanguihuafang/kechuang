"""Retrieval 侧字段权限适配；权限真源位于共享 core.contracts。"""

from typing import Any

from core.contracts.field_permissions import NON_PUBLIC_FIELDS, PUBLIC_FIELDS


def split_business_fields(
    knowledge_type: str,
    fields: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """按默认字段策略和管理员的单文档覆写拆分字段。

    未映射字段默认进入内部结构化字段集合；只有管理员在解析预览中显式改为
    public 时，才允许作为普通文档内容写入公开库。MySQL 业务记录由独立同步链路
    索引完整业务字段，外部检索再按字段权限裁剪。
    """
    public_names = PUBLIC_FIELDS.get(knowledge_type)
    if not public_names or not fields:
        return {}, {}, False
    normalized_overrides = {
        str(name).strip(): str(visibility).strip()
        for name, visibility in (overrides or {}).items()
        if str(visibility).strip() in {"public", "internal"}
    }
    public: dict[str, Any] = {}
    internal: dict[str, Any] = {}
    for name, value in fields.items():
        key = str(name).strip()
        if value is None or not str(value).strip():
            continue
        if key in NON_PUBLIC_FIELDS.get(knowledge_type, ()):
            internal[key] = value
            continue
        visibility = normalized_overrides.get(key)
        if visibility is None:
            visibility = "public" if key in public_names else "internal"
        (public if visibility == "public" else internal)[key] = value
    return public, internal, bool(set(public) | set(internal))


def fields_to_text(fields: dict[str, Any]) -> str:
    return "\n".join(f"{key}：{value}" for key, value in fields.items() if value is not None and str(value).strip())


