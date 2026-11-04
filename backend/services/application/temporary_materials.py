"""会话临时材料的过期与清理规则。"""

from __future__ import annotations

import logging
import asyncio
import os
from services.persistence.retention import (
    PostgresRetentionService,
    cleanup_expired_admin_documents,
    collect_storage_references,
    seconds_until_daily_cleanup,
    serialize_storage_references,
    storage_reference_guard,
)
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.storage.minio.minio_storage import MinioUploadService
from datetime import datetime, timedelta, timezone
from typing import Any


TEMPORARY_MATERIAL_TTL = timedelta(hours=24)
SAVED_DRAFT_ATTACHMENT_TTL = timedelta(days=30)
SAVED_DRAFT_STATUSES = frozenset({"CONFIRMED", "PENDING_REVIEW"})
logger = logging.getLogger(__name__)


def temporary_expires_at() -> str:
    return (datetime.now(timezone.utc) + TEMPORARY_MATERIAL_TTL).isoformat()


def is_expired_attachment(item: dict[str, Any], *, now: datetime | None = None) -> bool:
    if not item.get("temporary"):
        return False
    value = str(item.get("expires_at") or "").strip()
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= (now or datetime.now(timezone.utc))


def is_saved_draft_expired(draft_record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """判断未发布草稿是否已超过附件保留期；已发布/下架草稿不在此策略内。"""

    if str(draft_record.get("status") or "") not in SAVED_DRAFT_STATUSES:
        return False
    value = str(draft_record.get("updated_at") or "").strip()
    if not value:
        return False
    try:
        updated_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at + SAVED_DRAFT_ATTACHMENT_TTL <= (now or datetime.now(timezone.utc))


def mark_saved_draft_attachments_expired(
    draft: dict[str, Any], *, expired_at: datetime | None = None
) -> dict[str, Any]:
    """保留草稿展示信息，但移除已清理附件的 MinIO 定位。"""

    timestamp = (expired_at or datetime.now(timezone.utc)).isoformat()
    updated = dict(draft)
    updated["attachments"] = [
        {
            **item,
            "storage_key": "",
            "retention_status": "EXPIRED",
            "retention_expired_at": timestamp,
        }
        for item in draft_attachments(draft)
    ]
    return updated


def draft_attachments(draft: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [item for item in (draft or {}).get("attachments") or [] if isinstance(item, dict)]


def bind_thread_upload_attachments(
    draft: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    thread_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Add this user's active uploads from the draft's conversation to a new draft."""
    attachments = draft_attachments(draft)
    positions = {
        str(item["document_id"]): index
        for index, item in enumerate(attachments)
        if item.get("document_id")
    }
    for task in tasks:
        if (
            task.get("operation") != "user_draft"
            or task.get("status") not in {"PARSING", "WAITING_DRAFT_CONFIRM"}
            or str(task.get("user_id") or "") != user_id
            or str(task.get("thread_id") or "") != thread_id
        ):
            continue
        for attachment in task.get("attachments") or []:
            if not isinstance(attachment, dict) or not attachment.get("document_id"):
                continue
            document_id = str(attachment["document_id"])
            if document_id in positions:
                index = positions[document_id]
                attachments[index] = {**attachments[index], **attachment}
            else:
                positions[document_id] = len(attachments)
                attachments.append(dict(attachment))
    return {**draft, "attachments": attachments}


def has_expired_attachment(draft: dict[str, Any] | None) -> bool:
    return any(is_expired_attachment(item) for item in draft_attachments(draft))


def retain_draft_attachments(draft: dict[str, Any]) -> dict[str, Any]:
    """保存草稿后移除临时标记，后续不再参与 24 小时清理。"""
    retained = dict(draft)
    retained["attachments"] = [
        {key: value for key, value in item.items() if key not in {"temporary", "expires_at"}}
        for item in draft_attachments(draft)
    ]
    return retained


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


async def _delete_user_attachment(
    storage: MinioUploadService,
    lifecycle: DocumentLifecycleStore,
    attachment: dict[str, object],
) -> None:
    storage_key = str(attachment.get("storage_key") or "").strip()
    if not storage_key:
        return
    document_id = str(attachment.get("document_id") or "").strip()
    await storage.delete_document(storage_key, document_id or None)
    if document_id:
        lifecycle.purge_document(document_id)


async def _cleanup_saved_draft_attachments(
    draft_store: object,
    storage: MinioUploadService,
    drafts: list[dict[str, object]],
    published_document_ids: set[str],
    lifecycle: DocumentLifecycleStore,
) -> int:
    cleaned = 0
    for record in drafts:
        if not is_saved_draft_expired(record):
            continue
        draft = record.get("draft") if isinstance(record.get("draft"), dict) else {}
        attachments = [item for item in draft.get("attachments") or [] if isinstance(item, dict)]
        if not attachments:
            continue
        if any(
            str(item.get("document_id") or "").strip() in published_document_ids
            for item in attachments
        ):
            continue
        shared = []
        for item in attachments:
            shared.extend(
                draft_store.list_storage_key_references(str(item.get("storage_key") or ""))
            )
            if item.get("document_id"):
                shared.extend(draft_store.list_for_document(str(item["document_id"])))
        if any(
            row.get("draft_id") != record["draft_id"]
            and row.get("status") != "DELETED"
            and not is_saved_draft_expired(row)
            for row in shared
        ):
            continue
        try:
            for item in attachments:
                await _delete_user_attachment(storage, lifecycle, item)
            updated = mark_saved_draft_attachments_expired(draft)
            draft_store.update(
                str(record.get("draft_id") or ""),
                user_id=str(record.get("user_id") or ""),
                draft=updated,
                status=str(record.get("status") or "CONFIRMED"),
            )
            cleaned += len(attachments)
        except Exception:
            logger.exception("保存草稿附件 30 天保留清理失败: draft_id=%s", record.get("draft_id"))
    return cleaned


def _deleted_draft_expired(record: dict[str, Any], cutoff: datetime) -> bool:
    if record.get("status") != "DELETED":
        return False
    draft = record.get("draft") if isinstance(record.get("draft"), dict) else {}
    value = str(draft.get("deleted_at") or "").strip()
    try:
        deleted_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if deleted_at.tzinfo is None:
        deleted_at = deleted_at.replace(tzinfo=timezone.utc)
    return deleted_at is not None and deleted_at < cutoff


async def _cleanup_deleted_drafts(
    runtime: Any,
    drafts: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> int:
    """Purge only 30-day-old deleted groups with no live or recent shared references."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=30)
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in drafts:
        draft = record.get("draft") if isinstance(record.get("draft"), dict) else {}
        source_id = str(draft.get("source_id") or "").strip()
        group_id = source_id or str(record.get("draft_id") or "")
        if group_id:
            groups.setdefault(group_id, []).append(record)

    purged = 0
    for group in groups.values():
        if not group or not all(_deleted_draft_expired(record, cutoff) for record in group):
            continue
        target_ids = {str(record.get("draft_id") or "") for record in group}
        refs = collect_storage_references(documents=(), drafts=group, tasks=())
        target_source_ids = {
            str((record.get("draft") or {}).get("source_id") or "").strip()
            for record in group
        } - {""}
        target_business_ids = set()
        for record in group:
            draft = record.get("draft") if isinstance(record.get("draft"), dict) else {}
            mysql_id = str(draft.get("mysql_record_id") or "").strip()
            if mysql_id:
                draft_type = str(record.get("draft_type") or draft.get("draft_type") or "")
                owner = str(record.get("user_id") or "") if draft_type == "customer_visit" else ""
                target_business_ids.add((draft_type, mysql_id, owner))

        shared = False
        for record in drafts:
            if str(record.get("draft_id") or "") in target_ids:
                continue
            external_refs = collect_storage_references(documents=(), drafts=[record], tasks=())
            external_draft = record.get("draft") if isinstance(record.get("draft"), dict) else {}
            external_source = str(external_draft.get("source_id") or "").strip()
            external_mysql_id = str(external_draft.get("mysql_record_id") or "").strip()
            external_type = str(record.get("draft_type") or external_draft.get("draft_type") or "")
            external_owner = str(record.get("user_id") or "") if external_type == "customer_visit" else ""
            external_business = (external_type, external_mysql_id, external_owner)
            if (
                refs.document_ids & external_refs.document_ids
                or refs.storage_keys & external_refs.storage_keys
                or (external_source and external_source in target_source_ids)
                or (external_mysql_id and external_business in target_business_ids)
            ) and not _deleted_draft_expired(record, cutoff):
                shared = True
                break
        if shared:
            continue

        lifecycle = getattr(runtime, "lifecycle_service", None)
        if refs.document_ids:
            if lifecycle is None:
                continue
            try:
                if any(
                    version.get("publication_status") == "PUBLISHED"
                    and version.get("status") != "PURGED"
                    for document_id in refs.document_ids
                    for version in lifecycle.list_versions(document_id)
                ):
                    continue
            except Exception:
                logger.exception(
                    "读取已删除草稿的附件版本失败，跳过硬删除: draft_ids=%s",
                    sorted(target_ids),
                )
                continue

        try:
            result = await runtime.admin_service.delete_draft(str(group[0]["draft_id"]))
            if not isinstance(result, dict) or result.get("status") != "PURGED":
                raise RuntimeError("硬删除未确认完成")
            purged += len(group)
        except Exception:
            logger.exception("30 天已删除草稿清理失败: draft_ids=%s", sorted(target_ids))
    return purged


@serialize_storage_references
async def _cleanup_orphaned_storage(runtime: Any, storage: MinioUploadService) -> dict[str, int]:
    retention = PostgresRetentionService()
    references = await asyncio.to_thread(retention.storage_references)
    grace = timedelta(hours=_positive_env_int("MINIO_ORPHAN_GRACE_HOURS", 48))
    return await storage.cleanup_orphaned_objects(
        referenced_document_ids=references.document_ids,
        referenced_storage_keys=references.storage_keys,
        older_than=datetime.now(timezone.utc) - grace,
        is_referenced=retention.is_storage_referenced,
    )


async def cleanup_temporary_materials(runtime: Any, retention_stop: asyncio.Event) -> None:
    storage = MinioUploadService()
    while not retention_stop.is_set():
        try:
            delay = seconds_until_daily_cleanup()
            try:
                await asyncio.wait_for(retention_stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            if retention_stop.is_set():
                break
            async with storage_reference_guard():
                all_drafts = _all_cleanup_pages(runtime.draft_store_service.list_page)
                purged_drafts = await _cleanup_deleted_drafts(runtime, all_drafts)
                if purged_drafts:
                    logger.info("30 天已删除草稿清理完成: drafts=%s", purged_drafts)
                all_drafts = _all_cleanup_pages(runtime.draft_store_service.list_page)
                published_document_ids = _published_document_ids(runtime.lifecycle_service)
                published_document_ids.update(
                    str(attachment.get("document_id") or "")
                    for record in _all_cleanup_pages(
                        runtime.draft_store_service.list_page, status="PUBLISHED"
                    )
                    for attachment in (record.get("draft") or {}).get("attachments") or []
                    if isinstance(attachment, dict) and attachment.get("document_id")
                )
                try:
                    await cleanup_expired_admin_documents(
                        runtime.lifecycle_service,
                        storage,
                        protected_document_ids=published_document_ids,
                    )
                except Exception:
                    logger.exception("管理台企业/专家文档过期清理失败")
                saved_documents = {
                    str(item.get("document_id") or "")
                    for item in all_drafts
                    for attachment in (item.get("draft") or {}).get("attachments") or []
                    if isinstance(attachment, dict) and attachment.get("document_id")
                }
                for task in _all_cleanup_pages(
                    runtime.task_store.list_page, operation="user_draft"
                ):
                    attachments = [
                        item for item in task.get("attachments") or [] if isinstance(item, dict)
                    ]
                    expired = [
                        item
                        for item in attachments
                        if is_expired_attachment(item)
                        and str(item.get("document_id") or "") not in saved_documents
                    ]
                    cleanup_failed = False
                    for item in expired:
                        storage_key = str(item.get("storage_key") or "").strip()
                        try:
                            await _delete_user_attachment(storage, runtime.lifecycle_service, item)
                        except Exception:
                            cleanup_failed = True
                            logger.exception(
                                "临时附件清理失败: task_id=%s, document_id=%s, storage_key=%s",
                                task.get("task_id"),
                                item.get("document_id"),
                                storage_key,
                            )
                    if expired and not cleanup_failed:
                        runtime.task_store.update(
                            task["task_id"],
                            status="EXPIRED",
                            stage="expired",
                            error="临时附件已超过 24 小时，原文件及解析产物已清理",
                        )
                await _cleanup_saved_draft_attachments(
                    runtime.draft_store_service,
                    storage,
                    all_drafts,
                    published_document_ids,
                    runtime.lifecycle_service,
                )
                try:
                    asr_cutoff = datetime.now(timezone.utc) - timedelta(
                        hours=_positive_env_int("ASR_AUDIO_OBJECT_RETENTION_HOURS", 2)
                    )
                    await storage.cleanup_objects_older_than("asr/", older_than=asr_cutoff)
                except Exception:
                    logger.exception("ASR 临时音频兜底清理失败")
                try:
                    orphan_counts = await _cleanup_orphaned_storage(runtime, storage)
                    logger.info("MinIO 孤儿对象扫描完成: %s", orphan_counts)
                except Exception:
                    logger.exception("MinIO 孤儿对象扫描失败")
        except Exception:
            logger.exception("临时附件清理任务失败")


def _all_cleanup_pages(query: Any, *, items_key: str = "items", **filters: Any) -> list[Any]:
    records = []
    page = 1
    expected_total = None
    while True:
        result = query(page=page, page_size=100, **filters)
        items = result[items_key]
        total = result.get("total")
        if total is not None:
            total = int(total)
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise RuntimeError("Cleanup inventory changed during pagination")
        if not items:
            if expected_total is not None and len(records) != expected_total:
                raise RuntimeError("Incomplete cleanup inventory")
            return records
        records.extend(items)
        if expected_total is not None and len(records) > expected_total:
            raise RuntimeError("Cleanup inventory exceeded reported total")
        if expected_total is not None and len(records) == expected_total:
            return records
        page += 1


def _published_document_ids(lifecycle: Any) -> set[str]:
    return {
        row["document_id"]
        for row in _all_cleanup_pages(
            lifecycle.list_documents_page, items_key="documents", include_purged=True
        )
        if any(
            version.get("publication_status") == "PUBLISHED" and version.get("status") != "PURGED"
            for version in lifecycle.list_versions(row["document_id"])
        )
    }
