from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine

import services.persistence.repositories.runtime_evidence as runtime_module
from services.persistence.sqlalchemy import metadata, runtime_evidence_config, runtime_heartbeats, runtime_sessions
from services.persistence.repositories.runtime_evidence import calculate_runtime_evidence
from services.persistence.repositories.runtime_evidence import PostgresRuntimeEvidenceStore


UTC = timezone.utc


def test_runtime_evidence_counts_heartbeat_windows_and_manual_tail():
    baseline = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    started = baseline
    ended = baseline + timedelta(hours=2, minutes=10)
    result = calculate_runtime_evidence(
        system_start_at=baseline,
        sessions=[{"session_id": "s1", "started_at": started, "ended_at": ended, "end_reason": "MANUAL_STOP"}],
        heartbeats=[
            {"session_id": "s1", "observed_at": started},
            {"session_id": "s1", "observed_at": baseline + timedelta(minutes=30)},
            {"session_id": "s1", "observed_at": baseline + timedelta(hours=1)},
            {"session_id": "s1", "observed_at": baseline + timedelta(hours=1, minutes=30)},
            {"session_id": "s1", "observed_at": baseline + timedelta(hours=2)},
        ],
        now=ended,
    )
    assert result["valid_runtime_seconds"] == 130 * 60
    assert result["abnormal_interruptions"] == []


def test_runtime_evidence_excludes_gap_and_reports_abnormal_interruption():
    baseline = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    result = calculate_runtime_evidence(
        system_start_at=baseline,
        sessions=[{"session_id": "s1", "started_at": baseline, "ended_at": baseline + timedelta(hours=2)}],
        heartbeats=[
            {"session_id": "s1", "observed_at": baseline},
            {"session_id": "s1", "observed_at": baseline + timedelta(hours=2)},
        ],
        now=baseline + timedelta(hours=2),
    )
    assert result["valid_runtime_seconds"] == 1800
    assert len(result["abnormal_interruptions"]) == 1
    assert result["abnormal_interruptions"][0]["reason"] == "HEARTBEAT_MISSED"
    assert result["abnormal_interruptions"][0]["reason_text"]


def test_runtime_evidence_ignores_small_heartbeat_jitter():
    baseline = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    result = calculate_runtime_evidence(
        system_start_at=baseline,
        sessions=[{"session_id": "s1", "started_at": baseline, "ended_at": baseline + timedelta(minutes=60), "end_reason": "MANUAL_STOP"}],
        heartbeats=[
            {"session_id": "s1", "observed_at": baseline},
            {"session_id": "s1", "observed_at": baseline + timedelta(seconds=1805)},
            {"session_id": "s1", "observed_at": baseline + timedelta(seconds=3605)},
        ],
        now=baseline + timedelta(minutes=60),
    )
    assert result["abnormal_interruptions"] == []


def test_runtime_evidence_ignores_pre_baseline_history():
    baseline = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    result = calculate_runtime_evidence(
        system_start_at=baseline,
        sessions=[{"session_id": "s1", "started_at": baseline - timedelta(hours=2), "ended_at": baseline + timedelta(hours=1), "end_reason": "MANUAL_STOP"}],
        heartbeats=[
            {"session_id": "s1", "observed_at": baseline - timedelta(hours=1)},
            {"session_id": "s1", "observed_at": baseline + timedelta(minutes=30)},
        ],
        now=baseline + timedelta(hours=1),
    )
    assert result["valid_runtime_seconds"] == 3600
    assert result["system_start_at"] == baseline.isoformat()


def test_runtime_evidence_without_baseline_is_empty():
    result = calculate_runtime_evidence(system_start_at=None, sessions=[], heartbeats=[])
    assert result["valid_runtime_hours"] == 0
    assert result["latest_heartbeat"] is None


def test_runtime_evidence_reset_starts_a_new_period_without_deleting_history(monkeypatch):
    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[runtime_evidence_config, runtime_sessions, runtime_heartbeats])
    first = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    second = first + timedelta(hours=2)
    values = iter([first, first, second, second])
    monkeypatch.setattr(runtime_module, "_now", lambda: next(values))

    store = PostgresRuntimeEvidenceStore(engine=engine)
    store.set_system_start()
    result = store.reset_system_start()

    assert result["system_start_at"] == second.isoformat()
    with engine.connect() as conn:
        assert conn.execute(runtime_evidence_config.select()).fetchone() is not None
