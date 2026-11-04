"""PostgreSQL 平台状态保留期清理。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from typing import Any
from zoneinfo import ZoneInfo

from .sqlalchemy import sqlalchemy_connection as _pooled_connection

CLEANUP_TIMEZONE = ZoneInfo("Asia/Shanghai")
CLEANUP_TIME = time(hour=2)


def seconds_until_daily_cleanup(now: datetime | None = None) -> int:
    current = (now or datetime.now(timezone.utc)).astimezone(CLEANUP_TIMEZONE)
    scheduled = datetime.combine(current.date(), CLEANUP_TIME, tzinfo=CLEANUP_TIMEZONE)
    if scheduled <= current:
        scheduled += timedelta(days=1)
    return max(1, int((scheduled - current).total_seconds()))


@asynccontextmanager
async def storage_reference_guard():
    """Serialize source changes and cleanup across workers, reentrant per async task."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if getattr(loop, "_kc_storage_reference_owner", None) is task:
        yield
        return
    lock = getattr(loop, "_kc_storage_reference_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        loop._kc_storage_reference_lock = lock
    async with lock:
        loop._kc_storage_reference_owner = task
        database_guard = None
        acquired = False
        try:
            dsn = os.getenv("DATABASE_URL", "").strip()
            if dsn:
                from services.persistence.sqlalchemy import postgres_advisory_lock

                database_guard = postgres_advisory_lock(dsn, 0x4B43534F55524345, shared=False)
                pending = asyncio.create_task(asyncio.to_thread(database_guard.__enter__))
                try:
                    await asyncio.shield(pending)
                    acquired = True
                except asyncio.CancelledError:
                    await pending
                    acquired = True
                    raise
            yield
        finally:
            try:
                if database_guard is not None and acquired:
                    await asyncio.shield(asyncio.to_thread(database_guard.__exit__, None, None, None))
            finally:
                loop._kc_storage_reference_owner = None


def serialize_storage_references(operation):
    @wraps(operation)
    async def run(*args, **kwargs):
        async with storage_reference_guard():
            return await operation(*args, **kwargs)

    return run


@dataclass(frozen=True)
class StorageReferences:
    document_ids: set[str]
    storage_keys: set[str]


def collect_storage_references(
    *,
    documents: Iterable[dict[str, Any]],
    drafts: Iterable[dict[str, Any]],
    tasks: Iterable[dict[str, Any]],
) -> StorageReferences:
    """收集数据库仍可能使用的原文引用，供 MinIO 孤儿扫描保护。"""

    document_ids: set[str] = set()
    storage_keys: set[str] = set()

    def add(item: Any) -> None:
        if isinstance(item, str):
            if item.startswith(("uploads/", "derived/")):
                storage_keys.add(item)
            return
        if isinstance(item, list):
            for child in item:
                add(child)
            return
        if not isinstance(item, dict):
            return
        for key, value in item.items():
            if isinstance(value, str) and value.strip():
                if key in {"document_id", "source_document_id"}:
                    document_ids.add(value.strip())
                elif key in {"storage_key", "minio_object_key", "index_request_ref"}:
                    storage_keys.add(value.strip())
            add(value)

    for records in (documents, drafts, tasks):
        for record in records:
            add(record)
    return StorageReferences(document_ids=document_ids, storage_keys=storage_keys)


_STORAGE_REFERENCES_SQL = """
    SELECT jsonb_build_object(
        'document_id', document_id, 'storage_key', storage_key,
        'artifact_refs', artifact_refs
    ) AS reference FROM platform_documents WHERE status <> 'PURGED'
    UNION ALL
    SELECT jsonb_build_object('document_id', document_id, 'version', version)
        FROM platform_documents
        CROSS JOIN LATERAL jsonb_array_elements(version_history) AS version
        WHERE version->>'status' IS DISTINCT FROM 'PURGED'
    UNION ALL
    SELECT jsonb_build_object('source_document_id', source_document_id, 'draft', content)
        FROM platform_drafts
    UNION ALL
    SELECT jsonb_build_object('document_id', document_id, 'state', state)
        FROM platform_tasks
"""


class PostgresRetentionService:
    """清理平台运行状态；管理台企业/专家文档由独立保留期逻辑处理。"""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn:
            raise ValueError("DATABASE_URL 未配置")

    def storage_references(self) -> StorageReferences:
        """Consume every row; a failed page invalidates the whole cleanup snapshot."""
        with _pooled_connection(self.dsn) as conn:
            result = conn.exec_driver_sql(_STORAGE_REFERENCES_SQL)

            def rows():
                while batch := result.fetchmany(500):
                    for row in batch:
                        if not isinstance(row[0], dict):
                            raise TypeError("Invalid storage reference inventory row")
                        yield row[0]

            return collect_storage_references(documents=rows(), drafts=(), tasks=())

    def is_storage_referenced(self, storage_key: str, document_id: str | None) -> bool:
        """Recheck current references immediately before deleting a candidate."""
        variables = json.dumps({"key": storage_key, "doc": document_id or ""})
        with _pooled_connection(self.dsn) as conn:
            row = conn.exec_driver_sql(
                "SELECT EXISTS(SELECT 1 FROM ("
                + _STORAGE_REFERENCES_SQL
                + """
                ) AS sources WHERE jsonb_path_exists(reference,
                    '$.** ? (@ == $key || @.storage_key == $key || @.minio_object_key == $key
                        || @.index_request_ref == $key
                        || ($doc != "" && (@.document_id == $doc || @.source_document_id == $doc)))',
                    %s::jsonb))
                """,
                (variables,),
            ).fetchone()
        return bool(row[0])

    def cleanup_once(self) -> dict[str, int]:
        """执行一次安全清理；运行中任务和当前文档版本不会被删除。"""

        now = datetime.now(timezone.utc)
        task_cutoff = now - timedelta(days=_days("POSTGRES_TASK_RETENTION_DAYS", 30))
        session_cutoff = now - timedelta(days=_days("POSTGRES_SESSION_RETENTION_DAYS", 90))
        audit_cutoff = now - timedelta(days=_days("POSTGRES_AUDIT_RETENTION_DAYS", 180))
        memory_cutoff = now - timedelta(days=_days("POSTGRES_REJECTED_MEMORY_RETENTION_DAYS", 30))
        draft_cutoff = now - timedelta(days=_days("POSTGRES_ARCHIVED_DRAFT_RETENTION_DAYS", 180))
        outbox_cutoff = now - timedelta(days=_days("POSTGRES_OUTBOX_RETENTION_DAYS", 30))
        counts: dict[str, int] = {}
        with _pooled_connection(self.dsn) as conn:
            counts["session_messages"] = _delete_count(
                conn,
                """
                DELETE FROM platform_session_messages WHERE created_at < %s
            """,
                session_cutoff,
            )
            counts["tasks"] = _delete_count(
                conn,
                """
                DELETE FROM platform_tasks AS task
                WHERE updated_at < %s
                  AND status IN ('SUCCEEDED', 'FAILED', 'INTERRUPTED', 'CANCELLED', 'EXPIRED')
                  AND NOT EXISTS (
                    SELECT 1 FROM platform_documents AS document,
                    LATERAL jsonb_array_elements(document.version_history || jsonb_build_array(
                        jsonb_build_object('version_id', document.version_id,
                            'artifact_refs', document.artifact_refs, 'status', document.status)
                    )) AS version
                    WHERE task.status = 'SUCCEEDED'
                      AND task.state->>'index_request_ref' IS NOT NULL
                      AND document.document_id = task.document_id
                      AND version->>'version_id' = task.state->>'version_id'
                      AND version->>'status' IS DISTINCT FROM 'PURGED'
                      AND COALESCE(version #>> '{artifact_refs,confirmed_index_request,storage_key}', '') = ''
                  )
            """,
                task_cutoff,
            )
            counts["sessions"] = _delete_count(
                conn,
                """
                DELETE FROM platform_sessions WHERE updated_at < %s
            """,
                session_cutoff,
            )
            counts["audit_events"] = _delete_count(
                conn,
                """
                DELETE FROM platform_audit_events WHERE created_at < %s
            """,
                audit_cutoff,
            )
            counts["memories"] = _delete_count(
                conn,
                """
                DELETE FROM user_memories
                WHERE (expires_at IS NOT NULL AND expires_at <= %s)
                   OR (status IN ('EXPIRED', 'REJECTED') AND updated_at < %s)
            """,
                now,
                memory_cutoff,
            )
            counts["drafts"] = _delete_count(
                conn,
                """
                DELETE FROM platform_drafts
                WHERE updated_at < %s AND status IN ('ARCHIVED', 'REJECTED')
            """,
                draft_cutoff,
            )
            counts["outbox_events"] = _delete_count(
                conn,
                """
                DELETE FROM platform_outbox_events
                WHERE (status = 'PROCESSED' AND processed_at < %s)
                   OR (status = 'FAILED' AND attempts >= %s AND created_at < %s)
            """,
                outbox_cutoff,
                _positive_int("OUTBOX_MAX_ATTEMPTS", 10),
                outbox_cutoff,
            )
            counts.update(self._cleanup_checkpoints(conn, now))
        return counts

    @staticmethod
    def _cleanup_checkpoints(conn: Any, now: datetime) -> dict[str, int]:
        """清理 LangGraph 旧线程；表不存在时安全跳过。"""

        if not _table_exists(conn, "checkpoints"):
            return {"checkpoint_threads": 0, "checkpoints": 0}
        cutoff = now - timedelta(days=_days("POSTGRES_CHECKPOINT_RETENTION_DAYS", 30))
        conn.exec_driver_sql("DROP TABLE IF EXISTS tmp_expired_checkpoint_threads")
        conn.exec_driver_sql(
            """
            CREATE TEMP TABLE tmp_expired_checkpoint_threads ON COMMIT DROP AS
            SELECT thread_id FROM checkpoints
            GROUP BY thread_id
            HAVING MAX((checkpoint->>'ts')::timestamptz) < %s
        """,
            (cutoff,),
        )
        thread_count = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM tmp_expired_checkpoint_threads"
        ).fetchone()[0]
        counts = {"checkpoint_threads": int(thread_count), "checkpoints": 0}
        for table in ("checkpoint_writes", "checkpoint_blobs"):
            if _table_exists(conn, table):
                conn.exec_driver_sql(
                    f"DELETE FROM {table} WHERE thread_id IN "
                    "(SELECT thread_id FROM tmp_expired_checkpoint_threads)"
                )
        result = conn.exec_driver_sql(
            "DELETE FROM checkpoints WHERE thread_id IN "
            "(SELECT thread_id FROM tmp_expired_checkpoint_threads)"
        )
        counts["checkpoints"] = int(result.rowcount or 0)
        return counts


def _days(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _table_exists(conn: Any, table: str) -> bool:
    return bool(conn.exec_driver_sql("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0])


def _delete_count(conn: Any, statement: str, *params: Any) -> int:
    return int(conn.exec_driver_sql(statement, params).rowcount or 0)


async def run_retention_loop(stop_event: asyncio.Event, *, audit_sink: Any) -> None:
    """每天北京时间 02:00 清理过期运行状态和审计记录。"""
    while not stop_event.is_set():
        delay = seconds_until_daily_cleanup()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            async with storage_reference_guard():
                counts = await asyncio.to_thread(PostgresRetentionService().cleanup_once)
            _record_safely(
                audit_sink,
                "retention_cleanup_completed",
                service="operations",
                counts=counts,
                scheduled_time="02:00 Asia/Shanghai",
            )
        except Exception as exc:  # noqa: BLE001 - 运营清理不能终止业务网关
            _record_safely(
                audit_sink,
                "retention_cleanup_failed",
                service="operations",
                error_type=type(exc).__name__,
            )


async def cleanup_expired_admin_documents(
    lifecycle: Any,
    storage: Any,
    *,
    protected_document_ids: set[str] | None = None,
) -> dict[str, int]:
    """只清理 30 天未更新、且没有已发布版本的企业库/专家库原件。"""

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=_days("ADMIN_SOURCE_DOCUMENT_RETENTION_DAYS", 30)
    )
    expired = 0
    deleted_objects = 0
    documents = []
    for knowledge_type in ("enterprise", "expert"):
        page = 1
        seen = set()
        expected_total = None
        while True:
            result = lifecycle.list_documents_page(
                knowledge_type=knowledge_type, page=page, page_size=100, include_purged=True
            )
            batch = result["documents"]
            total = result["total"]
            if expected_total is None:
                expected_total = total
            if total != expected_total:
                raise RuntimeError("Document inventory changed during cleanup scan")
            if not batch:
                if len(seen) != expected_total:
                    raise RuntimeError("Incomplete document cleanup inventory")
                break
            for document in batch:
                document_id = document["document_id"]
                if not document_id or document_id in seen:
                    raise RuntimeError("Invalid document cleanup pagination")
                seen.add(document_id)
                documents.append({
                    **document,
                    "versions": list(lifecycle.list_versions(document_id)),
                })
            page += 1
    for document in documents:
        updated_at = _parse_datetime(document.get("updated_at"))
        versions = [item for item in document.get("versions") or [] if isinstance(item, dict)]
        if (
            updated_at is None
            or updated_at >= cutoff
            or document.get("status") in {"EXPIRED", "PURGED"}
            or document.get("document_id") in (protected_document_ids or set())
            or not versions
            or any(
                version.get("publication_status") not in {"UNPUBLISHED", "EXPIRED"}
                for version in versions
            )
        ):
            continue
        document_id = str(document.get("document_id") or "")
        keys = {
            str(version.get("storage_key") or "").strip()
            for version in document.get("versions") or []
            if isinstance(version, dict) and version.get("storage_key")
        }
        for storage_key in keys:
            await storage.delete_document(storage_key, document_id or None)
            deleted_objects += 1
        if lifecycle.expire_document(document_id):
            expired += 1
    return {"admin_documents_expired": expired, "admin_document_objects_deleted": deleted_objects}


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_safely(audit_sink: Any, event: str, **fields: Any) -> None:
    try:
        audit_sink.record(event, **fields)
    except Exception:  # noqa: BLE001 - 状态库不可用时不让清理协程退出
        return
