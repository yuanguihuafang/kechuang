"""管理员治理服务：指标、审核、反馈、审计和运行维护。"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from admin.api.v1.schemas import (
    AdminKnowledgeUpdateConfirmRequest,
    AdminRetentionRunRequest,
    AdminRuntimeStartRequest,
)
from core.contracts.workflows import (
    DraftPublishRequest,
)
from agents.retrieval_agent.schemas import RetrievalRequest, RetrievalResult
from core.contracts.field_permissions import list_field_policies
from core.governance.access_policy import UserType
from services.observability.system import (
    collect_health,
    collect_metrics,
    collect_operational_alerts,
    collect_persistent_metrics,
)
from services.persistence.retention import serialize_storage_references
from services.persistence.repositories.observability_repository import PostgresAuditSink
from services.persistence.repositories.runtime_evidence import PostgresRuntimeEvidenceStore
from services.persistence.retention import PostgresRetentionService
from services.application.draft_publishing import DraftPublishingApplicationService
from runtime.contracts import TaskStore


def _task_status_counts(store: TaskStore) -> dict[str, int]:
    return store.count_by_status()


class AdminGovernanceMixin:
    runtime: Any

    _USER_ROLE_LABELS = {
        "admin": "管理员",
        "internal": "内部人员",
        "external": "外部用户",
    }

    async def retrieve(self, payload: RetrievalRequest) -> RetrievalResult:
        identity = payload.identity.model_copy(update={"user_role": UserType.ADMIN})
        return await self.runtime.embedded_retrieval.agent.retrieve(
            payload.model_copy(update={"identity": identity})
        )

    def metrics(self) -> dict[str, object]:
        """管理员专用指标入口，统一从管理员服务汇总。"""
        if self.supervisor_metrics is None:
            raise RuntimeError("问答指标读取器尚未装配")
        return collect_metrics(
            perception=self.runtime.embedded_perception,
            retrieval=self.runtime.embedded_retrieval,
            supervisor_metrics=self.supervisor_metrics,
            persistent_metrics=lambda: collect_persistent_metrics(),
        )

    def field_policies(self) -> dict[str, object]:
        return {"policies": list_field_policies(), "source": "core.contracts.field_permissions"}

    def document_catalog(
        self,
        *,
        scope_id: str | None,
        user_id: str | None,
        knowledge_type: str | None,
        status: str | None,
        search: str | None = None,
        published_or_admin_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, object]:
        result = self.runtime.require_lifecycle().list_documents_page(
            scope_id=scope_id,
            user_id=user_id,
            knowledge_type=knowledge_type,
            status=status,
            search=search,
            published_or_admin_only=published_or_admin_only,
            page=page,
            page_size=page_size,
        )
        documents = result["documents"]
        reader = getattr(self.runtime, "user_identity_reader", None)
        profiles: dict[str, Any] = {}
        for document in documents:
            user_id = str(document.get("user_id") or "").strip()
            document["role_label"] = self._USER_ROLE_LABELS.get(
                str(document.get("user_role") or ("admin" if user_id == "admin" else ""))
                .strip()
                .lower(),
                "未知角色",
            )
            if user_id == "admin":
                document.update(user_name="管理员", company_name="平台管理员")
            elif reader is not None and user_id:
                if user_id not in profiles:
                    try:
                        profiles[user_id] = reader.get(user_id)
                    except Exception:  # noqa: BLE001 - 目录增强失败不阻断文档列表
                        profiles[user_id] = None
                profile = profiles[user_id]
                if profile is not None:
                    document.update(
                        user_name=getattr(profile, "name", None) or user_id,
                        company_name=getattr(profile, "company", None),
                    )
                    profile_role = (
                        "internal" if getattr(profile, "is_inner_user", False) else "external"
                    )
                    document["role_label"] = self._USER_ROLE_LABELS[profile_role]
            elif user_id:
                document["user_name"] = user_id
        return {
            "documents": documents,
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "total_pages": result["total_pages"],
            "source": "postgres",
        }

    @staticmethod
    def _draft_summary(item: dict[str, Any]) -> dict[str, Any]:
        draft_value = item.get("draft")
        draft = draft_value if isinstance(draft_value, dict) else item
        fields = draft.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        return {
            "draft_id": item.get("draft_id") or draft.get("draft_id"),
            "draft_type": item.get("draft_type") or draft.get("draft_type"),
            "title": item.get("draft_title")
            or draft.get("title")
            or fields.get("title")
            or fields.get("enterprise_name")
            or fields.get("user_name"),
            "status": item.get("status") or draft.get("status"),
        }

    @staticmethod
    def _task_drafts(task: dict[str, Any]) -> list[dict[str, Any]]:
        values = task.get("draft_previews")
        if not isinstance(values, list):
            values = (
                [task.get("draft_preview")] if isinstance(task.get("draft_preview"), dict) else []
            )
        return [item for item in values if isinstance(item, dict)]

    def _knowledge_update_item(self, item: dict[str, Any]) -> dict[str, Any]:
        draft = item.get("draft") if isinstance(item.get("draft"), dict) else {}
        fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
        draft_type = str(item.get("draft_type") or draft.get("draft_type") or "")
        user_id = str(item.get("user_id") or "") or None
        user_name, company_name = user_id, None
        if user_id == "admin":
            user_name, company_name = "管理员", "平台管理员"
        elif user_id and getattr(self.runtime, "user_identity_reader", None) is not None:
            profile = self.runtime.user_identity_reader.get(user_id)
            if profile is not None:
                user_name = getattr(profile, "name", None) or user_id
                company_name = getattr(profile, "company", None)
        attachments = [
            value
            for value in (draft.get("attachments") or [])
            if isinstance(value, dict) and value.get("document_id")
        ]
        document_ids = list(
            dict.fromkeys(
                str(value)
                for value in [
                    item.get("source_document_id"),
                    *(a.get("document_id") for a in attachments),
                ]
                if value
            )
        )
        lifecycle = getattr(self.runtime, "lifecycle_service", None)
        documents = [
            document
            for document_id in document_ids
            if lifecycle is not None
            and (document := lifecycle.get_document(document_id, include_purged=True)) is not None
        ]
        document_names = (
            [
                str(document.get("source_name") or document.get("document_id"))
                for document in documents
            ]
            or [str(value.get("file_name") or value.get("document_id")) for value in attachments]
            or ["—"]
        )
        return {
            "record_id": item.get("draft_id"),
            "draft_id": item.get("draft_id"),
            "thread_id": item.get("thread_id"),
            "source_type": "admin_upload" if user_id == "admin" else "user_chat",
            "user_id": user_id,
            "user_name": user_name,
            "company_name": company_name,
            "draft_title": draft.get("title")
            or fields.get("title")
            or fields.get("enterprise_name")
            or "—",
            "draft_type": draft_type,
            "drafts": [self._draft_summary(item)],
            "document_ids": document_ids,
            "documents": documents,
            "document_names": document_names,
            "attachments": attachments,
            "attachment_count": len(attachments or document_ids),
            "status": item.get("status") or "UNKNOWN",
            "updated_at": item.get("updated_at"),
        }

    def knowledge_updates(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        published_only: bool = True,
        knowledge_type: str | None = None,
    ) -> dict[str, object]:
        """只读取知识更新目录当前页；草稿状态与类型在数据库分页前过滤。"""
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        draft_types = None
        if knowledge_type:
            draft_types = [knowledge_type]
            if knowledge_type in {"solution", "achievement"}:
                draft_types.append("supply_draft")
        result = self.runtime.require_draft_store().list_page(
            status="PUBLISHED" if published_only else None,
            draft_types=draft_types,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._knowledge_update_item(item) for item in result["items"]],
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "total_pages": result["total_pages"],
            "source": "postgres",
        }

    def knowledge_update_detail(self, record_id: str) -> dict[str, object] | None:
        """返回单条草稿的完整内容、原文文档和下载信息。"""
        base_record_id = str(record_id)
        draft_store = self.runtime.require_draft_store()
        stored = draft_store.get_admin(base_record_id)
        if stored is None:
            return None
        summary = self._knowledge_update_item(stored)
        attachments = [
            {
                "document_id": item.get("document_id"),
                "file_name": item.get("file_name")
                or item.get("source_name")
                or item.get("document_id"),
                "file_url": f"/api/v1/knowledge/documents/{quote(str(item.get('document_id')), safe='')}/file?user_role=admin&user_id=admin",
            }
            for item in (summary.get("attachments") or [])
            if item.get("document_id")
        ]
        for document in summary.get("documents", []):
            document_id = str(document.get("document_id"))
            if any(item.get("document_id") == document_id for item in attachments):
                continue
            version = document.get("active_version") or {}
            attachments.append(
                {
                    "document_id": document_id,
                    "file_name": document.get("source_name") or document_id,
                    "file_url": f"/api/v1/knowledge/documents/{quote(document_id, safe='')}/file?user_role=admin&user_id=admin"
                    + (
                        f"&version_id={quote(str(version['version_id']), safe='')}"
                        if version.get("version_id")
                        else ""
                    ),
                }
            )
        return {**summary, "drafts": [stored], "attachments": attachments}

    def purge_deleted_draft(self, draft_id: str) -> dict[str, object]:
        draft_id = str(draft_id or "").strip()
        if not draft_id:
            raise ValueError("草稿 ID 不能为空")
        if not self.runtime.require_draft_store().purge_deleted(draft_id):
            raise LookupError("仅能删除已标记为 DELETED 的草稿记录")
        return {"draft_id": draft_id, "status": "PURGED"}

    @serialize_storage_references
    async def confirm_knowledge_update(
        self,
        record_id: str,
        payload: AdminKnowledgeUpdateConfirmRequest,
    ) -> dict[str, object]:
        detail = self.knowledge_update_detail(record_id)
        if detail is None:
            raise ValueError("知识库更新记录不存在")
        stored_drafts_value = detail.get("drafts")
        stored_drafts = (
            [item for item in stored_drafts_value if isinstance(item, dict)]
            if isinstance(stored_drafts_value, list)
            else []
        )
        edited_by_id = {
            str(item.get("draft_id")): item
            for item in (payload.drafts or [])
            if item.get("draft_id")
        }
        publisher = DraftPublishingApplicationService(
            self.runtime, lambda user_id: (str(user_id), "admin", None)
        )
        published = []
        for item in stored_drafts[:1]:
            draft_id = str(item.get("draft_id") or "")
            user_id = str(item.get("user_id") or detail.get("user_id") or "")
            if not draft_id:
                continue
            edited = edited_by_id.get(draft_id)
            if edited is not None:
                draft = edited.get("draft") if isinstance(edited.get("draft"), dict) else edited
                draft = {key: value for key, value in draft.items() if key != "_index_snapshot"}
                updated = self.runtime.require_draft_store().update(
                    draft_id,
                    user_id=user_id,
                    draft=draft,
                    status="CONFIRMED",
                    replace_source_blocks="source_blocks" in draft,
                    replace_fields=True,
                )
                if updated is None:
                    raise ValueError(f"草稿不存在：{draft_id}")
            result = await publisher.publish(
                draft_id,
                DraftPublishRequest(
                    user_id=user_id,
                    scope_id="public",
                    replace_existing=True,
                ),
            )
            published.append(result)
        if not published:
            raise ValueError("该记录没有可确认的已保存草稿")
        return {
            "record_id": record_id,
            "published": published,
            "detail": self.knowledge_update_detail(record_id),
        }

    def overview(self) -> dict[str, object]:
        summary = self.runtime.require_lifecycle().summary()
        counts = (
            _task_status_counts(self.runtime.task_store)
            if self.runtime.task_store is not None
            else {}
        )
        summary["task_total"] = sum(counts.values())
        summary["task_active"] = sum(
            counts.get(status, 0)
            for status in (
                "QUEUED",
                "RUNNING",
                "PARSING",
                "INDEXING",
                "RECOVERING",
            )
        )
        summary["task_failed"] = counts.get("FAILED", 0)
        summary["task_manual_review"] = counts.get("MANUAL_REVIEW", 0)
        return summary

    def audit_events(
        self,
        *,
        page: int,
        page_size: int,
        service: str | None,
        user_id: str | None = None,
        task_id: str | None = None,
        document_id: str | None = None,
        event_name: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        store = PostgresAuditSink()
        total = store.count(
            service=service,
            user_id_filter=user_id,
            task_id=task_id,
            document_id=document_id,
            event_name=event_name,
            user_only=None,
            trace_id_filter=trace_id,
        )
        items = store.list(
            limit=page_size,
            offset=(page - 1) * page_size,
            service=service,
            user_id_filter=user_id,
            task_id=task_id,
            document_id=document_id,
            event_name=event_name,
            user_only=None,
            trace_id_filter=trace_id,
        )
        return {
            "events": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def audit_event_detail(self, event_id: int) -> dict[str, object] | None:
        return PostgresAuditSink().get_event(event_id)

    async def operations(self) -> dict[str, object]:
        health_task = collect_health(
            perception=self.runtime.embedded_perception,
            retrieval=self.runtime.embedded_retrieval,
        )
        persistent_task = asyncio.to_thread(collect_persistent_metrics)
        counts_task = (
            asyncio.to_thread(_task_status_counts, self.runtime.task_store)
            if self.runtime.task_store is not None
            else asyncio.sleep(0, result={})
        )
        audit_task = asyncio.to_thread(lambda: PostgresAuditSink().list(limit=100))
        health, persistent, counts, audit_events = await asyncio.gather(
            health_task,
            persistent_task,
            counts_task,
            audit_task,
        )
        last_cleanup = next(
            (
                item
                for item in audit_events
                if item.get("event_name")
                in {
                    "retention_cleanup_completed",
                    "retention_cleanup_failed",
                }
            ),
            None,
        )
        return {
            "health": health,
            "persistent_metrics": persistent,
            "alerts": collect_operational_alerts(health, persistent),
            "tasks": {
                "active": sum(
                    counts.get(status, 0)
                    for status in (
                        "QUEUED",
                        "RUNNING",
                        "PARSING",
                        "INDEXING",
                        "RECOVERING",
                    )
                ),
                "failed": counts.get("FAILED", 0),
                "manual_review": counts.get("MANUAL_REVIEW", 0),
            },
            "last_retention": last_cleanup,
        }

    async def run_retention(self, payload: AdminRetentionRunRequest) -> dict[str, object]:
        if not payload.confirm:
            raise ValueError("请明确确认清理过期运行状态")
        counts = await asyncio.to_thread(PostgresRetentionService().cleanup_once)
        PostgresAuditSink().record(
            "retention_cleanup_completed",
            service="operations",
            counts=counts,
            operator_id=payload.operator_id,
            trigger="manual",
        )
        return {
            "message": "已完成过期运行状态清理；文档、MinIO 和 Milvus 未受影响。",
            "counts": counts,
        }

    def runtime_evidence(self) -> dict[str, object]:
        return PostgresRuntimeEvidenceStore().snapshot()

    def start_runtime_evidence(self, payload: AdminRuntimeStartRequest) -> dict[str, object]:
        if not payload.confirm:
            raise ValueError("请明确确认设置系统开始时间")
        return PostgresRuntimeEvidenceStore().set_system_start()

    def reset_runtime_evidence(self, payload: AdminRuntimeStartRequest) -> dict[str, object]:
        if not payload.confirm:
            raise ValueError("请明确确认重新开始计时")
        return PostgresRuntimeEvidenceStore().reset_system_start()

    def export_runtime_evidence(self, format_name: str) -> tuple[bytes, str, str]:
        return PostgresRuntimeEvidenceStore().export(format_name)
