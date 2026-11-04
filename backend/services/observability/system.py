"""平台健康、指标和运行告警采集；供 API 与管理服务共同使用。"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections.abc import Callable
from types import ModuleType

from services.observability.logging import get_logger, log_event
from services.persistence.factory import use_postgres
from services.persistence.postgres import check_postgres

READINESS_CACHE_TTL_SECONDS = 5.0
_health_cache: dict[tuple[int, int], tuple[float, dict[str, object]]] = {}
_health_tasks: dict[tuple[int, int], asyncio.Task[dict[str, object]]] = {}
_health_cache_lock = threading.Lock()
_system_logger = get_logger("gateway", "system")


def collect_persistent_metrics(*, metric_name: str | None = None,
                               window_hours: int = 24) -> dict[str, object]:
    try:
        from services.persistence.repositories.observability_repository import PostgresMetricsStore
        return PostgresMetricsStore().snapshot(
            metric_name=metric_name, window_hours=window_hours
        )
    except Exception as exc:  # noqa: BLE001 - 指标不可用不阻断运维接口
        return {"status": "unavailable", "error": type(exc).__name__}


async def _collect_health_uncached(
    *, perception: ModuleType, retrieval: ModuleType,
) -> dict[str, object]:
    started_at = time.monotonic()
    results: dict[str, object] = {}
    try:
        use_postgres()
        await asyncio.wait_for(asyncio.to_thread(check_postgres), timeout=5)
        results["platform_state"] = {"status": "ok", "backend": "postgres"}
    except Exception as exc:  # noqa: BLE001 - 健康接口统一汇总状态库异常
        results["platform_state"] = {
            "status": "unavailable", "backend": "postgres", "error": str(exc)
        }
    for name, module in (("perception", perception), ("retrieval", retrieval)):
        try:
            checks = await module.readiness_checks()
            results[name] = {
                "status": (
                    "ok" if all(item.get("status") == "ok" for item in checks.values())
                    else "degraded"
                ),
                "http_status": 200,
                "checks": checks,
            }
        except Exception as exc:  # noqa: BLE001 - 健康接口统一汇总异常
            results[name] = {"status": "unavailable", "error": str(exc)}
    status = "ok" if all(item.get("status") == "ok" for item in results.values()) else "degraded"
    failed_dependencies: list[str] = []
    checked = 0
    for service_name, service in results.items():
        if not isinstance(service, dict):
            continue
        checks = service.get("checks")
        if isinstance(checks, dict):
            for dependency, check in checks.items():
                checked += 1
                if isinstance(check, dict) and check.get("status") != "ok":
                    failed_dependencies.append(f"{service_name}.{dependency}")
        elif service.get("status") != "ok":
            failed_dependencies.append(service_name)
    log_event(
        _system_logger,
        "readiness_summary",
        status=status,
        checked=checked,
        failed=failed_dependencies,
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )
    return {"status": status, "services": results}


async def collect_health(*, perception: ModuleType, retrieval: ModuleType) -> dict[str, object]:
    """返回短缓存的健康结果，避免管理台并发请求重复探测依赖。"""

    key = (id(perception), id(retrieval))
    now = time.monotonic()
    with _health_cache_lock:
        cached = _health_cache.get(key)
        if cached and now - cached[0] < READINESS_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])
        task = _health_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                _collect_health_uncached(perception=perception, retrieval=retrieval)
            )
            _health_tasks[key] = task
    try:
        result = await asyncio.shield(task)
        with _health_cache_lock:
            _health_cache[key] = (time.monotonic(), result)
        return copy.deepcopy(result)
    finally:
        with _health_cache_lock:
            if _health_tasks.get(key) is task:
                _health_tasks.pop(key, None)


def collect_metrics(
    *,
    perception: ModuleType,
    retrieval: ModuleType,
    supervisor_metrics: Callable[[], object],
    persistent_metrics: Callable[[], object] | None = None,
) -> dict[str, object]:
    def safe_snapshot(label: str, callback: Callable[[], object], fallback: object) -> object:
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001 - 指标接口不能阻断治理页面
            return {"status": "unavailable", "service": label, "error": type(exc).__name__, **(fallback if isinstance(fallback, dict) else {})}

    return {
        "services": {
            "perception": safe_snapshot("perception", perception.metrics.snapshot, {"stages": {}, "error_count": 0}),
            "retrieval": safe_snapshot("retrieval", retrieval.metrics.snapshot, {"stages": {}, "error_count": 0}),
            "supervisor": safe_snapshot("supervisor", supervisor_metrics, {"count": 0, "status_counts": {}, "error_count": 0}),
        },
        "persistent": safe_snapshot("persistent", persistent_metrics, {"metrics": {}}) if persistent_metrics else {"status": "disabled"},
    }


def collect_operational_alerts(
    health: dict[str, object], persistent: dict[str, object], *, error_rate_threshold: float = 0.2,
    minimum_sample_count: int = 5,
) -> list[dict[str, object]]:
    """只产生可行动的健康和错误率告警，不把少量测试样本当成故障。"""

    alerts: list[dict[str, object]] = []
    for service_name, service in (health.get("services") or {}).items():
        if isinstance(service, dict) and service.get("status") != "ok":
            alerts.append({
                "severity": "PAGE", "type": "DEPENDENCY_UNAVAILABLE", "service": service_name,
                "checks": service.get("checks") if isinstance(service, dict) else {},
                "runbook": "检查 /api/v1/health 的 checks、容器与配置。",
            })
    for name, item in (persistent.get("metrics") or {}).items():
        count = int(item.get("count") or 0)
        errors = int(item.get("error_count") or 0)
        if count >= minimum_sample_count and errors / count >= error_rate_threshold:
            alerts.append({
                "severity": "TICKET", "type": "ERROR_RATE_HIGH", "metric_name": name,
                "count": count, "error_count": errors, "error_rate": round(errors / count, 4),
                "threshold": error_rate_threshold,
                "runbook": "在审核治理中查看审计事件与失败任务，确认依赖或输入问题。",
            })
    return alerts
