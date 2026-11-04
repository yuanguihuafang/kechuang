"""文档原始附件的统一访问授权规则。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_EXTERNAL_DOWNLOADABLE_TYPES = frozenset({"enterprise", "achievement", "expert", "solution"})


def can_download_document(
    *,
    user_role: str | None,
    requested_user_id: str,
    owner_id: str | None,
    scope_id: str | None,
    publication_status: str | None,
) -> bool:
    """判断用户是否可下载指定版本；公开已发布文件不校验外部用户归属。"""
    if str(user_role or "").lower() != "external":
        return True
    return (
        str(owner_id or "") == requested_user_id
        or (str(scope_id or "public") == "public" and str(publication_status or "") == "PUBLISHED")
    )


def can_download_document_reference(user_role: str | None, knowledge_type: str | None) -> bool:
    """判断匹配结果中的原始附件是否应向外部用户展示下载入口。"""
    if str(user_role or "").lower() != "external":
        return True
    return str(knowledge_type or "").strip().lower() in _EXTERNAL_DOWNLOADABLE_TYPES


def can_delete_attachment(
    *,
    user_role: str | None,
    user_id: str,
    document_id: str,
    storage_key: str,
    records: Iterable[dict[str, Any]],
) -> bool:
    """外部用户只能删除归属自己的附件，内部身份可删除。"""
    if str(user_role or "").lower() != "external":
        return True
    for record in records:
        if str(record.get("user_id") or "") != user_id:
            continue
        candidates = [record, *(record.get("attachments") or [])]
        if any(
            isinstance(item, dict)
            and str(item.get("document_id") or "") == document_id
            and str(item.get("storage_key") or "") == storage_key
            for item in candidates
        ):
            return True
    return False
