"""Supervisor 任务执行器：以 PostgreSQL 任务状态为权威，兼容本地调度。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .task_store import SupervisorTaskStore


class SupervisorTaskRunner:
    """使用数据库状态抢占任务；当前提交入口仍保留闭包调度兼容性。"""

    def __init__(self, task_store: SupervisorTaskStore, *, max_concurrency: int = 2) -> None:
        self.task_store = task_store
        self.max_concurrency = max(1, max_concurrency)
        self._worker_id = f"local-worker-{uuid.uuid4().hex}"
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {}
        self._worker: asyncio.Task[Any] | None = None
        self._stop_grace_seconds = 1.0

    def register(self, operation: str,
                 handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> None:
        """注册可由数据库任务重建的操作处理器。"""
        self._handlers[operation] = handler

    def start(self, *, poll_seconds: float = 1.0) -> None:
        """启动数据库队列恢复 Worker。"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._poll(poll_seconds))

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        running = list(self._running.values())
        if running:
            _, pending = await asyncio.wait(running, timeout=self._stop_grace_seconds)
        else:
            pending = set()
        for task in pending:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._running.clear()

    def _remove_running(self, task_id: str, task: asyncio.Task[Any]) -> None:
        if self._running.get(task_id) is task:
            self._running.pop(task_id, None)

    async def _poll(self, poll_seconds: float) -> None:
        while True:
            self.task_store.requeue_expired()
            active = sum(not task.done() for task in self._running.values())
            for _ in range(max(0, self.max_concurrency - active)):
                item = self.task_store.claim_next(
                    worker_id=self._worker_id, operations=set(self._handlers),
                    lease_seconds=60,
                )
                if item is None:
                    break
                operation = str(item["operation"])
                handler = self._handlers[operation]
                task_id = str(item["task_id"])
                task = asyncio.create_task(self._run(
                    task_id, lambda item=item, handler=handler: handler(item),
                    900, 3, {"FAILED"}, None, preclaimed=True,
                ))
                self._running[task_id] = task
                task.add_done_callback(
                    lambda _done, task_id=task_id, task=task: self._remove_running(task_id, task)
                )
            await asyncio.sleep(max(0.1, poll_seconds))

    def submit(
        self,
        task_id: str,
        operation: str,
        work: Callable[[], Awaitable[dict[str, Any]]],
        *,
        timeout_seconds: float = 900,
        max_attempts: int = 3,
        retry_statuses: set[str] | None = None,
        reset_for_retry: Callable[[], None] | None = None,
    ) -> bool:
        """提交任务；同一 task_id 在运行中不会重复排队。"""
        current = self._running.get(task_id)
        if current and not current.done():
            return False
        self.task_store.update(
            task_id,
            status="QUEUED",
            stage=operation,
            attempt=0,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
        task = asyncio.create_task(self._run(
            task_id, work, timeout_seconds, max_attempts,
            retry_statuses or {"FAILED"}, reset_for_retry,
        ))
        self._running[task_id] = task
        task.add_done_callback(
            lambda _done, task_id=task_id, task=task: self._remove_running(task_id, task)
        )
        return True

    async def _run(
        self, task_id: str, work: Callable[[], Awaitable[dict[str, Any]]],
        timeout_seconds: float, max_attempts: int, retry_statuses: set[str],
        reset_for_retry: Callable[[], None] | None, *, preclaimed: bool = False,
    ) -> None:
        async with self._semaphore:
            worker_id = self._worker_id
            # 短租约由心跳续期；进程异常退出时无需等待整个任务超时。
            lease_seconds = 60
            if not preclaimed and self.task_store.claim(
                task_id, worker_id=worker_id, lease_seconds=lease_seconds
            ) is None:
                return
            for attempt in range(1, max_attempts + 1):
                self.task_store.update(task_id, status="RUNNING", attempt=attempt)
                heartbeat = asyncio.create_task(
                    self._renew_lease(task_id, worker_id, lease_seconds),
                    name=f"task-lease-{task_id}",
                )
                try:
                    result = await asyncio.wait_for(work(), timeout=timeout_seconds)
                except asyncio.CancelledError:
                    self.task_store.interrupt_claim(task_id, worker_id=worker_id)
                    raise
                except asyncio.TimeoutError:
                    result = self.task_store.update(
                        task_id, status="FAILED", error_code="TASK_TIMEOUT",
                        error=f"任务超过 {timeout_seconds:g} 秒未完成",
                    ) or {}
                except Exception as exc:  # noqa: BLE001 - 任务边界统一收敛
                    result = self.task_store.update(
                        task_id, status="FAILED", error_code="TASK_EXECUTION_FAILED",
                        error=str(exc),
                    ) or {}
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                if result.get("status") not in retry_statuses or attempt >= max_attempts:
                    return
                if reset_for_retry:
                    reset_for_retry()
                self.task_store.update(task_id, status="QUEUED", next_run_at=None)
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
                if self.task_store.claim(
                    task_id, worker_id=worker_id, lease_seconds=lease_seconds
                ) is None:
                    return

    async def _renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> None:
        interval = max(0.5, min(30.0, lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            if not self.task_store.renew_lease(
                task_id, worker_id=worker_id, lease_seconds=lease_seconds
            ):
                return
