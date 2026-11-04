"""Retrieval HTTP 适配器；业务 Agent 位于 ``agents.retrieval_agent``。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException

from agents.retrieval_agent.agent import RetrievalAgent
from agents.retrieval_agent.schemas import (
    IndexChunk,
    IndexRequest,
    IndexResult,
    RetrievalRequest,
    RetrievalResult,
    TaskSubmitResponse,
)
from core.contracts.knowledge import (
    BusinessImportRequest,
    DocumentKnowledgeRequest,
    DocumentUnpublishRequest,
    IndexTextRequest,
    SessionSummaryRequest,
)
from core.contracts.retrieval import ChunkPreviewRequest
from runtime.workflows.task_runner import SupervisorTaskRunner
from runtime.workflows.task_store import SupervisorTaskStore
from services.observability.logging import get_logger, log_event, setup_logger, trace_span
from services.observability.metrics import metrics
from services.observability.trace_context import get_trace_context

# 仅使用交付包根目录和当前 Agent 目录；Agent 不依赖总体项目的 backend 包。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parent

# 日志配置
LOG_FILE = PROJECT_ROOT / "logs" / "app.log"
logger = setup_logger("retrieval", log_path=LOG_FILE)
agent_logger = get_logger("retrieval", "agent")

app = APIRouter(tags=["知识检索"])


@dataclass
class _RouteContext:
    agent: RetrievalAgent
    task_store: SupervisorTaskStore
    task_runner: SupervisorTaskRunner | None = None


_route_context: _RouteContext | None = None
_route_context_lock = Lock()


def _context() -> _RouteContext:
    global _route_context
    with _route_context_lock:
        if _route_context is None:
            _route_context = _RouteContext(
                RetrievalAgent(), SupervisorTaskStore(service="retrieval")
            )
        return _route_context


def close() -> None:
    """释放此 worker 的检索 Agent，并允许后续 lifespan 重新装配。"""
    global _route_context
    with _route_context_lock:
        context = _route_context
        if context is None:
            return
        try:
            context.agent.close()
        finally:
            _route_context = None


class _ContextProxy:
    def __init__(self, attribute: str) -> None:
        self.attribute = attribute

    def __getattr__(self, name: str):
        return getattr(getattr(_context(), self.attribute), name)


agent = _ContextProxy("agent")
task_store = _ContextProxy("task_store")


def configure_task_store(store: object) -> None:
    """由统一网关注入共享任务仓储。"""
    _context().task_store = store


def _create_task(*, operation: str, document_id: str | None = None, status: str = "RUNNING") -> str:
    return _context().task_store.create(
        operation=operation, document_id=document_id, status=status
    )["task_id"]


def configure_task_runner(runner: SupervisorTaskRunner) -> None:
    _context().task_runner = runner


HEALTH_TIMEOUT_SECONDS = 4.0
_background_tasks: set[asyncio.Task[object]] = set()

logger.info(
    "Retrieval 服务初始化完成",
    extra={
        "structured": {
            "event": "app_init",
            "service": "retrieval",
        }
    },
)


def finish_index_task(task_id: str, result: IndexResult) -> IndexResult:
    terminal_status = "SUCCEEDED" if result.status in {"SUCCESS", "UNPUBLISHED"} else "FAILED"
    task_store.complete(
        task_id,
        status=terminal_status,
        stage="completed" if terminal_status == "SUCCEEDED" else "failed",
        result_status=result.status,
        result_ref=f"document:{result.document_id}" if result.document_id else None,
        indexed_count=result.indexed_count,
        error=result.error,
    )
    return result.model_copy(update={"task_id": task_id})


def _queue(coroutine: object, *, task_id: str, operation: str) -> None:
    if _context().task_runner is not None:
        _context().task_runner.submit(task_id, operation, lambda: coroutine)  # type: ignore[arg-type]
        return
    task = asyncio.create_task(coroutine)  # type: ignore[arg-type]
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_async_index(task_id: str, operation: str, action: object) -> dict[str, object]:
    # 索引涉及分块、Embedding 和 Milvus，结果以任务记录为准，不在路由层维护第二份状态。
    task_store.update(task_id, status="RUNNING", stage="executing", progress=5)
    try:
        result = await action  # type: ignore[misc]
        finished = finish_index_task(task_id, result)
        return {
            "status": "SUCCEEDED"
            if finished.status in {"SUCCESS", "UNPUBLISHED"}
            else "FAILED"
        }
    except Exception as exc:  # noqa: BLE001 - 异步任务状态必须持久化失败原因
        task = task_store.update(
            task_id, status="FAILED", progress=100, stage="failed", error=str(exc)
        )
        log_event(
            agent_logger,
            "async_task_failed",
            task_id=task_id,
            operation=operation,
            error_type=type(exc).__name__,
        )
        return task or {}


# ===== 检索 =====


@app.post(
    "/api/v1/knowledge/retrieve",
    response_model=RetrievalResult,
    summary="检索知识库",
    description="根据查询条件检索可见知识库内容。",
)
async def retrieve(request: RetrievalRequest) -> RetrievalResult:
    """从 Milvus 检索知识。"""
    log_event(agent_logger, "retrieve_request", query=request.query[:100], top_k=request.top_k)
    try:
        with metrics.stage("retrieval.total"):
            result = await agent.retrieve(request)
        log_event(
            agent_logger,
            "retrieve_complete",
            status=result.status,
            evidence_count=len(result.evidences),
        )
        return result
    except Exception as exc:
        agent_logger.error(f"检索失败: {exc}")
        raise


# ===== 索引 =====


@app.post(
    "/api/v1/knowledge/index",
    response_model=IndexResult,
    summary="建立知识索引",
    description="将文档内容切分并写入知识库索引。",
)
async def index(request: IndexRequest) -> IndexResult:
    """索引预处理的文本块到 Milvus。"""
    task_id = _create_task(operation="index", document_id=request.document_id)
    log_event(
        agent_logger,
        "index_request",
        document_id=request.document_id,
        chunk_count=len(request.chunks),
    )
    try:
        agent_logger.info(
            "正在切块",
            extra={
                "structured": {
                    "event": "chunking_start",
                    "task_id": task_id,
                    "document_id": request.document_id,
                }
            },
        )
        result = await agent.index(request)
        agent_logger.info(
            "切块完成",
            extra={
                "structured": {
                    "event": "chunking_complete",
                    "task_id": task_id,
                    "document_id": request.document_id,
                    "indexed_count": result.indexed_count,
                }
            },
        )
        log_event(
            agent_logger, "index_complete", status=result.status, indexed_count=result.indexed_count
        )
        return finish_index_task(task_id, result)
    except Exception as exc:
        task_store.update(task_id, status="FAILED", progress=100, stage="failed", error=str(exc))
        agent_logger.error(f"索引失败: {exc}")
        raise


@app.post(
    "/api/v1/knowledge/chunk-preview",
    response_model=list[IndexChunk],
    summary="预览知识分块",
    description="预览文档切分后的知识块。",
)
async def chunk_preview(request: ChunkPreviewRequest) -> list[IndexChunk]:
    """返回与正式索引完全一致的最终分块，不写入任何数据库。"""
    chunks = agent.preview_chunks_from_blocks(request)
    return chunks


@app.post(
    "/api/v1/knowledge/documents/import",
    response_model=IndexResult,
    summary="导入知识文档",
    description="导入文档并建立知识库索引。",
)
async def import_business_data(request: BusinessImportRequest) -> IndexResult:
    """导入平台已清洗、已发布的主业务知识数据。"""

    task_id = _create_task(operation="business_import", document_id=request.document_id)
    log_event(
        agent_logger,
        "business_import_request",
        document_id=request.document_id,
        record_count=len(request.records),
        scope_id=request.scope_id,
    )
    try:
        result = await agent.import_business_data(request)
        log_event(
            agent_logger,
            "business_import_complete",
            status=result.status,
            indexed_count=result.indexed_count,
        )
        return finish_index_task(task_id, result)
    except Exception as exc:
        task_store.update(task_id, status="FAILED", progress=100, stage="failed", error=str(exc))
        agent_logger.error(f"业务数据导入失败: {exc}")
        raise


@app.post(
    "/api/v1/knowledge/documents/{document_id}/publish",
    response_model=IndexResult,
    summary="发布知识文档",
    description="发布指定文档及其知识索引。",
)
async def publish_document(document_id: str, request: DocumentKnowledgeRequest) -> IndexResult:
    """发布完整清洗数据并写入主业务高可信库。"""

    task_id = _create_task(operation="publish", document_id=document_id)
    log_event(
        agent_logger, "document_publish_request", document_id=document_id, scope_id=request.scope_id
    )
    try:
        result = await agent.publish_document(document_id, request)
        log_event(
            agent_logger, "document_publish_complete", document_id=document_id, status=result.status
        )
        return finish_index_task(task_id, result)
    except Exception as exc:
        task_store.update(task_id, status="FAILED", progress=100, stage="failed", error=str(exc))
        raise


@app.post(
    "/api/v1/knowledge/documents/{document_id}/reindex",
    response_model=IndexResult,
    summary="重建知识索引",
    description="重新建立指定文档的知识索引。",
)
async def reindex_document(document_id: str, request: DocumentKnowledgeRequest) -> IndexResult:
    """删除文档旧向量后，用最新清洗数据重建索引。"""

    task_id = _create_task(operation="reindex", document_id=document_id)
    log_event(
        agent_logger, "document_reindex_request", document_id=document_id, scope_id=request.scope_id
    )
    try:
        result = await agent.reindex_document(document_id, request)
        log_event(
            agent_logger, "document_reindex_complete", document_id=document_id, status=result.status
        )
        return finish_index_task(task_id, result)
    except Exception as exc:
        task_store.update(task_id, status="FAILED", progress=100, stage="failed", error=str(exc))
        raise


@app.post(
    "/api/v1/knowledge/documents/{document_id}/unpublish",
    response_model=IndexResult,
    summary="下架知识文档",
    description="下架指定文档及其知识索引。",
)
async def unpublish_document(document_id: str, request: DocumentUnpublishRequest) -> IndexResult:
    """下架文档；默认保留向量，仅删除或彻底清理时移除。"""

    task_id = _create_task(operation="unpublish", document_id=document_id)
    log_event(
        agent_logger,
        "document_unpublish_request",
        document_id=document_id,
        scope_id=request.scope_id,
    )
    try:
        result = await agent.unpublish_document(document_id, request)
        log_event(
            agent_logger,
            "document_unpublish_complete",
            document_id=document_id,
            status=result.status,
        )
        return finish_index_task(task_id, result)
    except Exception as exc:
        task_store.update(task_id, status="FAILED", progress=100, stage="failed", error=str(exc))
        raise


@app.post(
    "/api/v1/knowledge/index-text",
    response_model=IndexResult,
    summary="索引文本内容",
    description="将文本内容直接写入知识库索引。",
)
async def index_text(request: IndexTextRequest) -> IndexResult:
    """将纯文本分块并索引到 Milvus。"""
    task_id = _create_task(operation="index_text", document_id=request.document_id)
    log_event(
        agent_logger,
        "index_text_request",
        document_id=request.document_id,
        text_length=len(request.text),
    )
    try:
        result = await agent.index_text(request)
        log_event(
            agent_logger,
            "index_text_complete",
            status=result.status,
            indexed_count=result.indexed_count,
        )
        return finish_index_task(task_id, result)
    except Exception as exc:
        task_store.update(task_id, status="FAILED", progress=100, stage="failed", error=str(exc))
        agent_logger.error(f"文本索引失败: {exc}")
        raise


@app.post(
    "/api/v1/knowledge/private/session-summary",
    response_model=IndexResult,
    summary="保存会话摘要索引",
    description="保存当前会话的私有摘要索引。",
)
async def store_session_summary(request: SessionSummaryRequest) -> IndexResult:
    """保存内部用户会话摘要；只进入 inner，不进入公开库。"""
    result = await agent.store_session_summary(request)
    return result


# ===== 异步索引任务 =====


@app.post(
    "/api/v1/knowledge/index/async",
    response_model=TaskSubmitResponse,
    status_code=202,
    summary="异步建立知识索引",
    description="提交异步知识索引任务。",
)
async def index_async(request: IndexRequest) -> TaskSubmitResponse:
    task_id = _create_task(operation="index", document_id=request.document_id, status="QUEUED")
    _queue(
        _run_async_index(task_id, "index", agent.index(request)), task_id=task_id, operation="index"
    )
    return TaskSubmitResponse(
        task_id=task_id,
        operation="index",
        document_id=request.document_id,
        status_url=f"/api/v1/knowledge/tasks/{task_id}",
    )


@app.post(
    "/api/v1/knowledge/documents/import/async",
    response_model=TaskSubmitResponse,
    status_code=202,
    summary="异步导入知识文档",
    description="提交异步文档导入任务。",
)
async def import_business_data_async(request: BusinessImportRequest) -> TaskSubmitResponse:
    task_id = _create_task(
        operation="business_import", document_id=request.document_id, status="QUEUED"
    )
    _queue(
        _run_async_index(task_id, "business_import", agent.import_business_data(request)),
        task_id=task_id,
        operation="business_import",
    )
    return TaskSubmitResponse(
        task_id=task_id,
        operation="business_import",
        document_id=request.document_id,
        status_url=f"/api/v1/knowledge/tasks/{task_id}",
    )


@app.post(
    "/api/v1/knowledge/documents/{document_id}/publish/async",
    response_model=TaskSubmitResponse,
    status_code=202,
    summary="异步发布知识文档",
    description="提交异步文档发布任务。",
)
async def publish_document_async(
    document_id: str, request: DocumentKnowledgeRequest
) -> TaskSubmitResponse:
    task_id = _create_task(operation="publish", document_id=document_id, status="QUEUED")
    _queue(
        _run_async_index(task_id, "publish", agent.publish_document(document_id, request)),
        task_id=task_id,
        operation="publish",
    )
    return TaskSubmitResponse(
        task_id=task_id,
        operation="publish",
        document_id=document_id,
        status_url=f"/api/v1/knowledge/tasks/{task_id}",
    )


@app.post(
    "/api/v1/knowledge/documents/{document_id}/reindex/async",
    response_model=TaskSubmitResponse,
    status_code=202,
    summary="异步重建知识索引",
    description="提交异步知识索引重建任务。",
)
async def reindex_document_async(
    document_id: str, request: DocumentKnowledgeRequest
) -> TaskSubmitResponse:
    task_id = _create_task(operation="reindex", document_id=document_id, status="QUEUED")
    _queue(
        _run_async_index(task_id, "reindex", agent.reindex_document(document_id, request)),
        task_id=task_id,
        operation="reindex",
    )
    return TaskSubmitResponse(
        task_id=task_id,
        operation="reindex",
        document_id=document_id,
        status_url=f"/api/v1/knowledge/tasks/{task_id}",
    )


@app.post(
    "/api/v1/knowledge/documents/{document_id}/unpublish/async",
    response_model=TaskSubmitResponse,
    status_code=202,
    summary="异步下架知识文档",
    description="提交异步文档下架任务。",
)
async def unpublish_document_async(
    document_id: str, request: DocumentUnpublishRequest
) -> TaskSubmitResponse:
    task_id = _create_task(operation="unpublish", document_id=document_id, status="QUEUED")
    _queue(
        _run_async_index(task_id, "unpublish", agent.unpublish_document(document_id, request)),
        task_id=task_id,
        operation="unpublish",
    )
    return TaskSubmitResponse(
        task_id=task_id,
        operation="unpublish",
        document_id=document_id,
        status_url=f"/api/v1/knowledge/tasks/{task_id}",
    )


# ===== 健康检查 =====


async def _run_readiness_probe(
    name: str,
    probe: Callable[[], Awaitable[None]],
) -> tuple[str, dict[str, str]]:
    try:
        with trace_span(
            "readiness_probe",
            {"dependency": name},
            agent_logger,
            level=logging.DEBUG,
            error_level=logging.DEBUG,
        ):
            await asyncio.wait_for(probe(), timeout=HEALTH_TIMEOUT_SECONDS)
        return name, {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - 健康端点需要汇总依赖异常
        error_message = str(exc) or type(exc).__name__
        timed_out = isinstance(exc, TimeoutError)
        agent_logger.warning(
            "readiness_probe_failed dependency=%s timeout_seconds=%s timed_out=%s "
            "error_type=%s error_module=%s error=%s",
            name,
            HEALTH_TIMEOUT_SECONDS,
            timed_out,
            type(exc).__name__,
            type(exc).__module__,
            error_message,
            exc_info=True,
            extra={
                "structured": {
                    "event": "readiness_probe_failed",
                    "dependency": name,
                    "timeout_seconds": HEALTH_TIMEOUT_SECONDS,
                    "timed_out": timed_out,
                    "error_type": type(exc).__name__,
                    "error_module": type(exc).__module__,
                    "error": error_message,
                    "traceparent": get_trace_context().traceparent,
                }
            },
        )
        return name, {
            "status": "unavailable",
            "error_type": type(exc).__name__,
            "error": error_message,
        }


async def readiness_checks() -> dict[str, dict[str, str]]:
    """检查 Retrieval 索引与检索所需的最小依赖。"""

    milvus_services = getattr(agent, "public_milvus", {}) or {
        "solution": agent.milvus,
    }
    milvus_probes = [
        _run_readiness_probe(f"milvus.{knowledge_type}", service.check_ready)
        for knowledge_type, service in milvus_services.items()
    ]
    milvus_probes.append(
        _run_readiness_probe(
            "milvus.private",
            getattr(agent, "private_milvus", agent.milvus).check_ready,
        )
    )
    checks = await asyncio.gather(
        *milvus_probes,
        _run_readiness_probe("embedding", agent.embedding.check_ready),
    )
    return dict(checks)


@app.get(
    "/api/v1/knowledge/tasks/{task_id}",
    summary="查询知识任务",
    description="查询知识索引和文档处理任务状态。",
)
async def get_index_task(task_id: str) -> dict[str, object]:
    """查询已持久化的索引任务状态。"""

    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="索引任务不存在")
    return task
