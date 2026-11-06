"""HTTP 与 MCP 共用的草稿应用编排，不构造 HTTP 请求或路由。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from core.contracts.knowledge import DocumentUnpublishRequest
from core.contracts.tools import ToolContext
from core.contracts.workflows import (
    UNPUBLISH_REASON_CODES,
    DraftPublishRequest,
    LifecycleDecisionRequest,
)
from services.application.draft_publishing import DraftPublishingApplicationService
from services.application.draft_session import sync_session_draft
from services.application.temporary_materials import (
    bind_thread_upload_attachments,
    has_expired_attachment,
    retain_draft_attachments,
)
from services.persistence.repositories.observability_repository import PostgresAuditSink
from services.persistence.retention import serialize_storage_references


def _audit_user_event(event: str, **fields: object) -> None:
    try:
        PostgresAuditSink().record(event, service="governance", **fields)
    except Exception:  # noqa: BLE001 - 审计失败不阻断草稿操作
        # 审计不可用不阻断草稿保存、状态变更等用户操作。
        return


class DraftApplicationService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _bind_attachment_types(self, draft: dict[str, Any], user_id: str) -> None:
        attachments = [item for item in draft.get("attachments") or [] if isinstance(item, dict)]
        if attachments and draft.get("draft_type") in {"demand", "solution", "achievement"}:
            lifecycle = self.runtime.require_lifecycle()
            untyped = []
            for attached in attachments:
                document = lifecycle.get_document(str(attached.get("document_id") or ""))
                if document and not document.get("knowledge_type"):
                    untyped.append(attached)
            if not untyped:
                return
            try:
                lifecycle.bind_draft_attachments(
                    untyped, user_id=user_id, knowledge_type=draft["draft_type"],
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    def owned_draft(self, draft_id: str, context: ToolContext) -> dict[str, Any]:
        result = self.runtime.require_draft_store().get(draft_id, user_id=context.user_id)
        if result is None or result.get("status") == "DELETED":
            raise HTTPException(status_code=404, detail="草稿不存在或不属于当前用户")
        if result.get("draft_type") == "customer_visit" and context.user_role != "internal":
            raise HTTPException(status_code=403, detail="当前暂不支持处理客户拜访记录")
        return result

    @staticmethod
    def identity(context: ToolContext):
        def resolve(user_id):
            if user_id != context.user_id:
                raise PermissionError("草稿不属于当前用户")
            return context.user_id, context.user_role, None

        return resolve

    def list(self, *, context: ToolContext, limit: int = 50) -> list[dict[str, Any]]:
        return [
            item
            for item in self.runtime.require_draft_store().list(
                user_id=context.user_id, limit=limit
            )
            if item.get("draft_type") != "customer_visit" or context.user_role == "internal"
        ]

    @serialize_storage_references
    async def create(self, content: dict[str, Any], *, context: ToolContext) -> dict[str, Any]:
        user_id, user_role = context.user_id, context.user_role
        draft = {
            key: value for key, value in content.items()
            if key not in {"_index_snapshot", "_explicit_mysql_field_clears"}
        }
        if draft.get("draft_type") == "customer_visit" and user_role != "internal":
            raise HTTPException(status_code=403, detail="当前暂不支持处理客户拜访记录")
        if context.thread_id:
            tasks = self.runtime.require_file_workflow().task_store.list(
                operation="user_draft",
                limit=10000,
            )
            draft = bind_thread_upload_attachments(
                draft,
                tasks,
                thread_id=context.thread_id,
                user_id=user_id,
            )
        if has_expired_attachment(draft):
            raise HTTPException(status_code=410, detail="草稿及文档已过期，请重新上传文档")
        self._bind_attachment_types(draft, user_id)
        result = self.runtime.require_draft_store().create(
            draft=retain_draft_attachments(draft),
            thread_id=context.thread_id,
            user_id=user_id,
            user_role=user_role,
        )
        sync_session_draft(
            self.runtime,
            context.thread_id,
            {**(result.get("draft") or draft), "draft_id": result.get("draft_id")},
            user_id=user_id,
            user_role=user_role,
            status=result.get("status"),
        )
        _audit_user_event(
            "user_draft_created",
            user_id=user_id,
            draft_id=result.get("draft_id"),
            draft_title=(result.get("draft") or {}).get("title"),
            knowledge_type=result.get("draft_type"),
            summary="用户生成草稿",
        )
        return result

    async def get(self, draft_id: str, *, context: ToolContext) -> dict[str, Any]:
        result = self.owned_draft(draft_id, context)
        if has_expired_attachment(result.get("draft") or {}):
            raise HTTPException(status_code=410, detail="草稿及文档已过期，请重新上传文档")
        return result

    @serialize_storage_references
    async def remove_attachment(
        self, draft_id: str, *, context: ToolContext,
        document_id: str | None = None, storage_key: str | None = None,
    ) -> dict[str, Any]:
        if not document_id and not storage_key:
            raise HTTPException(status_code=422, detail="必须提供附件 document_id 或 storage_key")
        current = self.owned_draft(draft_id, context)
        try:
            result = self.runtime.require_draft_store().remove_attachment(
                draft_id,
                user_id=context.user_id,
                document_id=document_id,
                storage_key=storage_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        sync_session_draft(
            self.runtime,
            context.thread_id or current.get("thread_id"),
            {**(result.get("draft") or {}), "draft_id": draft_id},
            user_id=context.user_id,
            status=result.get("status"),
        )
        return result

    @serialize_storage_references
    async def save(
        self,
        draft_id: str,
        content: dict[str, Any],
        *,
        context: ToolContext,
        expected_version: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        user_id = context.user_id
        current = self.owned_draft(draft_id, context)
        if "draft_type" in content and content["draft_type"] != current.get("draft_type"):
            raise HTTPException(
                status_code=409, detail="不能通过编辑改变草稿类型，请创建对应类型的草稿"
            )
        if has_expired_attachment(current.get("draft") or {}) or has_expired_attachment(content):
            raise HTTPException(status_code=410, detail="草稿及文档已过期，请重新上传文档")
        if status in {"PUBLISHED", "WITHDRAWN", "DELETED"}:
            raise HTTPException(
                status_code=409, detail="请使用对应的发布、下架或删除接口变更草稿状态"
            )
        content = {
            key: value for key, value in content.items()
            if key not in {"_index_snapshot", "_explicit_mysql_field_clears"}
        }
        saved_content = retain_draft_attachments(content) if "attachments" in content else content
        current_draft = dict(current.get("draft") or {})
        draft_to_save = {**current_draft, **saved_content}
        for key in ("fields", "mysql_fields"):
            if isinstance(current_draft.get(key), dict) and isinstance(saved_content.get(key), dict):
                draft_to_save[key] = {**current_draft[key], **saved_content[key]}

        if current.get("status") in {"PUBLISHED", "WITHDRAWN"}:
            cleared = set(current_draft.get("_explicit_mysql_field_clears") or [])
            for field_group in (content.get("fields"), content.get("mysql_fields")):
                if isinstance(field_group, dict):
                    for key, value in field_group.items():
                        if value is None or not str(value).strip():
                            cleared.add(str(key))
                        else:
                            cleared.discard(str(key))
            draft_to_save = {
                **draft_to_save,
                "_needs_mysql_sync_on_republish": True,
                "_explicit_mysql_field_clears": sorted(cleared),
            }
        next_status = "CONFIRMED" if current.get("status") == "PUBLISHED" else status
        update_kwargs = {"user_id": user_id, "draft": draft_to_save, "status": next_status}
        if "source_blocks" in content:
            update_kwargs["replace_source_blocks"] = True
        if "fields" in content or "mysql_fields" in content:
            update_kwargs["replace_fields"] = True
        if expected_version is not None:
            update_kwargs["expected_version"] = expected_version
        self._bind_attachment_types(draft_to_save, user_id)
        try:
            result = self.runtime.require_draft_store().update(draft_id, **update_kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        draft = result.get("draft") or {}
        sync_session_draft(
            self.runtime,
            context.thread_id or current.get("thread_id"),
            {**draft, "draft_id": draft_id},
            user_id=user_id,
            status=result.get("status"),
        )
        _audit_user_event(
            "user_draft_updated",
            user_id=user_id,
            draft_id=draft_id,
            draft_title=draft.get("title"),
            status=result.get("status"),
            summary="用户保存草稿内容",
        )
        return result

    @serialize_storage_references
    async def transition(
        self,
        draft_id: str,
        action: str,
        *,
        context: ToolContext,
        expected_version: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        user_id = context.user_id
        operator_id = user_id
        operator_role = context.user_role
        store = self.runtime.require_draft_store()
        current = self.owned_draft(draft_id, context)
        if current.get("draft_type") == "customer_visit":
            raise HTTPException(
                status_code=409, detail="拜访记录不对外发布，请使用拜访记录保存工具"
            )
        if has_expired_attachment(current.get("draft") or {}):
            raise HTTPException(status_code=410, detail="草稿及文档已过期，请重新上传文档")
        try:
            version_kwargs = (
                {"expected_version": expected_version} if expected_version is not None else {}
            )
            result = store.transition(
                draft_id,
                user_id=user_id,
                target_status=action,
                operator_id=operator_id,
                operator_role=operator_role,
                note=note,
                **version_kwargs,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="草稿不存在或不属于当前用户")
        _audit_user_event(
            "user_draft_updated",
            user_id=user_id,
            draft_id=draft_id,
            status=action,
            operator_id=operator_id,
            summary=f"草稿状态变更为 {action}",
        )
        return result

    @serialize_storage_references
    async def delete(self, draft_id: str, *, context: ToolContext) -> dict[str, Any]:
        user_id = context.user_id

        store = self.runtime.require_draft_store()
        current = self.owned_draft(draft_id, context)
        if current.get("draft_type") == "customer_visit":
            raise HTTPException(status_code=409, detail="拜访记录暂不支持通过公开草稿接口删除")
        if current.get("status") == "PUBLISHED":
            raise HTTPException(status_code=409, detail="已发布草稿请先下架后再删除")
        try:
            deleted = store.logical_delete(draft_id, user_id=user_id, operator_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if deleted is None:
            raise HTTPException(status_code=404, detail="草稿不存在或不属于当前用户")
        deleted_draft = deleted.get("draft") or {}
        attachments = [item for item in deleted_draft.get("attachments") or [] if isinstance(item, dict)]
        _audit_user_event(
            "user_draft_deleted",
            user_id=user_id,
            draft_id=draft_id,
            status="DELETED",
            draft_title=deleted_draft.get("title"),
            deleted_at=deleted_draft.get("deleted_at"),
            attachment_names=[item.get("file_name") or item.get("name") for item in attachments],
            document_ids=sorted({str(item["document_id"]) for item in attachments if item.get("document_id")}),
            summary="用户逻辑删除个人中心草稿",
        )
        return {"draft": deleted, "status": "DELETED"}

    @serialize_storage_references
    async def unpublish(
        self,
        draft_id: str,
        payload: LifecycleDecisionRequest,
        *,
        context: ToolContext,
        admin: bool = False,
    ) -> dict[str, Any]:
        """下架已发布草稿：先撤销向量检索，再更新草稿状态。"""
        if admin and not str(payload.user_id or "").strip():
            raise HTTPException(status_code=422, detail="管理员操作必须提供目标 user_id")
        if not admin:
            payload = payload.model_copy(update={"user_id": context.user_id})
        reason_code = str(payload.reason_code or "").strip()
        reason_text = str(payload.reason_text or "").strip()
        if reason_code not in UNPUBLISH_REASON_CODES:
            raise HTTPException(status_code=422, detail="请选择有效的下架原因")
        if reason_code == "OTHER" and not reason_text:
            raise HTTPException(status_code=422, detail="选择其他时必须填写下架原因")

        store = self.runtime.require_draft_store()
        draft = store.get(draft_id, user_id=payload.user_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在或不属于当前用户")
        if draft.get("draft_type") == "customer_visit":
            raise HTTPException(status_code=409, detail="拜访记录不支持公开上下架")
        if draft.get("status") != "PUBLISHED":
            raise HTTPException(status_code=409, detail="当前草稿不是已发布状态")

        draft_payload = draft.get("draft") or {}
        attachments = [
            item for item in draft_payload.get("attachments") or [] if isinstance(item, dict)
        ]
        source_document_ids = {
            str(item.get("document_id") or "").strip()
            for item in attachments
            if str(item.get("document_id") or "").strip()
        }
        if draft.get("source_document_id"):
            source_document_ids.add(str(draft["source_document_id"]).strip())
        pinned_sources: dict[tuple[str, str | None], dict[str, Any]] = {}
        for attachment in attachments:
            document_id = str(attachment.get("document_id") or "").strip()
            if document_id:
                version_id = str(attachment.get("version_id") or "").strip() or None
                pinned_sources[(document_id, version_id)] = attachment
        for document_id in source_document_ids:
            if not any(source_document_id == document_id for source_document_id, _ in pinned_sources):
                pinned_sources[(document_id, None)] = {}
        sources_to_unpublish = []
        for (document_id, version_id), attachment in pinned_sources.items():
            shared = any(
                row.get("draft_id") != draft_id
                and row.get("status") == "PUBLISHED"
                and any(
                    str(item.get("document_id") or "") == document_id
                    and (not version_id or not item.get("version_id") or str(item.get("version_id")) == version_id)
                    for item in (row.get("draft") or {}).get("attachments") or []
                    if isinstance(item, dict)
                )
                for row in store.list_for_document(document_id)
            )
            if not shared:
                sources_to_unpublish.append((document_id, version_id, attachment))
        mysql_id = str(draft_payload.get("mysql_record_id") or "").strip() or None
        operator_id = (
            str(payload.operator_id or payload.user_id).strip()
            if admin
            else str(payload.user_id).strip()
        )
        if admin:
            operator_role = "admin"
        else:
            operator_role = context.user_role
        retrieval = self.runtime.embedded_retrieval
        request_base = dict(
            scope_id="public",
            knowledge_type=str(draft.get("draft_type") or ""),
            identity={"user_id": payload.user_id, "user_role": operator_role},
        )
        index_results = []
        lifecycle = getattr(self.runtime, "lifecycle_service", None)
        changed_vectors: list[tuple[str, DocumentUnpublishRequest]] = []
        changed_versions: list[dict[str, Any]] = []
        try:
            draft_request = DocumentUnpublishRequest(**request_base, mysql_id=mysql_id)
            result = await retrieval.agent.unpublish_document(draft_id, draft_request)
            if result.status == "FAILED":
                raise HTTPException(status_code=502, detail=result.error or "草稿索引下架失败")
            changed_vectors.append((draft_id, draft_request))
            index_results.append(result)

            for document_id, version_id, _attachment in sources_to_unpublish:
                source_request = DocumentUnpublishRequest(**request_base, version_id=version_id)
                result = await retrieval.agent.unpublish_document(document_id, source_request)
                if result.status == "FAILED":
                    raise HTTPException(status_code=502, detail=result.error or "附件原文索引下架失败")
                changed_vectors.append((document_id, source_request))
                index_results.append(result)
                if version_id:
                    if lifecycle is None:
                        raise HTTPException(status_code=503, detail="附件版本生命周期服务不可用")
                    version = lifecycle.get_version(version_id)
                    if version is None or str(version.get("document_id") or "") != document_id:
                        raise HTTPException(status_code=409, detail=f"附件版本无效：{version_id}")
                    changed = lifecycle.update_version(
                        version_id,
                        status="UNPUBLISHED",
                        publication_status="UNPUBLISHED",
                        unpublish_reason_code=reason_code,
                        unpublish_reason_text=reason_text,
                        unpublished_by=operator_id,
                        unpublished_at=datetime.now(timezone.utc).isoformat(),
                    )
                    if changed is None:
                        raise HTTPException(status_code=503, detail=f"附件版本状态更新失败：{version_id}")
                    changed_versions.append(version)

            updated = store.transition(
                draft_id,
                user_id=payload.user_id,
                target_status="WITHDRAWN",
                operator_id=operator_id,
                operator_role=operator_role,
                note=f"{reason_code}:{reason_text}".rstrip(":"),
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="草稿不存在")
        except Exception as exc:
            compensation_errors = []
            for version in reversed(changed_versions):
                try:
                    lifecycle.update_version(
                        str(version["version_id"]),
                        status=version.get("status"),
                        publication_status=version.get("publication_status"),
                        unpublish_reason_code=version.get("unpublish_reason_code"),
                        unpublish_reason_text=version.get("unpublish_reason_text"),
                        unpublished_by=version.get("unpublished_by"),
                        unpublished_at=version.get("unpublished_at"),
                    )
                except Exception as compensation_error:
                    compensation_errors.append(str(compensation_error))
            for target_id, request in reversed(changed_vectors):
                try:
                    await retrieval.agent.republish_document(target_id, request)
                except Exception as compensation_error:
                    compensation_errors.append(str(compensation_error))
            if compensation_errors:
                raise HTTPException(
                    status_code=502,
                    detail=f"下架失败且状态补偿未完成：{'; '.join(compensation_errors)}",
                ) from exc
            raise
        # 同一附件版本仍被其他已发布草稿引用时保留其索引和发布状态。
        _audit_user_event(
            "admin_draft_unpublished" if admin else "user_draft_unpublished",
            user_id=operator_id,
            draft_id=draft_id,
            document_ids=sorted(source_document_ids),
            reason_code=reason_code,
            reason_text=reason_text,
            status=updated.get("status"),
            summary="用户下架草稿及关联知识块",
        )
        return {
            "draft": updated,
            "index_result": {
                "status": "UNPUBLISHED",
                "document_id": draft_id,
                "updated_count": sum(item.indexed_count for item in index_results),
            },
        }

    @serialize_storage_references
    async def republish(
        self, draft_id: str, payload: LifecycleDecisionRequest, *, context: ToolContext
    ) -> dict[str, Any]:
        user_id = context.user_id
        payload = payload.model_copy(update={"user_id": user_id})
        store = self.runtime.require_draft_store()
        draft_record = self.owned_draft(draft_id, context)
        if draft_record.get("draft_type") == "customer_visit":
            raise HTTPException(status_code=409, detail="拜访记录不支持公开上下架")
        if draft_record.get("status") != "WITHDRAWN":
            raise HTTPException(status_code=409, detail="当前草稿不是已下架状态")
        draft = draft_record.get("draft") or {}
        if has_expired_attachment(draft):
            raise HTTPException(status_code=410, detail="草稿及文档已过期，请重新上传文档")

        operator_id = str(payload.user_id).strip()
        operator_role = context.user_role
        if draft.get("_needs_mysql_sync_on_republish"):
            result = await DraftPublishingApplicationService(
                self.runtime, self.identity(context)
            ).publish(
                draft_id,
                DraftPublishRequest(user_id=payload.user_id, scope_id="public"),
            )
        else:
            mysql_id = str(draft.get("mysql_record_id") or "").strip()
            if not mysql_id:
                raise HTTPException(status_code=409, detail="草稿缺少对应 MySQL 记录，无法重新上架")
            retrieval = self.runtime.embedded_retrieval.agent
            request_base = {
                "scope_id": "public",
                "knowledge_type": str(draft_record.get("draft_type") or ""),
                "identity": {"user_id": payload.user_id, "user_role": operator_role},
            }
            changed_vectors: list[tuple[str, DocumentUnpublishRequest]] = []
            changed_versions: list[dict[str, Any]] = []
            try:
                draft_request = DocumentUnpublishRequest(**request_base, mysql_id=mysql_id)
                index_result = await retrieval.republish_document(draft_id, draft_request)
                if index_result.status == "FAILED":
                    raise HTTPException(status_code=502, detail=index_result.error or "草稿重新上架失败")
                changed_vectors.append((draft_id, draft_request))
                lifecycle = getattr(self.runtime, "lifecycle_service", None)
                for attachment in draft.get("attachments") or []:
                    if not isinstance(attachment, dict) or not attachment.get("document_id"):
                        continue
                    document_id = str(attachment["document_id"])
                    version_id = str(attachment.get("version_id") or "").strip() or None
                    if version_id:
                        if lifecycle is None:
                            raise HTTPException(status_code=503, detail="附件版本生命周期服务不可用")
                        version = lifecycle.get_version(version_id)
                        if (
                            version is None
                            or str(version.get("document_id") or "") != document_id
                            or str(version.get("status") or "") in {"RECYCLED", "PURGED", "EXPIRED"}
                        ):
                            raise HTTPException(status_code=409, detail=f"附件版本不可重新上架：{version_id}")
                        if version.get("publication_status") != "PUBLISHED":
                            source_request = DocumentUnpublishRequest(**request_base, version_id=version_id)
                            source_result = await retrieval.republish_document(document_id, source_request)
                            if source_result.status == "FAILED":
                                raise HTTPException(status_code=502, detail=source_result.error or "附件原文重新上架失败")
                            changed_vectors.append((document_id, source_request))
                            updated_version = lifecycle.update_version(
                                version_id,
                                status="INDEXED",
                                publication_status="PUBLISHED",
                                index_status="INDEXED",
                                unpublish_reason_code=None,
                                unpublish_reason_text=None,
                                unpublished_by=None,
                                unpublished_at=None,
                                expected_version=version.get("version"),
                            )
                            if updated_version is None:
                                raise HTTPException(status_code=503, detail=f"附件版本状态更新失败：{version_id}")
                            changed_versions.append(version)
                updated = store.transition(
                    draft_id,
                    user_id=payload.user_id,
                    target_status="PUBLISHED",
                    operator_id=operator_id,
                    operator_role=operator_role,
                    note="用户重新上架草稿",
                )
                if updated is None:
                    raise HTTPException(status_code=404, detail="草稿不存在")
                result = {"draft": updated, "index_result": index_result.model_dump()}
            except Exception as exc:
                compensation_errors = []
                for version in reversed(changed_versions):
                    try:
                        lifecycle.update_version(
                            str(version["version_id"]),
                            status=version.get("status"),
                            publication_status=version.get("publication_status"),
                            index_status=version.get("index_status"),
                            unpublish_reason_code=version.get("unpublish_reason_code"),
                            unpublish_reason_text=version.get("unpublish_reason_text"),
                            unpublished_by=version.get("unpublished_by"),
                            unpublished_at=version.get("unpublished_at"),
                        )
                    except Exception as compensation_error:
                        compensation_errors.append(str(compensation_error))
                for target_id, request in reversed(changed_vectors):
                    try:
                        await retrieval.unpublish_document(target_id, request)
                    except Exception as compensation_error:
                        compensation_errors.append(str(compensation_error))
                if compensation_errors:
                    raise HTTPException(
                        status_code=502,
                        detail=f"重新上架失败且状态补偿未完成：{'; '.join(compensation_errors)}",
                    ) from exc
                raise

        _audit_user_event(
            "user_draft_published",
            user_id=operator_id,
            draft_id=draft_id,
            status="PUBLISHED",
            summary="用户重新上架草稿",
        )
        return result
