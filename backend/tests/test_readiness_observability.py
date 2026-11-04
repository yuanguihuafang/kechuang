import asyncio
import logging

from services.observability import system
from services.observability.logging import trace_span
from services.observability.trace_context import (
    bind_trace_context,
    get_trace_context,
    reset_trace_context,
)


def test_trace_span_isolated_across_concurrent_tasks():
    token = bind_trace_context("trace-readiness")

    async def run_probe(name: str) -> bool:
        with trace_span("readiness_probe", {"dependency": name}):
            span_id = get_trace_context().current_span
            await asyncio.sleep(0)
            return get_trace_context().current_span == span_id

    async def run_probes():
        return await asyncio.gather(run_probe("minio"), run_probe("embedding"))

    try:
        assert all(asyncio.run(run_probes()))
    finally:
        reset_trace_context(token)


def test_trace_span_end_keeps_its_own_traceparent(caplog):
    token = bind_trace_context("a" * 32)
    logger = logging.getLogger("test.readiness.trace")
    caplog.set_level(logging.INFO, logger=logger.name)

    try:
        with trace_span("readiness_probe", {"dependency": "minio"}, logger):
            span_id = get_trace_context().current_span
    finally:
        reset_trace_context(token)

    end_record = next(
        record for record in caplog.records
        if getattr(record, "structured", {}).get("event") == "span_end"
    )
    assert end_record.structured["span_id"] == span_id
    assert end_record.structured["traceparent"].endswith(f"-{span_id}-01")


def test_collect_health_caches_checks_and_logs_summary(monkeypatch, caplog):
    calls = {"perception": 0, "retrieval": 0}

    class FakeModule:
        def __init__(self, name: str):
            self.name = name

        async def readiness_checks(self):
            calls[self.name] += 1
            return {self.name: {"status": "ok"}}

    monkeypatch.setattr(system, "use_postgres", lambda: None)
    monkeypatch.setattr(system, "check_postgres", lambda: None)
    caplog.set_level(logging.INFO)
    perception = FakeModule("perception")
    retrieval = FakeModule("retrieval")

    async def run_checks():
        first, _ = await asyncio.gather(
            system.collect_health(perception=perception, retrieval=retrieval),
            system.collect_health(perception=perception, retrieval=retrieval),
        )
        first["services"]["perception"]["checks"]["mutated"] = {"status": "bad"}
        second = await system.collect_health(perception=perception, retrieval=retrieval)
        return second

    result = asyncio.run(run_checks())

    assert calls == {"perception": 1, "retrieval": 1}
    assert "mutated" not in result["services"]["perception"]["checks"]
    summaries = [
        record for record in caplog.records
        if getattr(record, "structured", {}).get("event") == "readiness_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0].structured["status"] == "ok"
