"""文件入库工作流：上传、解析、人工确认、建立索引。"""

from __future__ import annotations
from services.persistence.retention import serialize_storage_references

import logging
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from core.contracts.operations import OperationState
from services.application.index_recovery_sources import load_confirmed_version_request
from services.application.document_lifecycle import cleanup_document_version
from services.persistence.repositories.observability_repository import PostgresAuditSink

from .knowledge_document_draft import KnowledgeDocumentDraftService
from .structured_record_draft import StructuredRecordDraftService
from .task_store import SupervisorTaskStore

logger = logging.getLogger(__name__)
PARSE_FAILURE_MESSAGE = "文件解析失败，请稍后重试"


def _draft_failure(exc: Exception) -> tuple[str, str]:
    extraction_errors = {
        "DRAFT_FIELDS_EMPTY": "模型未提取到有效业务字段，请检查解析原文后重新提取。",
        "DRAFT_SOURCE_EMPTY": "解析原文和文本块均为空，请先重新解析文档。",
        "DRAFT_LLM_NOT_CONFIGURED": "字段提取模型未配置，请检查模型服务配置。",
        "DRAFT_EXTRACTION_DISABLED": "字段提取模型调用已禁用，请检查配置。",
        "SOURCE_TOO_LARGE": "解析原文超出字段提取上限，请拆分材料后重试。",
    }
    if isinstance(exc, ValueError) and str(exc) in extraction_errors:
        return str(exc), extraction_errors[str(exc)]
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "LLM_TIMEOUT", "模型服务响应超时，请稍后重试。"
    if isinstance(exc, httpx.HTTPStatusError):
        return "LLM_PROVIDER_ERROR", "模型服务返回错误，请查看任务审计记录后重试。"
    if isinstance(exc, httpx.NetworkError):
        return "LLM_NETWORK_ERROR", "无法连接模型服务，请检查网络后重试。"
    return "DRAFT_EXTRACTION_FAILED", "字段提取处理异常，请查看任务审计记录后重试。"


class LifecycleStore(Protocol):
    def create_version(self, **fields: Any) -> dict[str, Any]: ...
    def update_version(self, version_id: str, **fields: Any) -> dict[str, Any] | None: ...
    def list_versions(self, document_id: str) -> list[dict[str, Any]]: ...
    def get_version(self, version_id: str) -> dict[str, Any] | None: ...


class FileToolAdapter(Protocol):
    async def upload_document(self, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def parse_document(self, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def index_document(self, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_version(self, arguments: dict[str, Any]) -> dict[str, Any]: ...


class _WorkflowState(TypedDict, total=False):
    file: Any
    scope_id: str
    user_id: str
    knowledge_type: str
    task_id: str
    chunks: list[dict[str, Any]]
    images: list[dict[str, Any]]
    images_confirmed: bool
    identity: dict[str, Any]
    result: dict[str, Any]
    thread_id: str
    idempotency_key: str | None
    existing_document_id: str | None
    user_role: str
    draft_id: str


def _parse_preview(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """生成可放入任务状态的小预览；完整结果已经持久化到 MinIO。"""

    blocks = result.get("blocks") or []
    preview_blocks: list[dict[str, Any]] = []
    for block in blocks[:3]:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        if text:
            preview_blocks.append(
                {
                    "type": str(block.get("type") or "text"),
                    "text": text[:500],
                }
            )
    raw_text = str(result.get("raw_text") or "").strip()
    return raw_text[:1200], preview_blocks


class FileWorkflowService:
    """Supervisor 的文件流程状态机；每一步都通过 Agent Tool Adapter 执行。"""

    def __init__(
        self,
        adapter: FileToolAdapter,
        task_store: SupervisorTaskStore | None = None,
        checkpointer: Any | None = None,
        lifecycle_store: LifecycleStore | None = None,
        draft_store: Any | None = None,
        outbox_repository: Any | None = None,
        storage_service: Any | None = None,
    ) -> None:
        self.adapter = adapter
        self.task_store = task_store or SupervisorTaskStore()
        self.checkpointer = checkpointer or MemorySaver()
        self.lifecycle_store = lifecycle_store
        self.outbox_repository = outbox_repository
        self.storage_service = storage_service
        self.knowledge_draft = KnowledgeDocumentDraftService(draft_store)
        self.structured_draft = StructuredRecordDraftService(draft_store)
        parse_graph = StateGraph(_WorkflowState)
        parse_graph.add_node("parse", self._parse_node)
        parse_graph.add_edge(START, "parse")
        parse_graph.add_edge("parse", END)
        self._parse_graph = parse_graph.compile(checkpointer=self.checkpointer)
        confirm_graph = StateGraph(_WorkflowState)
        confirm_graph.add_node("confirm", self._confirm_node)
        confirm_graph.add_edge(START, "confirm")
        confirm_graph.add_edge("confirm", END)
        self._confirm_graph = confirm_graph.compile(checkpointer=self.checkpointer)

    @staticmethod
    def _audit(event: str, **fields: Any) -> None:
        """审计只记录完整阶段结果；审计不可用不阻断文件流程。"""
        try:
            PostgresAuditSink().record(event, service="file_workflow", **fields)
        except Exception:  # noqa: BLE001 - 审计故障不得阻断业务流程
            return

    @staticmethod
    def _audit_context(task: dict[str, Any]) -> dict[str, Any]:
        """为文件阶段事件补齐追踪所需业务字段，不记录文件正文。"""
        return {
            "user_id": task.get("user_id"),
            "user_role": task.get("user_role"),
            "company": task.get("company") or task.get("enterprise_name"),
            "document_id": task.get("document_id"),
            "file_name": task.get("file_name"),
            "draft_id": task.get("draft_id"),
            "knowledge_type": task.get("knowledge_type"),
        }

    async def upload(
        self,
        file: Any,
        *,
        scope_id: str,
        user_id: str,
        knowledge_type: str | None,
        user_role: str = "internal",
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        existing_document_id: str | None = None,
    ) -> dict[str, Any]:
        # 上传阶段不猜知识类型；由对话意图或解析后的确认步骤确定。
        thread_id = thread_id or f"file-{uuid.uuid4().hex}"
        # UploadFile/TemporaryFile 是请求级对象，不能写入 LangGraph Checkpoint。
        # 上传是单步边界：直接调用 Perception Tool，后续任务元数据再进入持久化状态。
        return (
            await self._upload_node(
                {
                    "file": file,
                    "scope_id": scope_id,
                    "user_id": user_id,
                    "knowledge_type": knowledge_type,
                    "thread_id": thread_id,
                    "user_role": user_role,
                    "idempotency_key": idempotency_key,
                    "existing_document_id": existing_document_id,
                }
            )
        )["result"]

    @serialize_storage_references
    async def _upload_node(self, state: _WorkflowState) -> dict[str, Any]:
        started_at = time.perf_counter()
        previous_version = None
        existing_document_id = state.get("existing_document_id")
        if self.lifecycle_store and existing_document_id:
            versions = self.lifecycle_store.list_versions(str(existing_document_id))
            previous_version = max(
                versions,
                key=lambda item: int(item.get("version_number") or 0),
                default=None,
            )
        result = await self.adapter.upload_document(
            {
                "file": state["file"],
                "scope_id": state["scope_id"],
                "user_id": state["user_id"],
                "document_id": state.get("existing_document_id"),
                "new_version": bool(state.get("existing_document_id")),
            }
        )
        if result.get("status") != "SUCCESS":
            task = self.task_store.create(
                operation="file_ingestion",
                task_type="document_ingestion",
                status="FAILED",
                operation_state=OperationState.FAILED.value,
                error=result.get("error"),
            )
            self._audit(
                "knowledge_upload_completed",
                task_id=task.get("task_id"),
                status="FAILED",
                error=result.get("error", "上传失败"),
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                **self._audit_context(
                    {**state, "file_name": getattr(state.get("file"), "filename", None)}
                ),
            )
            return {"result": {**task, "stage": "failed", "upload_result": result}}
        task = self.task_store.create(
            operation="file_ingestion",
            task_type="document_ingestion",
            status="UPLOADED",
            operation_state=OperationState.STORAGE_READY.value,
            stage="upload",
            progress=25,
            document_id=result.get("document_id"),
            version_id=result.get("version_id")
            or (
                f"{result.get('document_id')}:v{result.get('version')}"
                if result.get("document_id") and result.get("version")
                else None
            ),
            storage_key=result.get("storage_key"),
            file_name=result.get("file_name"),
            scope_id=state["scope_id"],
            user_id=state["user_id"],
            user_role=state.get("user_role", "internal"),
            knowledge_type=str(state.get("knowledge_type") or ""),
            attachments=[
                {
                    "file_name": str(result.get("file_name") or ""),
                    "storage_key": str(result.get("storage_key") or ""),
                }
            ],
            thread_id=state["thread_id"],
            idempotency_key=state.get("idempotency_key"),
            upload_result=result,
            previous_version_id=(previous_version or {}).get("version_id"),
            previous_storage_key=(previous_version or {}).get("storage_key"),
        )
        self._audit(
            "knowledge_upload_completed",
            task_id=task.get("task_id"),
            status="SUCCESS",
            summary="文件已上传至 MinIO",
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
            **self._audit_context(task),
        )
        if self.lifecycle_store and task.get("version_id"):
            self.lifecycle_store.create_version(
                document_id=str(task.get("document_id")),
                version_id=str(task["version_id"]),
                source_name=str(task.get("file_name") or "未命名文件"),
                storage_key=str(task.get("storage_key") or ""),
                scope_id=str(task.get("scope_id") or "public"),
                user_id=str(task.get("user_id") or "") or None,
                version_number=int(result.get("version") or 1),
                knowledge_type=str(task.get("knowledge_type") or ""),
                status="UPLOADED",
            )
        return {"result": task}

    async def parse(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        thread_id = task.get("thread_id") or task_id
        state = await self._parse_graph.ainvoke(
            {"task_id": task_id, "thread_id": thread_id},
            config={"configurable": {"thread_id": thread_id}},
        )
        return state["result"]

    @serialize_storage_references
    async def _parse_node(self, state: _WorkflowState) -> dict[str, Any]:
        task_id = state["task_id"]
        task = self._require(task_id)
        if task.get("status") not in {"UPLOADED", "PARSING", "RUNNING"}:
            return {"result": self._failure(task_id, "INVALID_STAGE", "当前任务不是待解析状态")}
        self.task_store.update(
            task_id,
            status="PARSING",
            stage="parse",
            progress=35,
            operation_state=OperationState.STORAGE_READY.value,
        )
        if self.lifecycle_store and task.get("version_id"):
            self.lifecycle_store.update_version(str(task["version_id"]), status="PARSING")
        started_at = time.perf_counter()
        try:
            result = await self.adapter.parse_document(
                {
                    "document_id": task["document_id"],
                    "storage_key": task["storage_key"],
                    "file_name": task.get("file_name", ""),
                    "knowledge_type": task.get("knowledge_type") or None,
                }
            )
            if result.get("status") != "SUCCESS":
                self._audit(
                    "knowledge_parse_completed",
                    task_id=task_id,
                    status="FAILED",
                    error=result.get("error", "解析失败"),
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                    **self._audit_context(task),
                )
                return {
                    "result": self._failure(
                        task_id, "PARSE_FAILED", result.get("error", "解析失败")
                    )
                }
            if self.lifecycle_store and task.get("version_id"):
                self.lifecycle_store.update_version(
                    str(task["version_id"]),
                    status="READY_TO_PUBLISH",
                    parsed_block_count=len(result.get("blocks") or []),
                    artifact_refs={
                        "parse_result": result.get("metadata", {}).get("parse_result_key"),
                        "manifest": result.get("metadata", {}).get("artifact_manifest_key"),
                    },
                )
            preview_text, preview_blocks = _parse_preview(result)
            metadata = result.get("metadata") or {}
            artifact_ref = dict(metadata.get("artifact_refs") or {})
            for key in ("parse_result_key", "artifact_manifest_key"):
                if metadata.get(key):
                    artifact_ref[key] = metadata[key]
            task_fields: dict[str, Any] = {
                "status": "WAITING_CONFIRM",
                "operation_state": OperationState.PARSED.value,
                "stage": "confirm",
                "progress": 70,
                "block_count": len(result.get("blocks") or []),
                "preview_text": preview_text,
                "preview_blocks": preview_blocks,
                "artifact_ref": artifact_ref,
                "draft_status": "NOT_STARTED",
                "draft_error_code": None,
                "draft_error": None,
            }
            # 解析结果先进入确认态；LLM 字段提取由管理员显式触发。
            updated = self.task_store.update(task_id, **task_fields) or {}
            self._audit(
                "knowledge_parse_completed",
                task_id=task_id,
                status="SUCCESS",
                block_count=len(result.get("blocks") or []),
                summary="文件解析完成，等待字段提取或确认",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                **self._audit_context({**task, **updated}),
            )
            return {"result": updated}
        except Exception:  # noqa: BLE001 - 工作流统一记录失败状态
            logger.exception("文件解析失败: task_id=%s", task_id)
            self._audit(
                "knowledge_parse_completed",
                task_id=task_id,
                status="FAILED",
                error=PARSE_FAILURE_MESSAGE,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                **self._audit_context(task),
            )
            return {"result": self._failure(task_id, "PARSE_FAILED", PARSE_FAILURE_MESSAGE)}

    async def _build_draft_in_background(
        self, task_id: str, task: dict[str, Any], result: dict[str, Any], preview_text: str
    ) -> None:
        """执行显式触发的字段草稿提取；不覆盖已进入索引的任务状态。"""

        try:
            # 管理台保留 admin 身份；字段提取服务默认只提取草稿所需字段。
            draft_task = task
            service = (
                self.structured_draft
                if draft_task.get("knowledge_type") in {"enterprise", "expert"}
                else self.knowledge_draft
            )
            fields = await service.build_task_fields(draft_task, result, preview_text)
            if not fields:
                error_code = "DRAFT_NOT_CREATED"
                error = "未生成可确认的字段草稿，请检查解析原文、用户角色和知识类型。"
                current = self.task_store.get(task_id)
                if current and current.get("status") in {"WAITING_CONFIRM", "WAITING_DRAFT_CONFIRM"}:
                    self.task_store.update(
                        task_id,
                        draft_status="FAILED",
                        draft_error_code=error_code,
                        draft_error=error,
                    )
                logger.warning("字段提取未生成草稿: task_id=%s error_code=%s", task_id, error_code)
                self._audit(
                    "knowledge_draft_extraction_completed",
                    task_id=task_id,
                    status="FAILED",
                    error_code=error_code,
                    error="build_task_fields returned no fields",
                    summary=error,
                    **self._audit_context(task),
                )
                return
            current = self.task_store.get(task_id)
            if current and current.get("status") in {"WAITING_CONFIRM", "WAITING_DRAFT_CONFIRM"}:
                self.task_store.update(
                    task_id,
                    **fields,
                    draft_status="READY",
                    draft_error_code=None,
                    draft_error=None,
                )
        except Exception as exc:  # 草稿辅助失败不影响解析确认
            error_code, error = _draft_failure(exc)
            current = self.task_store.get(task_id)
            if current and current.get("status") in {"WAITING_CONFIRM", "WAITING_DRAFT_CONFIRM"}:
                self.task_store.update(
                    task_id,
                    draft_status="FAILED",
                    draft_error_code=error_code,
                    draft_error=error,
                )
            logger.exception("字段草稿生成失败: task_id=%s error_code=%s", task_id, error_code)
            self._audit(
                "knowledge_draft_extraction_completed",
                task_id=task_id,
                status="FAILED",
                summary=error,
                error_code=error_code,
                error=traceback.format_exc(),
                **self._audit_context(task),
            )

    async def regenerate_draft(self, task_id: str) -> dict[str, Any]:
        """为已解析但尚未生成草稿的历史任务补生成草稿。"""
        task = self._require(task_id)
        try:
            result = await self._storage().load_parse_result(str(task.get("document_id") or ""))
            if not result:
                raise ValueError("当前任务没有可用的解析结果")
            self.task_store.update(
                task_id,
                draft_status="RUNNING",
                draft_error_code=None,
                draft_error=None,
            )
            await self._build_draft_in_background(
                task_id, task, result, str(task.get("preview_text") or "")
            )
        except Exception:  # 后台任务必须落最终状态
            logger.exception("字段提取任务准备失败: task_id=%s", task_id)
            self.task_store.update(
                task_id,
                draft_status="FAILED",
                draft_error_code="DRAFT_SOURCE_UNAVAILABLE",
                draft_error="无法读取已保存的解析原文，请重新解析文档后重试。",
            )
            self._audit(
                "knowledge_draft_extraction_completed",
                task_id=task_id,
                status="FAILED",
                error_code="DRAFT_SOURCE_UNAVAILABLE",
                error=traceback.format_exc(),
                summary="无法读取字段提取所需的解析原文",
                **self._audit_context(task),
            )
        return self.task_store.get(task_id) or task

    async def confirm(
        self,
        task_id: str,
        *,
        chunks: list[dict[str, Any]],
        images: list[dict[str, Any]] | None = None,
        images_confirmed: bool = False,
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        task = self._require(task_id)
        thread_id = task.get("thread_id") or task_id
        state = await self._confirm_graph.ainvoke(
            {
                "task_id": task_id,
                "chunks": chunks,
                "images": images or [],
                "images_confirmed": images_confirmed,
                "identity": identity,
                "thread_id": thread_id,
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        return state["result"]

    @serialize_storage_references
    async def save_confirmation_request(
        self,
        task_id: str,
        *,
        chunks: list[dict[str, Any]],
        images: list[dict[str, Any]] | None,
        images_confirmed: bool,
        identity: dict[str, Any],
    ) -> str:
        """把确认请求放到 MinIO，任务表只保存引用，供重启后的 Worker 恢复。"""
        task = self._require(task_id)
        document_id = str(task.get("document_id") or "")
        if not document_id:
            raise ValueError("索引任务缺少 document_id")
        version = self.lifecycle_store.get_version(str(task.get("version_id") or "")) if self.lifecycle_store else None
        confirmed = ((version or {}).get("artifact_refs") or {}).get("confirmed_index_request")
        if confirmed and confirmed["task_id"] == task_id:
            raise RuntimeError("已成功确认的正文不可覆盖，请创建新的确认任务")
        expected_identity = {"user_id": task["user_id"], "user_role": task["user_role"]}
        if {key: identity.get(key) for key in expected_identity} != expected_identity:
            raise ValueError("确认身份必须与上传任务身份一致")
        reference = await self._storage().save_index_request(
            document_id,
            task_id,
            {
                "chunks": chunks,
                "images": images or [],
                "images_confirmed": images_confirmed,
                "identity": expected_identity,
                "document_id": document_id,
                "version_id": task["version_id"],
                "task_id": task_id,
                "scope_id": task["scope_id"],
                "knowledge_type": task["knowledge_type"],
            },
        )
        self.task_store.update(task_id, index_request_ref=reference)
        return reference

    async def resume_confirmation(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        request = await self._storage().load_index_request(
            str(task.get("document_id") or ""), task_id
        )
        if not request:
            return {"result": self._failure(task_id, "INDEX_REQUEST_MISSING", "索引请求内容不存在")}
        return await self.confirm(
            task_id,
            chunks=request.get("chunks") or [],
            images=request.get("images") or [],
            images_confirmed=bool(request.get("images_confirmed")),
            identity=request.get("identity") or {},
        )

    @serialize_storage_references
    async def _confirm_node(self, state: _WorkflowState) -> dict[str, Any]:
        task_id = state["task_id"]
        chunks = state["chunks"]
        images = state.get("images") or []
        images_confirmed = bool(state.get("images_confirmed"))
        identity = state["identity"]
        task = self._require(task_id)
        expected_identity = {
            "user_id": str(task.get("user_id") or ""),
            "user_role": str(task.get("user_role") or ""),
        }
        supplied_identity = {
            "user_id": str(identity.get("user_id") or ""),
            "user_role": str(identity.get("user_role") or identity.get("role") or ""),
        }
        if (
            supplied_identity["user_id"]
            and supplied_identity["user_id"] != expected_identity["user_id"]
        ) or (
            supplied_identity["user_role"]
            and supplied_identity["user_role"] != expected_identity["user_role"]
        ):
            return {
                "result": self._failure(
                    task_id, "IDENTITY_MISMATCH", "确认身份必须与上传任务身份一致"
                )
            }
        identity = expected_identity
        if task.get("status") not in {"WAITING_CONFIRM", "INDEXING", "RUNNING"}:
            return {"result": self._failure(task_id, "INVALID_STAGE", "当前任务不是待确认状态")}
        if not chunks:
            return {"result": self._failure(task_id, "EMPTY_CHUNKS", "确认结果没有有效分块")}
        knowledge_type = str(task.get("knowledge_type") or "").strip()
        if knowledge_type not in {"enterprise", "demand", "achievement", "expert", "solution"}:
            return {
                "result": self._failure(
                    task_id,
                    "KNOWLEDGE_TYPE_REQUIRED",
                    "确认索引前必须明确资料类型：企业、需求、成果、专家或技术方案",
                )
            }
        version = self.lifecycle_store.get_version(str(task.get("version_id") or "")) if self.lifecycle_store else None
        if not version or any(
            version.get(key) != task.get(key)
            for key in ("document_id", "version_id", "scope_id", "knowledge_type", "user_id")
        ):
            return {"result": self._failure(task_id, "VERSION_MISMATCH", "确认任务缺少匹配的文档版本")}
        self.task_store.update(
            task_id,
            status="INDEXING",
            stage="index",
            progress=80,
            operation_state=OperationState.INDEXING.value,
        )
        try:
            started_at = time.perf_counter()
            version = self.lifecycle_store.update_version(
                str(task["version_id"]), status="INDEXING", parsed_block_count=len(chunks),
                expected_version=version,
            )
            if version is None:
                raise RuntimeError("确认版本写入失败")
            result = await self.adapter.index_document(
                {
                    "document_id": task["document_id"],
                    "version_id": task.get("version_id"),
                    "scope_id": task.get("scope_id", "public"),
                    "knowledge_type": knowledge_type,
                    "chunks": chunks,
                    "chunks_are_final": True,
                    "identity": identity,
                }
            )
            if result.get("status") != "SUCCESS":
                self._audit(
                    "knowledge_index_completed",
                    task_id=task_id,
                    status="FAILED",
                    error=result.get("error", "索引失败"),
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                    **self._audit_context(task),
                )
                if self.lifecycle_store and task.get("version_id"):
                    self.lifecycle_store.update_version(
                        str(task["version_id"]),
                        status="FAILED",
                        error=result.get("error", "索引失败"),
                    )
                return {
                    "result": self._failure(
                        task_id, "INDEX_FAILED", result.get("error", "索引失败")
                    )
                }
            storage_key = await self.save_confirmation_request(
                task_id, chunks=chunks, images=images,
                images_confirmed=images_confirmed, identity=identity,
            )
            reference = {
                "storage_key": storage_key, "task_id": task_id,
                "document_id": task["document_id"], "version_id": task["version_id"],
                "identity": identity,
            }
            await load_confirmed_version_request(
                self._storage(), task,
                {**version, "artifact_refs": {"confirmed_index_request": reference}},
            )
            if self.lifecycle_store.update_version(
                    str(task["version_id"]),
                    status="INDEXED",
                    publication_status="PUBLISHED",
                    chunk_count=int(result.get("indexed_count") or 0),
                    indexed_count=int(result.get("indexed_count") or 0),
                    index_time=datetime.now(timezone.utc),
                    artifact_refs={"confirmed_index_request": reference},
                    expected_version=version,
                ) is None:
                raise RuntimeError("确认版本来源写入失败")
            cleanup = await self.cleanup_previous_version(task)
            self._audit(
                "knowledge_chunk_completed",
                task_id=task_id,
                status="SUCCESS",
                chunk_count=int(result.get("indexed_count") or 0),
                summary="解析结果已完成分块",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                **self._audit_context(task),
            )
            self._audit(
                "knowledge_index_completed",
                task_id=task_id,
                status="SUCCESS",
                indexed_count=int(result.get("indexed_count") or 0),
                summary="知识已写入向量库",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                **self._audit_context(task),
            )
            return {
                "result": self.task_store.update(
                    task_id,
                    status="SUCCEEDED",
                    operation_state=OperationState.PUBLISHED.value,
                    stage="completed",
                    progress=100,
                    indexed_count=result.get("indexed_count", 0),
                    indexed_chunks=result.get("indexed_chunks", []),
                    index_result=result,
                    replacement_cleanup=cleanup,
                )
                or {}
            }
        except Exception as exc:  # noqa: BLE001 - 工作流统一记录失败状态
            self._audit(
                "knowledge_index_completed",
                task_id=task_id,
                status="FAILED",
                error=str(exc),
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                **self._audit_context(task),
            )
            if self.lifecycle_store and task.get("version_id"):
                self.lifecycle_store.update_version(
                    str(task["version_id"]), status="FAILED", error=str(exc)
                )
            return {"result": self._failure(task_id, "INDEX_FAILED", str(exc))}

    @serialize_storage_references
    async def cleanup_previous_version(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """新版本索引成功后清理旧版本，避免同一文档重复检索和占用存储。"""

        previous_version_id = task.get("previous_version_id")
        previous_storage_key = task.get("previous_storage_key")
        if not previous_version_id or not previous_storage_key:
            return None
        if previous_storage_key == task.get("storage_key"):
            return {"status": "OVERWRITTEN"}
        if previous_version_id == task.get("version_id"):
            await self._storage().delete_original_file(str(previous_storage_key))
            return {"status": "DELETED", "storage_key": previous_storage_key}
        if self.outbox_repository is not None:
            event = self.outbox_repository.append(
                f"version_cleanup:{task.get('document_id')}:{previous_version_id}",
                "VERSION_CLEANUP",
                {
                    "document_id": task.get("document_id"),
                    "version_id": previous_version_id,
                    "storage_key": previous_storage_key,
                    "scope_id": task.get("scope_id", "public"),
                    "knowledge_type": task.get("knowledge_type"),
                    "user_id": task.get("user_id", ""),
                },
            )
            return {"status": "QUEUED", "event_id": event.get("event_id")}
        knowledge_type = str(task.get("knowledge_type") or "").strip()
        return await cleanup_document_version(self.adapter, self.lifecycle_store,
            {
                "document_id": task.get("document_id"),
                "version_id": previous_version_id,
                "storage_key": previous_storage_key,
                "scope_id": task.get("scope_id", "public"),
                "knowledge_type": knowledge_type,
                "user_id": task.get("user_id", ""),
            }
        )

    def status(self, task_id: str) -> dict[str, Any] | None:
        return self.task_store.get(task_id)

    def resume(self, task_id: str) -> dict[str, Any] | None:
        """从持久化任务恢复可继续的阶段；不伪造 LangGraph 的内存快照。"""
        task = self.task_store.get(task_id)
        if task is None:
            return None
        if task.get("status") == "PARSING":
            return self.task_store.update(
                task_id,
                status="UPLOADED",
                stage="upload",
                progress=25,
                error_code="RECOVERED_AFTER_RESTART",
                error="解析进程中断，可重新开始解析",
            )
        if task.get("status") == "INDEXING":
            if task.get("parsed_result"):
                return self.task_store.update(
                    task_id,
                    status="WAITING_CONFIRM",
                    stage="confirm",
                    progress=70,
                    error_code="RECOVERED_AFTER_RESTART",
                    error="索引进程中断，可重新确认并建立索引",
                )
            return self.task_store.update(
                task_id,
                status="UPLOADED",
                stage="upload",
                progress=25,
                error_code="RECOVERED_AFTER_RESTART",
                error="任务中断，可重新开始解析",
            )
        return task

    def _storage(self) -> Any:
        if self.storage_service is None:
            raise RuntimeError("MinIO 存储服务尚未装配")
        return self.storage_service

    def _require(self, task_id: str) -> dict[str, Any]:
        task = self.task_store.get(task_id)
        if task is None:
            raise KeyError("编排任务不存在")
        return task

    def _failure(self, task_id: str, error_code: str, error: str) -> dict[str, Any]:
        return self.task_store.update(
            task_id,
            status="FAILED",
            operation_state=OperationState.FAILED.value,
            stage="failed",
            progress=100,
            error_code=error_code,
            error=error,
        ) or {"task_id": task_id, "status": "FAILED", "error_code": error_code, "error": error}
