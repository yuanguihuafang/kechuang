"""会话材料路由：上传解析资料并返回可编辑草稿。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from assistant.api.v1.identity.dependencies import require_request_user_id
from runtime.container import GatewayRuntime

logger = logging.getLogger(__name__)
from assistant.application import materials
from assistant.application.materials import _material_token_event

def build_chat_router(runtime: GatewayRuntime, resolve_effective_identity: Any) -> APIRouter:
    router = APIRouter(tags=["会话附件"])

    @router.post(
        "/api/v1/chat/materials/prepare",
        summary="确认新增附件归属",
        description="确认单个新增附件所属的业务类型和用户范围。",
    )
    async def prepare_chat_materials(
        request: Request,
        count: int = Form(..., ge=1, le=1),
        thread_id: str | None = Form(default=None),
        intent_hint: str | None = Form(default=None),
    ) -> dict[str, object]:
        """单个文件已选中但尚未解析时，返回当前草稿的归属确认。"""
        resolved_user_id, resolved_role, _ = resolve_effective_identity(
            require_request_user_id(request)
        )
        # 首次选择方向前也先固定会话 ID，后续真正解析时沿用它。
        session_id = str(thread_id or f"session-{resolved_user_id}-{uuid.uuid4().hex[:12]}")
        flow_state = {}
        if session_id and runtime.require_conversation().session_store is not None:
            session = runtime.require_conversation().session_store.get(session_id) or {}
            flow_state = (session.get("state") or {}).get("chat_flow") or {}
        saved_intent = str((flow_state.get("last_intent") or {}).get("name") or "").strip().lower()
        if not intent_hint and not saved_intent:
            if str(resolved_role or "").lower() in {"internal", "admin"}:
                return {
                    "status": "UPLOAD_INTENT_REQUIRED",
                    "user_id": resolved_user_id,
                    "thread_id": session_id,
                    "answer": "已收到你上传的资料，请选择要处理的方向。选择后系统会按该方向解析文档。",
                    "intent": {"name": "upload_intent_required", "confirmation_required": True},
                    "actions": [
                        {
                            "id": "upload_as_enterprise",
                            "type": "SELECT_UPLOAD_INTENT",
                            "value": "enterprise_profile",
                            "label": "企业画像",
                            "enabled": True,
                        },
                        {
                            "id": "upload_as_demand_discovery",
                            "type": "SELECT_UPLOAD_INTENT",
                            "value": "demand_discovery",
                            "label": "需求发现",
                            "enabled": True,
                        },
                        {
                            "id": "upload_for_matching",
                            "type": "SELECT_MATCH_INTENT",
                            "value": "supply_demand_match",
                            "label": "供需匹配",
                            "enabled": True,
                        },
                        {
                            "id": "upload_as_customer_visit",
                            "type": "SELECT_UPLOAD_INTENT",
                            "value": "customer_visit",
                            "label": "客户拜访",
                            "enabled": True,
                        },
                    ],
                }
            return {
                "status": "UPLOAD_INTENT_REQUIRED",
                "user_id": resolved_user_id,
                "thread_id": session_id,
                "answer": (
                    "已收到你上传的资料，但目前还没有确定处理意图，所以暂未开始解析。"
                    "你可以直接在输入框说明希望如何处理：整理成需求草稿、技术方案草稿或科研成果草稿；"
                    "也可以说明要进行供需匹配，并补充你是有需求还是有方案/成果。"
                    "下方按钮只是快捷选择，不选择按钮也可以直接输入处理意图。"
                ),
                "intent": {"name": "upload_intent_required", "confirmation_required": True},
                "actions": [
                    {
                        "id": "upload_as_demand",
                        "type": "SELECT_UPLOAD_INTENT",
                        "value": "demand",
                        "label": "整理成需求",
                        "enabled": True,
                    },
                    {
                        "id": "upload_as_solution",
                        "type": "SELECT_UPLOAD_INTENT",
                        "value": "solution",
                        "label": "整理成技术方案",
                        "enabled": True,
                    },
                    {
                        "id": "upload_as_achievement",
                        "type": "SELECT_UPLOAD_INTENT",
                        "value": "achievement",
                        "label": "整理成科研成果",
                        "enabled": True,
                    },
                    {
                        "id": "upload_for_matching",
                        "type": "SELECT_MATCH_INTENT",
                        "value": "supply_demand_match",
                        "label": "供需匹配",
                        "enabled": True,
                    },
                ],
            }
        intent = str(intent_hint or saved_intent).lower()
        supply = intent in {"solution", "achievement"}
        kind = "草稿"
        published = (
            str(flow_state.get("last_status") or "").upper() == "PUBLISHED"
            or str((flow_state.get("active_draft") or {}).get("status") or "").upper()
            == "PUBLISHED"
        )
        actions = (
            [
                {
                    "id": "create_new_draft",
                    "type": "CREATE_NEW_DRAFT",
                    "label": "新建草稿",
                    "enabled": True,
                },
            ]
            if published
            else [
                {
                    "id": "supplement_current_draft",
                    "type": "SUPPLEMENT_CURRENT_DRAFT",
                    "label": "补充当前草稿",
                    "enabled": True,
                },
                {
                    "id": "create_new_draft",
                    "type": "CREATE_NEW_DRAFT",
                    "label": "新建草稿",
                    "enabled": True,
                },
            ]
        )
        return {
            "status": "MATERIAL_ATTACHMENTS_PENDING_CONFIRM",
            "user_id": resolved_user_id,
            "answer": f"当前草稿已发布，请创建新的{kind}。"
            if published
            else f"已收到 {count} 个文件。请问是补充当前草稿，还是创建新的{kind}？",
            "actions": actions,
            "intent": {"name": intent, "draft_type": "solution" if supply else "demand"},
        }

    @router.post(
        "/api/v1/chat/materials/parse",
        summary="上传并解析附件",
        description="可直接上传资料；已选择意图时生成对应草稿，未选择时由会话流程识别。",
    )
    async def chat_material_parse(
        request: Request,
        file: UploadFile = File(...),  # noqa: B008 - FastAPI 文件表单声明必须使用 File
        thread_id: str | None = Form(default=None),
        message: str = Form(default=""),
        intent_hint: str | None = Form(default=None),
        knowledge_type: str | None = Form(default=None),
        attachment_mode: str = Form(default="new"),
        debug: bool = Form(default=False),
    ) -> dict[str, object]:
        return await materials.parse_chat_material(
            runtime, resolve_effective_identity, state=request.state,
            user_id=require_request_user_id(request), authorization=request.headers.get("Authorization", ""),
            file=file, thread_id=thread_id, message=message, intent_hint=intent_hint,
            knowledge_type=knowledge_type, attachment_mode=attachment_mode, debug=debug,
        )

    @router.post(
        "/api/v1/chat/materials/parse/stream",
        summary="流式上传并解析附件",
        description="以 text/event-stream 返回标准 start/progress/token/done/error 事件。",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    async def chat_material_parse_stream(
        request: Request,
        file: UploadFile = File(...),  # noqa: B008
        thread_id: str | None = Form(default=None),
        message: str = Form(default=""),
        intent_hint: str | None = Form(default=None),
        knowledge_type: str | None = Form(default=None),
        attachment_mode: str = Form(default="new"),
        debug: bool = Form(default=False),
    ) -> StreamingResponse:
        resolved_user_id = require_request_user_id(request)

        async def stream():
            stream_started = time.perf_counter()
            file_name = file.filename or "未命名文件"
            stream_thread_id = str(thread_id or "").strip() or (
                f"session-{resolved_user_id}-{uuid.uuid4().hex[:12]}"
            )
            yield _material_sse("start", {"thread_id": stream_thread_id, "file_name": file_name})
            event_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            request.state.material_event_queue = event_queue
            request.state.material_parse_completed = False
            streamed_answer = False

            async def emit_token(phase: str, token: str) -> None:
                nonlocal streamed_answer
                if phase == "answer":
                    streamed_answer = True
                value = str(token or "")
                if value:
                    await event_queue.put(_material_token_event(phase, value))

            request.state.material_summary_token_callback = lambda token: emit_token(
                "document_summary", token
            )
            request.state.material_answer_token_callback = lambda token: emit_token("answer", token)
            task = asyncio.create_task(
                chat_material_parse(
                    request=request,
                    file=file,
                    thread_id=stream_thread_id,
                    message=message,
                    intent_hint=intent_hint,
                    knowledge_type=knowledge_type,
                    attachment_mode=attachment_mode,
                    debug=debug,
                )
            )
            try:
                while not task.done() or not event_queue.empty():
                    try:
                        event = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                        if event.get("event") == "token":
                            yield _material_sse(
                                "token",
                                _material_token_event(
                                    str(event.get("phase") or "answer"),
                                    str(event.get("text") or ""),
                                ),
                            )
                        else:
                            yield _material_sse("progress", _material_progress_payload(event))
                    except asyncio.TimeoutError:
                        if not task.done():
                            yield _material_sse(
                                "progress",
                                {
                                    "stage": "processing",
                                    "text": "正在处理，请稍候…",
                                    "status": "running",
                                },
                            )
                result = task.result()
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else "文件处理失败，请稍后重试。"
                stage = "processing" if request.state.material_parse_completed else "parse"
                public_message = (
                    detail
                    if exc.status_code < 500
                    else (
                        "文件解析已完成，后续处理失败，请稍后重试。"
                        if stage == "processing"
                        else "文件解析服务暂时不可用，请稍后重试。"
                    )
                )
                yield _material_sse(
                    "error",
                    {
                        "code": exc.status_code,
                        "stage": stage,
                        "message": public_message,
                    },
                )
                return
            except Exception:
                stage = "processing" if request.state.material_parse_completed else "parse"
                logger.exception(
                    "material parse stream failed trace_id=%s thread_id=%s stage=%s",
                    getattr(request.state, "trace_id", None),
                    stream_thread_id,
                    stage,
                )
                public_message = (
                    "文件解析已完成，后续处理失败，请稍后重试。"
                    if stage == "processing"
                    else "文件解析失败，请稍后重试。"
                )
                yield _material_sse("error", {"stage": stage, "message": public_message})
                return
            finally:
                try:
                    delattr(request.state, "material_event_queue")
                except AttributeError:
                    pass
                for callback_name in (
                    "material_summary_token_callback",
                    "material_answer_token_callback",
                    "material_parse_completed",
                ):
                    try:
                        delattr(request.state, callback_name)
                    except AttributeError:
                        pass
            if result.get("status") == "FAILED":
                # 失败结果通过 done 统一展示，不能再补发一遍 answer token。
                streamed_answer = True
            if not streamed_answer:
                answer = str(result.get("answer") or "")
                if answer:
                    yield _material_sse("token", _material_token_event("answer", answer))
            yield _material_sse(
                "progress", {"stage": "completed", "text": "文件处理完成。", "status": "completed"}
            )
            done_result = {key: value for key, value in result.items() if key != "events"}
            done_result["processing_elapsed_ms"] = round(
                (time.perf_counter() - stream_started) * 1000,
                2,
            )
            if done_result.get("status") == "FAILED":
                for key in ("answer", "error"):
                    done_result[key] = str(done_result.get(key) or "").removeprefix(
                        "本次请求未完成："
                    )
            yield _material_sse("done", {"result": done_result})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _material_sse(event: str, data: dict[str, object]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _material_progress_payload(event: dict[str, object]) -> dict[str, object]:
        event_name = str(event.get("event") or "processing")
        stage = {
            "document_summary": "generating",
            "retrieval_started": "retrieval",
            "retrieval_completed": "completed",
            "draft_generated": "draft",
            "completed": "completed",
        }.get(event_name, "processing")
        text = event.get("message") or event.get("text")
        if not text:
            text = {
                "file_received": "文件已接收。",
                "file_uploaded": "文件上传完成。",
                "file_parse_started": "正在解析文档，请稍候。",
                "text_cleaning_started": "正在清洗文档内容。",
                "file_parsed": "文档解析完成。",
                "text_cleaned": "文档内容处理完成。",
                "document_summary": "已提取文档内容。",
                "retrieval_started": "正在根据解析内容检索知识库…",
                "retrieval_completed": "知识库检索完成。",
                "draft_generated": "草稿生成完成。",
                "completed": "文件处理完成。",
            }.get(event_name, "正在处理，请稍候…")
        payload: dict[str, object] = {
            "stage": stage,
            "text": str(text),
            "status": event.get("status") or "running",
        }
        for key in (
            "summary",
            "query",
            "document_id",
            "file_name",
            "extraction_elapsed_ms",
            "retrieval_elapsed_ms",
        ):
            if key in event and event[key] is not None:
                payload[key] = event[key]
        return payload

    return router
