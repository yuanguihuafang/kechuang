"""管理员 HTTP 边界。

管理员页面只依赖本模块；解析、索引、检索仍由公共 Agent 提供。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pymysql.err import DataError

from admin.api.v1.schemas import (
    AdminCollectionActivateRequest,
    AdminCollectionRestoreRequest,
    AdminKnowledgeUpdateConfirmRequest,
    AdminMysqlSyncRequest,
    AdminRetentionRunRequest,
    AdminRuntimeStartRequest,
    SupervisorRecoveryRequest,
)
from agents.retrieval_agent.schemas import RetrievalRequest, RetrievalResult
from core.contracts.knowledge import KnowledgeType
from core.contracts.tools import ToolContext
from core.contracts.workflows import (
    FileConfirmRequest,
    LifecycleDecisionRequest,
    SupervisorTaskResponse,
    UnifiedDeleteRequest,
)
from runtime.container import GatewayRuntime
from runtime.contracts import AdminServiceContract
from services.application.drafts import DraftApplicationService
from services.mysql.mysql_sink import MySQLUnavailableError

logger = logging.getLogger(__name__)


def _service(runtime: GatewayRuntime) -> AdminServiceContract:
    return runtime.require_admin()


def build_admin_router(runtime: GatewayRuntime) -> APIRouter:
    router = APIRouter(tags=["管理员接口"])

    @router.post("/api/v1/admin/drafts/{draft_id}/unpublish", summary="管理员下架草稿",
                 description="管理员下架已发布草稿及其检索内容。")
    async def unpublish_admin_draft(draft_id: str, payload: LifecycleDecisionRequest) -> dict[str, object]:
        if not str(payload.user_id or "").strip():
            raise HTTPException(status_code=422, detail="管理员操作必须提供目标 user_id")
        context = ToolContext(user_id=payload.user_id, user_role="admin", trace_id="draft-admin")
        return await DraftApplicationService(runtime).unpublish(draft_id, payload, context=context, admin=True)

    @router.get(
        "/api/v1/admin/overview", summary="获取管理总览", description="返回管理台首页统计数据。"
    )
    async def overview() -> dict[str, object]:
        return _service(runtime).overview()

    @router.get("/api/v1/admin/documents", summary="获取管理文档目录", description="返回文档目录。")
    async def documents(
        scope_id: str | None = Query(default=None, max_length=128),
        user_id: str | None = Query(default=None, max_length=128),
        knowledge_type: str | None = Query(default=None, max_length=32),
        status: str | None = Query(default=None, max_length=32),
        search: str | None = Query(default=None, max_length=200),
        published_or_admin_only: bool = Query(default=False),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, object]:
        return _service(runtime).document_catalog(
            scope_id=scope_id,
            user_id=user_id,
            knowledge_type=knowledge_type,
            status=status,
            search=search,
            published_or_admin_only=published_or_admin_only,
            page=page,
            page_size=page_size,
        )

    @router.get(
        "/api/v1/admin/knowledge-updates",
        summary="分页获取知识库更新",
        description="按草稿类型和状态过滤后分页返回知识库更新。",
    )
    async def knowledge_updates(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        published_only: bool = Query(default=True),
        knowledge_type: str | None = Query(default=None, max_length=32),
    ) -> dict[str, object]:
        return _service(runtime).knowledge_updates(
            page=page,
            page_size=page_size,
            published_only=published_only,
            knowledge_type=knowledge_type,
        )

    async def _knowledge_update_detail(record_id: str) -> dict[str, object]:
        result = _service(runtime).knowledge_update_detail(record_id)
        if result is None:
            raise HTTPException(status_code=404, detail="知识库更新记录不存在")
        return result

    @router.get(
        "/api/v1/admin/knowledge-updates/{record_id}",
        summary="获取知识库更新详情",
        description="返回草稿、附件和关联文档。",
    )
    async def knowledge_update_detail(record_id: str) -> dict[str, object]:
        return await _knowledge_update_detail(record_id)

    @router.delete(
        "/api/v1/admin/drafts/{draft_id}",
        summary="删除草稿及关联数据",
        description="删除未发布草稿，并清理 PostgreSQL、Milvus、MinIO 中的关联数据。已发布草稿必须先下架。",
    )
    async def delete_draft(draft_id: str) -> dict[str, object]:
        try:
            return await _service(runtime).delete_draft(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/api/v1/admin/knowledge-updates/{record_id}/confirm",
        summary="确认知识库更新",
        description="更新原 MySQL 记录和对应 Milvus 分块。",
    )
    async def confirm_knowledge_update(
        record_id: str, payload: AdminKnowledgeUpdateConfirmRequest
    ) -> dict[str, object]:
        try:
            return await _service(runtime).confirm_knowledge_update(record_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get(
        "/api/v1/admin/business-records",
        summary="获取业务记录",
        description="获取管理员可查看的正式业务记录。",
    )
    async def business_records(
        record_type: str | None = Query(default=None, max_length=32),
    ) -> dict[str, object]:
        """读取 MySQL 正式技术方案和内部拜访记录，不混入 PostgreSQL 文档目录。"""

        try:
            records = runtime.require_business_record_sink().list(
                user_id="", record_type=record_type
            )
        except MySQLUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={"message": str(exc), "dependency": "mysql"},
            ) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"records": records, "total": len(records), "source": "mysql"}

    @router.get(
        "/api/v1/admin/field-policies",
        summary="获取字段配置",
        description="返回字段名称、顺序和显示策略。",
    )
    async def field_policies() -> dict[str, object]:
        return _service(runtime).field_policies()

    @router.get(
        "/api/v1/admin/metrics",
        summary="获取管理指标",
        description="返回管理台所需的系统和业务运行指标。",
    )
    async def metrics() -> dict[str, object]:
        return _service(runtime).metrics()

    @router.post(
        "/api/v1/admin/knowledge/retrieve",
        response_model=RetrievalResult,
        summary="管理员检索知识库",
        description="以管理员权限检索全部可管理的知识库内容。",
    )
    async def retrieve(payload: RetrievalRequest) -> RetrievalResult:
        return await _service(runtime).retrieve(payload)

    @router.post(
        "/api/v1/admin/knowledge/sync-mysql",
        summary="同步 MySQL 知识库",
        description="启动从 MySQL 业务表重建知识索引的任务。",
    )
    async def sync_mysql(payload: AdminMysqlSyncRequest) -> dict[str, object]:
        try:
            return await _service(runtime).start_mysql_sync(payload)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get(
        "/api/v1/admin/knowledge/sync-mysql/{task_id}",
        summary="查询 MySQL 同步任务",
        description="查询 MySQL 知识库同步任务状态。",
    )
    async def sync_mysql_status(task_id: str) -> dict[str, object]:
        result = _service(runtime).mysql_sync_status(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail="MySQL 同步任务不存在")
        return result

    @router.post(
        "/api/v1/admin/knowledge/sync-mysql/{task_id}/stop",
        summary="停止 MySQL 同步任务",
        description="请求停止指定的 MySQL 知识库同步任务。",
    )
    async def stop_mysql_sync(task_id: str) -> dict[str, object]:
        result = _service(runtime).stop_mysql_sync(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail="MySQL 同步任务不存在")
        return result

    @router.get(
        "/api/v1/admin/knowledge/mysql-counts",
        summary="获取知识库数量",
        description="获取 MySQL 记录数和 Milvus 索引数；默认读取缓存，refresh=true 时才查询最新数量。",
    )
    async def mysql_counts(refresh: bool = Query(default=False)) -> dict[str, object]:
        return {
            "counts": _service(runtime).mysql_sync_counts(refresh=refresh),
            "source": "mysql_and_milvus",
            "refreshed": refresh,
        }

    @router.post(
        "/api/v1/admin/knowledge/collections/{knowledge_type}/rebuild",
        summary="检查知识库恢复来源",
        description="只读检查可恢复的 PUBLISHED 草稿、附件来源和 MySQL 数量；不重建或修改活动 Collection。",
    )
    async def rebuild_collection(
        knowledge_type: str, confirm: bool = Query(default=False)
    ) -> dict[str, object]:
        try:
            return await _service(runtime).rebuild_collection(knowledge_type, confirm)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.exception(
                "Milvus Collection 重建请求失败：knowledge_type=%s", knowledge_type
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/api/v1/admin/knowledge/collections/{knowledge_type}/restore",
        summary="恢复完整公开索引",
        description="按已确认预检报告写入并校验影子 Collection；校验通过后等待管理员单独确认切换稳定 Alias。",
    )
    async def restore_collection(
        knowledge_type: str, payload: AdminCollectionRestoreRequest
    ) -> dict[str, object]:
        try:
            return await _service(runtime).restore_collection(
                knowledge_type,
                payload.confirm,
                payload.preflight_id,
                payload.accepted_missing_fingerprint,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.exception(
                "Milvus Collection 索引恢复请求失败：knowledge_type=%s", knowledge_type
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/api/v1/admin/knowledge/collections/{knowledge_type}/activate",
        summary="管理员确认切换恢复后的 Collection",
        description="重新校验恢复来源与影子库后，切换稳定 Alias。",
    )
    async def activate_collection_recovery(
        knowledge_type: str, payload: AdminCollectionActivateRequest
    ) -> dict[str, object]:
        try:
            return await _service(runtime).activate_collection_recovery(
                knowledge_type, payload.task_id, payload.confirm
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.exception(
                "Milvus Collection 切换确认失败：knowledge_type=%s task_id=%s",
                knowledge_type,
                payload.task_id,
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/api/v1/admin/knowledge/collections/{knowledge_type}/rebuild-mysql",
        summary="仅重建 MySQL 同步数据",
        description="只清理对应 Collection 中的 MySQL 同步向量，保留用户和管理台文档。",
    )
    async def rebuild_mysql_data(
        knowledge_type: str, confirm: bool = Query(default=False)
    ) -> dict[str, object]:
        try:
            return _service(runtime).rebuild_mysql_data(knowledge_type, confirm)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get(
        "/api/v1/admin/audit-events",
        summary="查询审计事件",
        description="按条件分页查询系统审计事件。",
    )
    async def audit_events(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        service: str | None = Query(default=None, max_length=64),
        user_id: str | None = Query(default=None, max_length=128),
        task_id: str | None = Query(default=None, max_length=128),
        trace_id: str | None = Query(default=None, max_length=128),
        document_id: str | None = Query(default=None, max_length=256),
        event_name: str | None = Query(default=None, max_length=128),
    ) -> dict[str, object]:
        return _service(runtime).audit_events(
            page=page,
            page_size=page_size,
            service=service,
            user_id=user_id,
            task_id=task_id,
            document_id=document_id,
            event_name=event_name,
            trace_id=trace_id,
        )

    @router.get(
        "/api/v1/admin/operations", summary="查询管理操作", description="获取管理台操作和治理状态。"
    )
    async def operations() -> dict[str, object]:
        return await _service(runtime).operations()

    @router.post(
        "/api/v1/admin/operations/retention",
        summary="执行数据保留清理",
        description="按管理员确认执行过期运行数据清理。",
    )
    async def retention(payload: AdminRetentionRunRequest) -> dict[str, object]:
        try:
            return await _service(runtime).run_retention(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/v1/admin/runtime-evidence", summary="获取累计运行时长证据")
    async def runtime_evidence() -> dict[str, object]:
        return _service(runtime).runtime_evidence()

    @router.post("/api/v1/admin/runtime-evidence/start", summary="设置系统开始时间")
    async def start_runtime_evidence(payload: AdminRuntimeStartRequest) -> dict[str, object]:
        try:
            return _service(runtime).start_runtime_evidence(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v1/admin/runtime-evidence/reset", summary="重新开始累计运行时长统计")
    async def reset_runtime_evidence(payload: AdminRuntimeStartRequest) -> dict[str, object]:
        try:
            return _service(runtime).reset_runtime_evidence(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/v1/admin/runtime-evidence/export", summary="导出累计运行时长证据")
    async def export_runtime_evidence(
        format_name: str = Query(default="json", alias="format", pattern="^(json|csv)$"),
    ) -> Response:
        content, media_type, filename = _service(runtime).export_runtime_evidence(format_name)
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get(
        "/api/v1/admin/documents/{document_id}",
        summary="获取管理文档详情",
        description="返回文档详情和关联版本。",
    )
    async def document_detail(document_id: str) -> dict[str, object]:
        result = _service(runtime).document_detail(document_id)
        if result is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        return result

    @router.get("/api/v1/admin/conversations", summary="分页获取用户会话")
    async def conversations(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, object]:
        try:
            return _service(runtime).conversations(page=page, page_size=page_size)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/api/v1/admin/conversations/{session_id}", summary="获取用户会话详情")
    async def conversation_detail(session_id: str) -> dict[str, object]:
        try:
            result = _service(runtime).conversation_detail(session_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return result

    @router.delete(
        "/api/v1/admin/conversations/{session_id}",
        summary="删除用户会话及关联数据",
        description="删除会话及其已发布或未发布草稿；其他活动草稿仍引用的数据会保留。",
    )
    async def delete_conversation(session_id: str) -> dict[str, object]:
        try:
            return await _service(runtime).delete_conversation(session_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get(
        "/api/v1/admin/tasks",
        summary="分页查询管理任务",
        description="按需分页查询 Supervisor 管理任务，可按文档或操作筛选。",
    )
    async def task_catalog(
        operation: str | None = Query(default=None, max_length=64),
        document_id: str | None = Query(default=None, max_length=256),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        return _service(runtime).task_catalog(
            operation=operation,
            document_id=document_id,
            page=page,
            page_size=page_size,
        )

    @router.get(
        "/api/v1/admin/tasks/{task_id}",
        response_model=SupervisorTaskResponse,
        summary="查询管理任务",
        description="返回上传、解析、提取和确认任务状态及结果。",
    )
    async def task_status(task_id: str) -> SupervisorTaskResponse:
        task = _service(runtime).task_status(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Supervisor 编排任务不存在")
        return SupervisorTaskResponse.model_validate(
            await _service(runtime).with_parse_result(task)
        )

    @router.post(
        "/api/v1/admin/tasks/{task_id}/recover",
        summary="恢复管理任务",
        description="恢复或重试指定的管理任务。",
    )
    async def recover_task(task_id: str, payload: SupervisorRecoveryRequest) -> dict[str, object]:
        try:
            return await _service(runtime).recover_task(task_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post(
        "/api/v1/admin/tasks/{task_id}/parse",
        response_model=SupervisorTaskResponse,
        status_code=202,
        summary="解析管理文档",
        description="启动 MinerU 文档解析任务。",
    )
    async def parse_task(task_id: str) -> SupervisorTaskResponse:
        try:
            return SupervisorTaskResponse.model_validate(
                await _service(runtime).parse_task(task_id)
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/api/v1/admin/tasks/{task_id}/extract-fields",
        response_model=SupervisorTaskResponse,
        status_code=202,
        summary="提取草稿字段",
        description="根据解析原文调用 LLM 提取一个或多个草稿。",
    )
    async def extract_fields_task(
        task_id: str, knowledge_type: KnowledgeType | None = Query(default=None)
    ) -> SupervisorTaskResponse:
        try:
            return SupervisorTaskResponse.model_validate(
                await _service(runtime).extract_fields_task(task_id, knowledge_type)
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/api/v1/admin/tasks/{task_id}/confirm",
        response_model=SupervisorTaskResponse,
        status_code=202,
        summary="确认管理草稿",
        description="确认草稿后写入 MySQL 并建立 Milvus 索引。",
    )
    async def confirm_task(task_id: str, payload: FileConfirmRequest) -> SupervisorTaskResponse:
        try:
            return SupervisorTaskResponse.model_validate(
                await _service(runtime).confirm_task(task_id, payload)
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DataError as exc:
            if exc.args and exc.args[0] == 1406:
                raise HTTPException(status_code=422, detail="MySQL 字段长度超限") from exc
            raise
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get(
        "/api/v1/admin/documents/{document_id}/versions",
        summary="获取文档版本",
        description="返回指定文档的版本列表。",
    )
    async def versions(document_id: str) -> dict[str, object]:
        return _service(runtime).document_versions(document_id)

    @router.get(
        "/api/v1/admin/documents/{document_id}/parse-result",
        summary="获取解析结果",
        description="返回 MinerU 解析原文和分块结果。",
    )
    async def parse_result(document_id: str) -> dict[str, object]:
        result = await _service(runtime).parse_result(document_id)
        if result is None:
            raise HTTPException(status_code=404, detail="文档解析结果不存在")
        return result

    async def lifecycle(
        version_id: str, payload: LifecycleDecisionRequest, action: str
    ) -> dict[str, object]:
        if not str(payload.user_id or "").strip():
            raise HTTPException(status_code=422, detail="管理员操作必须提供目标 user_id")
        try:
            result, task = await _service(runtime).lifecycle_action(
                action,
                version_id,
                payload.user_id,
                reason_code=payload.reason_code,
                reason_text=payload.reason_text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": f"{action} 失败",
                    "task_id": task.get("task_id") if task else None,
                },
            )
        return {**result, "task_id": task.get("task_id") if task else None}

    for action in ("publish", "unpublish", "recycle", "restore", "purge"):

        async def handler(
            version_id: str, payload: LifecycleDecisionRequest, _action: str = action
        ) -> dict[str, object]:
            return await lifecycle(version_id, payload, _action)

        router.add_api_route(
            f"/api/v1/admin/versions/{{version_id}}/{action}",
            handler,
            methods=["POST"],
            summary={
                "publish": "发布文档版本",
                "unpublish": "下架文档版本",
                "recycle": "回收文档版本",
                "restore": "恢复文档版本",
                "purge": "彻底删除文档版本",
            }[action],
            description=f"对指定文档版本执行{action}操作。",
        )

    @router.delete(
        "/api/v1/admin/documents/{document_id}",
        summary="删除管理文档",
        description="删除指定文档及其关联生命周期数据。",
    )
    async def delete_document(document_id: str, payload: UnifiedDeleteRequest) -> JSONResponse:
        try:
            result = await _service(runtime).delete_document(document_id, payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(
            status_code=200 if result.get("status") == "DELETED" else 502,
            content={**result, "document_id": document_id},
        )

    @router.post(
        "/api/v1/admin/files/upload",
        response_model=SupervisorTaskResponse,
        status_code=202,
        summary="管理员上传文档",
        description="上传文档并创建管理台解析任务。",
    )
    async def upload_file(
        file: UploadFile = File(...),  # noqa: B008 - FastAPI 文件表单声明
        scope_id: str = Form(default="public"),
        user_id: str = Form(default="admin"),
        user_role: str = Form(default="admin"),
        knowledge_type: str = Form(default=""),
        thread_id: str | None = Form(default=None),
        idempotency_key: str | None = Form(default=None),
        document_id: str | None = Form(default=None),
    ) -> SupervisorTaskResponse:
        try:
            result = await _service(runtime).upload_file(
                file,
                scope_id=scope_id,
                user_id=user_id,
                user_role=user_role,
                knowledge_type=knowledge_type,
                thread_id=thread_id,
                idempotency_key=idempotency_key,
                document_id=document_id,
            )
            return SupervisorTaskResponse.model_validate(result)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return router
