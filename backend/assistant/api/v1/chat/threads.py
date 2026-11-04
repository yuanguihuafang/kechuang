"""会话历史与线程管理接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from assistant.api.v1.identity.dependencies import require_request_user_id
from assistant.api.v1.schemas import (
    ChatSessionRenameRequest,
)
from runtime.container import GatewayRuntime


def build_threads_router(runtime: GatewayRuntime) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/v1/me/conversations",
        summary="获取会话列表",
        description="按用户返回历史会话，按更新时间倒序排列。",
    )
    async def my_conversations(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, object]:
        user_id = require_request_user_id(request)
        session_store = getattr(runtime.require_conversation(), "session_store", None)
        sessions = (
            session_store.list_sessions(user_id=user_id, limit=limit) if session_store else []
        )
        sessions = [{**item, "thread_id": item.get("session_id")} for item in sessions]
        for item in sessions:
            item.pop("session_id", None)
        return {"sessions": sessions, "total": len(sessions), "user_id": user_id}

    @router.get(
        "/api/v1/chat/sessions/{thread_id}/messages",
        summary="获取会话消息",
        description="返回指定会话的消息记录。",
    )
    async def chat_history(
        thread_id: str,
        request: Request,
        limit: int = Query(default=20, ge=1, le=20),
    ) -> list[dict[str, object]]:
        user_id = require_request_user_id(request)
        supervisor = runtime.require_conversation()
        if supervisor.session_store is None:
            return []
        messages = supervisor.session_store.list_messages(thread_id, user_id=user_id, limit=limit)
        return [
            {key: value for key, value in item.items() if key != "session_id"}
            | {"thread_id": item.get("session_id")}
            for item in messages
        ]

    @router.patch(
        "/api/v1/chat/sessions/{thread_id}",
        summary="修改会话名称",
        description="修改当前用户的会话名称。",
    )
    async def rename_chat_session(
        thread_id: str, payload: ChatSessionRenameRequest, request: Request
    ) -> dict[str, object]:
        user_id = require_request_user_id(request)
        supervisor = runtime.require_conversation()
        if supervisor.session_store is None:
            raise HTTPException(status_code=503, detail="会话存储未配置")
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="会话标题不能为空")
        try:
            session = supervisor.session_store.rename_session(
                thread_id,
                user_id=user_id,
                title=title,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"session": session}

    @router.delete(
        "/api/v1/chat/sessions/{thread_id}",
        summary="删除会话",
        description="删除当前用户的会话及其历史记录。",
    )
    async def delete_chat_session(thread_id: str, request: Request) -> dict[str, object]:
        user_id = require_request_user_id(request)
        supervisor = runtime.require_conversation()
        if supervisor.session_store is None:
            raise HTTPException(status_code=503, detail="会话存储未配置")
        try:
            draft_store = getattr(runtime, "draft_store_service", None)
            sink = getattr(runtime, "business_record_sink", None)
            deleted_mysql_ids: set[str] = set()
            if draft_store is not None and sink is not None:
                for item in draft_store.list(user_id=user_id, limit=10000):
                    if (
                        str(item.get("thread_id") or "") != thread_id
                        or item.get("status") == "DELETED"
                    ):
                        continue
                    draft = item.get("draft") or {}
                    draft_type = str(item.get("draft_type") or draft.get("draft_type") or "")
                    mysql_id = str(draft.get("mysql_record_id") or "").strip()
                    record_type = draft_type
                    if (
                        record_type in {"demand", "achievement", "solution", "customer_visit"}
                        and mysql_id
                        and mysql_id not in deleted_mysql_ids
                    ):
                        sink.delete(record_type=record_type, record_id=mysql_id, user_id=user_id)
                        deleted_mysql_ids.add(mysql_id)
            deleted = supervisor.session_store.delete_session(thread_id, user_id=user_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"deleted": True, "thread_id": thread_id}

    return router
