"""管理员文档服务：上传、解析、任务和版本生命周期操作。"""

from __future__ import annotations
from services.persistence.retention import serialize_storage_references

import asyncio
from datetime import datetime, timezone
from typing import Any, cast

from fastapi import UploadFile

from core.contracts.knowledge import (
    VALID_KNOWLEDGE_TYPES,
    DocumentUnpublishRequest,
    resolve_knowledge_type,
)
from core.contracts.operations import OperationState
from core.contracts.workflows import (
    DraftPublishRequest,
    FileConfirmRequest,
    UnifiedDeleteRequest,
)
from admin.api.v1.schemas import (
    SupervisorRecoveryRequest,
)
from services.application.draft_cleanup import delete_linked_drafts
from services.application.document_lifecycle import DocumentLifecycleApplicationService
from services.application.draft_publishing import DraftPublishingApplicationService, plan_draft_cleanup
from services.application.business_records import canonical_source_id
from runtime.contracts import StorageService
from services.application.document_images import hydrate_parse_result_images


def _draft_content(draft: dict[str, Any]) -> dict[str, Any]:
    """比较可发布字段；界面控制标记和原文块不参与字段草稿变更判断。"""
    return {
        key: value
        for key, value in draft.items()
        if not key.startswith("_") and key != "source_blocks"
    }


def _cleanup_superseded_source_drafts(
    runtime: Any,
    *,
    source_id: str | None,
    document_id: str | None = None,
    previous_version_id: str | None = None,
    user_id: str,
    draft_type: str,
    retained_draft_ids: set[str],
) -> dict[str, int]:
    """Remove prior retries only after the replacement drafts have all published."""
    source_id = canonical_source_id(source_id)
    if not source_id:
        return {"drafts": 0, "mysql_records": 0, "milvus_records": 0}

    store = runtime.require_draft_store()
    lookup_ids = {source_id}
    if source_id.startswith("source:"):
        lookup_ids.add(f"source:batch:{source_id[len('source:'):]}" )
    rows = {
        str(row.get("draft_id") or ""): row
        for lookup_id in lookup_ids
        for row in store.list_for_source_id(lookup_id)
        if isinstance(row, dict) and row.get("draft_id")
    }
    if document_id:
        rows.update({
            str(row.get("draft_id") or ""): row
            for row in store.list_for_document(document_id)
            if isinstance(row, dict) and row.get("draft_id")
        })
    retained = [row for row in rows.values() if str(row.get("draft_id")) in retained_draft_ids]
    created_at = [str(row["created_at"]) for row in retained if row.get("created_at")]
    cutoff = min(created_at) if len(created_at) == len(retained) and created_at else None

    def superseded(row: dict[str, Any]) -> bool:
        draft = row.get("draft") or {}
        attachments = [item for item in draft.get("attachments") or [] if isinstance(item, dict)]
        if document_id:
            linked_ids = {str(item["document_id"]) for item in attachments if item.get("document_id")}
            if row.get("source_document_id"):
                linked_ids.add(str(row["source_document_id"]))
            if linked_ids != {document_id}:
                return False
        same_source = canonical_source_id(draft.get("source_id")) == source_id
        if same_source:
            return bool(cutoff and row.get("created_at") and str(row["created_at"]) < cutoff)
        return bool(document_id and previous_version_id and attachments and all(
            item.get("document_id") == document_id
            and item.get("version_id") == previous_version_id
            for item in attachments
        ))

    stale = [
        row for row in rows.values()
        if row.get("user_id") == user_id
        and row.get("draft_type") == draft_type
        and row.get("status") == "PUBLISHED"
        and str(row.get("draft_id")) not in retained_draft_ids
        and superseded(row)
    ]
    retained_mysql_ids = {
        str((row.get("draft") or {}).get("mysql_record_id") or "").strip()
        for row in retained
    } - {""}
    stale_mysql_ids = {
        str((row.get("draft") or {}).get("mysql_record_id") or "").strip()
        for row in stale
    } - {"", *retained_mysql_ids}
    stale_draft_ids = {str(row.get("draft_id") or "") for row in stale}

    agent = runtime.embedded_retrieval.agent
    target = getattr(agent, "public_milvus", {}).get(draft_type)
    sink = runtime.require_business_record_sink()
    deleted_mysql_ids: set[str] = set()
    deleted_mysql_count = 0
    for mysql_id in sorted(stale_mysql_ids):
        references = store.list_business_references(
            draft_type, mysql_id, user_id=user_id if draft_type == "customer_visit" else None,
        )
        if any(str(row.get("draft_id") or "") not in stale_draft_ids for row in references):
            continue
        if target is None or not callable(getattr(target, "delete_by_mysql_id", None)):
            raise RuntimeError(f"{draft_type} Milvus 不支持按 mysql_id 清理旧草稿")
        target.delete_by_mysql_id(mysql_id)
        exists = sink.find_by_id(record_type=draft_type, record_id=mysql_id)
        if exists:
            sink.delete(record_type=draft_type, record_id=mysql_id, user_id=user_id)
            deleted_mysql_count += 1
        deleted_mysql_ids.add(mysql_id)

    for row in stale:
        store.logical_delete(
            str(row["draft_id"]), user_id=user_id, operator_id=user_id, allow_published=True
        )
    return {
        "drafts": len(stale),
        "mysql_records": deleted_mysql_count,
        "milvus_records": len(deleted_mysql_ids),
    }


class AdminDocumentMixin:
    runtime: Any

    def _delete_linked_drafts(self, document_id: str) -> int:
        return delete_linked_drafts(self.runtime, document_id)

    @serialize_storage_references
    async def _purge_missing_document(self, document_id: str) -> dict[str, object]:
        """清理数据库已不存在但仍残留在向量库/对象存储中的文档。"""

        deleted_drafts = self._delete_linked_drafts(document_id)
        agent = self.runtime.embedded_retrieval.agent
        deleted_vectors = 0
        seen_targets: set[int] = set()
        for target in getattr(agent, "public_milvus", {}).values():
            if target is None or id(target) in seen_targets:
                continue
            seen_targets.add(id(target))
            delete = getattr(target, "delete_by_document_id", None)
            if delete is not None:
                delete(document_id)
                deleted_vectors += 1

        deleted_files = 0
        storage = cast(StorageService | None, getattr(self.runtime, "storage_service", None))
        if storage is not None:
            for item in await storage.find_by_document_id(document_id):
                storage_key = str(item.get("storage_key") or "").strip()
                if storage_key:
                    await storage.delete_document(storage_key, document_id)
                    deleted_files += 1
        lifecycle = self.runtime.require_lifecycle()
        if lifecycle.get_document(document_id, include_purged=True) is not None:
            if not lifecycle.purge_document(document_id):
                raise RuntimeError("文档目录清理失败")
        return {
            "status": "PURGED",
            "document_id": document_id,
            "orphaned": True,
            "deleted_draft_count": deleted_drafts,
            "deleted_vector_target_count": deleted_vectors,
            "deleted_file_count": deleted_files,
        }

    def document_detail(self, document_id: str) -> dict[str, object] | None:
        document = self.runtime.require_lifecycle().get_document(document_id, include_purged=True)
        if document is None or not document.get("versions"):
            return None
        draft_store = getattr(self.runtime, "draft_store_service", None)
        task_store = getattr(self.runtime, "task_store", None)
        try:
            associated_drafts = (
                draft_store.list_for_document(document_id) if draft_store else []
            )
        except Exception:  # noqa: BLE001 - 详情增强失败不影响文档版本查看
            associated_drafts = []
        try:
            task_page = (
                task_store.list_page(document_id=document_id, page=1, page_size=100)
                if task_store
                else {}
            )
            task_records = task_page.get("items", [])
        except Exception:  # noqa: BLE001 - 详情增强失败不影响文档版本查看
            task_records = []
        return {
            "document": document,
            "versions": document["versions"],
            "associated_drafts": associated_drafts,
            "task_records": task_records,
        }

    def task_catalog(
        self,
        *,
        operation: str | None = None,
        document_id: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        task_store = getattr(self.runtime, "task_store", None)
        if task_store is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}
        return task_store.list_page(
            operation=operation,
            document_id=document_id,
            page=page,
            page_size=page_size,
        )

    def document_versions(self, document_id: str) -> dict[str, object]:
        return {
            "document_id": document_id,
            "versions": self.runtime.require_lifecycle().list_versions(document_id),
        }

    async def upload_file(
        self,
        file: UploadFile,
        *,
        scope_id: str = "public",
        user_id: str = "admin",
        user_role: str = "admin",
        knowledge_type: str = "",
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        """管理员资料入口；实际上传和解析能力由 FileWorkflow/Perception 提供。"""

        if user_role != "admin":
            raise PermissionError("管理员接口只接受 admin 身份")
        result = await self.runtime.require_file_workflow().upload(
            file,
            scope_id=scope_id,
            user_id=user_id,
            knowledge_type=knowledge_type,
            user_role="admin",
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            existing_document_id=document_id,
        )
        return result

    def task_status(self, task_id: str) -> dict[str, Any] | None:
        return self.runtime.require_file_workflow().status(task_id)

    async def parse_result(self, document_id: str) -> dict[str, object] | None:
        storage = cast(StorageService | None, self.runtime.storage_service)
        if storage is None:
            raise RuntimeError("MinIO 存储服务尚未装配")
        result = await storage.load_parse_result(document_id)
        if result is None:
            return None
        return await hydrate_parse_result_images(document_id, result, storage)

    async def recover_task(
        self, task_id: str, payload: SupervisorRecoveryRequest
    ) -> dict[str, object]:
        store = self.runtime.task_store
        task = store.get(task_id) if store is not None else None
        if task is not None and str(task.get("operation", "")).startswith("document_"):
            action = str(task.get("lifecycle_action") or "")
            if action not in {"publish", "unpublish", "recycle", "restore", "purge"}:
                raise ValueError("补偿任务动作不受支持")
            result, updated = await self.lifecycle_action(
                action, str(task.get("version_id") or ""), payload.operator_id, task=task,
                direct_delete=bool((task.get("compensation") or {}).get("direct_delete")),
            )
            if result is None:
                raise RuntimeError("补偿重试失败")
            return {
                **result,
                "task_id": updated.get("task_id") if updated else task_id,
                "operator_id": payload.operator_id,
            }
        if task is None:
            raise KeyError("补偿任务不存在")
        raise ValueError(
            "该任务不属于文档生命周期补偿，请使用对应文件任务恢复入口或重新提交业务请求"
        )

    @serialize_storage_references
    async def delete_document(
        self, document_id: str, payload: UnifiedDeleteRequest
    ) -> dict[str, object]:
        store = self.runtime.require_lifecycle()
        document = store.get_document(document_id, include_purged=True)
        version_id = (document or {}).get("active_version_id")
        if version_id:
            result, task = await self.lifecycle_action(
                "purge", str(version_id), str(payload.user_id or "admin"), direct_delete=True,
            )
        else:
            if any(row.get("status") != "PURGED" for row in store.list_versions(document_id)):
                raise ValueError("文档缺少活动版本，请刷新目录后重试")
            result, task = await self._purge_missing_document(document_id), None
        if result is None:
            raise RuntimeError("文档删除失败")
        return {
            **result, "status": "DELETED",
            **({"task_id": task["task_id"]} if task else {}),
        }

    async def parse_task(self, task_id: str) -> dict[str, Any]:
        workflow = self.runtime.require_file_workflow()
        task = workflow.status(task_id)
        if task is None:
            raise LookupError("Supervisor 编排任务不存在")
        if task.get("draft_status") == "RUNNING":
            raise ValueError("字段提取进行中，暂不能重新解析")
        if task.get("status") in {"QUEUED", "RUNNING", "PARSING"}:
            return task
        if task.get("status") in {"WAITING_CONFIRM", "WAITING_DRAFT_CONFIRM", "SUCCEEDED", "FAILED"} and task.get("storage_key"):
            task = (
                workflow.task_store.update(
                    task_id,
                    status="UPLOADED",
                    stage="upload",
                    progress=25,
                    operation_state=OperationState.STORAGE_READY.value,
                    preview_blocks=None,
                    block_count=None,
                    draft_status="NOT_STARTED",
                    draft_error_code=None,
                    draft_error=None,
                    draft_id=None,
                    draft_ids=[],
                    draft_preview=None,
                    draft_previews=[],
                    error_code=None,
                    error=None,
                    result_status=None,
                    attempt=0,
                )
                or task
            )
        if task.get("status") != "UPLOADED":
            return {**task, "error_code": "INVALID_STAGE", "error": "当前任务不是待解析状态"}
        if self.runtime.task_runner is None:
            raise RuntimeError("任务队列尚未就绪")
        self.runtime.task_runner.submit(
            task_id,
            "parse",
            lambda: workflow.parse(task_id),
            reset_for_retry=lambda: workflow.task_store.update(
                task_id, status="UPLOADED", stage="upload", progress=25
            ),
        )
        return workflow.status(task_id) or task

    async def extract_fields_task(
        self, task_id: str, knowledge_type: str | None = None
    ) -> dict[str, Any]:
        """显式启动 LLM 字段提取，不重复执行 Perception 原文解析。"""
        workflow = self.runtime.require_file_workflow()
        task = workflow.status(task_id)
        if task is None:
            raise LookupError("Supervisor 编排任务不存在")
        try:
            workflow.validate_task_source(task)
        except ValueError as exc:
            if str(exc) != "STALE_SOURCE":
                raise
            return {**task, "error_code": "STALE_SOURCE", "error": "任务引用的附件版本已失效，请从当前文档重新操作"}
        if task.get("status") in {"QUEUED", "RUNNING", "PARSING"}:
            return task
        if task.get("status") not in {"WAITING_CONFIRM", "WAITING_DRAFT_CONFIRM", "SUCCEEDED"}:
            return {**task, "error_code": "INVALID_STAGE", "error": "请先完成文件解析"}
        if task.get("draft_status") == "RUNNING":
            return task
        selected_type = str(knowledge_type or task.get("knowledge_type") or "").strip().lower()
        if not selected_type:
            return workflow.task_store.update(
                task_id,
                draft_status="EMPTY",
                draft_error_code=None,
                draft_error=None,
                error_code=None,
                error=None,
            ) or task
        if selected_type not in VALID_KNOWLEDGE_TYPES:
            raise ValueError("知识类型无效")
        workflow.task_store.update(
            task_id,
            status="WAITING_DRAFT_CONFIRM" if task.get("status") == "SUCCEEDED" else task["status"],
            result_status=None,
            knowledge_type=selected_type,
            draft_status="RUNNING",
            draft_error_code=None,
            draft_error=None,
            draft_id=None,
            draft_ids=[],
            draft_preview=None,
            draft_previews=[],
            error_code=None,
            error=None,
        )
        asyncio.create_task(workflow.regenerate_draft(task_id))
        return workflow.status(task_id) or task

    @serialize_storage_references
    async def confirm_task(self, task_id: str, payload: FileConfirmRequest) -> dict[str, Any]:
        workflow = self.runtime.require_file_workflow()
        task = workflow.status(task_id)
        if task is None:
            raise LookupError("Supervisor 编排任务不存在")
        try:
            workflow.validate_task_source(task)
        except ValueError as exc:
            if str(exc) != "STALE_SOURCE":
                raise
            return {**task, "error_code": "STALE_SOURCE", "error": "任务引用的附件版本已失效，请从当前文档重新操作"}
        if task.get("status") in {"QUEUED", "RUNNING", "INDEXING"}:
            return task
        if task.get("draft_status") == "RUNNING":
            return {**task, "error_code": "INVALID_STAGE", "error": "字段提取进行中，暂不能确认发布"}
        if task.get("status") not in {"WAITING_CONFIRM", "WAITING_DRAFT_CONFIRM", "SUCCEEDED"} and not (
            task.get("status") == "FAILED" and task.get("preview_blocks") and task.get("storage_key")
        ):
            return {**task, "error_code": "INVALID_STAGE", "error": "当前任务不是待确认状态"}
        if task.get("status") == "SUCCEEDED" and task.get("storage_key"):
            task = (
                workflow.task_store.update(
                    task_id,
                    status="WAITING_CONFIRM",
                    stage="confirm",
                    progress=70,
                    error_code=None,
                    error=None,
                    indexed_count=None,
                    index_result=None,
                )
                or task
            )
        if (
            task.get("status") == "FAILED"
            and task.get("preview_blocks")
            and task.get("storage_key")
        ):
            task = (
                workflow.task_store.update(
                    task_id,
                    status="WAITING_CONFIRM",
                    stage="confirm",
                    progress=70,
                    error_code=None,
                    error=None,
                    indexed_count=None,
                )
                or task
            )
        drafts = payload.drafts or ([payload.draft] if isinstance(payload.draft, dict) else [])
        if task.get("status") != "WAITING_CONFIRM" or drafts:
            if task.get("status") == "WAITING_DRAFT_CONFIRM" or drafts:
                draft_ids = [str(item) for item in (task.get("draft_ids") or []) if item]
                if not draft_ids and task.get("draft_id"):
                    draft_ids = [str(task["draft_id"])]
                if not draft_ids:
                    raise ValueError("解析任务缺少草稿")
                published_items = []
                for index, draft_id in enumerate(draft_ids):
                    edited = drafts[index] if index < len(drafts) else None
                    if isinstance(edited, dict):
                        edited = {key: value for key, value in edited.items() if key != "_index_snapshot"}
                    existing = (
                        self.runtime.require_draft_store().get(
                            draft_id, user_id=str(task.get("user_id") or "admin")
                        )
                        or {}
                    )
                    existing_draft = dict(existing.get("draft") or {})
                    submitted_draft = (
                        dict(edited) if isinstance(edited, dict) else dict(existing_draft)
                    )
                    chunk_fields = (
                        "chunk_text", "text", "source_chunk_id", "source_locator",
                        "parent_id", "parent_text", "child_index", "visibility",
                    )
                    requested_chunks = [
                        {key: block.get(key) for key in chunk_fields}
                        for block in payload.chunks
                        if isinstance(block, dict)
                    ]
                    existing_chunks = [
                        {key: block.get(key) for key in chunk_fields}
                        for block in (existing_draft.get("source_blocks") or [])
                        if isinstance(block, dict)
                    ]
                    chunks_unchanged = (
                        not payload.chunks
                        or (index > 0 and not existing_chunks)
                        or requested_chunks == existing_chunks
                    )
                    if (
                        existing.get("status") == "PUBLISHED"
                        and chunks_unchanged
                        and _draft_content(submitted_draft) == _draft_content(existing_draft)
                    ):
                        # 发布已完成但后续清理可能失败；重试时只续跑清理，避免重复写入。
                        published_items.append(
                            {
                                "draft": {"draft": existing_draft},
                                "index_result": {"indexed_count": 0},
                            }
                        )
                        continue
                    replace_source_blocks = False
                    if isinstance(edited, dict):
                        # 发布草稿时一并带上当前附件内容；原文和最终编辑分块分别保存。
                        document_id = str(task.get("document_id") or "")
                        parsed = await self.parse_result(document_id) if document_id else None
                        saved_chunks = (parsed or {}).get("saved_chunks") or []
                        # 同一附件可生成多条草稿，附件原文只跟随第一条草稿写入索引。
                        if index > 0:
                            source_blocks = []
                            chunks_are_final = False
                        elif payload.chunks:
                            source_blocks = payload.chunks
                            chunks_are_final = True
                        elif saved_chunks:
                            source_blocks = saved_chunks
                            chunks_are_final = True
                        elif parsed and parsed.get("edited"):
                            raw_text = str(parsed.get("raw_text") or "")
                            if not raw_text.strip():
                                raise ValueError("编辑后的解析原文为空，无法随草稿发布附件")
                            source_blocks = [{
                                "block_id": f"{task.get('document_id')}:edited-source",
                                "text": raw_text,
                                "metadata": {"field_policy_managed": False},
                            }]
                            chunks_are_final = False
                        else:
                            source_blocks = (parsed or {}).get("blocks") or existing_draft.get("source_blocks") or []
                            chunks_are_final = False
                        source_blocks = [
                            {
                                **block,
                                "document_id": block.get("document_id") or task.get("document_id"),
                                "version_id": block.get("version_id") or task.get("version_id"),
                                "metadata": {
                                    **(block.get("metadata") or {}),
                                    **({"chunks_are_final": True} if chunks_are_final else {}),
                                },
                            }
                            for block in source_blocks if isinstance(block, dict)
                        ]
                        edited = {**edited, "source_blocks": source_blocks}
                        replace_source_blocks = True
                        updated = self.runtime.require_draft_store().update(
                            draft_id,
                            user_id=str(task.get("user_id") or "admin"),
                            draft=edited,
                            status="CONFIRMED",
                            replace_source_blocks=replace_source_blocks,
                            replace_fields=True,
                        )
                        if updated is None:
                            raise ValueError("草稿不存在或不属于当前任务")
                    published = await DraftPublishingApplicationService(
                        self.runtime, lambda user_id: (str(user_id or "admin"), "admin", None)
                    ).publish(
                        draft_id,
                        DraftPublishRequest(
                            user_id=str(task.get("user_id") or "admin"),
                            scope_id="public",
                            replace_existing=True,
                            idempotency_key=payload.idempotency_key
                            or f"admin:{task_id}:{draft_id}",
                        ),
                    )
                    published_items.append(published)
                published = published_items[0]
                source_record = self.runtime.require_draft_store().get(
                    draft_ids[0], user_id=str(task.get("user_id") or "admin")
                ) or {}
                source_draft = source_record.get("draft") or {}
                source_group_cleanup = _cleanup_superseded_source_drafts(
                    self.runtime,
                    source_id=source_draft.get("source_id"),
                    document_id=str(task.get("document_id") or "") or None,
                    previous_version_id=str(task.get("previous_version_id") or "") or None,
                    user_id=str(task.get("user_id") or "admin"),
                    draft_type=str(source_record.get("draft_type") or task.get("knowledge_type") or "solution"),
                    retained_draft_ids=set(draft_ids),
                )
                cleanup = await workflow.cleanup_previous_version(task) or {}
                cleanup["source_group_replacement"] = source_group_cleanup
                published_drafts = []
                for item in published_items:
                    published_record = item.get("draft") or {}
                    published_drafts.append(
                        published_record.get("draft")
                        if isinstance(published_record, dict)
                        and isinstance(published_record.get("draft"), dict)
                        else published_record
                    )
                return (
                    workflow.task_store.update(
                        task_id,
                        status="SUCCEEDED",
                        stage="completed",
                        progress=100,
                        result_status="PUBLISHED",
                        indexed_count=sum(
                            (item.get("index_result") or {}).get("indexed_count", 0)
                            for item in published_items
                        ),
                        draft_preview=published_drafts[0],
                        draft_previews=published_drafts,
                        index_result=published.get("index_result"),
                        replacement_cleanup=cleanup,
                    )
                    or task
                )
            return {**task, "error_code": "INVALID_STAGE", "error": "当前任务不是待确认状态"}
        raise ValueError("管理台资料必须先完成字段提取，再确认写入 MySQL + Milvus")

    async def with_parse_result(self, task: dict[str, Any]) -> dict[str, Any]:
        return await DocumentLifecycleApplicationService(self.runtime).with_parse_result(
            task, load_parse_result=self.parse_result,
        )

    async def lifecycle_action(
        self, action: str, version_id: str, user_id: str, *,
        task: dict[str, Any] | None = None,
        reason_code: str | None = None, reason_text: str | None = None,
        direct_delete: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        store = self.runtime.require_lifecycle()
        if store.get_version(version_id) is None:
            document_id = str(version_id).rsplit(":v", 1)[0]
            if action in {"recycle", "purge"}:
                document = store.get_document(document_id, include_purged=True)
                if action == "purge" and document is not None:
                    if any(row.get("status") != "PURGED" for row in store.list_versions(document_id)):
                        raise ValueError("所选版本不存在，文档仍有其他版本，请刷新目录后选择正确版本")
                    return await self._purge_missing_document(document_id), task
                if document is None:
                    return await self._purge_missing_document(document_id), task
                return {
                    "status": "PURGED" if action == "purge" else "RECYCLED",
                    "document_id": document_id, "version_id": version_id,
                    "already_missing": True,
                }, task
            return None, task
        return await DocumentLifecycleApplicationService(self.runtime).run_action(
            action, version_id, user_id, task, reason_code, reason_text, user_role="admin",
            **({"direct_delete": True} if direct_delete else {}),
        )
