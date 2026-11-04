"""PostgreSQL 平台状态仓储。

这里只保存平台运行状态，不写入 chunk_text、向量、原始文件或 MinerU 产物。
同步仓储使用 worker 级 psycopg 连接池；异步会话持久化由 LangGraph Checkpointer 管理独立连接池。
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from psycopg_pool import ConnectionPool

from services.persistence.sqlalchemy import close_sqlalchemy_engines

_POOL_LOCK = threading.Lock()
_POOLS: dict[str, Any] = {}
_DRAFT_VERSION_KEY = "_draft_version"

# platform_tasks.state 只保存运行状态和小型索引信息；正文、完整解析结果、向量
# 和大段请求上下文必须放在 MinIO 或其他专用存储中。
TASK_STATE_FIELDS = {
    "stage",
    "progress",
    "version_id",
    "storage_key",
    "file_name",
    "scope_id",
    "user_id",
    "user_role",
    "company",
    "knowledge_type",
    "thread_id",
    "idempotency_key",
    "attachments",
    "artifact_ref",
    "artifact_refs",
    "preview_text",
    "preview_blocks",
    "index_request_ref",
    "block_count",
    "chunk_count",
    "indexed_count",
    "index_time",
    "result_status",
    "indexed_chunks",
    "result_ref",
    "result",
    "error_code",
    "error",
    "attempt",
    "max_attempts",
    "timeout_seconds",
    "selected_tool",
    "evaluation",
    "compensation",
    "recovery_attempts",
    "recovery_error",
    "recovery_result",
    "recovered_by",
    "decision_note",
    "recovery_message",
    "recovery_tool",
    "recovery_arguments",
    "draft_id",
    "draft_ids",
    "draft_preview",
    "draft_previews",
    "draft_status",
    "intent_candidates",
    "intent_knowledge_type",
    "lifecycle_action",
    "remove_vectors",
    "operator_id",
    "operation_state",
    "last_id",
    "batch_size",
    "started",
    "replace_existing",
    "stop_requested",
    "total",
    "processed",
    "skipped_count",
    "query",
    "document_ids",
    "channel",
    "backup_generation",
    "displaced_generation",
    "snapshot_fingerprint",
    "accepted_missing_fingerprint",
    "active_generation",
    "shadow_generation",
    "expected_ids",
}


@contextmanager
def _pooled_connection(dsn: str):
    """使用共享平台状态连接池。"""

    with _POOL_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            min_size = int(os.getenv("POSTGRES_POOL_MIN_SIZE", "1"))
            max_size = int(os.getenv("POSTGRES_POOL_MAX_SIZE", "8"))
            timeout = float(os.getenv("POSTGRES_POOL_TIMEOUT_SECONDS", "10"))
            if min_size < 1 or max_size < min_size or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError(
                    "PostgreSQL pool requires min_size >= 1, max_size >= min_size, and timeout > 0"
                )
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=min_size,
                max_size=max_size,
                timeout=timeout,
                open=True,
            )
            _POOLS[dsn] = pool
    with pool.connection() as conn:
        yield conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def close_postgres_pools() -> None:
    """关闭本进程创建的 psycopg 与 SQLAlchemy PostgreSQL 池。"""

    with _POOL_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    failures = []
    for pool in pools:
        try:
            pool.close()
        except Exception as exc:  # noqa: BLE001 - 清理每个池后统一报告所有失败
            failures.append(exc)
    try:
        close_sqlalchemy_engines()
    except Exception as exc:  # noqa: BLE001 - 即使该池失败也已尝试关闭全部 psycopg 池
        failures.append(exc)
    if failures:
        raise RuntimeError(f"Failed to close {len(failures)} PostgreSQL pool(s)") from failures[0]


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def memory_content_hash(content: str) -> str:
    normalized = " ".join(str(content or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def memory_content_similarity(left: str, right: str) -> float:
    """计算记忆文本相似度；只用于保守合并近重复内容。"""

    a = " ".join(str(left or "").split()).casefold()
    b = " ".join(str(right or "").split()).casefold()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _draft_version(draft: dict[str, Any]) -> int:
    try:
        return max(1, int(draft.get(_DRAFT_VERSION_KEY, 1)))
    except (TypeError, ValueError):
        return 1


def _is_empty_draft_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_source_blocks(
    existing: dict[str, Any], incoming: dict[str, Any], *, replace: bool,
) -> list[Any]:
    old_blocks = existing.get("source_blocks") or []
    new_blocks = incoming.get("source_blocks") or []
    old_attachments = existing.get("attachments") or []
    new_attachments = incoming.get("attachments", old_attachments) or []

    def identity(block: dict[str, Any], attachments: list[Any]) -> tuple[Any, Any]:
        metadata = block.get("metadata") or {}
        for key in ("document_id", "version_id"):
            if block.get(key) and metadata.get(key) and block[key] != metadata[key]:
                raise ValueError(f"source_blocks contain conflicting {key}")
        document = block.get("document_id") or metadata.get("document_id")
        version = block.get("version_id") or metadata.get("version_id")
        candidates = {
            item["document_id"] for item in attachments
            if item.get("document_id") and (not version or item.get("version_id") == version)
        }
        if not document and len(candidates) == 1:
            document = next(iter(candidates))
        versions = {item.get("version_id") for item in attachments
                    if item.get("document_id") == document and item.get("version_id")}
        if not version and len(versions) == 1:
            version = next(iter(versions))
        return document, version

    old_context = old_attachments + [
        {**(block.get("metadata") or {}), **block} for block in old_blocks
    ]
    old_ids = [identity(block, old_context) for block in old_blocks]
    new_ids = [identity(block, new_attachments) for block in new_blocks]
    old_versions: dict[Any, set[Any]] = {}
    for document, version in old_ids + [identity(a, []) for a in old_attachments]:
        if document:
            old_versions.setdefault(document, set()).add(version)
    incoming_ids = new_ids + [identity(a, []) for a in incoming.get("attachments") or []]
    if any(not document for document, _ in old_ids) and any(
        document and version for document, version in incoming_ids
    ):
        raise ValueError("source_blocks document ownership is ambiguous")
    incoming_versions: dict[Any, set[Any]] = {}
    for document, version in incoming_ids:
        if document and version:
            incoming_versions.setdefault(document, set()).add(version)
    if any(len(versions) > 1 for versions in incoming_versions.values()):
        raise ValueError("source_blocks contain conflicting document versions")
    replaced = {
        document for document, version in incoming_ids
        if document and version and document in old_versions
        and old_versions[document] != {version}
    }
    if replaced and not isinstance(incoming.get("source_blocks"), list):
        raise ValueError("source_blocks must be supplied for a new document version")
    if replace:
        targets = {document for document, _ in new_ids if document}
        if not targets:
            targets = {a.get("document_id") for a in new_attachments if a.get("document_id")}
        replaced.update(targets)
        if not replaced:
            if any(document for document, _ in old_ids):
                raise ValueError("source_blocks replacement requires document_id")
            return deepcopy(new_blocks)
    if replaced:
        if any(not document for document, _ in old_ids + new_ids):
            raise ValueError("source_blocks document ownership is ambiguous")
        old_blocks = [block for block, (document, _) in zip(old_blocks, old_ids)
                      if document not in replaced]
    target = deepcopy(old_blocks)
    for block in new_blocks:
        if block not in target:
            target.append(deepcopy(block))
    return target


def _merge_draft(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    replace_source_blocks: bool = False,
    replace_fields: bool = False,
) -> dict[str, Any]:
    """合并字段增量和附件列表；新版本或显式编辑按文档替换来源块。"""
    merged = deepcopy(existing or {})
    source_blocks = (
        _merge_source_blocks(existing, incoming, replace=replace_source_blocks)
        if "source_blocks" in incoming or "attachments" in incoming else None
    )
    for key, value in (incoming or {}).items():
        if key == _DRAFT_VERSION_KEY:
            continue
        if key in {"fields", "mysql_fields"} and isinstance(value, dict):
            target = merged.get(key) if isinstance(merged.get(key), dict) else {}
            target = deepcopy(target)
            for field, field_value in value.items():
                if replace_fields or not _is_empty_draft_value(field_value) or field not in target:
                    target[field] = deepcopy(field_value)
            merged[key] = target
            continue
        if key == "attachments" and isinstance(value, list):
            merged[key] = deepcopy(value)
            continue
        if key == "source_blocks" and isinstance(value, list):
            merged[key] = source_blocks
            continue
        if not _is_empty_draft_value(value) or key not in merged:
            merged[key] = deepcopy(value)
    return merged


def _version_mismatch(current: int, expected: int | str | None) -> bool:
    if expected is None:
        return False
    try:
        expected_value = int(expected)
    except (TypeError, ValueError) as exc:
        raise ValueError("draft version must be an integer") from exc
    return expected_value != current


class InMemoryDraftStore:
    """无 PostgreSQL 时的开发回退；进程重启后清空。"""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def create(
        self,
        *,
        user_id: str,
        user_role: str,
        draft: dict[str, Any],
        thread_id: str | None = None,
        reuse_existing_thread_draft: bool = False,
    ) -> dict[str, Any]:
        if thread_id and reuse_existing_thread_draft:
            existing = [
                item
                for item in self._items.values()
                if item.get("user_id") == user_id
                and item.get("thread_id") == thread_id
                and item.get("draft_type") == draft.get("draft_type", "unknown")
                and item.get("status") != "DELETED"
            ]
            if existing:
                existing.sort(
                    key=lambda item: str(item.get("updated_at") or ""), reverse=True
                )
                return self.update(
                    existing[0]["draft_id"], user_id=user_id, draft=draft
                )
        now = _iso(_now())
        item = {
            "draft_id": f"draft_{uuid.uuid4().hex}",
            "user_id": user_id,
            "user_role": user_role,
            "draft_type": draft.get("draft_type", "unknown"),
            "status": "CONFIRMED",
            "thread_id": thread_id,
            "source_document_id": None,
            "draft": deepcopy(draft),
            "created_at": now,
            "updated_at": now,
            "version": 1,
            "revision": 1,
        }
        self._items[item["draft_id"]] = item
        return deepcopy(item)

    def create_many(
        self,
        *,
        user_id: str,
        user_role: str,
        drafts: list[dict[str, Any]],
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = _now()
        created: list[dict[str, Any]] = []
        for index, draft in enumerate(drafts):
            attachments = draft.get("attachments") or []
            first_attachment = attachments[0] if attachments else {}
            item = {
                "draft_id": f"draft_{uuid.uuid4().hex}",
                "user_id": user_id,
                "user_role": user_role,
                "draft_type": draft.get("draft_type", "unknown"),
                "status": "CONFIRMED",
                "thread_id": thread_id,
                "source_document_id": (
                    first_attachment.get("document_id")
                    if isinstance(first_attachment, dict)
                    else None
                ),
                "draft": deepcopy(draft),
                "created_at": _iso(now),
                "updated_at": _iso(now + timedelta(microseconds=len(drafts) - index - 1)),
                "version": 1,
                "revision": 1,
            }
            created.append(item)
        self._items.update({item["draft_id"]: item for item in created})
        return deepcopy(created)

    def get(self, draft_id: str, *, user_id: str) -> dict[str, Any] | None:
        item = self._items.get(draft_id)
        return item if item and item["user_id"] == user_id else None

    def list(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return [
            item
            for item in self._items.values()
            if item["user_id"] == user_id and item.get("status") != "DELETED"
        ][-limit:]

    def list_for_document(self, document_id: str) -> list[dict[str, Any]]:
        items = [
            item
            for item in self._items.values()
            if item.get("status") != "DELETED"
            and (
                item.get("source_document_id") == document_id
                or any(
                    isinstance(attachment, dict) and attachment.get("document_id") == document_id
                    for attachment in (item.get("draft") or {}).get("attachments", [])
                )
            )
        ]
        items.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("draft_id") or "")),
            reverse=True,
        )
        return deepcopy(items)

    def list_business_references(
        self, draft_type: str, mysql_record_id: str, *, user_id: str | None
    ) -> list[dict[str, Any]]:
        if draft_type == "customer_visit" and (not user_id or not user_id.strip()):
            raise ValueError("私有草稿引用查询需要 user_id")
        items = [
            item
            for item in self._items.values()
            if item.get("status") != "DELETED"
            and item.get("draft_type") == draft_type
            and (item.get("draft") or {}).get("mysql_record_id") is not None
            and str((item.get("draft") or {}).get("mysql_record_id")) == mysql_record_id
            and (draft_type != "customer_visit" or item.get("user_id") == user_id)
        ]
        items.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("draft_id") or "")),
            reverse=True,
        )
        return deepcopy(items)

    def list_storage_key_references(self, storage_key: str) -> list[dict[str, Any]]:
        items = [
            item
            for item in self._items.values()
            if item.get("status") != "DELETED"
            and any(
                isinstance(attachment, dict) and attachment.get("storage_key") == storage_key
                for attachment in (item.get("draft") or {}).get("attachments", [])
            )
        ]
        items.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("draft_id") or "")),
            reverse=True,
        )
        return deepcopy(items)

    def list_all(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = list(self._items.values())
        if status:
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[
            :limit
        ]

    def list_page(
        self,
        *,
        status: str | None = None,
        draft_types: list[str] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        items = list(self._items.values())
        if status:
            items = [item for item in items if item.get("status") == status]
        if draft_types:
            allowed_types = set(draft_types)
            items = [item for item in items if item.get("draft_type") in allowed_types]
        items.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("draft_id") or "")),
            reverse=True,
        )
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        start = (page - 1) * page_size
        total = len(items)
        return {
            "items": deepcopy(items[start : start + page_size]),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def get_admin(self, draft_id: str) -> dict[str, Any] | None:
        item = self._items.get(draft_id)
        return deepcopy(item) if item else None

    def list_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        items = [item for item in self._items.values() if item.get("thread_id") == thread_id]
        items.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("draft_id") or "")),
            reverse=True,
        )
        return deepcopy(items)

    def list_for_source_id(self, source_id: str) -> list[dict[str, Any]]:
        source_id = str(source_id or "").strip()
        if not source_id:
            return []
        items = [
            item for item in self._items.values()
            if str((item.get("draft") or {}).get("source_id") or "").strip() == source_id
        ]
        items.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("draft_id") or "")),
            reverse=True,
        )
        return deepcopy(items)

    def owner_hints_for_threads(self, thread_ids: list[str]) -> dict[str, dict[str, str]]:
        wanted = set(thread_ids)
        hints: dict[str, dict[str, str]] = {}
        items = sorted(
            self._items.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True
        )
        for item in items:
            thread_id = str(item.get("thread_id") or "")
            user_id = str(item.get("user_id") or "").strip()
            if thread_id in wanted and user_id and thread_id not in hints:
                hints[thread_id] = {
                    "user_id": user_id,
                    "user_role": str(item.get("user_role") or "").strip(),
                }
        return hints

    def update(
        self,
        draft_id: str,
        *,
        user_id: str,
        draft: dict[str, Any],
        status: str | None = None,
        expected_version: int | str | None = None,
        replace_source_blocks: bool = False,
        replace_fields: bool = False,
    ) -> dict[str, Any] | None:
        item = self.get(draft_id, user_id=user_id)
        if not item:
            return None
        current_version = int(item.get("version", 1))
        if _version_mismatch(current_version, expected_version):
            raise ValueError("draft version mismatch")
        item["draft"] = _merge_draft(
            item.get("draft") or {}, draft, replace_source_blocks=replace_source_blocks,
            replace_fields=replace_fields,
        )
        item["draft_type"] = item["draft"].get("draft_type", item["draft_type"])
        item["source_document_id"] = (item["draft"].get("attachments") or [{}])[0].get(
            "document_id"
        )
        item["status"] = status or item["status"]
        item["version"] = current_version + 1
        item["revision"] = item["version"]
        item["updated_at"] = _iso(_now())
        return item

    def remove_attachment(
        self, draft_id: str, *, user_id: str, document_id: str | None = None,
        storage_key: str | None = None,
    ) -> dict[str, Any] | None:
        item = self._items.get(draft_id)
        if item is None or item.get("user_id") != user_id:
            return None
        if item.get("status") == "PUBLISHED":
            raise ValueError("已发布草稿不能删除附件，请先下架")
        draft = item.setdefault("draft", {})
        attachments = draft.get("attachments") or []
        kept = [attachment for attachment in attachments if not (
            (document_id and attachment.get("document_id") == document_id)
            or (not document_id and storage_key and attachment.get("storage_key") == storage_key)
        )]
        if len(kept) == len(attachments):
            return deepcopy(item)
        draft["attachments"] = kept
        draft["source_blocks"] = []
        item["source_document_id"] = next(
            (attachment.get("document_id") for attachment in kept if attachment.get("document_id")),
            None,
        )
        item["version"] = int(item.get("version", 1)) + 1
        item["revision"] = item["version"]
        item["updated_at"] = _iso(_now())
        return deepcopy(item)

    def logical_delete(
        self,
        draft_id: str,
        *,
        user_id: str,
        operator_id: str | None = None,
        allow_published: bool = False,
    ) -> dict[str, Any] | None:
        item = self.get(draft_id, user_id=user_id)
        if not item:
            return None
        if item.get("status") == "PUBLISHED" and not allow_published:
            raise ValueError("已发布草稿请先下架后再删除")
        deleted_at = _iso(_now())
        item["status"] = "DELETED"
        item.setdefault("draft", {}).update({
            "deleted_by": operator_id or user_id,
            "deleted_at": deleted_at,
        })
        item["version"] = int(item.get("version", 1)) + 1
        item["revision"] = item["version"]
        item["updated_at"] = deleted_at
        return item

    def purge_deleted(self, draft_id: str) -> bool:
        item = self._items.get(draft_id)
        if not item or item.get("status") != "DELETED":
            return False
        del self._items[draft_id]
        return True

    def transition(
        self,
        draft_id: str,
        *,
        user_id: str,
        target_status: str,
        operator_id: str | None = None,
        operator_role: str = "internal",
        note: str | None = None,
        expected_version: int | str | None = None,
    ) -> dict[str, Any] | None:
        item = self.get(draft_id, user_id=user_id)
        if item is None:
            return None
        _validate_draft_transition(item["status"], target_status)
        _validate_draft_transition_permission(
            item,
            target_status=target_status,
            operator_id=operator_id,
            operator_role=operator_role,
        )
        current_version = int(item.get("version", 1))
        if _version_mismatch(current_version, expected_version):
            raise ValueError("draft version mismatch")
        item["status"] = target_status
        item["draft"] = _merge_draft(
            item.get("draft") or {},
            _transition_metadata(target_status, operator_id, operator_role, note),
        )
        if target_status == "PUBLISHED":
            item["draft"].pop("status_note", None)
        item["version"] = current_version + 1
        item["revision"] = item["version"]
        item["updated_at"] = _iso(_now())
        return item


_DRAFT_TRANSITIONS: dict[str, set[str]] = {
    "CONFIRMED": {"PENDING_REVIEW", "PUBLISHED", "WITHDRAWN"},
    "PENDING_REVIEW": {"PUBLISHED", "CONFIRMED"},
    "PUBLISHED": {"WITHDRAWN"},
    "WITHDRAWN": {"PENDING_REVIEW", "PUBLISHED"},
}


def _draft_text(draft: dict[str, Any]) -> str:
    """把草稿转换为相似度比较文本，不把附件二进制或私密元数据送入比较。"""

    values: list[str] = [str(draft.get("title") or "")]
    for key in ("requirement_summary", "technical_summary", "achievement_summary", "summary"):
        values.append(str(draft.get(key) or ""))
    values.extend(str(value) for value in (draft.get("fields") or {}).values() if value)
    return "\n".join(value for value in values if value.strip())


def _validate_draft_transition(current_status: str, target_status: str) -> None:
    if target_status not in _DRAFT_TRANSITIONS.get(current_status, set()):
        raise ValueError(f"草稿状态不允许从 {current_status} 转为 {target_status}")


def _validate_publish_permission(operator_role: str) -> None:
    if operator_role not in {"admin", "internal", "external"}:
        raise PermissionError("当前身份无权提交公开草稿")


def _validate_draft_transition_permission(
    draft: dict[str, Any],
    *,
    target_status: str,
    operator_id: str | None,
    operator_role: str,
) -> None:
    """校验草稿状态变更；外部用户只能提交或撤回自己的草稿。"""

    if target_status == "PUBLISHED":
        _validate_publish_permission(operator_role)
        if operator_role in {"admin", "internal"}:
            return
        if operator_role == str(draft.get("user_role") or "") and str(operator_id or "") == str(
            draft.get("user_id") or ""
        ):
            return
        raise PermissionError("外部用户只能发布自己的公开草稿")
    if target_status not in {"PENDING_REVIEW", "WITHDRAWN"}:
        return
    if operator_role in {"admin", "internal"}:
        return
    owner_role = str(draft.get("user_role") or "")
    owner_id = str(draft.get("user_id") or "")
    if (
        operator_role == "external"
        and operator_role == owner_role
        and str(operator_id or "") == owner_id
    ):
        return
    raise PermissionError("外部用户只能提交或撤回本人草稿，不能替他人变更状态")


def _transition_metadata(
    target_status: str, operator_id: str | None, operator_role: str, note: str | None
) -> dict[str, Any]:
    if target_status != "PUBLISHED":
        return {"status_note": note} if note else {}
    return {
        "approval": {
            "operator_id": operator_id or "unknown-operator",
            "operator_role": operator_role,
            "note": note or "测试阶段审核通过",
            "approved_at": _iso(_now()),
        }
    }


def check_postgres(dsn: str | None = None) -> None:
    """连接平台状态库并执行轻量查询，供健康检查使用。"""

    target = dsn or os.getenv("DATABASE_URL", "")
    if not target:
        raise ValueError("DATABASE_URL 未配置")
    from .sqlalchemy import get_engine

    with get_engine(target).connect() as conn:
        conn.exec_driver_sql("SELECT 1").fetchone()


def check_schema_version(dsn: str | None = None) -> None:
    target = dsn or os.getenv("DATABASE_URL", "")
    if not target:
        raise ValueError("DATABASE_URL 未配置")
    from .sqlalchemy import get_engine

    with get_engine(target).connect() as conn:
        row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").fetchone()
    if not row:
        raise RuntimeError("数据库未执行 Alembic baseline，请先运行 alembic upgrade head")
