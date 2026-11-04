"""统一网关运行时依赖。

HTTP 路由只依赖这个容器，避免直接读取 main.py 中的可变全局变量。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from runtime.contracts import (
    AdminServiceContract,
    BusinessRecordSink,
    DraftStore,
    ExecutionService,
    FileWorkflow,
    GatewayAgentEndpoint,
    IdentityReader,
    LifecycleService,
    MemoryService,
    RetrievalAgentEndpoint,
    StorageService,
    TaskRunner,
    TaskStore,
    ToolAdapter,
)
from services.application.conversation_state import ConversationStateService


@dataclass
class GatewayRuntime:
    """统一保存网关装配后的 Agent、工作流和持久化服务。"""

    embedded_perception: GatewayAgentEndpoint
    embedded_retrieval: RetrievalAgentEndpoint
    tool_adapter: ToolAdapter | None = None
    conversation_service: ConversationStateService | None = None
    deerflow_runner: Any | None = None
    file_workflow: FileWorkflow | None = None
    task_runner: TaskRunner | None = None
    task_store: TaskStore | None = None
    sync_state_store: object | None = None
    execution_service: ExecutionService | None = None
    memory_service: MemoryService | None = None
    draft_store_service: DraftStore | None = None
    lifecycle_service: LifecycleService | None = None
    business_record_sink: BusinessRecordSink | None = None
    match_result_store: object | None = None
    user_identity_reader: IdentityReader | None = None
    admin_service: AdminServiceContract | None = None
    outbox_repository: object | None = None
    outbox_processor: object | None = None
    storage_service: StorageService | None = None
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    background_stop_events: dict[asyncio.Task[Any], threading.Event] = field(default_factory=dict)

    def track_background_thread(
        self, task: asyncio.Task[Any], stop_event: threading.Event
    ) -> None:
        self.background_tasks.add(task)
        self.background_stop_events[task] = stop_event

        def discard(done: asyncio.Task[Any]) -> None:
            self.background_tasks.discard(done)
            self.background_stop_events.pop(done, None)

        task.add_done_callback(discard)

    def require_file_workflow(self) -> FileWorkflow:
        if self.file_workflow is None:
            raise HTTPException(status_code=503, detail="网关尚未完成 Checkpointer 初始化")
        return self.file_workflow

    def require_conversation(self) -> ConversationStateService:
        if self.conversation_service is None:
            raise HTTPException(status_code=503, detail="会话状态服务尚未就绪")
        return self.conversation_service

    def require_execution(self) -> ExecutionService:
        if self.execution_service is None:
            raise HTTPException(status_code=503, detail="执行服务尚未就绪")
        return self.execution_service

    def require_memory(self) -> MemoryService:
        if self.memory_service is None:
            raise HTTPException(status_code=503, detail="长期记忆 Store 未启用，请配置 PostgreSQL")
        return self.memory_service

    def require_draft_store(self) -> DraftStore:
        if self.draft_store_service is None:
            raise HTTPException(status_code=503, detail="草稿仓储尚未就绪")
        return self.draft_store_service

    def require_lifecycle(self) -> LifecycleService:
        if self.lifecycle_service is None:
            raise HTTPException(status_code=503, detail="文档生命周期仓储尚未就绪")
        return self.lifecycle_service

    def require_business_record_sink(self) -> BusinessRecordSink:
        if self.business_record_sink is None:
            raise HTTPException(status_code=503, detail="真实业务系统写入适配器尚未接入")
        return self.business_record_sink

    def require_user_identity_reader(self) -> IdentityReader:
        if self.user_identity_reader is None:
            raise HTTPException(status_code=503, detail="MySQL 用户身份读取器尚未就绪")
        return self.user_identity_reader

    def require_admin(self) -> AdminServiceContract:
        if self.admin_service is None:
            raise HTTPException(status_code=503, detail="管理员服务尚未就绪")
        return self.admin_service
