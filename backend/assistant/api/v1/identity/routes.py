"""身份与个人资料接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from assistant.api.v1.schemas import (
    AuthIdentityResponse,
    AuthLoginRequest,
)
from runtime.config import get_settings
from runtime.container import GatewayRuntime

from services.application.identity import TestIdentityResolver
from .dependencies import issue_access_token, require_request_user_id


def build_identity_router(runtime: GatewayRuntime) -> APIRouter:
    router = APIRouter(tags=["身份与个人中心"])

    @router.post(
        "/api/v1/auth/login",
        response_model=AuthIdentityResponse,
        summary="用户登录",
        description="接收原系统登录后返回的 user_id，查询本地用户并签发智能体令牌。",
    )
    async def login(payload: AuthLoginRequest) -> AuthIdentityResponse:
        """原系统 access_token 由宿主小程序完成登录；本接口不保存原系统令牌。"""

        resolver = TestIdentityResolver(runtime.user_identity_reader)
        _user_id, role, profile = resolver.resolve_required(payload.user_id)
        user = profile.as_dict()
        user["user_role"] = role
        return AuthIdentityResponse(
            authenticated=True,
            mode="test",
            user=user,
            access_token=issue_access_token(_user_id),
            expires_in=get_settings().auth_token_ttl_seconds,
        )

    @router.get(
        "/api/v1/auth/me",
        response_model=AuthIdentityResponse,
        summary="获取当前身份",
        description="返回当前用户的身份和企业信息。",
    )
    async def current_identity(request: Request) -> AuthIdentityResponse:
        """当前身份只从 Bearer Token 解析，不读取查询参数。"""

        resolver = TestIdentityResolver(runtime.user_identity_reader)
        user_id = require_request_user_id(request)
        _user_id, role, profile = resolver.resolve_required(user_id)
        user = profile.as_dict()
        user["user_role"] = role
        return AuthIdentityResponse(authenticated=True, mode="test", user=user)

    @router.post("/api/v1/auth/logout", summary="用户退出登录", description="结束当前登录状态。")
    async def logout() -> dict[str, object]:
        """测试模式无服务端会话；正式模式保留为宿主项目退出登录的适配入口。"""

        return {"authenticated": False, "mode": "test"}

    @router.get(
        "/api/v1/identity/test-users",
        summary="获取测试用户",
        description="返回本地联调可用的测试用户列表。",
    )
    async def list_test_users(
        limit: int = Query(default=100, ge=1, le=200),
    ) -> list[dict[str, object]]:
        """本地联调用的用户下拉选项，不作为正式登录接口。"""

        return runtime.require_user_identity_reader().list_test_users(limit)

    @router.get(
        "/api/v1/identity/{user_id}",
        summary="查询用户身份",
        description="根据用户 ID 查询基础身份信息。",
    )
    async def resolve_user_identity(user_id: str) -> dict[str, object]:
        """读取公司用户表中的最小身份标记。"""

        identity = runtime.require_user_identity_reader().get(user_id)
        if identity is None:
            raise HTTPException(status_code=404, detail="用户不存在或 user_id 无效")
        return identity.as_dict()

    @router.get(
        "/api/v1/me/profile", summary="获取个人资料", description="返回个人中心展示所需的用户资料。"
    )
    async def my_profile(request: Request) -> dict[str, object]:
        """个人中心身份信息；用户身份只从 Bearer Token 获取。"""

        user_id = require_request_user_id(request)
        identity = runtime.require_user_identity_reader().get(user_id)
        if identity is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        return identity.as_dict()

    @router.get(
        "/api/v1/me/match-results",
        summary="获取匹配记录",
        description="只返回已经实际执行过的供需匹配记录。",
    )
    async def my_match_results(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, object]]:
        user_id = require_request_user_id(request)
        store = getattr(runtime, "match_result_store", None)
        return store.list(user_id=user_id, limit=limit) if store else []

    @router.get(
        "/api/v1/me/visit-records", summary="获取拜访记录", description="返回当前用户的拜访记录。"
    )
    async def my_visit_records(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, object]]:
        user_id = require_request_user_id(request)
        records = runtime.require_business_record_sink().list(
            user_id=user_id, record_type="customer_visit"
        )[:limit]
        draft_store = getattr(runtime, "draft_store_service", None)
        drafts = draft_store.list(user_id=user_id, limit=10000) if draft_store is not None else []
        drafts_by_mysql_id: dict[str, dict[str, object]] = {}
        drafts_by_thread_id: dict[str, dict[str, object]] = {}
        for item in drafts or []:
            if not isinstance(item, dict):
                continue
            payload = item.get("draft") if isinstance(item.get("draft"), dict) else {}
            draft_type = str(item.get("draft_type") or payload.get("draft_type") or "").strip()
            if draft_type != "customer_visit":
                continue
            mysql_id = str(
                item.get("mysql_record_id") or payload.get("mysql_record_id") or ""
            ).strip()
            thread_id = str(
                item.get("thread_id")
                or payload.get("thread_id")
                or payload.get("chat_session_id")
                or ""
            ).strip()
            if mysql_id:
                drafts_by_mysql_id.setdefault(mysql_id, item)
            if thread_id:
                drafts_by_thread_id.setdefault(thread_id, item)
        match_results_by_thread: dict[str, list[dict[str, object]]] = {}
        match_store = getattr(runtime, "match_result_store", None)
        if match_store is not None:
            for item in match_store.list(user_id=user_id, limit=200):
                if not isinstance(item, dict):
                    continue
                thread_id = str(item.get("session_id") or "").strip()
                result_payload = item.get("result") if isinstance(item.get("result"), dict) else {}
                result_items = (
                    result_payload.get("match_results")
                    if isinstance(result_payload.get("match_results"), list)
                    else []
                )
                if not result_items:
                    result_items = [
                        candidate
                        for block in result_payload.get("ui_blocks") or []
                        if isinstance(block, dict) and block.get("type") == "recommendation_list"
                        for candidate in (
                            (block.get("data") if isinstance(block.get("data"), dict) else {}).get(
                                "items"
                            )
                            or []
                        )
                        if isinstance(candidate, dict)
                    ]
                if thread_id and result_items:
                    match_results_by_thread.setdefault(thread_id, []).extend(result_items)
        session_store = getattr(
            getattr(runtime, "conversation_service", None), "session_store", None
        )
        result = []
        for record in records:
            mysql_record_id = str(record.get("business_record_id") or "").strip()
            structured_data = (
                record.get("structured_data")
                if isinstance(record.get("structured_data"), dict)
                else {}
            )
            mysql_thread_id = str(structured_data.get("chat_session_id") or "").strip()
            draft_record = (
                drafts_by_mysql_id.get(mysql_record_id)
                or drafts_by_thread_id.get(mysql_thread_id)
                or {}
            )
            draft_payload = (
                draft_record.get("draft") if isinstance(draft_record.get("draft"), dict) else {}
            )
            draft = {**structured_data, **draft_payload}
            thread_id = (
                str(
                    draft_record.get("thread_id")
                    or draft_payload.get("thread_id")
                    or mysql_thread_id
                    or ""
                ).strip()
                or None
            )
            attachments = (
                draft_payload.get("attachments")
                if isinstance(draft_payload.get("attachments"), list)
                else None
            ) or (
                structured_data.get("attachments")
                if isinstance(structured_data.get("attachments"), list)
                else []
            )
            if not attachments and session_store is not None and thread_id:
                session = session_store.get(thread_id) or {}
                state = session.get("state") if isinstance(session, dict) else {}
                attachments = (
                    state.get("attachments")
                    if isinstance(state, dict) and isinstance(state.get("attachments"), list)
                    else []
                )
            matches = (
                draft_payload.get("match_results")
                or draft_payload.get("demand_matches")
                or draft_payload.get("solution_evidences")
                or structured_data.get("match_results")
                or structured_data.get("demand_matches")
                or structured_data.get("solution_evidences")
                or match_results_by_thread.get(thread_id or "")
                or []
            )
            result.append(
                {
                    **record,
                    "record_id": draft_record.get("draft_id") or mysql_record_id,
                    "mysql_record_id": mysql_record_id or None,
                    "draft_id": draft_record.get("draft_id"),
                    "thread_id": thread_id,
                    "session_id": thread_id,
                    "draft": draft,
                    "attachments": attachments,
                    "match_results": matches if isinstance(matches, list) else [],
                }
            )
        return result

    @router.delete(
        "/api/v1/me/visit-records/{record_id}",
        summary="删除拜访记录",
        description="删除当前用户的一条拜访记录及其私有检索索引。",
    )
    async def delete_visit_record(record_id: str, request: Request) -> dict[str, object]:
        user_id = require_request_user_id(request)
        try:
            deleted = runtime.require_business_record_sink().delete(
                record_type="customer_visit",
                record_id=record_id,
                user_id=user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result = await runtime.embedded_retrieval.agent.delete_private_record_by_mysql_id(
            record_id, user_id
        )
        if result.status != "DELETED":
            raise HTTPException(status_code=502, detail=result.error or "私密拜访记录删除失败")
        return {"record": deleted, "index_result": result.model_dump()}

    return router
