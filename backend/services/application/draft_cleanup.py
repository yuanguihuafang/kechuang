"""共享草稿来源清理；HTTP 管理入口与生命周期补偿共用。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runtime.contracts import BusinessRecordSink
from services.application.draft_publishing import plan_draft_cleanup

BUSINESS_RECORD_TYPES = frozenset({"demand", "solution", "achievement", "customer_visit"})
MISSING_BUSINESS_RECORD = "MySQL 业务记录不存在或无权删除"


def normalize_record_type(value: object) -> str:
    return str(value or "").strip()


def draft_attachments(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    draft = item.get("draft")
    values = draft.get("attachments") if isinstance(draft, Mapping) else None
    return [value for value in values or [] if isinstance(value, dict)]


def delete_business_record(
    sink: BusinessRecordSink | None,
    *,
    record_type: str,
    record_id: str,
    user_id: str,
) -> bool:
    if sink is None or not record_id or record_type not in BUSINESS_RECORD_TYPES:
        return False
    try:
        sink.delete(record_type=record_type, record_id=record_id, user_id=user_id)
    except ValueError as exc:
        if str(exc) != MISSING_BUSINESS_RECORD:
            raise
        return False
    return True


def delete_linked_drafts(runtime: Any, document_id: str) -> int:
    store = runtime.require_draft_store()
    linked = [
        row for row in store.list_for_document(document_id) if row.get("status") != "DELETED"
    ]
    targets_by_id = {str(row["draft_id"]): row for row in linked}
    list_for_source_id = getattr(store, "list_for_source_id", None)
    if callable(list_for_source_id):
        for row in linked:
            source_id = str((row.get("draft") or {}).get("source_id") or "").strip()
            if not source_id:
                continue
            for sibling in list_for_source_id(source_id):
                if sibling.get("status") != "DELETED":
                    targets_by_id[str(sibling["draft_id"])] = sibling
    targets = list(targets_by_id.values())
    plan = plan_draft_cleanup(store, targets, allow_published_targets=True)
    deleted_keys = set()
    count = 0
    for item in targets:
        draft_type = str(
            item.get("draft_type") or (item.get("draft") or {}).get("draft_type") or ""
        )
        mysql_id = str((item.get("draft") or {}).get("mysql_record_id") or "").strip()
        user_id = str(item.get("user_id") or "")
        owner = user_id if draft_type == "customer_visit" else None
        key = (draft_type, mysql_id, owner)
        if key in plan["business_keys"] and key not in deleted_keys:
            delete_business_record(
                getattr(runtime, "business_record_sink", None),
                record_type=draft_type,
                record_id=mysql_id,
                user_id=user_id,
            )
            retrieval = runtime.embedded_retrieval.agent
            target = (
                retrieval.private_milvus
                if owner is not None
                else retrieval.public_milvus.get(draft_type)
            )
            if target is not None:
                target.delete_by_mysql_id(
                    mysql_id, **({"user_id": owner} if owner is not None else {})
                )
            deleted_keys.add(key)
        deleted = store.logical_delete(
            str(item["draft_id"]),
            user_id=user_id,
            operator_id="admin",
            allow_published=True,
        )
        if deleted is None:
            raise RuntimeError(f"关联草稿清理失败：{item['draft_id']}")
        count += 1
    return count
