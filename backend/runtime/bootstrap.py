"""网关组合根：加载 Agent 并初始化运行时服务。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

from admin import AdminService
from services.application.document_lifecycle import (
    DocumentLifecycleApplicationService,
    cleanup_document_version,
)
from runtime.config import get_settings
from runtime.container import GatewayRuntime
from assistant.integrations.deerflow_gateway import DeerFlowGatewayRunner
from runtime.embedded_agents import (
    configure_task_runner,
    configure_task_store,
    load_embedded_agents,
)
from runtime.tool_adapters import InProcessToolAdapter
from runtime.workflows.file_workflow import FileWorkflowService
from runtime.workflows.task_runner import SupervisorTaskRunner
from runtime.workflows.task_store import SupervisorTaskStore
from services.application.conversation_state import ConversationStateService
from services.application.execution_service import ExecutionService, InMemoryApprovalStore
from services.application.temporary_materials import (
    cleanup_temporary_materials,
)
from services.mysql import MySQLBusinessRecordSink, MySQLUserIdentityReader, mysql_identity_enabled
from services.persistence.checkpoint import PersistentCheckpointer
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.persistence.outbox_worker import OutboxProcessor
from services.persistence.postgres import check_schema_version, close_postgres_pools
from services.persistence.repositories.match_results import PostgresMatchResultStore
from services.persistence.repositories.observability_repository import PostgresAuditSink
from services.persistence.repositories.outbox_repository import OutboxRepository
from services.persistence.repositories.runtime_evidence import PostgresRuntimeEvidenceStore
from services.persistence.repositories.runtime_repository import (
    PostgresMysqlSyncStateStore,
    PostgresSessionStore,
)
from services.persistence.repositories.workflow_repository import PostgresDraftStore
from services.persistence.retention import (
    run_retention_loop,
)
from services.storage.minio.minio_storage import MinioUploadService
from services.web_search import TavilySearchClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
# 本地可用 .env.local 覆盖通用 .env；服务器只保留 .env 即可。
# 先加载 .env，再加载 .env.local，确保本地配置优先且无需手动切换文件。
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env.local", override=True)


def build_runtime() -> GatewayRuntime:
    """加载两个内嵌 Agent，返回尚未启动持久化服务的运行时容器。"""

    perception, retrieval = load_embedded_agents()
    return GatewayRuntime(perception, retrieval)


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """为 Windows 下的 psycopg 异步连接提供 Selector 事件循环。"""

    return asyncio.SelectorEventLoop()


async def _close_database_resources(persistent: PersistentCheckpointer | None) -> None:
    if persistent is not None:
        try:
            await persistent.close()
        except Exception:
            logger.exception("PostgreSQL Checkpointer 关闭失败")
    try:
        close_postgres_pools()
    except Exception:
        logger.exception("PostgreSQL 连接池关闭失败")


async def _stop_runtime(runtime: GatewayRuntime) -> None:
    tasks = list(runtime.background_tasks)
    for task in tasks:
        stop_event = runtime.background_stop_events.get(task)
        if stop_event is not None:
            stop_event.set()
        else:
            task.cancel()
    try:
        if runtime.task_runner is not None:
            await runtime.task_runner.stop()
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.get_running_loop().shutdown_default_executor()
        runtime.background_tasks.clear()
        runtime.background_stop_events.clear()
        runtime.conversation_service = None
        runtime.deerflow_runner = None
        runtime.tool_adapter = None
        runtime.file_workflow = None
        runtime.task_runner = None
        runtime.task_store = None
        runtime.sync_state_store = None
        runtime.execution_service = None
        runtime.memory_service = None
        runtime.draft_store_service = None
        runtime.lifecycle_service = None
        runtime.business_record_sink = None
        runtime.match_result_store = None
        runtime.user_identity_reader = None
        runtime.admin_service = None
        runtime.outbox_repository = None
        runtime.outbox_processor = None
        runtime.storage_service = None


def _close_retrieval(runtime: GatewayRuntime) -> None:
    try:
        runtime.embedded_retrieval.close()
    except Exception:
        logger.exception("检索 Agent 资源关闭失败")


@asynccontextmanager
async def lifespan(runtime: GatewayRuntime):
    """初始化并释放网关共享服务。

    所有进程内 Agent、任务 Runner、数据库仓储和外部存储适配器都在这里
    装配；请求路由不负责创建这些长生命周期依赖。
    """

    async with AsyncExitStack() as resources:
        resources.callback(_close_retrieval, runtime)
        resources.push_async_callback(_close_database_resources, None)
        check_schema_version()
        settings = get_settings()
        retrieval_agent = getattr(runtime.embedded_retrieval, "agent", None)
        initialize_aliases = getattr(
            retrieval_agent, "initialize_public_collection_aliases", None
        )
        if callable(initialize_aliases):
            await asyncio.to_thread(initialize_aliases)
        if settings.deerflow_enabled and settings.deerflow_base_url:
            runtime.deerflow_runner = DeerFlowGatewayRunner(
                settings.deerflow_base_url,
                api_key=settings.deerflow_api_key,
                internal_auth_token=settings.deerflow_internal_auth_token,
                agent_owner_user_id=settings.deerflow_agent_owner_user_id,
                internal_token_header=settings.deerflow_internal_token_header,
                owner_user_id_header=settings.deerflow_owner_user_id_header,
                timeout=settings.deerflow_timeout_seconds,
            )
        persistent = PersistentCheckpointer()
        resources.push_async_callback(persistent.close)
        resources.push_async_callback(_stop_runtime, runtime)
        checkpointer = await persistent.start()

        runtime.execution_service = ExecutionService(store=InMemoryApprovalStore())
        # 长期用户记忆暂不启用；保留仓储、接口和 Supervisor 接入点，后续评估确认流程后再装配。
        # 短期会话记忆仍由 LangGraph PostgreSQL Checkpointer 负责。
        runtime.memory_service = None
        runtime.draft_store_service = PostgresDraftStore()
        runtime.lifecycle_service = DocumentLifecycleStore()
        runtime.storage_service = MinioUploadService()
        runtime.embedded_perception.configure_storage_service(runtime.storage_service)
        # MySQL 允许业务读写，但本平台生命周期清理只针对 PostgreSQL 测试状态；
        # 不在启动和 retention 流程中执行 MySQL 清空或批量删除。
        runtime.user_identity_reader = (
            MySQLUserIdentityReader() if mysql_identity_enabled() else None
        )
        runtime.business_record_sink = MySQLBusinessRecordSink()
        runtime.match_result_store = PostgresMatchResultStore()
        runtime.task_store = SupervisorTaskStore()
        runtime.sync_state_store = PostgresMysqlSyncStateStore()
        runtime.outbox_repository = OutboxRepository()
        configure_task_store(
            (runtime.embedded_perception, runtime.embedded_retrieval), runtime.task_store
        )
        # 只回收真正过期的租约；未过期任务交给原 Worker，避免重启直接丢失可恢复任务。
        runtime.task_store.requeue_expired()
        adapter = InProcessToolAdapter(
            runtime.embedded_perception,
            runtime.embedded_retrieval,
            runtime.lifecycle_service,
            TavilySearchClient(),
        )
        runtime.tool_adapter = adapter
        session_store = PostgresSessionStore()
        runtime.conversation_service = ConversationStateService(session_store)
        runtime.admin_service = AdminService(
            runtime,
            session_store=session_store,
            supervisor_metrics=runtime.conversation_service.metrics.snapshot,
        )
        resume_task = asyncio.create_task(
            runtime.admin_service.resume_mysql_syncs(),
            name="kechuang-resume-mysql-syncs",
        )
        runtime.background_tasks.add(resume_task)
        resume_task.add_done_callback(runtime.background_tasks.discard)
        runtime.file_workflow = FileWorkflowService(
            adapter,
            runtime.task_store,
            checkpointer=checkpointer,
            lifecycle_store=runtime.lifecycle_service,
            draft_store=runtime.draft_store_service,
            outbox_repository=runtime.outbox_repository,
            storage_service=runtime.storage_service,
        )

        runtime.outbox_processor = OutboxProcessor(
            runtime.outbox_repository,
            {
                "VERSION_CLEANUP": lambda event: cleanup_document_version(
                    runtime.tool_adapter,
                    runtime.lifecycle_service,
                    dict(event.get("payload") or {}),
                ),
                "LIFECYCLE_RECOVERY": lambda event: DocumentLifecycleApplicationService(
                    runtime
                ).recover(event),
            },
        )
        runtime.task_runner = SupervisorTaskRunner(
            runtime.task_store,
            max_concurrency=get_settings().document_parse_concurrency,
        )
        runtime.task_runner.register(
            "parse", lambda task: runtime.file_workflow.parse(str(task["task_id"]))
        )
        runtime.task_runner.register(
            "index", lambda task: runtime.file_workflow.resume_confirmation(str(task["task_id"]))
        )
        runtime.task_runner.start()
        outbox_task = asyncio.create_task(
            runtime.outbox_processor.run(), name="kechuang-outbox-worker"
        )
        runtime.background_tasks.add(outbox_task)
        configure_task_runner(
            (runtime.embedded_perception, runtime.embedded_retrieval), runtime.task_runner
        )
        retention_stop = asyncio.Event()
        retention_task = asyncio.create_task(
            run_retention_loop(retention_stop, audit_sink=PostgresAuditSink()),
            name="kechuang-retention-loop",
        )
        runtime.background_tasks.add(retention_task)

        temporary_material_task = asyncio.create_task(
            cleanup_temporary_materials(runtime, retention_stop),
            name="kechuang-temporary-material-cleanup",
        )
        runtime.background_tasks.add(temporary_material_task)
        runtime_evidence_store = PostgresRuntimeEvidenceStore()
        runtime_session_id = runtime_evidence_store.start_session(
            version=os.getenv("APP_VERSION") or os.getenv("GIT_COMMIT"),
        )
        resources.callback(runtime_evidence_store.close_session, runtime_session_id)
        runtime_heartbeat_stop = asyncio.Event()

        async def record_runtime_heartbeats() -> None:
            sequence = 0
            while not runtime_heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(
                        runtime_heartbeat_stop.wait(),
                        timeout=30 * 60,
                    )
                except asyncio.TimeoutError:
                    sequence += 1
                    try:
                        runtime_evidence_store.record_heartbeat(
                            runtime_session_id,
                            sequence,
                            version=os.getenv("APP_VERSION") or os.getenv("GIT_COMMIT"),
                        )
                    except Exception:
                        logger.exception("运行时长心跳写入失败")

        runtime_heartbeat_task = asyncio.create_task(
            record_runtime_heartbeats(),
            name="kechuang-runtime-heartbeat",
        )
        runtime.background_tasks.add(runtime_heartbeat_task)

        yield runtime
