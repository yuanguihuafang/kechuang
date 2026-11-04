"""管理员应用服务。

这里不启动独立网关，也不复制 Agent 逻辑；管理员操作直接复用运行时中
已经装配好的 Supervisor、Perception、Retrieval 和 PostgreSQL 服务。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Protocol

from admin.api.v1.schemas import AdminMysqlSyncRequest
from agents.retrieval_agent.application.retrieval_service import index_record_id
from core.contracts.knowledge import (
    BusinessImportRequest,
    DocumentUnpublishRequest,
)
from core.contracts.mysql_schema import MYSQL_FORM_CONTRACTS
from core.governance.access_policy import ManualIdentity, UserType
from services.application.business_records import canonical_source_id, stable_source_id
from services.mysql.business_search import MySQLBusinessSearchService
from services.persistence.retention import serialize_storage_references

from .index_recovery import (
    mysql_source_snapshot,
    prepare_collection_recovery,
    published_source_ids_by_mysql_id,
    recovery_snapshot_fingerprint,
    validate_recovery_source,
)

logger = logging.getLogger(__name__)


# ponytail: 进程内互斥门闩用于保护单个 Milvus 实例；若后端以后部署多个 Worker，
# 则改用 PostgreSQL advisory lock。
_MYSQL_SYNC_GATE = threading.Lock()
_MYSQL_COUNT_REFRESH_INTERVAL_SECONDS = 180.0


@contextmanager
def _milvus_write_preflight():
    """API 不等待后台长任务持有的写锁；拒绝竞争，避免阻塞事件循环。"""
    if not _MYSQL_SYNC_GATE.acquire(blocking=False):
        raise RuntimeError("Milvus 写入任务正在执行，请稍后重试维护操作")
    try:
        yield
    finally:
        _MYSQL_SYNC_GATE.release()


class _MilvusTarget(Protocol):
    async def check_ready(self) -> None: ...
    def count_by_mysql_records(self, document_id: str) -> int: ...
    def count_by_logical_records(self) -> int: ...
    def delete_by_chunk_role(self, chunk_role: str, user_id: str | None = None) -> None: ...
    def delete_by_mysql_id(self, mysql_id: str, user_id: str | None = None) -> None: ...
    def delete_by_source_id(self, source_id: str, user_id: str | None = None) -> None: ...
    def flush_for_mysql_sync(self) -> None: ...
    def _maintenance_guard(self): ...
    def _resolve_collection_name(self) -> str | None: ...
    def has_physical_collection(self, collection_name: str) -> bool: ...
    def new_shadow_generation(self, collection_name: str | None = None) -> Any: ...
    legacy_collection_name: str

    def activate_shadow_generation(
        self,
        shadow: Any,
        *,
        backup_collection_name: str,
        displaced_collection_name: str,
    ) -> dict[str, str]: ...
    def count_entities(self) -> int: ...
    def existing_ids(self, record_ids: set[str]) -> set[str]: ...


class _RetrievalAdminAgent(Protocol):
    public_milvus: Mapping[str, _MilvusTarget]
    private_milvus: _MilvusTarget | None

    async def import_business_data(
        self,
        request: BusinessImportRequest,
        *,
        target_override: Any = None,
        insert_only: bool = False,
    ) -> Any: ...
    async def index(
        self, request: Any, *, target_override: Any = None, insert_only: bool = False
    ) -> Any: ...
    async def unpublish_document(
        self, document_id: str, request: DocumentUnpublishRequest
    ) -> Any: ...


class _SyncStateStore(Protocol):
    def get(self, knowledge_type: str, source_table: str) -> dict[str, Any] | None: ...
    def save(self, knowledge_type: str, source_table: str, **fields: Any) -> Any: ...
    def reset(self, knowledge_type: str, source_table: str) -> None: ...
    def save_milvus_count(self, knowledge_type: str, count: int) -> dict[str, int]: ...


class AdminMysqlSyncMixin:
    async def start_mysql_sync(self, payload: AdminMysqlSyncRequest) -> dict[str, Any]:
        target = self._retrieval_agent().public_milvus.get(payload.knowledge_type)
        if target is None:
            raise RuntimeError(f"Collection {payload.knowledge_type} 尚未装配")
        await target.check_ready()
        result = await asyncio.to_thread(self._prepare_mysql_sync, payload)
        if "message" not in result:
            self._launch_mysql_sync(str(result["task_id"]), payload)
        return result

    def _prepare_mysql_sync(self, payload: AdminMysqlSyncRequest) -> dict[str, Any]:
        with _milvus_write_preflight():
            return self._start_mysql_sync(payload)

    def _start_mysql_sync(self, payload: AdminMysqlSyncRequest) -> dict[str, Any]:
        pending_recovery = self._collection_recovery_task(payload.knowledge_type)
        if pending_recovery and pending_recovery["status"] in {"QUEUED", "RUNNING", "SWITCHING"}:
            raise RuntimeError("该 Collection 尚未完成索引恢复，请先完成显式恢复任务")
        task_store = self._task_store_required()
        active = next(
            (
                item
                for item in task_store.list(operation="mysql_sync", limit=500)
                if item.get("knowledge_type") == payload.knowledge_type
                and item.get("status") in {"QUEUED", "RUNNING", "STOPPING"}
            ),
            None,
        )
        if active:
            return {
                "task_id": str(active["task_id"]),
                "status": active["status"],
                "status_url": f"/api/v1/admin/knowledge/sync-mysql/{active['task_id']}",
                "message": "该知识库已有同步任务正在执行",
            }
        previous = max(
            (
                item
                for item in task_store.list(operation="mysql_sync", limit=500)
                if item.get("knowledge_type") == payload.knowledge_type
                and item.get("last_id") is not None
            ),
            key=lambda item: int(item.get("last_id") or 0),
            default=None,
        )
        source_table = str(MYSQL_FORM_CONTRACTS[payload.knowledge_type]["table"])
        checkpoint = (
            self._sync_state_store.get(payload.knowledge_type, source_table)
            if self._sync_state_store
            else None
        )
        if checkpoint is not None and checkpoint.get("status") == "RESET":
            # 重建后的首次同步必须忽略旧任务历史，从 MySQL 重新全量回灌。
            previous = None
        elif checkpoint is not None:
            previous = {
                **(previous or {}),
                "last_id": checkpoint["last_mysql_id"],
                "processed": checkpoint["processed_count"],
            }
        target = self._retrieval_agent().public_milvus.get(payload.knowledge_type)
        if target is None:
            raise RuntimeError(f"Collection {payload.knowledge_type} 尚未装配")
        current_indexed_count = target.count_by_mysql_records(f"mysql:{payload.knowledge_type}")
        recorded_count = int((checkpoint or previous or {}).get("indexed_count") or 0)
        if (
            previous
            and int(previous.get("last_id") or 0) > 0
            and (current_indexed_count == 0 or current_indexed_count < recorded_count)
        ):
            raise RuntimeError("Milvus 数据少于同步断点记录，拒绝增量同步；请显式恢复索引")
        if checkpoint is None and previous and self._sync_state_store:
            self._sync_state_store.save(
                payload.knowledge_type,
                source_table,
                last_mysql_id=int(previous.get("last_id", 0)),
                processed_count=int(previous.get("processed", 0)),
                indexed_count=int(previous.get("indexed_count", 0)),
                status="MIGRATED",
            )
        if self._sync_state_store is not None:
            try:
                self._sync_state_store.save_milvus_count(
                    payload.knowledge_type, current_indexed_count
                )
            except Exception as exc:  # noqa: BLE001 - 快照失败不能阻断同步任务
                logger.warning("保存 %s 同步前 Milvus 数量失败: %s", payload.knowledge_type, exc)
        state = task_store.create(
            operation="mysql_sync",
            status="QUEUED",
            knowledge_type=payload.knowledge_type,
            processed=int(previous.get("processed", 0)) if previous else 0,
            total=0,
            indexed_count=current_indexed_count,
            batch_size=payload.batch_size,
            skipped_count=0,
            error=None,
            last_id=int(previous.get("last_id", 0)) if previous else 0,
            # 只要存在历史断点就增量续传；旧任务可能没有 started 字段。
            replace_existing=not bool(previous and int(previous.get("last_id") or 0)),
        )
        task_id = str(state["task_id"])
        return {
            "task_id": task_id,
            "status": "QUEUED",
            "status_url": f"/api/v1/admin/knowledge/sync-mysql/{task_id}",
        }

    def _launch_mysql_sync(self, task_id: str, payload: AdminMysqlSyncRequest) -> None:
        """把同步放到后台线程，避免同步 MySQL/Milvus 调用阻塞 API 事件循环。"""
        stop_event = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                self._run_mysql_sync_serialized,
                task_id,
                payload,
                stop_event,
            ),
            name=f"mysql-sync-{task_id}",
        )
        self.runtime.track_background_thread(task, stop_event)

    def mysql_sync_status(self, task_id: str) -> dict[str, Any] | None:
        return self._task_store_required().get(task_id)

    def stop_mysql_sync(self, task_id: str) -> dict[str, Any] | None:
        task_store = self._task_store_required()
        state = task_store.get(task_id)
        if state is None:
            return None
        if state["status"] in {"QUEUED", "RUNNING"}:
            return task_store.update(task_id, stop_requested=True, status="STOPPING")
        return state

    def mysql_sync_counts(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh:
            return self._cached_mysql_sync_counts()
        with self._mysql_counts_refresh_lock:
            counts = self._collect_mysql_sync_counts()
            self._mysql_counts_cache = counts
            self._mysql_counts_refreshed_at = time.monotonic()
            return self._copy_mysql_sync_counts(counts)

    @staticmethod
    def _copy_mysql_sync_counts(counts: dict[str, Any]) -> dict[str, Any]:
        return {
            key: dict(value) if isinstance(value, dict) else value for key, value in counts.items()
        }

    def _cached_mysql_sync_counts(self) -> dict[str, Any]:
        cache = getattr(self, "_mysql_counts_cache", None)
        return self._copy_mysql_sync_counts(cache or {})

    def _collect_mysql_sync_counts(self) -> dict[str, Any]:
        source = MySQLBusinessSearchService()
        counts: dict[str, Any] = {}
        for knowledge_type, contract in MYSQL_FORM_CONTRACTS.items():
            if not contract.get("public_fields") or contract.get("identity_only"):
                continue
            item: dict[str, Any] = {"mysql_count": None, "milvus_count": None, "consistent": None}
            try:
                item["mysql_count"] = source.count_for_sync(knowledge_type)
                target = self._retrieval_agent().public_milvus.get(knowledge_type)
                if target is not None:
                    count_records = getattr(target, "count_by_logical_records", None)
                    item["milvus_count"] = (
                        count_records()
                        if count_records is not None
                        else target.count_by_mysql_records(f"mysql:{knowledge_type}")
                    )
                    if self._sync_state_store is not None:
                        try:
                            item["comparison"] = self._sync_state_store.save_milvus_count(
                                knowledge_type,
                                item["milvus_count"],
                            )
                        except Exception as exc:  # noqa: BLE001 - 趋势快照失败不能影响 Milvus 数量展示
                            logger.warning(
                                "保存 Milvus 数量快照失败: knowledge_type=%s error=%s",
                                knowledge_type,
                                exc,
                            )
                item["consistent"] = item["mysql_count"] == item["milvus_count"]
            except Exception as exc:  # noqa: BLE001 - 单类依赖异常不阻断总览
                item["error"] = str(exc)
            counts[knowledge_type] = item
        return counts

    def _maybe_refresh_mysql_counts(self, *, since: float | None = None) -> bool:
        cache = getattr(self, "_mysql_counts_cache", None)
        refreshed_at = float(getattr(self, "_mysql_counts_refreshed_at", 0.0) or 0.0)
        if cache is None:
            refreshed_at = since
        if refreshed_at is None:
            return False
        if time.monotonic() - refreshed_at < _MYSQL_COUNT_REFRESH_INTERVAL_SECONDS:
            return False
        try:
            self.mysql_sync_counts(refresh=True)
            return True
        except Exception as exc:  # noqa: BLE001 - 统计失败不能中断同步任务
            self._mysql_counts_refreshed_at = time.monotonic()
            logger.warning("Milvus 数量定时刷新失败，将在下一周期重试: %s", exc)
            return False

    def _refresh_mysql_counts_after_sync(self, knowledge_type: str) -> None:
        try:
            self.mysql_sync_counts(refresh=True)
        except Exception as exc:  # noqa: BLE001 - 统计失败不能回滚已完成同步
            logger.warning("MySQL %s 同步后的 Milvus 数量刷新失败: %s", knowledge_type, exc)

    def _ensure_no_active_mysql_sync(self) -> None:
        active = next(
            (
                item
                for item in self._task_store_required().list(operation="mysql_sync", limit=500)
                if item.get("status") in {"QUEUED", "RUNNING", "STOPPING"}
            ),
            None,
        )
        if active:
            raise RuntimeError(f"知识库正在同步，不能重建：{active['task_id']}")

    def _collection_recovery_task(self, knowledge_type: str) -> dict[str, Any] | None:
        page = 1
        while True:
            result = self._task_store_required().list_page(
                operation="milvus_recovery", page=page, page_size=100
            )
            for task in result["items"]:
                if (
                    task.get("knowledge_type") == knowledge_type
                    and task.get("status") != "PREFLIGHT_COMPLETE"
                ):
                    return task
            if page >= result["total_pages"]:
                return None
            page += 1

    def _recovery_target(self, knowledge_type: str, confirm: bool) -> _MilvusTarget:
        if not confirm:
            raise ValueError("索引重建或全量恢复必须显式 confirm=true")
        if knowledge_type not in MYSQL_FORM_CONTRACTS:
            raise ValueError("不支持的知识库类型")
        target = self._retrieval_agent().public_milvus.get(knowledge_type)
        if target is None:
            raise RuntimeError(f"Collection {knowledge_type} 尚未装配")
        pending = self._collection_recovery_task(knowledge_type)
        if pending and pending["status"] in {"QUEUED", "RUNNING", "SWITCHING"}:
            raise RuntimeError(f"索引恢复正在执行：{pending['task_id']}")
        return target

    async def rebuild_collection(self, knowledge_type: str, confirm: bool) -> dict[str, Any]:
        self._recovery_target(knowledge_type, confirm)
        requests, references, skipped = await prepare_collection_recovery(self.runtime, knowledge_type)
        mysql_count, mysql_fingerprint = await asyncio.to_thread(
            mysql_source_snapshot, MySQLBusinessSearchService(), knowledge_type
        )
        snapshot_fingerprint, missing_fingerprint = recovery_snapshot_fingerprint(
            requests, references, skipped, mysql_count, mysql_fingerprint
        )
        report = {
            "recoverable_sources": references[: len(requests)],
            "object_checks": references[len(requests) :],
            "skipped_sources": skipped,
            "recoverable_count": len(requests),
            "skipped_count": len(skipped),
            "mysql_record_count": mysql_count,
            "mysql_fingerprint": mysql_fingerprint,
            "snapshot_fingerprint": snapshot_fingerprint,
            "missing_fingerprint": missing_fingerprint,
        }
        task = self._task_store_required().create(
            operation="milvus_recovery",
            status="PREFLIGHT_COMPLETE",
            knowledge_type=knowledge_type,
            stage="preflight",
            total=len(requests) + len(skipped),
            processed=len(requests),
            skipped_count=len(skipped),
            recovery_result=report,
        )
        return {
            "status": "PREFLIGHT_COMPLETE",
            "collection": knowledge_type,
            "task_id": str(task["task_id"]),
            "preflight_id": str(task["task_id"]),
            "status_url": f"/api/v1/admin/tasks/{task['task_id']}",
            **report,
            "message": "只读预检完成；未修改同步断点、草稿、文档或 Milvus Collection。",
        }

    async def restore_collection(
        self,
        knowledge_type: str,
        confirm: bool,
        preflight_id: str | None = None,
        accepted_missing_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        target = self._recovery_target(knowledge_type, confirm)
        if _MYSQL_SYNC_GATE.locked():
            raise RuntimeError("Milvus 写入任务正在执行，请稍后重试维护操作")
        task_store = self._task_store_required()
        preflight = task_store.get(str(preflight_id or ""))
        if (
            not preflight
            or preflight.get("operation") != "milvus_recovery"
            or preflight.get("status") != "PREFLIGHT_COMPLETE"
            or preflight.get("knowledge_type") != knowledge_type
        ):
            raise ValueError("请先对该知识库完成预检，并提供有效的 preflight_id")
        report = preflight.get("recovery_result") or {}
        if accepted_missing_fingerprint != report.get("missing_fingerprint"):
            raise ValueError("必须明确接受该预检报告的完整缺失清单指纹")
        requests, references, skipped, mysql_count, mysql_fingerprint = (
            await self._collection_recovery_snapshot(knowledge_type)
        )
        snapshot_fingerprint, missing_fingerprint = recovery_snapshot_fingerprint(
            requests, references, skipped, mysql_count, mysql_fingerprint
        )
        if (
            snapshot_fingerprint != report.get("snapshot_fingerprint")
            or missing_fingerprint != accepted_missing_fingerprint
        ):
            raise ValueError("预检后来源发生变化，请重新预检并确认新的缺失清单")
        active_generation = target._resolve_collection_name()
        with _milvus_write_preflight():
            task = task_store.create(
                operation="milvus_recovery",
                status="QUEUED",
                knowledge_type=knowledge_type,
                stage="queued",
                preflight_id=str(preflight_id),
                snapshot_fingerprint=snapshot_fingerprint,
                accepted_missing_fingerprint=accepted_missing_fingerprint,
                active_generation=active_generation,
                shadow_generation=None,
                total=len(requests) + mysql_count,
                source_total=len(requests),
                mysql_total=mysql_count,
                processed=0,
                source_processed=0,
                mysql_processed=0,
                indexed_count=0,
                skipped_count=len(skipped),
                recovery_result=report,
            )
            stop_event = threading.Event()
            background = asyncio.create_task(
                asyncio.to_thread(
                    self._run_collection_recovery_serialized,
                    str(task["task_id"]),
                    knowledge_type,
                    str(preflight_id),
                    accepted_missing_fingerprint,
                    stop_event,
                ),
                name=f"milvus-recovery-{knowledge_type}-{task['task_id']}",
            )
            self.runtime.track_background_thread(background, stop_event)
        return {
            "status": "QUEUED",
            "collection": knowledge_type,
            "task_id": str(task["task_id"]),
            "status_url": f"/api/v1/admin/tasks/{task['task_id']}",
            "active_generation": active_generation,
            "accepted_missing_fingerprint": accepted_missing_fingerprint,
        }

    async def activate_collection_recovery(
        self, knowledge_type: str, task_id: str, confirm: bool
    ) -> dict[str, Any]:
        if not confirm:
            raise ValueError("切换活动 Alias 必须显式 confirm=true")
        self._recovery_target(knowledge_type, True)
        task_store = self._task_store_required()
        task = task_store.get(task_id)
        if (
            not task
            or task.get("operation") != "milvus_recovery"
            or task.get("knowledge_type") != knowledge_type
            or task.get("status") != "READY_TO_SWITCH"
        ):
            raise ValueError("只能确认切换该知识库已验证且等待确认的恢复任务")
        if _MYSQL_SYNC_GATE.locked():
            raise RuntimeError("Milvus 写入任务正在执行，请稍后重试切换")
        with _milvus_write_preflight():
            task = task_store.get(task_id) or {}
            if task.get("status") != "READY_TO_SWITCH":
                raise ValueError("恢复任务状态已变化，请刷新后重试")
            task_store.update(task_id, status="SWITCHING", stage="switch_revalidation")
            stop_event = threading.Event()
            background = asyncio.create_task(
                asyncio.to_thread(
                    self._run_collection_activation_serialized,
                    task_id,
                    knowledge_type,
                ),
                name=f"milvus-activation-{knowledge_type}-{task_id}",
            )
            self.runtime.track_background_thread(background, stop_event)
        return {
            "status": "SWITCHING",
            "collection": knowledge_type,
            "task_id": task_id,
            "status_url": f"/api/v1/admin/tasks/{task_id}",
            "active_generation": task.get("active_generation"),
            "shadow_generation": task.get("shadow_generation"),
        }

    def _run_collection_activation_serialized(
        self, task_id: str, knowledge_type: str
    ) -> None:
        with _MYSQL_SYNC_GATE:
            task_store = self._task_store_required()
            try:
                current = task_store.get(task_id) or {}
                if current.get("status") != "SWITCHING":
                    return
                self._ensure_no_active_mysql_sync()
                active = self._retrieval_agent().public_milvus[knowledge_type]
                with active._maintenance_guard():
                    current = task_store.get(task_id) or {}
                    if current.get("status") != "SWITCHING":
                        return
                    if active._resolve_collection_name() != current.get("active_generation"):
                        task_store.update(
                            task_id,
                            status="FAILED",
                            stage="switch_revalidation",
                            error_code="ACTIVE_ALIAS_CHANGED",
                            error="活动 Alias 已变化，拒绝切换；请重新预检并恢复。",
                        )
                        return
                    asyncio.run(
                        self._activate_collection_recovery_locked(
                            task_id, knowledge_type, active, current
                        )
                    )
            except Exception as exc:
                logger.exception("Milvus 影子 Collection 切换确认失败：task_id=%s", task_id)
                current = task_store.get(task_id) or {}
                if current.get("status") != "SWITCHING":
                    return
                active = self._retrieval_agent().public_milvus.get(knowledge_type)
                try:
                    active_generation = self._reconciled_recovery_generation(active, current)
                    if active_generation:
                        self._complete_collection_activation(
                            task_id,
                            knowledge_type,
                            current.get("backup_generation") or current.get("active_generation"),
                            active_generation,
                        )
                    else:
                        task_store.update(
                            task_id,
                            status="FAILED",
                            stage=current.get("stage") or "switch_revalidation",
                            error_code="ACTIVATION_FAILED",
                            error=str(exc),
                        )
                except Exception:
                    task_store.update(
                        task_id, error_code="ACTIVATION_RECONCILIATION_FAILED", error=str(exc)
                    )
                    logger.critical(
                        "Alias 切换结果无法与任务状态核对：task_id=%s shadow=%s",
                        task_id,
                        current.get("shadow_generation"),
                        exc_info=True,
                    )

    @serialize_storage_references
    async def _activate_collection_recovery_locked(
        self,
        task_id: str,
        knowledge_type: str,
        active: Any,
        task: dict[str, Any],
    ) -> None:
        task_store = self._task_store_required()
        requests, references, skipped, mysql_count, mysql_fingerprint = (
            await self._collection_recovery_snapshot(knowledge_type)
        )
        snapshot_fingerprint, missing_fingerprint = recovery_snapshot_fingerprint(
            requests, references, skipped, mysql_count, mysql_fingerprint
        )
        if (
            snapshot_fingerprint != task.get("snapshot_fingerprint")
            or missing_fingerprint != task.get("accepted_missing_fingerprint")
        ):
            task_store.update(
                task_id,
                status="FAILED",
                stage="switch_revalidation",
                error_code="RECOVERY_SNAPSHOT_CHANGED",
                error="等待管理员确认期间来源发生变化，拒绝切换；请重新预检并恢复。",
            )
            return
        for request, reference in zip(
            requests, references[: len(requests)], strict=True
        ):
            validate_recovery_source(self.runtime, request, reference)

        shadow = active.new_shadow_generation(str(task.get("shadow_generation") or ""))
        await shadow.check_ready()
        actual_count = await asyncio.to_thread(shadow.count_entities)
        expected_ids = set(task.get("expected_ids") or [])
        actual_ids = await asyncio.to_thread(shadow.existing_ids, expected_ids)
        if (
            "expected_ids" not in task
            or actual_count != len(expected_ids)
            or actual_ids != expected_ids
            or actual_count != int(task.get("indexed_count") or 0)
        ):
            task_store.update(
                task_id,
                status="FAILED",
                stage="switch_revalidation",
                error_code="SHADOW_VALIDATION_FAILED",
                error=(
                    f"等待确认期间影子 Collection 行数或主键变化："
                    f"预期 {len(expected_ids)}，实际 {actual_count} 行/{len(actual_ids)} 个主键"
                ),
            )
            return

        if self._sync_state_store is None:
            raise RuntimeError("同步断点存储未初始化，不能切换影子 Collection")
        suffix = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
        base_name = active.legacy_collection_name
        backup_name = f"{base_name}__backup_{suffix}"
        displaced_name = f"{base_name}__backup_stale_{suffix}"
        task_store.update(
            task_id,
            stage="collection_rename",
            backup_generation=backup_name,
            displaced_generation=displaced_name,
        )
        generations = active.activate_shadow_generation(
            shadow,
            backup_collection_name=backup_name,
            displaced_collection_name=displaced_name,
        )
        self._complete_collection_activation(
            task_id, knowledge_type, generations["previous"], generations["active"]
        )
        self._refresh_mysql_counts_after_sync(knowledge_type)

    def _complete_collection_activation(
        self, task_id: str, knowledge_type: str, previous: str, active: str
    ) -> None:
        if self._sync_state_store is None:
            raise RuntimeError("同步断点存储未初始化，不能完成影子 Collection 切换")
        self._sync_state_store.reset(
            knowledge_type, str(MYSQL_FORM_CONTRACTS[knowledge_type]["table"])
        )
        task_store = self._task_store_required()
        task_store.update(
            task_id,
            status="SUCCESS",
            stage="completed",
            active_generation=active,
            previous_generation=previous,
            shadow_generation=active,
            error_code=None,
            error=None,
        )

    @staticmethod
    def _reconciled_recovery_generation(active: Any, task: dict[str, Any]) -> str | None:
        """Treat either the legacy alias swap or the completed canonical promotion as committed."""
        if active is None:
            return None
        generation = active._resolve_collection_name()
        if generation and generation == task.get("shadow_generation"):
            return generation
        canonical = getattr(active, "legacy_collection_name", None)
        backup = task.get("backup_generation")
        if (
            generation
            and canonical
            and generation == canonical
            and backup
            and active.has_physical_collection(backup)
        ):
            return generation
        return None

    async def _collection_recovery_snapshot(self, knowledge_type: str):
        requests, references, skipped = await prepare_collection_recovery(
            self.runtime, knowledge_type
        )
        mysql_count, mysql_fingerprint = await asyncio.to_thread(
            mysql_source_snapshot, MySQLBusinessSearchService(), knowledge_type
        )
        return requests, references, skipped, mysql_count, mysql_fingerprint

    def _run_collection_recovery_serialized(
        self,
        task_id: str,
        knowledge_type: str,
        preflight_id: str,
        accepted_missing_fingerprint: str,
        stop_event: threading.Event,
    ) -> None:
        with _MYSQL_SYNC_GATE:
            task_store = self._task_store_required()
            try:
                self._ensure_no_active_mysql_sync()
                active = self._retrieval_agent().public_milvus[knowledge_type]
                task_store.update(task_id, status="RUNNING", stage="source_revalidation")
                with active._maintenance_guard():
                    asyncio.run(
                        self._run_collection_recovery(
                            task_id,
                            knowledge_type,
                            preflight_id,
                            accepted_missing_fingerprint,
                            active,
                            stop_event,
                        )
                    )
            except Exception as exc:
                logger.exception("Milvus 影子 Collection 恢复任务失败：task_id=%s", task_id)
                current = task_store.get(task_id) or {}
                if current.get("status") == "SWITCHING":
                    active = self._retrieval_agent().public_milvus.get(knowledge_type)
                    try:
                        active_generation = self._reconciled_recovery_generation(active, current)
                        if active_generation:
                            self._complete_collection_activation(
                                task_id,
                                knowledge_type,
                                current.get("backup_generation") or current.get("active_generation"),
                                active_generation,
                            )
                        else:
                            task_store.update(
                                task_id,
                                status="FAILED",
                                stage=current.get("stage") or "alias_switch",
                                error_code="ALIAS_SWITCH_FAILED",
                                error=str(exc),
                            )
                    except Exception:
                        logger.critical(
                            "Alias 切换结果无法与任务状态核对：task_id=%s shadow=%s",
                            task_id,
                            current.get("shadow_generation"),
                            exc_info=True,
                        )
                elif current.get("status") != "SUCCESS":
                    task_store.update(
                        task_id,
                        status="FAILED",
                        stage=current.get("stage") or "restore",
                        error_code="COLLECTION_RESTORE_FAILED",
                        error=str(exc),
                    )
                else:
                    logger.critical(
                        "恢复已完成后出现任务异常：task_id=%s error=%s",
                        task_id,
                        exc,
                    )

    async def _run_collection_recovery(
        self,
        task_id: str,
        knowledge_type: str,
        preflight_id: str,
        accepted_missing_fingerprint: str,
        active: Any,
        stop_event: threading.Event,
    ) -> None:
        task_store = self._task_store_required()
        other_active = [
            task for task in task_store.list(operation="milvus_recovery", limit=500)
            if task.get("task_id") != task_id
            and task.get("knowledge_type") == knowledge_type
            and task.get("status") in {"QUEUED", "RUNNING", "SWITCHING"}
        ]
        if other_active:
            raise RuntimeError(f"该知识库已有恢复任务：{other_active[0]['task_id']}")
        preflight = task_store.get(preflight_id) or {}
        report = preflight.get("recovery_result") or {}
        requests, references, skipped, mysql_count, mysql_fingerprint = (
            await self._collection_recovery_snapshot(knowledge_type)
        )
        snapshot_fingerprint, missing_fingerprint = recovery_snapshot_fingerprint(
            requests, references, skipped, mysql_count, mysql_fingerprint
        )
        if (
            snapshot_fingerprint != report.get("snapshot_fingerprint")
            or missing_fingerprint != accepted_missing_fingerprint
        ):
            raise RuntimeError("取得维护锁后来源快照已变化，旧 Collection 保持活动，请重新预检")

        shadow = active.new_shadow_generation()
        task_store.update(
            task_id,
            stage="create_shadow",
            active_generation=active._resolve_collection_name(),
            shadow_generation=shadow.collection_name,
            total=len(requests) + mysql_count,
            processed=0,
            indexed_count=0,
        )
        await asyncio.to_thread(shadow.ensure_collection)
        agent = self._retrieval_agent()
        expected_ids: set[str] = set()
        indexed_count = 0
        processed = 0
        for index, (request, reference) in enumerate(
            zip(requests, references[: len(requests)], strict=True), start=1
        ):
            if stop_event.is_set():
                raise RuntimeError("管理员取消了 Collection 恢复任务")
            validate_recovery_source(self.runtime, request, reference)
            result = await agent.index(request, target_override=shadow, insert_only=True)
            if result.status != "SUCCESS":
                raise RuntimeError(result.error or f"恢复来源索引失败：{reference}")
            if not result.indexed_chunks or result.indexed_count != len(result.indexed_chunks):
                raise RuntimeError("恢复来源索引结果不完整，拒绝将跳过写入视为恢复成功")
            expected_ids.update(
                index_record_id(request.document_id, chunk) for chunk in result.indexed_chunks
            )
            indexed_count = len(expected_ids)
            processed += 1
            task_store.update(
                task_id,
                stage="published_content",
                processed=processed,
                source_processed=processed,
                indexed_count=indexed_count,
                current_source=reference,
                source_index=index,
            )

        published_mysql_ids = {
            str(chunk.mysql_id)
            for request in requests
            for chunk in request.chunks
            if chunk.mysql_id and chunk.chunk_role in {"admin_draft", "agent_draft"}
        }
        published_mysql_ids.update(
            str(item["mysql_id"])
            for item in skipped
            if item.get("source_type") == "draft_conflict" and item.get("mysql_id")
        )
        source_ids = published_source_ids_by_mysql_id(requests)
        source = MySQLBusinessSearchService()
        after_id = 0
        mysql_processed = 0
        while True:
            records = source.records_for_sync(knowledge_type, after_id=after_id, limit=64)
            if not records:
                raw_rows = source.list_for_sync(knowledge_type, after_id=after_id, limit=64)
                if not raw_rows:
                    break
                after_id = max(int(row["id"]) for row in raw_rows if row.get("id") is not None)
                mysql_processed += len(raw_rows)
                task_store.update(
                    task_id,
                    stage="mysql",
                    processed=len(requests) + mysql_processed,
                    mysql_processed=mysql_processed,
                    indexed_count=indexed_count,
                    last_id=after_id,
                )
                continue
            batch_last_id = max(int(record.mysql_id) for record in records if record.mysql_id)
            records = [
                record.model_copy(
                    update={
                        "source_id": source_ids.get(str(record.mysql_id))
                        or canonical_source_id(record.source_id)
                        or stable_source_id(str(record.mysql_id))
                    }
                )
                for record in records
                if str(record.mysql_id) not in published_mysql_ids
            ]
            if records:
                result = await agent.import_business_data(
                    BusinessImportRequest(
                        document_id=f"mysql:{knowledge_type}",
                        source_name=f"MySQL:{knowledge_type}",
                        scope_id="public",
                        knowledge_type=knowledge_type,
                        records=records,
                        publication_status="PUBLISHED",
                        replace_existing=False,
                        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
                    ),
                    target_override=shadow,
                    insert_only=True,
                )
                if result.status != "SUCCESS":
                    raise RuntimeError(result.error or "MySQL 补充数据恢复失败")
                if not result.indexed_chunks or result.indexed_count != len(result.indexed_chunks):
                    raise RuntimeError("MySQL 恢复索引结果不完整，拒绝将跳过写入视为恢复成功")
                expected_ids.update(
                    index_record_id(f"mysql:{knowledge_type}", chunk)
                    for chunk in result.indexed_chunks
                )
                indexed_count = len(expected_ids)
            mysql_processed += len(records)
            after_id = batch_last_id
            task_store.update(
                task_id,
                stage="mysql",
                processed=len(requests) + mysql_processed,
                mysql_processed=mysql_processed,
                indexed_count=indexed_count,
                last_id=after_id,
            )

        task_store.update(task_id, stage="final_revalidation")
        final_requests, final_references, final_skipped, final_mysql_count, final_mysql_fingerprint = (
            await self._collection_recovery_snapshot(knowledge_type)
        )
        final_snapshot, final_missing = recovery_snapshot_fingerprint(
            final_requests,
            final_references,
            final_skipped,
            final_mysql_count,
            final_mysql_fingerprint,
        )
        if final_snapshot != snapshot_fingerprint or final_missing != accepted_missing_fingerprint:
            raise RuntimeError("恢复期间发布来源或 MySQL 内容发生变化，拒绝切换活动 Alias")
        for request, reference in zip(
            final_requests, final_references[: len(final_requests)], strict=True
        ):
            validate_recovery_source(self.runtime, request, reference)

        task_store.update(task_id, stage="validating", processed=len(requests) + mysql_count)
        await asyncio.to_thread(shadow.flush)
        await shadow.check_ready()
        actual_count = await asyncio.to_thread(shadow.count_entities)
        actual_ids = await asyncio.to_thread(shadow.existing_ids, expected_ids)
        if actual_count != indexed_count or actual_ids != expected_ids:
            raise RuntimeError(
                f"影子 Collection 校验失败：预期 {indexed_count} 行/{len(expected_ids)} 个主键，"
                f"实际 {actual_count} 行/{len(actual_ids)} 个主键"
            )
        task_store.update(
            task_id,
            status="READY_TO_SWITCH",
            stage="ready_to_switch",
            processed=len(requests) + mysql_count,
            indexed_count=indexed_count,
            expected_ids=sorted(expected_ids),
            error_code=None,
            error=None,
        )

    def rebuild_mysql_data(self, knowledge_type: str, confirm: bool) -> dict[str, Any]:
        """只清理 MySQL 同步向量，保留用户和管理台文档向量。"""
        if not confirm:
            raise ValueError("仅重建 MySQL 同步数据也会删除现有 MySQL 向量，必须显式 confirm=true")
        if knowledge_type not in MYSQL_FORM_CONTRACTS:
            raise ValueError("不支持的知识库类型")
        target = self._retrieval_agent().public_milvus.get(knowledge_type)
        if target is None:
            raise RuntimeError(f"Collection {knowledge_type} 尚未装配")
        with _milvus_write_preflight():
            self._ensure_no_active_mysql_sync()
            if self._sync_state_store is None:
                raise RuntimeError("同步断点存储未初始化，不能清理 MySQL 索引")
            pending = self._collection_recovery_task(knowledge_type)
            if pending and pending["status"] in {"QUEUED", "RUNNING", "SWITCHING"}:
                raise RuntimeError("该 Collection 尚未完成完整索引恢复，不能单独清理 MySQL 向量")
            self._sync_state_store.reset(
                knowledge_type, str(MYSQL_FORM_CONTRACTS[knowledge_type]["table"])
            )
            target.delete_by_chunk_role("mysql")
        return {
            "status": "MYSQL_DATA_REBUILT",
            "collection": knowledge_type,
            "message": "已仅清理 MySQL 同步数据，用户和管理台文档已保留，请手动启动 MySQL 同步。",
        }

    async def resume_mysql_syncs(self) -> None:
        """服务重启后只标记未完成的同步任务，不自动继续写入 Milvus。"""
        task_store = self._task_store_required()
        for state in task_store.list(operation="milvus_recovery", limit=500):
            if state.get("status") not in {"QUEUED", "RUNNING", "SWITCHING"}:
                continue
            if state.get("status") == "SWITCHING":
                target = self._retrieval_agent().public_milvus.get(
                    str(state.get("knowledge_type") or "")
                )
                try:
                    active_generation = self._reconciled_recovery_generation(target, state)
                except Exception:
                    active_generation = None
                if active_generation:
                    try:
                        self._complete_collection_activation(
                            state["task_id"], state["knowledge_type"],
                            state.get("backup_generation") or state.get("active_generation"),
                            active_generation,
                        )
                    except Exception as exc:
                        logger.exception("已切换 Alias 的同步断点重置失败：task_id=%s", state["task_id"])
                        task_store.update(
                            state["task_id"],
                            error_code="ACTIVATION_RECONCILIATION_FAILED", error=str(exc),
                        )
                    continue
            task_store.update(
                state["task_id"],
                status="INTERRUPTED",
                stage="interrupted",
                error_code="RECOVERY_INTERRUPTED",
                error="服务重启导致索引恢复中断；活动 Alias 未确认切换，请检查任务记录后重新预检",
            )
        for state in task_store.list(operation="mysql_sync", limit=500):
            if state.get("status") == "STOPPING":
                task_store.update(state["task_id"], status="STOPPED", stop_requested=True)
                continue
            if state.get("status") not in {"QUEUED", "RUNNING", "INTERRUPTED"}:
                continue
            task_store.update(
                state["task_id"],
                status="INTERRUPTED",
                stop_requested=True,
                error_code="ROLLED_BACK_AFTER_RESTART",
                stage="interrupted",
                error="服务重启导致任务中断，请由管理员手动重新同步",
            )

    def _run_mysql_sync_serialized(
        self,
        task_id: str,
        payload: AdminMysqlSyncRequest,
        stop_event: threading.Event,
    ) -> None:
        """在单独线程中串行执行 Milvus 同步，避免多个库同时写入。"""
        with _MYSQL_SYNC_GATE:
            try:
                asyncio.run(self._run_mysql_sync(task_id, payload, stop_event=stop_event))
            except Exception as exc:  # 防止后台任务永久停留在 RUNNING
                logger.exception("MySQL 同步任务 %s 未处理异常", task_id)
                self._task_store_required().update(
                    task_id, status="FAILED", stage="sync", error=str(exc)
                )

    def _flush_mysql_collection(self, knowledge_type: str) -> None:
        target = self._retrieval_agent().public_milvus.get(knowledge_type)
        flush = getattr(target, "flush_for_mysql_sync", None) if target is not None else None
        if callable(flush):
            flush()

    async def _run_mysql_sync(
        self,
        task_id: str,
        payload: AdminMysqlSyncRequest,
        *,
        stop_event: threading.Event,
        excluded_mysql_ids: set[str] | None = None,
    ) -> None:
        source = MySQLBusinessSearchService()
        knowledge_type = payload.knowledge_type
        task_store = self._task_store_required()
        state = task_store.get(task_id) or {}
        source_count = int(state.get("processed", 0))
        indexed_count = int(state.get("indexed_count", 0))
        skipped_count = int(state.get("skipped_count", 0))
        after_id = int(state.get("last_id", 0))
        last_success_id = after_id
        last_success_processed = source_count
        last_success_indexed = indexed_count
        last_success_skipped = skipped_count
        count_refresh_started_at = time.monotonic()
        source_table = str(MYSQL_FORM_CONTRACTS[knowledge_type]["table"])
        excluded_mysql_ids = {str(mysql_id) for mysql_id in excluded_mysql_ids or set()}

        def shutdown_requested() -> bool:
            current = task_store.get(task_id) or {}
            return bool(current.get("stop_requested") or stop_event.is_set())

        def stop_sync(total_count: int | None = None) -> None:
            nonlocal last_success_indexed
            self._flush_mysql_collection(knowledge_type)
            target = self._retrieval_agent().public_milvus.get(knowledge_type)
            if target:
                last_success_indexed = target.count_by_mysql_records(f"mysql:{knowledge_type}")
            fields = {
                "status": "STOPPED",
                "last_id": last_success_id,
                "processed": last_success_processed,
                "skipped_count": last_success_skipped,
                "indexed_count": last_success_indexed,
            }
            if total_count is not None:
                fields.update(total=total_count, started=True)
            task_store.update(task_id, **fields)
            self._refresh_mysql_counts_after_sync(knowledge_type)
            self._save_sync_checkpoint(
                knowledge_type,
                source_table,
                last_success_id,
                last_success_processed,
                last_success_indexed,
                "STOPPED",
            )

        try:
            total = source.count_for_sync(knowledge_type)
            if shutdown_requested():
                stop_sync(total)
                return
            task_store.update(task_id, status="RUNNING", total=total, started=True)
            while True:
                if shutdown_requested():
                    stop_sync()
                    return
                batch_size = max(1, min(int(payload.batch_size), 64))
                records = source.records_for_sync(
                    knowledge_type, after_id=after_id, limit=batch_size
                )
                if shutdown_requested():
                    stop_sync()
                    return
                if not records:
                    raw_rows = source.list_for_sync(
                        knowledge_type, after_id=after_id, limit=batch_size
                    )
                    if shutdown_requested():
                        stop_sync()
                        return
                    if not raw_rows:
                        break
                    after_id = max(int(row["id"]) for row in raw_rows if row.get("id") is not None)
                    source_count += len(raw_rows)
                    skipped_count += len(raw_rows)
                    task_store.update(
                        task_id,
                        processed=source_count,
                        skipped_count=skipped_count,
                        last_id=after_id,
                    )
                    self._save_sync_checkpoint(
                        knowledge_type,
                        source_table,
                        after_id,
                        source_count,
                        indexed_count,
                        "RUNNING",
                    )
                    last_success_id = after_id
                    last_success_processed = source_count
                    last_success_skipped = skipped_count
                    continue
                batch_last_id = max(
                    int(record.mysql_id) for record in records if record.mysql_id is not None
                )
                batch_count = len(records)
                records = [
                    record for record in records if str(record.mysql_id) not in excluded_mysql_ids
                ]
                target = self._retrieval_agent().public_milvus.get(knowledge_type)
                if target is None:
                    raise RuntimeError(f"{knowledge_type} 的 Milvus collection 不可用")
                covered_mysql_ids = target.published_draft_mysql_ids(
                    [str(record.mysql_id) for record in records if record.mysql_id]
                )
                records = [
                    record for record in records
                    if str(record.mysql_id) not in covered_mysql_ids
                ]
                skipped_count += batch_count - len(records)
                if shutdown_requested():
                    stop_sync()
                    return
                if records:
                    existing = target.source_ids_by_mysql_ids(
                        [str(record.mysql_id) for record in records]
                    )
                    existing = {
                        key: sorted({canonical_source_id(value) for value in values if canonical_source_id(value)})
                        for key, values in existing.items()
                    }
                    associations = {
                        mysql_id: canonical_source_id(source_ids[0])
                        for mysql_id, source_ids in existing.items() if len(source_ids) == 1
                    }
                    conflicts = {key: values for key, values in existing.items() if len(values) > 1}
                    if conflicts:
                        raise ValueError(f"MySQL 同步 source_id 关联冲突：{conflicts}")
                    records = [
                        record.model_copy(
                            update={
                                "source_id": associations.get(str(record.mysql_id))
                                or canonical_source_id(record.source_id)
                                or stable_source_id(str(record.mysql_id))
                            }
                        )
                        for record in records
                    ]
                    result = await self._retrieval_agent().import_business_data(
                        BusinessImportRequest(
                            document_id=f"mysql:{knowledge_type}",
                            source_name=f"MySQL:{knowledge_type}",
                            scope_id="public",
                            knowledge_type=knowledge_type,
                            records=records,
                            publication_status="PUBLISHED",
                            # 替换范围仅为本批 mysql_id，续传同样清理这些记录的旧分块。
                            replace_existing=True,
                            identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
                        )
                    )
                    if result.status != "SUCCESS":
                        self._flush_mysql_collection(knowledge_type)
                        task_store.update(
                            task_id,
                            status=result.status,
                            error=result.error,
                            last_id=last_success_id,
                            processed=last_success_processed,
                            skipped_count=last_success_skipped,
                            indexed_count=last_success_indexed,
                        )
                        self._save_sync_checkpoint(
                            knowledge_type,
                            source_table,
                            last_success_id,
                            last_success_processed,
                            last_success_indexed,
                            result.status,
                        )
                        return
                source_count += batch_count
                after_id = batch_last_id
                task_store.update(
                    task_id,
                    processed=source_count,
                    skipped_count=skipped_count,
                    indexed_count=indexed_count,
                    status="RUNNING",
                    error=None,
                    last_id=after_id,
                    replace_existing=False,
                )
                self._save_sync_checkpoint(
                    knowledge_type, source_table, after_id, source_count, indexed_count, "RUNNING"
                )
                last_success_id = after_id
                last_success_processed = source_count
                last_success_indexed = indexed_count
                last_success_skipped = skipped_count
                self._maybe_refresh_mysql_counts(since=count_refresh_started_at)
                if shutdown_requested():
                    stop_sync()
                    return
            self._flush_mysql_collection(knowledge_type)
            target = self._retrieval_agent().public_milvus.get(knowledge_type)
            if target:
                indexed_count = target.count_by_mysql_records(f"mysql:{knowledge_type}")
            self._refresh_mysql_counts_after_sync(knowledge_type)
            task_store.update(
                task_id,
                status="SUCCESS",
                processed=source_count,
                skipped_count=skipped_count,
                indexed_count=indexed_count,
                last_id=after_id,
            )
            self._save_sync_checkpoint(
                knowledge_type, source_table, after_id, source_count, indexed_count, "SUCCESS"
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务状态必须可查询
            try:
                self._flush_mysql_collection(knowledge_type)
            except Exception:  # 保留原始同步异常
                logger.exception("同步 %s 失败后的 Milvus flush 也失败", knowledge_type)
            task_store.update(
                task_id,
                status="FAILED",
                processed=last_success_processed,
                skipped_count=last_success_skipped,
                indexed_count=last_success_indexed,
                last_id=last_success_id,
                error=str(exc),
            )
            self._save_sync_checkpoint(
                knowledge_type,
                source_table,
                last_success_id,
                last_success_processed,
                last_success_indexed,
                "FAILED",
            )

    def _save_sync_checkpoint(
        self,
        knowledge_type: str,
        source_table: str,
        last_id: int,
        processed_count: int,
        indexed_count: int,
        status: str,
    ) -> None:
        if self._sync_state_store is not None:
            self._sync_state_store.save(
                knowledge_type,
                source_table,
                last_mysql_id=last_id,
                indexed_count=indexed_count,
                status=status,
                processed_count=processed_count,
            )
