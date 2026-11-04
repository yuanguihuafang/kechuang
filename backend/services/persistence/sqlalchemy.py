"""SQLAlchemy Core 连接与平台表定义。"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

# PostgreSQL 使用 JSONB；SQLite 测试继续使用 JSON。
JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")
_ENGINE_LOCK = threading.Lock()
_ENGINES = {}


def sqlalchemy_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1)


def get_engine(dsn: str):
    with _ENGINE_LOCK:
        engine = _ENGINES.get(dsn)
        if engine is None:
            pool_size = int(os.getenv("POSTGRES_SQLALCHEMY_POOL_SIZE", "4"))
            max_overflow = int(os.getenv("POSTGRES_SQLALCHEMY_MAX_OVERFLOW", "2"))
            if pool_size < 1 or max_overflow < 0:
                raise ValueError(
                    "POSTGRES_SQLALCHEMY_POOL_SIZE must be >= 1 and "
                    "POSTGRES_SQLALCHEMY_MAX_OVERFLOW must be >= 0"
                )
            engine = create_engine(
                sqlalchemy_url(dsn),
                pool_pre_ping=True,
                pool_size=pool_size,
                max_overflow=max_overflow,
            )
            _ENGINES[dsn] = engine
        return engine


def close_sqlalchemy_engines() -> None:
    with _ENGINE_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()

    failures = []
    for engine in engines:
        try:
            engine.dispose()
        except Exception as exc:  # noqa: BLE001 - 清理全部引擎后统一报告
            failures.append(exc)
    if failures:
        raise RuntimeError(f"Failed to dispose {len(failures)} SQLAlchemy engine(s)") from failures[
            0
        ]


@contextmanager
def sqlalchemy_connection(dsn: str):
    with get_engine(dsn).begin() as connection:
        yield connection


@contextmanager
def postgres_advisory_lock(dsn: str, key: int, *, shared: bool):
    """Hold a session-level PostgreSQL advisory lock until the critical section exits."""
    if not dsn:
        raise RuntimeError("PostgreSQL DSN 未配置，不能取得 Milvus 写入栅栏")
    connection = get_engine(dsn).connect()
    acquired = False
    lock_function = "pg_try_advisory_lock_shared" if shared else "pg_advisory_lock"
    unlock_function = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
    try:
        result = connection.execute(
            text(f"SELECT {lock_function}(:lock_key)"), {"lock_key": int(key)}
        ).scalar()
        if shared and not result:
            raise RuntimeError("Milvus Collection 正在恢复，写入已被维护栅栏拒绝")
        acquired = True
        connection.commit()
        yield
    finally:
        try:
            if acquired:
                connection.execute(
                    text(f"SELECT {unlock_function}(:lock_key)"),
                    {"lock_key": int(key)},
                )
                connection.commit()
        finally:
            connection.close()


metadata = MetaData()

platform_tasks = Table(
    "platform_tasks",
    metadata,
    Column("task_id", String, primary_key=True),
    Column("service", String, nullable=False),
    Column("operation", String, nullable=False),
    Column("document_id", String),
    Column("status", String, nullable=False),
    Column("state", JSON_TYPE, nullable=False),
    Column("next_run_at", DateTime(timezone=True)),
    Column("lease_until", DateTime(timezone=True)),
    Column("worker_id", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

mysql_sync_states = Table(
    "mysql_sync_states",
    metadata,
    Column("knowledge_type", String, primary_key=True),
    Column("source_table", String, primary_key=True),
    Column("last_mysql_id", Integer, nullable=False, default=0),
    Column("processed_count", Integer, nullable=False, default=0),
    Column("indexed_count", Integer, nullable=False, default=0),
    Column("status", String, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

milvus_count_snapshots = Table(
    "milvus_count_snapshots",
    metadata,
    Column("knowledge_type", String, primary_key=True),
    Column("snapshot_date", Date, primary_key=True),
    Column("count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

platform_documents = Table(
    "platform_documents",
    metadata,
    Column("document_id", String, primary_key=True),
    Column("source_name", String, nullable=False),
    Column("storage_key", String, nullable=False),
    Column("scope_id", String, nullable=False),
    Column("user_id", String),
    Column("knowledge_type", String, nullable=False),
    Column("status", String, nullable=False),
    Column("active_version_id", String),
    Column("version_number", Integer),
    Column("version_id", String),
    Column("publication_status", String, nullable=False),
    Column("index_status", String, nullable=False),
    Column("artifact_refs", JSON_TYPE, nullable=False),
    Column("parsed_block_count", Integer),
    Column("chunk_count", Integer),
    Column("indexed_count", Integer),
    Column("index_time", DateTime(timezone=True)),
    Column("error", String),
    Column("version_history", JSON_TYPE, nullable=False),
    Column("unpublish_reason_code", String),
    Column("unpublish_reason_text", String),
    Column("unpublished_by", String),
    Column("unpublished_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

platform_sessions = Table(
    "platform_sessions",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("user_id", String),
    Column("user_role", String),
    Column("state", JSON_TYPE, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

platform_session_messages = Table(
    "platform_session_messages",
    metadata,
    Column("message_id", String, primary_key=True),
    Column("session_id", String, nullable=False),
    Column("user_id", String, nullable=False),
    Column("role", String, nullable=False),
    Column("content", String, nullable=False),
    Column("response", JSON_TYPE, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

platform_match_results = Table(
    "platform_match_results",
    metadata,
    Column("result_id", String, primary_key=True),
    Column("user_id", String, nullable=False),
    Column("session_id", String),
    Column("query", String, nullable=False),
    Column("draft_id", String),
    Column("result", JSON_TYPE, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "idx_platform_match_results_user",
    platform_match_results.c.user_id,
    platform_match_results.c.created_at.desc(),
)

user_memories = Table(
    "user_memories",
    metadata,
    Column("memory_id", String, primary_key=True),
    Column("user_id", String, nullable=False),
    Column("memory_type", String, nullable=False),
    Column("content", String, nullable=False),
    Column("content_hash", String),
    Column("source", String, nullable=False),
    Column("importance", Float),
    Column("confirmed", Boolean),
    Column("status", String, nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

platform_audit_events = Table(
    "platform_audit_events",
    metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("event_name", String, nullable=False),
    Column("request_id", String),
    Column("session_id", String),
    Column("task_id", String),
    Column("trace_id", String),
    Column("service", String),
    Column("payload", JSON_TYPE, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

platform_metric_events = Table(
    "platform_metric_events",
    metadata,
    Column("metric_id", Integer, primary_key=True, autoincrement=True),
    Column("metric_name", String, nullable=False),
    Column("status", String, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("error_code", String),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("estimated_cost", Float),
    Column("trace_id", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("idx_platform_metric_events_created_at", platform_metric_events.c.created_at)

runtime_evidence_config = Table(
    "runtime_evidence_config",
    metadata,
    Column("config_id", Integer, primary_key=True),
    Column("system_start_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

runtime_sessions = Table(
    "runtime_sessions",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("instance_id", String, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("status", String, nullable=False),
    Column("end_reason", String),
    Column("version", String),
    Column("host", String),
    Column("pid", Integer),
)
Index("idx_runtime_sessions_started_at", runtime_sessions.c.started_at)

runtime_heartbeats = Table(
    "runtime_heartbeats",
    metadata,
    Column("heartbeat_id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("status", String, nullable=False),
    Column("version", String),
)
Index(
    "idx_runtime_heartbeats_session_observed",
    runtime_heartbeats.c.session_id,
    runtime_heartbeats.c.observed_at,
)

platform_outbox_events = Table(
    "platform_outbox_events",
    metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("operation_id", String, nullable=False),
    Column("event_type", String, nullable=False),
    Column("payload", JSON_TYPE, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True)),
    Column("lease_until", DateTime(timezone=True)),
)
Index(
    "idx_platform_outbox_operation_type",
    platform_outbox_events.c.operation_id,
    platform_outbox_events.c.event_type,
    unique=True,
)
Index(
    "idx_platform_outbox_status_created",
    platform_outbox_events.c.status,
    platform_outbox_events.c.created_at,
)
