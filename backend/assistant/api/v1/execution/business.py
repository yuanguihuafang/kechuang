"""临时业务记录测试接口。"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query

from core.contracts.business_records import ConfirmedBusinessRecordRequest
from runtime.container import GatewayRuntime
from services.application.business_records import (
    BusinessRecordApplicationService,
)


def build_business_router(runtime: GatewayRuntime) -> APIRouter:
    router = APIRouter(tags=["业务记录"])
    application = BusinessRecordApplicationService(runtime)

    @router.post("/api/v1/business/records", status_code=201, summary="保存业务记录", description="保存一条业务记录。")
    async def save_test_business_record(
        payload: ConfirmedBusinessRecordRequest,
    ) -> dict[str, object]:
        try:
            return runtime.require_business_record_sink().save(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v1/business/records/confirm", status_code=201, summary="确认业务记录", description="确认业务草稿并写入对应知识库。")
    async def confirm_business_record(payload: ConfirmedBusinessRecordRequest) -> dict[str, object]:
        """确认草稿后写入对应知识库；技术方案文档不写既有 MySQL 业务表。"""

        try:
            return await application.confirm(payload)
        except HTTPException:
            raise
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if os.getenv("ENABLE_TEST_ENDPOINTS", "").lower() == "true" or os.getenv("PYTEST_CURRENT_TEST"):
        router.add_api_route(
            "/api/v1/business/test-records",
            save_test_business_record,
            methods=["POST"],
            status_code=201,
            summary="保存测试业务记录",
            description="仅用于本地测试环境保存业务记录。",
        )

        @router.get("/api/v1/business/test-records", summary="获取测试业务记录", description="仅用于本地测试环境查询业务记录。")
        async def list_test_business_records(
            user_id: str = Query(min_length=1, max_length=128),
            record_type: str | None = Query(default=None, max_length=32),
        ) -> list[dict[str, object]]:
            return runtime.require_business_record_sink().list(
                user_id=user_id, record_type=record_type
            )

    return router
