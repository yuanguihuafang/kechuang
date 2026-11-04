"""运行时 PostgreSQL 仓储：保存任务状态、会话元数据和会话消息。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text

from ..postgres import TASK_STATE_FIELDS, _iso, _now
from ..sqlalchemy import (
    get_engine,
    milvus_count_snapshots,
    mysql_sync_states,
    platform_session_messages,
    platform_sessions,
    platform_tasks,
)


def _session_state(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _sanitize_json(value: Any) -> Any:
    """清理 PostgreSQL JSONB 不接受的 NUL 字符，递归处理状态嵌套值。"""

    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_sanitize_json(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json(item) for item in value]
    return value


class PostgresStateStore:
    """持久化 Agent 和 Supervisor 任务状态。"""

    def __init__(self, *, service: str, dsn: str | None = None, engine: Any = None) -> None:
        self.service = service
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn and engine is None:
            raise ValueError("DATABASE_URL 未配置")
        self.engine = engine or get_engine(self.dsn)
        self._lock = threading.Lock()

    @staticmethod
    def _state_fields(fields: dict[str, Any]) -> dict[str, Any]:
        return {
            key: _sanitize_json(value)
            for key, value in fields.items()
            if key in TASK_STATE_FIELDS
            and key
            not in {
                "parsed_result",
                "upload_result",
                "index_result",
                "request_payload",
            }
        }

    def create(
        self,
        *,
        operation: str,
        document_id: str | None = None,
        status: str = "RUNNING",
        state: dict[str, Any] | None = None,
    ) -> str:
        task_id = f"task_{uuid.uuid4().hex}"
        now = _now()
        state_payload = self._state_fields(dict(state or {}))
        with self._lock, self.engine.begin() as conn:
            conn.execute(
                platform_tasks.insert().values(
                    task_id=task_id,
                    service=self.service,
                    operation=operation,
                    document_id=document_id,
                    status=status,
                    state=state_payload,
                    created_at=now,
                    updated_at=now,
                )
            )
        return task_id

    def update(
        self, task_id: str, *, connection: Any = None, **fields: Any
    ) -> dict[str, Any] | None:
        if not fields:
            return self.get(task_id)
        status = fields.pop("status", None)
        queue_fields = {
            key: fields.pop(key)
            for key in ("next_run_at", "lease_until", "worker_id")
            if key in fields
        }
        fields = self._state_fields(fields)
        updated_at = _now()

        def update_connection(conn: Any) -> None:
            row = conn.execute(
                platform_tasks.select()
                .with_only_columns(platform_tasks.c.state)
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                )
            ).first()
            if row is None:
                return
            state = dict(row.state or {})
            state.update(fields)
            values = {"state": state, "updated_at": updated_at}
            if status is not None:
                values["status"] = status
            if status is not None and status not in {"RUNNING", "PARSING", "INDEXING"}:
                queue_fields.setdefault("worker_id", None)
                queue_fields.setdefault("lease_until", None)
            values.update(queue_fields)
            conn.execute(
                platform_tasks.update()
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                )
                .values(**values)
            )

        if connection is not None:
            update_connection(connection)
        else:
            with self._lock, self.engine.begin() as conn:
                update_connection(conn)
        return self.get(task_id)

    def claim(
        self, task_id: str, *, worker_id: str, lease_seconds: int = 120
    ) -> dict[str, Any] | None:
        """使用行锁抢占队列任务，避免多 Worker 重复执行。"""
        now = _now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._lock, self.engine.begin() as conn:
            row = conn.execute(
                platform_tasks.select()
                .with_only_columns(
                    platform_tasks.c.state,
                )
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                    platform_tasks.c.status == "QUEUED",
                )
                .with_for_update()
            ).first()
            if row is None:
                return None
            state = dict(row.state or {})
            state.pop("worker_id", None)
            state.pop("lease_until", None)
            conn.execute(
                platform_tasks.update()
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                )
                .values(
                    status="RUNNING",
                    state=state,
                    lease_until=lease_until,
                    worker_id=worker_id,
                    updated_at=now,
                )
            )
        return self.get(task_id)

    def claim_next(
        self, *, worker_id: str, operations: set[str], lease_seconds: int = 120
    ) -> dict[str, Any] | None:
        """原子认领最早的可执行排队任务，支持多 Worker 并发消费。"""
        if not operations:
            return None
        now = _now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._lock, self.engine.begin() as conn:
            row = (
                conn.execute(
                    platform_tasks.select()
                    .where(
                        platform_tasks.c.service == self.service,
                        platform_tasks.c.status == "QUEUED",
                        platform_tasks.c.operation.in_(operations),
                        or_(
                            platform_tasks.c.next_run_at.is_(None),
                            platform_tasks.c.next_run_at <= now,
                        ),
                    )
                    .order_by(
                        platform_tasks.c.created_at.asc(),
                        platform_tasks.c.task_id.asc(),
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            state = dict(row["state"] or {})
            state.pop("worker_id", None)
            state.pop("lease_until", None)
            conn.execute(
                platform_tasks.update()
                .where(
                    platform_tasks.c.task_id == row["task_id"],
                    platform_tasks.c.service == self.service,
                    platform_tasks.c.status == "QUEUED",
                )
                .values(
                    status="RUNNING",
                    state=state,
                    lease_until=lease_until,
                    worker_id=worker_id,
                    updated_at=now,
                )
            )
        return self.get(str(row["task_id"]))

    def find_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        """通过统一 SQLAlchemy 连接查询幂等任务。"""
        query = (
            platform_tasks.select()
            .where(
                platform_tasks.c.service == self.service,
                platform_tasks.c.state["idempotency_key"].as_string() == idempotency_key,
            )
            .order_by(platform_tasks.c.created_at.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().first()
        if row is None:
            return None
        return self.get(row["task_id"])

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    platform_tasks.select().where(
                        platform_tasks.c.task_id == task_id,
                        platform_tasks.c.service == self.service,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        state = {
            key: value
            for key, value in (row["state"] or {}).items()
            if key not in {"worker_id", "lease_until", "next_run_at", "last_error"}
        }
        return {
            "task_id": row["task_id"],
            "operation": row["operation"],
            "document_id": row["document_id"],
            "status": row["status"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "worker_id": row["worker_id"],
            "lease_until": _iso(row["lease_until"]) if row["lease_until"] else None,
            "next_run_at": _iso(row["next_run_at"]) if row["next_run_at"] else None,
            **state,
        }

    def requeue_expired(self) -> int:
        """回收租约过期的运行阶段任务，允许其他 Worker 重新抢占。"""
        now = _now()
        with self._lock, self.engine.begin() as conn:
            result = conn.execute(
                platform_tasks.update()
                .where(
                    platform_tasks.c.service == self.service,
                    platform_tasks.c.status.in_({"RUNNING", "PARSING", "INDEXING"}),
                    platform_tasks.c.lease_until.is_not(None),
                    platform_tasks.c.lease_until < now,
                )
                .values(status="QUEUED", worker_id=None, lease_until=None, updated_at=now)
            )
        return int(result.rowcount or 0)

    def interrupt_claim(self, task_id: str, *, worker_id: str) -> bool:
        """仅中断当前 Worker 仍持有租约的活动任务。"""
        now = _now()
        active_statuses = {"RUNNING", "PARSING", "INDEXING"}
        with self._lock, self.engine.begin() as conn:
            row = conn.execute(
                platform_tasks.select()
                .with_only_columns(platform_tasks.c.state)
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                    platform_tasks.c.worker_id == worker_id,
                    platform_tasks.c.status.in_(active_statuses),
                )
                .with_for_update()
            ).first()
            if row is None:
                return False
            state = dict(row.state or {})
            state.update(
                status="INTERRUPTED",
                stage="interrupted",
                error_code="WORKER_SHUTDOWN",
                error="服务正在关闭，任务已中断；请从最近的可操作阶段重新提交",
            )
            result = conn.execute(
                platform_tasks.update()
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                    platform_tasks.c.worker_id == worker_id,
                    platform_tasks.c.status.in_(active_statuses),
                )
                .values(
                    status="INTERRUPTED",
                    state=state,
                    worker_id=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
        return bool(result.rowcount)

    def renew_lease(self, task_id: str, *, worker_id: str, lease_seconds: int = 120) -> bool:
        """仅允许当前 Worker 续租，避免旧 Worker 覆盖新租约。"""
        now = _now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._lock, self.engine.begin() as conn:
            result = conn.execute(
                platform_tasks.update()
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                    platform_tasks.c.status.in_({"RUNNING", "PARSING", "INDEXING"}),
                    platform_tasks.c.worker_id == worker_id,
                )
                .values(lease_until=lease_until, updated_at=now)
            )
        return bool(result.rowcount)

    def list(
        self, *, operation: str | None = None, document_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10000))
        query = platform_tasks.select().where(platform_tasks.c.service == self.service)
        if operation:
            query = query.where(platform_tasks.c.operation == operation)
        if document_id:
            query = query.where(platform_tasks.c.document_id == document_id)
        query = query.order_by(platform_tasks.c.updated_at.desc()).limit(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        return [
            {
                "task_id": row["task_id"],
                "operation": row["operation"],
                "document_id": row["document_id"],
                "status": row["status"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
                **{
                    key: value
                    for key, value in (row["state"] or {}).items()
                    if key not in {"worker_id", "lease_until", "next_run_at", "last_error"}
                },
            }
            for row in rows
        ]

    def list_page(
        self,
        *,
        operation: str | None = None,
        page: int = 1,
        page_size: int = 100,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 500))
        where = [platform_tasks.c.service == self.service]
        if operation:
            where.append(platform_tasks.c.operation == operation)
        if document_id:
            where.append(platform_tasks.c.document_id == document_id)
        with self.engine.connect() as conn:
            total = int(
                conn.execute(
                    select(func.count()).select_from(platform_tasks).where(*where)
                ).scalar_one()
            )
            rows = (
                conn.execute(
                    platform_tasks.select()
                    .where(*where)
                    .order_by(
                        platform_tasks.c.updated_at.desc(),
                        platform_tasks.c.created_at.desc(),
                        platform_tasks.c.task_id.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .mappings()
                .all()
            )
        items = [
            {
                "task_id": row["task_id"],
                "operation": row["operation"],
                "document_id": row["document_id"],
                "status": row["status"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
                **{
                    key: value
                    for key, value in (row["state"] or {}).items()
                    if key not in {"worker_id", "lease_until", "next_run_at", "last_error"}
                },
            }
            for row in rows
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def count_by_status(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(platform_tasks.c.status, func.count())
                .where(platform_tasks.c.service == self.service)
                .group_by(platform_tasks.c.status)
            ).all()
        return {str(status): int(count) for status, count in rows}

    def delete_record(self, task_id: str) -> int:
        with self.engine.begin() as conn:
            row = conn.execute(
                platform_tasks.select()
                .with_only_columns(
                    platform_tasks.c.document_id,
                    platform_tasks.c.status,
                    platform_tasks.c.state,
                )
                .where(
                    platform_tasks.c.task_id == task_id,
                    platform_tasks.c.service == self.service,
                )
            ).first()
            if row is None:
                return 0
            state = dict(row.state or {})
            lifecycle_action = str(state.get("lifecycle_action") or "").lower()
            is_purged = row.status == "PURGED" or (
                row.status == "SUCCEEDED" and lifecycle_action == "purge"
            )
            if not is_purged:
                return 0
            if row.document_id:
                result = conn.execute(
                    platform_tasks.delete().where(
                        platform_tasks.c.service == self.service,
                        platform_tasks.c.document_id == row.document_id,
                    )
                )
            else:
                result = conn.execute(
                    platform_tasks.delete().where(
                        platform_tasks.c.service == self.service,
                        platform_tasks.c.task_id == task_id,
                    )
                )
            return result.rowcount

    def interrupt_running(self) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                platform_tasks.select()
                .with_only_columns(
                    platform_tasks.c.task_id,
                    platform_tasks.c.state,
                )
                .where(
                    platform_tasks.c.service == self.service,
                    platform_tasks.c.status.in_(["RUNNING", "QUEUED"]),
                )
            ).all()
            for row in rows:
                state = dict(row.state or {})
                state.update(stage="interrupted", error="服务重启导致任务中断，请重新提交")
                conn.execute(
                    platform_tasks.update()
                    .where(
                        platform_tasks.c.task_id == row.task_id,
                        platform_tasks.c.service == self.service,
                    )
                    .values(status="INTERRUPTED", state=state, updated_at=_now())
                )


class PostgresMysqlSyncStateStore:
    """持久化 MySQL 到 Milvus 的独立同步断点。"""

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        self.engine = engine or get_engine(self.dsn)

    def get(self, knowledge_type: str, source_table: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    mysql_sync_states.select().where(
                        mysql_sync_states.c.knowledge_type == knowledge_type,
                        mysql_sync_states.c.source_table == source_table,
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def save(
        self,
        knowledge_type: str,
        source_table: str,
        *,
        last_mysql_id: int,
        processed_count: int,
        indexed_count: int,
        status: str,
    ) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as conn:
            current = (
                conn.execute(
                    mysql_sync_states.select().where(
                        mysql_sync_states.c.knowledge_type == knowledge_type,
                        mysql_sync_states.c.source_table == source_table,
                    )
                )
                .mappings()
                .first()
            )
            values = {
                "knowledge_type": knowledge_type,
                "source_table": source_table,
                "last_mysql_id": max(0, int(last_mysql_id)),
                "processed_count": max(0, int(processed_count)),
                "indexed_count": max(0, int(indexed_count)),
                "status": status,
                "updated_at": now,
            }
            if current is None:
                conn.execute(mysql_sync_states.insert().values(**values))
            else:
                conn.execute(
                    mysql_sync_states.update()
                    .where(
                        mysql_sync_states.c.knowledge_type == knowledge_type,
                        mysql_sync_states.c.source_table == source_table,
                    )
                    .values(
                        last_mysql_id=max(int(current["last_mysql_id"]), values["last_mysql_id"]),
                        processed_count=values["processed_count"],
                        indexed_count=values["indexed_count"],
                        status=status,
                        updated_at=now,
                    )
                )
        return self.get(knowledge_type, source_table) or values

    def reset(self, knowledge_type: str, source_table: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                mysql_sync_states.delete().where(
                    mysql_sync_states.c.knowledge_type == knowledge_type,
                    mysql_sync_states.c.source_table == source_table,
                )
            )
            conn.execute(
                mysql_sync_states.insert().values(
                    knowledge_type=knowledge_type,
                    source_table=source_table,
                    last_mysql_id=0,
                    processed_count=0,
                    indexed_count=0,
                    status="RESET",
                    updated_at=_now(),
                )
            )

    def save_milvus_count(self, knowledge_type: str, count: int) -> dict[str, int]:
        now = _now()
        snapshot_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        current_count = max(0, int(count))
        with self.engine.begin() as conn:
            current = conn.execute(
                milvus_count_snapshots.select().where(
                    milvus_count_snapshots.c.knowledge_type == knowledge_type,
                    milvus_count_snapshots.c.snapshot_date == snapshot_date,
                )
            ).first()
            if current is None:
                conn.execute(
                    milvus_count_snapshots.insert().values(
                        knowledge_type=knowledge_type,
                        snapshot_date=snapshot_date,
                        count=current_count,
                        created_at=now,
                    )
                )
                baseline_count = current_count
            else:
                baseline_count = int(current.count)
        return {
            "today": current_count,
            "baseline": baseline_count,
            "delta": current_count - baseline_count,
        }


class PostgresSessionStore:
    """会话元数据和可恢复的对话展示历史。"""

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        self.engine = engine or get_engine(self.dsn)

    def upsert(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        user_role: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as conn:
            existing = conn.execute(
                platform_sessions.select()
                .with_only_columns(
                    platform_sessions.c.user_id,
                )
                .where(platform_sessions.c.session_id == session_id)
            ).first()
            if (
                existing is not None
                and existing.user_id
                and user_id
                and existing.user_id != user_id
            ):
                raise PermissionError("当前用户无权使用该会话")
            values = {
                "session_id": session_id,
                "user_id": user_id,
                "user_role": user_role,
                "state": state or {},
                "created_at": now,
                "updated_at": now,
            }
            if existing is None:
                conn.execute(platform_sessions.insert().values(**values))
            else:
                current = (
                    conn.execute(
                        platform_sessions.select().where(
                            platform_sessions.c.session_id == session_id,
                        )
                    )
                    .mappings()
                    .one()
                )
                conn.execute(
                    platform_sessions.update()
                    .where(
                        platform_sessions.c.session_id == session_id,
                    )
                    .values(
                        user_id=current["user_id"] or user_id,
                        user_role=user_role or current["user_role"],
                        state={**_session_state(current["state"]), **(state or {})},
                        updated_at=now,
                    )
                )
        return self.get(session_id) or {"session_id": session_id}

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    platform_sessions.select().where(
                        platform_sessions.c.session_id == session_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "user_role": row["user_role"],
            "title": _session_state(row["state"]).get("title") or "",
            "state": _session_state(row["state"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    def list_sessions(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    platform_sessions.select()
                    .where(
                        platform_sessions.c.user_id == user_id,
                    )
                    .order_by(platform_sessions.c.updated_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [
            {
                "session_id": row["session_id"],
                "user_id": row["user_id"],
                "user_role": row["user_role"],
                "title": _session_state(row["state"]).get("title") or "",
                "state": _session_state(row["state"]),
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in rows
        ]

    def list_all_sessions_page(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """管理员只读取当前页会话摘要；正文仍在打开详情时按需读取。"""
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        with self.engine.connect() as conn:
            total = int(conn.execute(text("SELECT COUNT(*) FROM platform_sessions")).scalar_one())
            rows = (
                conn.execute(
                    text("""
                SELECT s.session_id, s.user_id, s.user_role, s.state,
                       s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM platform_session_messages m
                        WHERE m.session_id = s.session_id) AS message_count,
                       (SELECT COUNT(*) FROM platform_match_results r
                        WHERE r.session_id = s.session_id) AS match_count,
                       (SELECT COUNT(*) FROM platform_drafts d
                        WHERE d.thread_id = s.session_id AND d.status <> 'DELETED') AS draft_count
                FROM platform_sessions s
                ORDER BY s.updated_at DESC, s.session_id DESC
                LIMIT :limit OFFSET :offset
            """),
                    {"limit": page_size, "offset": (page - 1) * page_size},
                )
                .mappings()
                .all()
            )
        return {
            "items": [
                {
                    "session_id": row["session_id"],
                    "user_id": row["user_id"],
                    "user_role": row["user_role"],
                    "title": _session_state(row["state"]).get("title") or "",
                    "state": _session_state(row["state"]),
                    "created_at": _iso(row["created_at"]),
                    "updated_at": _iso(row["updated_at"]),
                    "message_count": int(row["message_count"] or 0),
                    "match_count": int(row["match_count"] or 0),
                    "draft_count": int(row["draft_count"] or 0),
                }
                for row in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def list_session_messages_admin(
        self, session_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    platform_session_messages.select()
                    .where(
                        platform_session_messages.c.session_id == session_id,
                    )
                    .order_by(platform_session_messages.c.created_at.asc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def delete_session_data(self, session_id: str) -> bool:
        """级联清理 PostgreSQL 中与会话绑定的消息、草稿、匹配和 checkpoint。"""
        with self.engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM platform_sessions WHERE session_id = :session_id"),
                {"session_id": session_id},
            ).first()
            if exists is None:
                return False
            conn.execute(
                text("DELETE FROM platform_match_results WHERE session_id = :session_id"),
                {"session_id": session_id},
            )
            conn.execute(
                platform_session_messages.delete().where(
                    platform_session_messages.c.session_id == session_id
                )
            )
            for table in (
                "platform_drafts",
                "checkpoint_writes",
                "checkpoint_blobs",
                "checkpoints",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :session_id"),
                    {"session_id": session_id},
                )
            conn.execute(
                text("DELETE FROM platform_sessions WHERE session_id = :session_id"),
                {"session_id": session_id},
            )
        return True

    def rename_session(self, session_id: str, *, user_id: str, title: str) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    platform_sessions.select().where(
                        platform_sessions.c.session_id == session_id,
                        platform_sessions.c.user_id == user_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                conn.execute(
                    platform_sessions.update()
                    .where(
                        platform_sessions.c.session_id == session_id,
                    )
                    .values(state={**(row["state"] or {}), "title": title}, updated_at=now)
                )
                row = {**row, "state": {**(row["state"] or {}), "title": title}, "updated_at": now}
        if row is None:
            raise PermissionError("会话不存在或当前用户无权修改")
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "user_role": row["user_role"],
            "title": (row["state"] or {}).get("title") or "",
            "state": row["state"] or {},
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    def delete_session(self, session_id: str, *, user_id: str) -> bool:
        with self.engine.begin() as conn:
            owner = conn.execute(
                platform_sessions.select()
                .with_only_columns(
                    platform_sessions.c.user_id,
                )
                .where(platform_sessions.c.session_id == session_id)
            ).first()
            if owner is None:
                return False
            if owner.user_id and owner.user_id != user_id:
                raise PermissionError("当前用户无权删除该会话")
            conn.execute(
                platform_session_messages.delete().where(
                    platform_session_messages.c.session_id == session_id
                )
            )
            for table in (
                "platform_drafts",
                "checkpoint_writes",
                "checkpoint_blobs",
                "checkpoints",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :session_id"),
                    {"session_id": session_id},
                )
            deleted = conn.execute(
                platform_sessions.delete().where(
                    platform_sessions.c.session_id == session_id,
                    platform_sessions.c.user_id == user_id,
                )
            ).rowcount
        return bool(deleted)

    def append_message(
        self,
        session_id: str,
        *,
        user_id: str,
        role: str,
        content: str = "",
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"不支持的会话消息角色：{role}")
        message_id = f"message_{uuid.uuid4().hex}"
        created_at = _now()
        with self.engine.begin() as conn:
            owner = conn.execute(
                platform_sessions.select()
                .with_only_columns(
                    platform_sessions.c.user_id,
                )
                .where(platform_sessions.c.session_id == session_id)
            ).first()
            if owner is not None and owner.user_id and owner.user_id != user_id:
                raise PermissionError("当前用户无权访问该会话")
            if role == "assistant":
                previous = (
                    conn.execute(
                        platform_session_messages.select()
                        .where(
                            platform_session_messages.c.session_id == session_id,
                        )
                        .order_by(platform_session_messages.c.created_at.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if (
                    previous
                    and previous["role"] == "assistant"
                    and str(previous["content"] or "").strip() == str(content or "").strip()
                ):
                    return {
                        "message_id": previous["message_id"],
                        "session_id": session_id,
                        "user_id": user_id,
                        "role": previous["role"],
                        "content": previous["content"],
                        "response": previous["response"] or {},
                        "created_at": _iso(previous["created_at"]),
                    }
            conn.execute(
                platform_session_messages.insert().values(
                    message_id=message_id,
                    session_id=session_id,
                    user_id=user_id,
                    role=role,
                    content=str(content or ""),
                    response=response or {},
                    created_at=created_at,
                )
            )
            if owner is not None:
                current = (
                    conn.execute(
                        platform_sessions.select().where(
                            platform_sessions.c.session_id == session_id,
                        )
                    )
                    .mappings()
                    .one()
                )
                state = {**(current["state"] or {}), "last_message_id": message_id}
                if role == "user" and not state.get("title") and str(content or "").strip():
                    state["title"] = " ".join(str(content).split())[:40]
                conn.execute(
                    platform_sessions.update()
                    .where(
                        platform_sessions.c.session_id == session_id,
                    )
                    .values(updated_at=created_at, state=state)
                )
        return {
            "message_id": message_id,
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "content": str(content or ""),
            "response": response or {},
            "created_at": _iso(created_at),
        }

    def list_messages(
        self, session_id: str, *, user_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        return self._list_messages(session_id, user_id=user_id, limit=limit, maximum=20)

    def list_context_messages(
        self, session_id: str, *, user_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """只读取上下文需要的消息正文，不把完整回答 JSON 再注入模型。"""

        limit = max(1, min(int(limit), 200))
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    platform_session_messages.select()
                    .with_only_columns(
                        platform_session_messages.c.message_id,
                        platform_session_messages.c.session_id,
                        platform_session_messages.c.user_id,
                        platform_session_messages.c.role,
                        platform_session_messages.c.content,
                        platform_session_messages.c.created_at,
                    )
                    .join(
                        platform_sessions,
                        platform_sessions.c.session_id == platform_session_messages.c.session_id,
                    )
                    .where(
                        platform_session_messages.c.session_id == session_id,
                        platform_session_messages.c.user_id == user_id,
                        platform_sessions.c.user_id == user_id,
                    )
                    .order_by(platform_session_messages.c.created_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [
            {
                "message_id": row["message_id"],
                "session_id": row["session_id"],
                "user_id": row["user_id"],
                "role": row["role"],
                "content": row["content"],
                "response": {},
                "created_at": _iso(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def _list_messages(
        self, session_id: str, *, user_id: str, limit: int, maximum: int
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), maximum))
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    platform_session_messages.select()
                    .join(
                        platform_sessions,
                        platform_sessions.c.session_id == platform_session_messages.c.session_id,
                    )
                    .where(
                        platform_session_messages.c.session_id == session_id,
                        platform_session_messages.c.user_id == user_id,
                        platform_sessions.c.user_id == user_id,
                    )
                    .order_by(platform_session_messages.c.created_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [
            {
                "message_id": row["message_id"],
                "session_id": row["session_id"],
                "user_id": row["user_id"],
                "role": row["role"],
                "content": row["content"],
                "response": row["response"] or {},
                "created_at": _iso(row["created_at"]),
            }
            for row in reversed(rows)
        ]
