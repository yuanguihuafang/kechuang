"""Supervisor 请求级耗时和错误指标。"""

from __future__ import annotations

import math
import statistics
import threading
from collections.abc import Callable
from typing import Any


class SupervisorMetrics:
    def __init__(self, persist: Callable[..., Any] | None = None) -> None:
        self._lock = threading.Lock()
        self._durations: list[float] = []
        self._counts: dict[str, int] = {}
        self._persist = persist

    def observe(self, *, status: str, duration_ms: float,
                trace_id: str | None = None) -> None:
        with self._lock:
            self._durations.append(round(duration_ms, 3))
            del self._durations[:-1000]
            self._counts[status] = self._counts.get(status, 0) + 1
        if self._persist is not None:
            try:
                self._persist(status=status, duration_ms=duration_ms, trace_id=trace_id)
            except Exception:  # noqa: BLE001 - 指标失败不能阻断业务
                return

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            durations = list(self._durations)
            counts = dict(self._counts)
        if not durations:
            return {"count": 0, "status_counts": counts, "error_count": counts.get("FAILED", 0)}
        ordered = sorted(durations)
        p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
        return {
            "count": len(durations),
            "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": ordered[p95_index],
            "last_ms": durations[-1],
            "status_counts": counts,
            "error_count": counts.get("FAILED", 0),
        }
