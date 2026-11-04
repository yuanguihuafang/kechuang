"""轻量进程内指标：阶段耗时、错误计数和 P50/P95。"""

from __future__ import annotations

import math
import os
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager


def _persist_metric(*, name: str, status: str, duration_ms: float,
                    error_code: str | None = None,
                    trace_id: str | None = None) -> None:
    if os.getenv("METRICS_PERSISTENCE_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return
    try:
        from services.persistence.repositories.observability_repository import PostgresMetricsStore

        if not os.getenv("DATABASE_URL"):
            return
        PostgresMetricsStore().record(
            metric_name=name,
            status=status,
            duration_ms=duration_ms,
            error_code=error_code,
            trace_id=trace_id,
        )
    except Exception:  # noqa: BLE001 - 指标写入失败不能阻断业务阶段
        return


def persist_supervisor_metric(*, status: str, duration_ms: float,
                              trace_id: str | None = None) -> None:
    """Supervisor 指标的 PostgreSQL 持久化适配器。"""

    _persist_metric(
        name="supervisor",
        status=status,
        duration_ms=duration_ms,
        error_code="SUPERVISOR_FAILED" if status == "FAILED" else None,
        trace_id=trace_id,
    )
    try:
        from services.persistence.repositories.observability_repository import PostgresMetricsStore
        if not os.getenv("DATABASE_URL"):
            return
        PostgresMetricsStore().record(
            metric_name="supervisor", status=status, duration_ms=duration_ms,
            error_code="SUPERVISOR_FAILED" if status == "FAILED" else None,
            trace_id=trace_id,
        )
    except Exception:  # noqa: BLE001 - 指标失败不能阻断业务
        # 指标链路不能影响解析、Embedding 或检索主链路。
        return


class MetricsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._durations: dict[str, list[float]] = defaultdict(list)
        self._errors: dict[str, int] = defaultdict(int)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        from services.observability.trace_context import get_trace_context

        trace_id = get_trace_context().trace_id or None
        started = time.perf_counter()
        failed = False
        error_code: str | None = None
        try:
            yield
        except Exception as exc:
            failed = True
            # 记录阶段和异常类型，便于治理页定位；不记录异常正文，避免泄露输入或密钥。
            error_code = f"{name.upper().replace('.', '_')}_{type(exc).__name__.upper()}"
            with self._lock:
                self._errors[name] += 1
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            with self._lock:
                values = self._durations[name]
                values.append(round(elapsed, 3))
                del values[:-1000]
            _persist_metric(
                name=name, status="FAILED" if failed else "SUCCESS",
                duration_ms=elapsed, error_code=error_code, trace_id=trace_id,
            )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            durations = {name: list(values) for name, values in self._durations.items()}
            errors = dict(self._errors)
        stages: dict[str, object] = {}
        for name, values in durations.items():
            if not values:
                continue
            stages[name] = {
                "count": len(values),
                "rate": len(values),
                "p50_ms": round(statistics.median(values), 3),
                "p95_ms": round(_percentile(values, 0.95), 3),
                "p99_ms": round(_percentile(values, 0.99), 3),
                "last_ms": values[-1],
                "error_count": errors.get(name, 0),
                "error_rate": round(errors.get(name, 0) / len(values), 4),
            }
        return {"stages": stages, "error_count": sum(errors.values())}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


metrics = MetricsStore()
