"""管理员应用服务。

这里不启动独立网关，也不复制 Agent 逻辑；管理员操作直接复用运行时中
已经装配好的 Supervisor、Perception、Retrieval 和 PostgreSQL 服务。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Protocol, cast
from urllib.parse import quote

from core.contracts.knowledge import (
    DocumentUnpublishRequest,
    resolve_knowledge_type,
)
from core.governance.access_policy import ManualIdentity, UserType
from runtime.container import GatewayRuntime
from runtime.contracts import StorageService, TaskStore
from services.application.business_records import canonical_source_id, _source_id
from services.application.draft_cleanup import (
    delete_business_record,
    draft_attachments,
    normalize_record_type,
)
from services.application.draft_publishing import plan_draft_cleanup
from services.persistence.retention import serialize_storage_references

from .documents import AdminDocumentMixin
from .governance import AdminGovernanceMixin
from .mysql_sync import AdminMysqlSyncMixin, _MilvusTarget, _RetrievalAdminAgent, _SyncStateStore


class _SessionStore(Protocol):
    def list_all_sessions_page(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]: ...
    def get(self, session_id: str) -> dict[str, Any] | None: ...
    def list_session_messages_admin(self, session_id: str) -> list[dict[str, Any]]: ...
    def delete_session_data(self, session_id: str) -> bool: ...


class _MatchResultStore(Protocol):
    def list_by_session(self, *, session_id: str, limit: int = 100) -> list[dict[str, Any]]: ...


class AdminService(AdminGovernanceMixin, AdminDocumentMixin, AdminMysqlSyncMixin):
    """管理员控制台的应用层门面。

    HTTP 路由只负责参数和状态码；审核、治理、指标及 Agent 调用集中在这里。
    """

    def __init__(self, runtime: GatewayRuntime, *, session_store: Any = None, supervisor_metrics: Any = None) -> None:
        self.runtime = runtime
        self.session_store = session_store
        self.supervisor_metrics = supervisor_metrics
        self._task_store = runtime.task_store
        self._sync_state_store: _SyncStateStore | None = cast(
            _SyncStateStore | None,
            getattr(runtime, "sync_state_store", None),
        )
        self._mysql_counts_cache: dict[str, Any] | None = None
        self._mysql_counts_refreshed_at = 0.0
        self._mysql_counts_refresh_lock = threading.Lock()

    def _task_store_required(self) -> TaskStore:
        if self._task_store is None:
            raise RuntimeError("任务存储尚未初始化")
        return self._task_store

    def _retrieval_agent(self) -> _RetrievalAdminAgent:
        return cast(_RetrievalAdminAgent, self.runtime.embedded_retrieval.agent)

    def _session_store(self) -> _SessionStore:
        session_store = self.session_store
        if session_store is None:
            raise RuntimeError("会话存储未初始化")
        return cast(_SessionStore, session_store)

    def conversations(self, *, page: int = 1, page_size: int = 20) -> dict[str, object]:
        session_store = self._session_store()
        result = session_store.list_all_sessions_page(page=page, page_size=page_size)
        sessions = result["items"]
        missing_user_ids = [
            str(item.get("session_id") or "") for item in sessions if not item.get("user_id")
        ]
        owner_hints = self.runtime.require_draft_store().owner_hints_for_threads(missing_user_ids)
        items = [
            self._with_session_owner(
                item, owner_hint=owner_hints.get(str(item.get("session_id") or ""))
            )
            for item in sessions
        ]
        return {
            "items": items,
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "total_pages": result["total_pages"],
        }

    @staticmethod
    def _profile_value(profile: object, key: str) -> object | None:
        if isinstance(profile, dict):
            return profile.get(key)
        return getattr(profile, key, None)

    @staticmethod
    def _owner_hint_from_drafts(
        session_id: str, drafts: list[dict[str, Any]]
    ) -> dict[str, str] | None:
        for draft in drafts:
            if str(draft.get("thread_id") or "") != session_id:
                continue
            user_id = str(draft.get("user_id") or "").strip()
            if user_id:
                return {
                    "user_id": user_id,
                    "user_role": str(draft.get("user_role") or "").strip(),
                }
        return None

    def _with_session_owner(
        self, session: dict[str, Any], *, owner_hint: dict[str, str] | None = None
    ) -> dict[str, Any]:
        session = {**session}
        if not session.get("user_id") and owner_hint:
            session.update(owner_hint)
        user_id = str(session.get("user_id") or "").strip()
        user_role = str(session.get("user_role") or "").strip().lower()
        if user_id == "admin" or user_role == "admin":
            return {**session, "user_name": "管理员", "company_name": "平台管理员"}
        reader = getattr(self.runtime, "user_identity_reader", None)
        if reader is None or not user_id:
            return {**session, "user_name": user_id or "未知用户", "company_name": ""}
        try:
            profile = reader.get(user_id)
        except Exception:  # noqa: BLE001 - 身份增强失败不阻断会话查看
            profile = None
        return {
            **session,
            "user_name": self._profile_value(profile, "name") or user_id,
            "company_name": self._profile_value(profile, "company") or "",
        }

    def conversation_detail(self, session_id: str) -> dict[str, object] | None:
        session_id = str(session_id or "").strip()
        session_store = self._session_store()
        session = session_store.get(session_id)
        if session is None:
            return None
        all_drafts = self.runtime.require_draft_store().list_for_thread(session_id)
        session = self._with_session_owner(
            session,
            owner_hint=self._owner_hint_from_drafts(session_id, all_drafts),
        )
        drafts = [
            item
            for item in all_drafts
            if str(item.get("thread_id") or "") == session_id and item.get("status") != "DELETED"
        ]
        attachments: dict[str, dict[str, object]] = {}
        for item in drafts:
            for attachment in (item.get("draft") or {}).get("attachments") or []:
                if not isinstance(attachment, dict) or not attachment.get("document_id"):
                    continue
                document_id = str(attachment["document_id"])
                attachments.setdefault(
                    document_id,
                    {
                        "document_id": document_id,
                        "file_name": attachment.get("file_name") or document_id,
                        "file_url": f"/api/v1/knowledge/documents/{quote(document_id, safe='')}/file?user_role=admin&user_id=admin",
                    },
                )
        messages = session_store.list_session_messages_admin(session_id)
        match_results = []
        match_store = cast(
            _MatchResultStore | None, getattr(self.runtime, "match_result_store", None)
        )
        stored_matches = (
            match_store.list_by_session(session_id=session_id, limit=100)
            if match_store is not None and callable(getattr(match_store, "list_by_session", None))
            else []
        )
        for item in stored_matches:
            raw_result = item.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
            raw_candidates = result.get("match_results")
            candidates: list[dict[str, Any]] = (
                [candidate for candidate in raw_candidates if isinstance(candidate, dict)]
                if isinstance(raw_candidates, list)
                else []
            )
            if not candidates:
                raw_ui_blocks = result.get("ui_blocks")
                ui_blocks = raw_ui_blocks if isinstance(raw_ui_blocks, list) else []
                for block in ui_blocks:
                    if not isinstance(block, dict) or block.get("type") != "recommendation_list":
                        continue
                    data = block.get("data")
                    if not isinstance(data, dict):
                        continue
                    items = data.get("items")
                    if isinstance(items, list):
                        candidates.extend(
                            candidate for candidate in items if isinstance(candidate, dict)
                        )
            match_results.append(
                {
                    "result_id": item.get("result_id"),
                    "query": item.get("query") or "",
                    "draft_id": item.get("draft_id"),
                    "created_at": item.get("created_at"),
                    "items": candidates,
                    "answer": result.get("answer") or "",
                }
            )
        return {
            "session": session,
            "messages": messages,
            "drafts": drafts,
            "attachments": list(attachments.values()),
            "match_results": match_results,
        }

    @serialize_storage_references
    async def delete_conversation(self, session_id: str) -> dict[str, object]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("会话 ID 不能为空")
        session_store = self._session_store()
        if session_store.get(session_id) is None:
            raise LookupError("会话不存在")

        draft_store = self.runtime.require_draft_store()
        drafts = [
            item
            for item in draft_store.list_for_thread(session_id)
            if item.get("status") != "DELETED"
        ]
        plan = plan_draft_cleanup(
            draft_store, drafts, allow_published_targets=True
        )
        deleted_mysql: set[tuple[str, str]] = set()
        deleted_vectors: set[tuple[str, str, str | None]] = set()
        deleted_files: set[tuple[str, str | None]] = set()
        retrieval = self._retrieval_agent()
        sink = getattr(self.runtime, "business_record_sink", None)
        for item in drafts:
            draft = item.get("draft") or {}
            draft_type = str(item.get("draft_type") or draft.get("draft_type") or "")
            record_type = normalize_record_type(draft_type)
            mysql_id = str(draft.get("mysql_record_id") or "").strip()
            if (
                (record_type, mysql_id, str(item.get("user_id") or "") if record_type == "customer_visit" else None) in plan["business_keys"]
                and (record_type, mysql_id) not in deleted_mysql
                and delete_business_record(
                    sink,
                    record_type=record_type,
                    record_id=mysql_id,
                    user_id=str(item.get("user_id") or ""),
                )
            ):
                deleted_mysql.add((record_type, mysql_id))

            attachments = draft_attachments(item)
            source_id = str(draft.get("source_id") or "").strip() or _source_id(attachments)
            target = (
                retrieval.private_milvus
                if record_type == "customer_visit"
                else retrieval.public_milvus.get(record_type)
            )
            if target is not None:
                for value, method in (
                    (mysql_id, "delete_by_mysql_id"),
                    (source_id, "delete_by_source_id"),
                ):
                    if not value:
                        continue
                    cleanup_id = canonical_source_id(value) if method == "delete_by_source_id" else value
                    key = (
                        record_type,
                        cleanup_id,
                        session_id if target is retrieval.private_milvus else None,
                    )
                    if (
                        getattr(target, method, None)
                        and key not in deleted_vectors
                        and (record_type, cleanup_id, str(item.get("user_id") or "") if record_type == "customer_visit" else None)
                        in plan["source_keys" if method == "delete_by_source_id" else "business_keys"]
                    ):
                        kwargs = (
                            {"user_id": str(item.get("user_id") or "")}
                            if target is retrieval.private_milvus
                            else {}
                        )
                        getattr(target, method)(value, **kwargs)
                        deleted_vectors.add(key)
            for attachment in attachments:
                document_id = str(attachment.get("document_id") or "").strip()
                storage_key = str(attachment.get("storage_key") or "").strip()
                file_key = (storage_key, document_id or None)
                storage = cast(
                    StorageService | None, getattr(self.runtime, "storage_service", None)
                )
                if (
                    storage_key
                    and file_key in plan["files"]
                    and (not document_id or document_id in plan["document_ids"])
                    and file_key not in deleted_files
                    and storage is not None
                ):
                    await storage.delete_document(storage_key, document_id or None)
                    deleted_files.add(file_key)

        session_store.delete_session_data(session_id)
        return {
            "status": "DELETED",
            "session_id": session_id,
            "draft_count": len(drafts),
            "mysql_count": len(deleted_mysql),
            "milvus_count": len(deleted_vectors),
            "attachment_count": len(deleted_files),
        }

    @serialize_storage_references
    async def delete_draft(self, draft_id: str) -> dict[str, object]:
        """管理员硬删除草稿批次，并清理其业务记录、向量、目录和附件。"""
        draft_id = str(draft_id or "").strip()
        if not draft_id:
            raise ValueError("草稿 ID 不能为空")

        store = self.runtime.require_draft_store()
        item = store.get_admin(draft_id)
        if item is None:
            raise LookupError("草稿不存在")
        draft = item.get("draft") if isinstance(item.get("draft"), dict) else {}
        source_id = str(draft.get("source_id") or "").strip()
        list_for_source_id = getattr(store, "list_for_source_id", None)
        targets = list_for_source_id(source_id) if source_id and callable(list_for_source_id) else [item]
        if not any(str(row.get("draft_id")) == draft_id for row in targets):
            targets.append(item)
        allowed_statuses = {
            "CONFIRMED", "PENDING_REVIEW", "DRAFT_GENERATED", "PUBLISHED", "WITHDRAWN", "DELETED"
        }
        for target_item in targets:
            status = str(target_item.get("status") or "")
            if status not in allowed_statuses:
                raise ValueError(f"当前状态不允许删除：{status or 'UNKNOWN'}")

        attachments_by_item = {
            str(row["draft_id"]): draft_attachments(row) for row in targets
        }
        document_ids: set[str] = set()
        for target_item in targets:
            document_ids.update(
                str(value.get("document_id") or "").strip()
                for value in attachments_by_item[str(target_item["draft_id"])]
                if str(value.get("document_id") or "").strip()
            )
            source_document_id = str(target_item.get("source_document_id") or "").strip()
            if source_document_id:
                document_ids.add(source_document_id)

        plan = plan_draft_cleanup(store, targets, allow_published_targets=True)
        lifecycle = getattr(self.runtime, "lifecycle_service", None)
        storage = cast(StorageService | None, getattr(self.runtime, "storage_service", None))
        target_ids = {str(row["draft_id"]) for row in targets}
        prepared_documents: dict[str, set[str]] = {}
        for document_id in sorted(document_ids & plan["document_ids"]):
            keys = {
                str(value["storage_key"])
                for attachments in attachments_by_item.values()
                for value in attachments
                if value.get("document_id") == document_id and value.get("storage_key")
            }
            if lifecycle is not None:
                keys.update(
                    str(value["storage_key"])
                    for value in lifecycle.list_versions(document_id)
                    if value.get("storage_key")
                )
            if storage is not None:
                keys.update(
                    str(value["storage_key"])
                    for value in await storage.find_by_document_id(document_id)
                    if value.get("storage_key")
                )
            if not any(
                ref.get("status") != "DELETED"
                and str(ref["draft_id"]) not in target_ids
                for key in keys
                for ref in store.list_storage_key_references(key)
            ):
                prepared_documents[document_id] = keys

        sink = getattr(self.runtime, "business_record_sink", None)
        retrieval = self._retrieval_agent()
        private_target = cast(_MilvusTarget | None, getattr(retrieval, "private_milvus", None))
        public_targets = cast(
            Mapping[str, _MilvusTarget],
            getattr(retrieval, "public_milvus", {}),
        )
        deleted_business: set[tuple[str, str, str | None]] = set()
        deleted_sources: set[tuple[str, str, str | None]] = set()
        for target_item in targets:
            target_draft = (
                target_item.get("draft")
                if isinstance(target_item.get("draft"), dict)
                else {}
            )
            record_type = normalize_record_type(
                target_item.get("draft_type") or target_draft.get("draft_type") or "solution"
            )
            user_id = str(target_item.get("user_id") or "")
            owner = user_id if record_type == "customer_visit" else None
            mysql_id = str(target_draft.get("mysql_record_id") or "").strip()
            business_key = (record_type, mysql_id, owner)
            target = private_target if owner is not None else public_targets.get(record_type)
            if mysql_id and business_key in plan["business_keys"] and business_key not in deleted_business:
                delete_business_record(
                    sink, record_type=record_type, record_id=mysql_id, user_id=user_id
                )
                if target is not None and callable(getattr(target, "delete_by_mysql_id", None)):
                    target.delete_by_mysql_id(
                        mysql_id, **({"user_id": owner} if target is private_target else {})
                    )
                deleted_business.add(business_key)

            item_source_id = (
                str(target_draft.get("source_id") or "").strip()
                or _source_id(attachments_by_item[str(target_item["draft_id"])])
            )
            source_key = (record_type, canonical_source_id(item_source_id), owner)
            if item_source_id and source_key in plan["source_keys"] and source_key not in deleted_sources:
                if target is not None and callable(getattr(target, "delete_by_source_id", None)):
                    target.delete_by_source_id(
                        item_source_id, **({"user_id": owner} if target is private_target else {})
                    )
                deleted_sources.add(source_key)

        deleted_documents = 0
        for document_id, storage_keys in prepared_documents.items():
            document_type = next(
                (
                    str(row.get("draft_type") or (row.get("draft") or {}).get("draft_type") or "solution")
                    for row in targets
                    if str(row.get("source_document_id") or "") == document_id
                    or any(
                        str(attachment.get("document_id") or "") == document_id
                        for attachment in attachments_by_item[str(row["draft_id"])]
                    )
                ),
                "solution",
            )
            result = await retrieval.unpublish_document(
                document_id,
                DocumentUnpublishRequest(
                    scope_id="public",
                    knowledge_type=resolve_knowledge_type(document_type),
                    remove_vectors=True,
                    identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
                ),
            )
            if result.status == "FAILED":
                raise RuntimeError(result.error or f"删除文档向量失败：{document_id}")

            if storage is not None:
                for storage_key in storage_keys:
                    await storage.delete_document(storage_key, document_id)
            if lifecycle is not None:
                lifecycle.purge_document(document_id)
            deleted_documents += 1

        for target_item in targets:
            target_id = str(target_item["draft_id"])
            user_id = str(target_item.get("user_id") or "")
            if (
                target_item.get("status") != "DELETED"
                and store.logical_delete(
                    target_id,
                    user_id=user_id,
                    operator_id="admin",
                    allow_published=True,
                ) is None
            ):
                raise LookupError(f"草稿不存在：{target_id}")
            if not store.purge_deleted(target_id):
                raise RuntimeError(f"草稿记录删除失败：{target_id}")
        return {
            "draft_id": draft_id,
            "status": "PURGED",
            "document_count": deleted_documents,
        }
