"""运行时长证据的计算与 PostgreSQL 持久化。"""

from __future__ import annotations

import csv
import io
import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import select

from ..postgres import _iso, _now
from ..sqlalchemy import (
    get_engine,
    runtime_evidence_config,
    runtime_heartbeats,
    runtime_sessions,
)

RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 30 * 60
RUNTIME_HEARTBEAT_GRACE_SECONDS = 60


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _seconds(value: timedelta) -> float:
    return max(0.0, value.total_seconds())


def calculate_runtime_evidence(
    *,
    system_start_at: datetime | str | None,
    sessions: list[Mapping[str, Any]],
    heartbeats: list[Mapping[str, Any]],
    now: datetime | str | None = None,
    heartbeat_interval_seconds: int = RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """按心跳证据计算有效时长；未被心跳覆盖的时间不计入。"""

    baseline = _datetime(system_start_at)
    current = _datetime(now) or _now()
    interval = max(1, int(heartbeat_interval_seconds))
    allowed_gap = interval + RUNTIME_HEARTBEAT_GRACE_SECONDS
    if baseline is None:
        return {
            "system_start_at": None,
            "valid_runtime_seconds": 0,
            "valid_runtime_hours": 0,
            "latest_heartbeat": None,
            "abnormal_interruptions": [],
            "sessions": [],
            "generated_at": _iso(current),
        }

    grouped: dict[str, list[datetime]] = {}
    for heartbeat in heartbeats:
        observed_at = _datetime(heartbeat.get("observed_at"))
        session_id = str(heartbeat.get("session_id") or "")
        if observed_at is not None and session_id:
            grouped.setdefault(session_id, []).append(observed_at)

    valid_seconds = 0.0
    latest: datetime | None = None
    interruptions: list[dict[str, Any]] = []
    session_items: list[dict[str, Any]] = []
    for session in sorted(sessions, key=lambda item: _datetime(item.get("started_at")) or current):
        session_id = str(session.get("session_id") or "")
        started_at = _datetime(session.get("started_at"))
        ended_at = _datetime(session.get("ended_at"))
        if not session_id or started_at is None or started_at > current:
            continue
        session_end = min(ended_at or current, current)
        session_items.append({
            "session_id": session_id,
            "instance_id": str(session.get("instance_id") or ""),
            "started_at": _iso(started_at),
            "ended_at": _iso(ended_at) if ended_at else None,
            "status": str(session.get("status") or "RUNNING"),
            "end_reason": session.get("end_reason"),
        })
        if session_end <= baseline:
            continue
        window_start = max(started_at, baseline)
        observed = sorted(item for item in grouped.get(session_id, []) if window_start <= item <= session_end)
        if observed:
            latest = max(latest, observed[-1]) if latest else observed[-1]
            valid_seconds += min(_seconds(observed[0] - window_start), interval)
            for previous, current_heartbeat in zip(observed, observed[1:]):
                gap = _seconds(current_heartbeat - previous)
                valid_seconds += min(gap, interval)
                if gap > allowed_gap:
                    interruptions.append({
                        "session_id": session_id,
                        "from": _iso(previous),
                        "to": _iso(current_heartbeat),
                        "duration_seconds": round(gap, 3),
                        "reason": "HEARTBEAT_MISSED",
                        "reason_text": "两次心跳间隔超过允许阈值，可能是服务阻塞或心跳写入失败。",
                    })
            tail = _seconds(session_end - observed[-1])
            if str(session.get("end_reason") or "") == "MANUAL_STOP":
                valid_seconds += min(tail, interval)
            elif tail > allowed_gap:
                interruptions.append({
                    "session_id": session_id,
                    "from": _iso(observed[-1]),
                    "to": _iso(session_end),
                    "duration_seconds": round(tail, 3),
                    "reason": "PROCESS_TERMINATED" if str(session.get("end_reason") or "") == "PROCESS_TERMINATED" else "HEARTBEAT_MISSED",
                    "reason_text": "服务进程异常结束，最后一次心跳后未再收到新的心跳。" if str(session.get("end_reason") or "") == "PROCESS_TERMINATED" else "最后一次心跳后超过允许阈值未收到新的心跳。",
                })
        elif str(session.get("end_reason") or "") != "MANUAL_STOP":
            gap = _seconds(session_end - window_start)
            if gap > allowed_gap:
                interruptions.append({
                    "session_id": session_id,
                    "from": _iso(window_start),
                    "to": _iso(session_end),
                    "duration_seconds": round(gap, 3),
                    "reason": "PROCESS_TERMINATED" if str(session.get("end_reason") or "") == "PROCESS_TERMINATED" else "HEARTBEAT_MISSED",
                    "reason_text": "服务进程异常结束，未记录到有效心跳。" if str(session.get("end_reason") or "") == "PROCESS_TERMINATED" else "运行期间超过允许阈值未记录到有效心跳。",
                })

    return {
        "system_start_at": _iso(baseline),
        "valid_runtime_seconds": round(valid_seconds, 3),
        "valid_runtime_hours": round(valid_seconds / 3600, 4),
        "latest_heartbeat": _iso(latest) if latest else None,
        "abnormal_interruptions": interruptions,
        "sessions": session_items,
        "generated_at": _iso(current),
    }


class PostgresRuntimeEvidenceStore:
    """记录服务会话和心跳，并导出可核验的运行时长证据。"""

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn and engine is None:
            raise ValueError("DATABASE_URL 未配置")
        self.engine = engine or get_engine(self.dsn)

    def start_session(self, *, version: str | None = None) -> str:
        session_id = f"runtime_{uuid.uuid4().hex}"
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(runtime_sessions.update().where(
                runtime_sessions.c.ended_at.is_(None),
            ).values(
                ended_at=now, status="ABNORMAL", end_reason="PROCESS_TERMINATED",
            ))
            conn.execute(runtime_sessions.insert().values(
                session_id=session_id, instance_id=socket.gethostname(),
                started_at=now, ended_at=None, status="RUNNING", end_reason=None,
                version=version, host=socket.gethostname(), pid=os.getpid(),
            ))
            conn.execute(runtime_heartbeats.insert().values(
                session_id=session_id, sequence=0, observed_at=now,
                status="HEALTHY", version=version,
            ))
        return session_id

    def record_heartbeat(self, session_id: str, sequence: int, *, version: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(runtime_heartbeats.insert().values(
                session_id=session_id, sequence=max(1, int(sequence)), observed_at=_now(),
                status="HEALTHY", version=version,
            ))

    def close_session(self, session_id: str, *, reason: str = "MANUAL_STOP") -> None:
        with self.engine.begin() as conn:
            conn.execute(runtime_sessions.update().where(
                runtime_sessions.c.session_id == session_id,
                runtime_sessions.c.ended_at.is_(None),
            ).values(ended_at=_now(), status="STOPPED", end_reason=reason))

    def set_system_start(self) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as conn:
            current = conn.execute(select(runtime_evidence_config).where(
                runtime_evidence_config.c.config_id == 1,
            )).mappings().first()
            if current and current["system_start_at"] is not None:
                raise ValueError("系统开始时间已经设置")
            values = {"config_id": 1, "system_start_at": now, "updated_at": now}
            if current:
                conn.execute(runtime_evidence_config.update().where(
                    runtime_evidence_config.c.config_id == 1,
                ).values(**values))
            else:
                conn.execute(runtime_evidence_config.insert().values(**values))
        return self.snapshot()

    def reset_system_start(self) -> dict[str, Any]:
        """从当前时间开始新的统计周期，不删除历史会话和心跳。"""
        now = _now()
        with self.engine.begin() as conn:
            current = conn.execute(select(runtime_evidence_config).where(
                runtime_evidence_config.c.config_id == 1,
            )).mappings().first()
            values = {"config_id": 1, "system_start_at": now, "updated_at": now}
            if current:
                conn.execute(runtime_evidence_config.update().where(
                    runtime_evidence_config.c.config_id == 1,
                ).values(**values))
            else:
                conn.execute(runtime_evidence_config.insert().values(**values))
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            config = conn.execute(select(runtime_evidence_config).where(
                runtime_evidence_config.c.config_id == 1,
            )).mappings().first()
            sessions = conn.execute(select(runtime_sessions)).mappings().all()
            heartbeats = conn.execute(select(runtime_heartbeats)).mappings().all()
        return calculate_runtime_evidence(
            system_start_at=config["system_start_at"] if config else None,
            sessions=[dict(item) for item in sessions],
            heartbeats=[dict(item) for item in heartbeats],
        )

    def export(self, format_name: str) -> tuple[bytes, str, str]:
        snapshot = self.snapshot()
        if format_name == "json":
            return json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"), "application/json", "runtime-evidence.json"
        if format_name != "csv":
            raise ValueError("仅支持 json 或 csv")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["type", "session_id", "started_at", "ended_at", "observed_at", "status", "end_reason", "reason_text", "duration_seconds"])
        writer.writerow(["summary", "", "", "", snapshot["latest_heartbeat"] or "", "", "", "", snapshot["valid_runtime_seconds"]])
        for session in snapshot["sessions"]:
            writer.writerow(["session", session["session_id"], session["started_at"], session["ended_at"] or "", "", session["status"], session["end_reason"] or "", "", ""])
        for item in snapshot["abnormal_interruptions"]:
            writer.writerow(["abnormal_interruption", item["session_id"], item["from"], item["to"], "", "", item["reason"], item["reason_text"], item["duration_seconds"]])
        return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "runtime-evidence.csv"
