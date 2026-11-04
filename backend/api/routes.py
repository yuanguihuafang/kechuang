"""统一网关的基础运维路由。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Query

from services.observability.system import (
    collect_health,
    collect_metrics,
    collect_operational_alerts,
    collect_persistent_metrics,
)


def build_core_router(
    *,
    perception: Any,
    retrieval: Any,
    supervisor_metrics: Callable[[], object],
    persistent_metrics: Callable[[], object] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["系统监控"])

    @router.get("/api/v1/health", summary="健康检查", description="检查网关及核心服务是否可用。")
    async def health() -> dict[str, object]:
        return await collect_health(perception=perception, retrieval=retrieval)

    @router.get("/api/v1/metrics", summary="获取运行指标", description="返回网关、Agent 和持久化服务的运行指标。")
    async def metrics() -> dict[str, object]:
        return collect_metrics(
            perception=perception,
            retrieval=retrieval,
            supervisor_metrics=supervisor_metrics,
            persistent_metrics=persistent_metrics,
        )

    @router.get("/api/v1/metrics/persistent", summary="获取持久化指标", description="返回指定时间窗口内的持久化指标和告警。")
    async def persistent_metrics_endpoint(
        metric_name: str | None = Query(default=None, max_length=64),
        window_hours: int = Query(default=24, ge=1, le=720),
        error_rate_threshold: float = Query(default=0.2, ge=0, le=1),
    ) -> dict[str, object]:
        snapshot = collect_persistent_metrics(
            metric_name=metric_name, window_hours=window_hours
        )
        alerts = collect_operational_alerts(
            {"services": {}}, snapshot, error_rate_threshold=error_rate_threshold,
        )
        return {**snapshot, "alerts": alerts}

    return router
