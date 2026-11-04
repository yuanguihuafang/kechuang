"""问答 HTTP 路由：只执行 DeerFlow，业务结果按统一 SSE 合同返回。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from assistant.integrations.deerflow_adapter import encode_sse_event
from assistant.api.v1.identity.dependencies import require_request_user_id
from assistant.api.v1.schemas import (
    ChatStreamRequest,
    RetrievalChunkDetailRequest,
    WeComContactRequest,
)
from core.governance.access_policy import ManualIdentity
from agents.retrieval_agent.retrieval.rerank_service import RerankUnavailableError
from core.governance.redaction import TextRedactionService
from runtime.container import GatewayRuntime
from assistant.application.chat_stream import deerflow_stream, is_natural_next_match_batch_request
from core.contracts.tools import ToolContext

logger = logging.getLogger(__name__)


def _require_stream_identity(
    request: Request, resolve_effective_identity: Any
) -> tuple[str, str, Any]:
    user_id = require_request_user_id(request)
    return resolve_effective_identity(user_id)


def build_supervisor_router(runtime: GatewayRuntime, resolve_effective_identity: Any) -> APIRouter:
    router = APIRouter(tags=["智能问答"])

    @router.post(
        "/api/v1/me/contact",
        summary="获取联系客服动作",
        description="为当前外部用户返回可由客户端呈现的联系客服动作。",
    )
    async def contact_wecom(payload: WeComContactRequest, request: Request) -> dict[str, object]:
        """返回客服动作契约；微信小程序端需将其渲染为原生 open-type="contact" 按钮。

        此接口只提供动作及上下文，不会由网页打开微信客服，也不创建人工工单。
        """

        _user_id, user_role, _profile = resolve_effective_identity(require_request_user_id(request))
        if user_role != "external":
            raise HTTPException(status_code=403, detail="当前账号不支持该客服入口")
        return {
            "status": "CONTACT_REQUIRED",
            "channel": "wecom",
            "message": "请使用当前客户端提供的‘联系客服’入口进入微信客服会话。",
            "action": {
                "id": "contact_wecom",
                "type": "CONTACT_WECOM",
                "label": "联系客服",
                "enabled": True,
                "query": payload.query,
                "document_ids": payload.document_ids,
            },
        }

    @router.post(
        "/api/v1/chat/stream",
        summary="流式智能问答",
        description="以 SSE 事件流返回统一的 start、progress、token、done、error 事件。",
    )
    async def chat_stream(payload: ChatStreamRequest, request: Request) -> StreamingResponse:
        resolved_user_id, _resolved_role, _profile = _require_stream_identity(
            request,
            resolve_effective_identity,
        )
        payload = payload.model_copy(update={"user_id": resolved_user_id})
        request_user_id = require_request_user_id(request)
        thread_id = payload.thread_id or f"session-{request_user_id}-{uuid.uuid4().hex[:12]}"

        if (
            payload.flow_action != "next_match_batch"
            and getattr(runtime, "deerflow_runner", None) is None
        ):
            if getattr(runtime, "conversation_service", None) is None:
                raise HTTPException(status_code=503, detail="问答运行服务尚未就绪")
            session_store = runtime.require_conversation().session_store
            session = session_store.get(thread_id) or {}
            flow = (session.get("state") or {}).get("chat_flow") or {}
            if not is_natural_next_match_batch_request(payload.message, flow):
                raise HTTPException(status_code=503, detail="问答运行服务尚未就绪")
        request_id = f"chat-{uuid.uuid4().hex[:12]}"


        return StreamingResponse(
            deerflow_stream(runtime, payload,
                            context=ToolContext(user_id=resolved_user_id, user_role=_resolved_role,
                                                thread_id=thread_id, trace_id=request.state.trace_id),
                            request_id=request_id, access_token=request.headers.get("Authorization", "")),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _sse(event: str, data: dict[str, object]) -> str:
        return encode_sse_event(event, data)

    @router.post(
        "/api/v1/knowledge/chunk-detail",
        summary="查看检索块详情",
        description="查看指定检索结果块的完整内容和来源信息。",
    )
    async def chunk_detail(
        payload: RetrievalChunkDetailRequest, request: Request
    ) -> dict[str, object]:
        """分步检索点击详情时，回查同一业务记录的草稿和附近原文。"""

        user_id, user_role, _profile = resolve_effective_identity(require_request_user_id(request))
        agent = getattr(runtime.embedded_retrieval, "agent", None)
        search_service = getattr(agent, "search_service", None)
        if search_service is None or not hasattr(search_service, "get_chunk_detail"):
            raise HTTPException(status_code=503, detail="检索详情服务尚未就绪")
        try:
            detail = await search_service.get_chunk_detail(
                document_id=payload.document_id,
                mysql_id=payload.mysql_id,
                source_id=payload.source_id,
                matched_draft_record_id=payload.matched_draft_record_id,
                chunk_role=payload.chunk_role,
                chunk_index=payload.chunk_index,
                knowledge_type=payload.knowledge_type,
                identity=ManualIdentity(user_id=user_id, user_role=user_role),
            )
        except RerankUnavailableError as exc:
            raise HTTPException(status_code=503, detail="关联草稿排序服务暂不可用，请稍后重试") from exc
        if detail is None:
            raise HTTPException(status_code=404, detail="未找到该检索块关联的完整记录")
        if (
            user_role == "external"
            and str(payload.knowledge_type or "").strip().lower() == "demand"
        ):
            detail = TextRedactionService().sanitize_demand_detail(detail)
        if not payload.include_summary:
            return detail
        return await search_service.summarize_chunk_detail(detail, payload.knowledge_type)

    return router
