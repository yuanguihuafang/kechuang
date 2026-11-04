"""Supervisor 编排任务的轻量持久化状态。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.persistence.factory import use_postgres
from services.persistence.repositories.runtime_repository import PostgresStateStore


class _TestTaskStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = str(Path(path).resolve()) if path else "__pytest_default__"
        self.items: dict[str, dict[str, Any]] = self._read()
        self._last_timestamp: datetime | None = None

    def _read(self) -> dict[str, dict[str, Any]]:
        if self.path == "__pytest_default__":
            return {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write(self) -> None:
        if self.path == "__pytest_default__":
            return
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(self.items, handle, ensure_ascii=False)

    def _now(self) -> str:
        current = datetime.now(timezone.utc)
        if self._last_timestamp is not None and current <= self._last_timestamp:
            current = self._last_timestamp + timedelta(microseconds=1)
        self._last_timestamp = current
        return current.isoformat()

    def create(self, *, operation: str, status: str, **fields: Any) -> dict[str, Any]:
        task_id = f"task_{uuid.uuid4().hex}"
        item = {
            "task_id": task_id,
            "operation": operation,
            "status": status,
            "created_at": self._now(),
            "updated_at": self._now(),
            **fields,
        }
        self.items[task_id] = item
        self._write()
        return dict(item)

    def get(self, task_id: str) -> dict[str, Any] | None:
        item = self.items.get(task_id)
        return dict(item) if item else None

    def update(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        item = self.items.get(task_id)
        if item is None:
            return None
        item.update(fields, updated_at=self._now())
        if "status" in fields and fields["status"] not in {"RUNNING", "PARSING", "INDEXING"}:
            item.update(worker_id=None, lease_until=None)
        self._write()
        return dict(item)

    def claim(
        self, task_id: str, *, worker_id: str, lease_seconds: int = 300
    ) -> dict[str, Any] | None:
        item = self.items.get(task_id)
        if item is None or item.get("status") != "QUEUED":
            return None
        item.update(
            {
                "status": "RUNNING",
                "worker_id": worker_id,
                "lease_until": (datetime.now(timezone.utc).timestamp() + max(1, lease_seconds)),
                "updated_at": self._now(),
            }
        )
        self._write()
        return dict(item)

    def claim_next(
        self, *, worker_id: str, operations: set[str], lease_seconds: int = 120
    ) -> dict[str, Any] | None:
        candidates = [
            item
            for item in self.items.values()
            if item.get("status") == "QUEUED" and item.get("operation") in operations
        ]
        if not candidates:
            return None
        item = min(
            candidates, key=lambda value: (value.get("created_at", ""), value.get("task_id", ""))
        )
        return self.claim(item["task_id"], worker_id=worker_id, lease_seconds=lease_seconds)

    def requeue_expired(self) -> int:
        now = datetime.now(timezone.utc).timestamp()
        count = 0
        for item in self.items.values():
            lease_until = item.get("lease_until")
            if item.get("status") not in {"RUNNING", "PARSING", "INDEXING"} or not isinstance(lease_until, (int, float)):
                continue
            if lease_until >= now:
                continue
            item.update(
                status="QUEUED",
                worker_id=None,
                lease_until=None,
                error_code="WORKER_LEASE_EXPIRED",
                updated_at=self._now(),
            )
            count += 1
        if count:
            self._write()
        return count

    def renew_lease(self, task_id: str, *, worker_id: str, lease_seconds: int = 120) -> bool:
        item = self.items.get(task_id)
        if (
            item is None
            or item.get("status") not in {"RUNNING", "PARSING", "INDEXING"}
            or item.get("worker_id") != worker_id
        ):
            return False
        item.update(
            lease_until=datetime.now(timezone.utc).timestamp() + max(1, lease_seconds),
            updated_at=self._now(),
        )
        self._write()
        return True

    def list(
        self, *, operation: str | None = None, document_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        values = list(self.items.values())
        if operation:
            values = [item for item in values if item.get("operation") == operation]
        if document_id:
            values = [item for item in values if item.get("document_id") == document_id]
        values.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return [dict(item) for item in values[:limit]]

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
        values = self.list(operation=operation, document_id=document_id, limit=10**9)
        start = (page - 1) * page_size
        return {
            "items": values[start : start + page_size],
            "total": len(values),
            "page": page,
            "page_size": page_size,
            "total_pages": (len(values) + page_size - 1) // page_size,
        }

    def count_by_status(self) -> dict[str, int]:
        return {
            status: sum(item.get("status") == status for item in self.items.values())
            for status in {str(item.get("status") or "") for item in self.items.values()}
        }

    def delete_record(self, task_id: str) -> int:
        item = self.items.get(task_id)
        if item is None:
            return 0
        lifecycle_action = str(item.get("lifecycle_action") or "").lower()
        is_purged = item.get("status") == "PURGED" or (
            item.get("status") == "SUCCEEDED" and lifecycle_action == "purge"
        )
        if not is_purged:
            return 0
        document_id = item.get("document_id")
        task_ids = (
            [
                current_id
                for current_id, current in self.items.items()
                if current.get("document_id") == document_id
            ]
            if document_id
            else [task_id]
        )
        for current_id in task_ids:
            self.items.pop(current_id, None)
        self._write()
        return len(task_ids)


class SupervisorTaskStore:
    """平台编排任务适配器；任务状态统一落 PostgreSQL。"""

    def __init__(
        self, path: str | os.PathLike[str] | None = None, *, service: str = "supervisor", **_: Any
    ) -> None:
        self._memory: _TestTaskStore | None = None
        if os.getenv("PYTEST_CURRENT_TEST"):
            self._memory = _TestTaskStore(str(path) if path else None)
            self._postgres = None
            return
        use_postgres()
        self._postgres = PostgresStateStore(service=service)

    def create(self, *, operation: str, status: str, **fields: Any) -> dict[str, Any]:
        if self._memory is not None:
            return self._memory.create(operation=operation, status=status, **fields)
        idempotency_key = fields.get("idempotency_key")
        if idempotency_key:
            existing = self.find_by_idempotency_key(str(idempotency_key))
            if existing is not None:
                return existing
        task_id = self._postgres.create(
            operation=operation,
            document_id=fields.get("document_id"),
            status=status,
            state={"status": status, **fields},
        )
        return self._postgres.get(task_id) or {
            "task_id": task_id,
            "operation": operation,
            "status": status,
            **fields,
        }

    def find_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        """按幂等键查找已提交任务，避免重复创建或重复执行。"""
        if self._memory is not None:
            return next(
                (
                    item
                    for item in self._memory.list(limit=500)
                    if item.get("idempotency_key") == idempotency_key
                ),
                None,
            )
        return self._postgres.find_by_idempotency_key(idempotency_key)

    def get(self, task_id: str) -> dict[str, Any] | None:
        if self._memory is not None:
            return self._memory.get(task_id)
        return self._postgres.get(task_id)

    def claim(
        self, task_id: str, *, worker_id: str, lease_seconds: int = 120
    ) -> dict[str, Any] | None:
        """原子抢占一个 QUEUED 任务；进程重启后可由其他 Worker 重新抢占。"""
        if self._memory is not None:
            return self._memory.claim(task_id, worker_id=worker_id, lease_seconds=lease_seconds)
        return self._postgres.claim(task_id, worker_id=worker_id, lease_seconds=lease_seconds)

    def claim_next(
        self, *, worker_id: str, operations: set[str], lease_seconds: int = 120
    ) -> dict[str, Any] | None:
        if self._memory is not None:
            return self._memory.claim_next(
                worker_id=worker_id,
                operations=operations,
                lease_seconds=lease_seconds,
            )
        return self._postgres.claim_next(
            worker_id=worker_id,
            operations=operations,
            lease_seconds=lease_seconds,
        )

    def complete(
        self,
        task_id: str,
        *,
        status: str,
        result_ref: str | None = None,
        connection: Any = None,
        **summary: Any,
    ) -> dict[str, Any] | None:
        """一次提交任务终态和结果引用；不接受完整业务结果。"""
        allowed = {
            "stage",
            "result_status",
            "source_name",
            "indexed_count",
            "error_code",
            "error",
            "artifact_ref",
            "artifact_refs",
            "version_id",
            "document_id",
            "compensation",
        }
        fields = {
            key: value for key, value in summary.items() if key in allowed and value is not None
        }
        fields.update(status=status, progress=100, result_ref=result_ref)
        return self.update(task_id, connection=connection, **fields)

    def requeue_expired(self) -> int:
        if self._memory is not None:
            return self._memory.requeue_expired()
        return self._postgres.requeue_expired()

    def interrupt_claim(self, task_id: str, *, worker_id: str) -> bool:
        if self._memory is not None:
            item = self._memory.get(task_id)
            if (
                item is None
                or item.get("worker_id") != worker_id
                or item.get("status") not in {"RUNNING", "PARSING", "INDEXING"}
            ):
                return False
            self._memory.update(
                task_id,
                status="INTERRUPTED",
                stage="interrupted",
                error_code="WORKER_SHUTDOWN",
                error="服务正在关闭，任务已中断；请从最近的可操作阶段重新提交",
            )
            return True
        return self._postgres.interrupt_claim(task_id, worker_id=worker_id)

    def renew_lease(self, task_id: str, *, worker_id: str, lease_seconds: int = 120) -> bool:
        if self._memory is not None:
            return self._memory.renew_lease(
                task_id, worker_id=worker_id, lease_seconds=lease_seconds
            )
        return self._postgres.renew_lease(task_id, worker_id=worker_id, lease_seconds=lease_seconds)

    def list(
        self, *, operation: str | None = None, document_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """读取任务列表；统一限制数量，避免前端一次加载无限历史。"""
        limit = max(1, min(int(limit), 10000))
        if self._memory is not None:
            return self._memory.list(operation=operation, document_id=document_id, limit=limit)
        return self._postgres.list(operation=operation, document_id=document_id, limit=limit)

    def list_page(
        self,
        *,
        operation: str | None = None,
        page: int = 1,
        page_size: int = 100,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        if self._memory is not None:
            return self._memory.list_page(
                operation=operation,
                document_id=document_id,
                page=page,
                page_size=page_size,
            )
        return self._postgres.list_page(
            operation=operation,
            document_id=document_id,
            page=page,
            page_size=page_size,
        )

    def count_by_status(self) -> dict[str, int]:
        if self._memory is not None:
            return self._memory.count_by_status()
        return self._postgres.count_by_status()

    def rollback_inflight(self) -> list[dict[str, Any]]:
        """服务重启后把进程内未完成任务退回最近的人工可操作阶段。

        Supervisor 队列不做跨进程自动续跑，避免重启后重复调用解析器或索引器。
        文件任务按当前阶段回退：解析中回到已上传，索引中回到待确认；其他
        平台任务仅标记为中断，交给对应流程决定后续处理。
        """

        changed: list[dict[str, Any]] = []
        for task in self.list(limit=500):
            if task.get("status") not in {"QUEUED", "RUNNING", "PARSING", "INDEXING"}:
                continue
            if task.get("operation") == "file_ingestion":
                if task.get("stage") in {"index", "confirm"} or task.get("preview_blocks"):
                    fields = {
                        "status": "WAITING_CONFIRM",
                        "stage": "confirm",
                        "progress": 70,
                        "error_code": "ROLLED_BACK_AFTER_RESTART",
                        "error": "服务重启，索引任务已回退到确认结果阶段，请重新确认并建立索引",
                    }
                else:
                    fields = {
                        "status": "UPLOADED",
                        "stage": "upload",
                        "progress": 25,
                        "error_code": "ROLLED_BACK_AFTER_RESTART",
                        "error": "服务重启，解析任务已回退到上传完成阶段，请重新开始解析",
                    }
            else:
                fields = {
                    "status": "INTERRUPTED",
                    "stage": "interrupted",
                    "error_code": "ROLLED_BACK_AFTER_RESTART",
                    "error": "服务重启导致任务中断，请重新提交或人工处理",
                }
            updated = self.update(task["task_id"], **fields)
            if updated is not None:
                changed.append(updated)
        return changed

    def interrupt_running(self) -> None:
        """将当前服务的运行中任务标记为中断。"""
        if self._postgres is not None:
            self._postgres.interrupt_running()

    def update(
        self, task_id: str, *, connection: Any = None, **fields: Any
    ) -> dict[str, Any] | None:
        if self._memory is not None:
            return self._memory.update(task_id, **fields)
        return self._postgres.update(task_id, connection=connection, **fields)

    def delete_record(self, task_id: str) -> int:
        if self._memory is not None:
            return self._memory.delete_record(task_id)
        return self._postgres.delete_record(task_id)
