"""平台受控动作服务：审批通过后执行有限动作。"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from typing import Any, ClassVar, Protocol

from core.contracts.execution import ExecutionDecision, ExecutionRequest, ExecutionResult


class ApprovalStore(Protocol):
    def upsert(self, approval_id: str, *, status: str, task_id: str | None = None,
               requested_by: str | None = None, decided_by: str | None = None,
               decision_note: str | None = None, state: dict[str, Any] | None = None) -> None: ...

    def get(self, approval_id: str) -> dict[str, Any] | None: ...

    def claim_for_execution(self, approval_id: str, *, decided_by: str,
                            decision_note: str | None = None) -> bool: ...


class PlatformActionAdapter(Protocol):
    """未来真实平台接口的最小适配契约。"""

    async def archive_draft(self, parameters: dict[str, Any]) -> dict[str, object]: ...
    async def send_notification(self, parameters: dict[str, Any]) -> dict[str, object]: ...


class TestPlatformActionAdapter:
    """测试接口实现，不访问真实归档或通知系统。"""

    async def archive_draft(self, parameters: dict[str, Any]) -> dict[str, object]:
        return {
            "executed": True,
            "mode": "test",
            "action": "archive_draft",
            "draft_id": parameters["draft_id"],
            "message": "测试归档接口调用成功，未写入真实业务系统。",
        }

    async def send_notification(self, parameters: dict[str, Any]) -> dict[str, object]:
        return {
            "executed": True,
            "mode": "test",
            "action": "send_notification",
            "message": "测试通知接口调用成功，未发送真实站内通知。",
            "content_length": len(str(parameters["message"])),
        }


class InMemoryApprovalStore:
    """测试和无 PostgreSQL 环境下使用的最小审批仓储。"""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def upsert(self, approval_id: str, *, status: str, task_id: str | None = None,
               requested_by: str | None = None, decided_by: str | None = None,
               decision_note: str | None = None, state: dict[str, Any] | None = None) -> None:
        with self._lock:
            current = self._items.setdefault(approval_id, {"approval_id": approval_id, "state": {}})
            current.update({
                "status": status,
                "task_id": task_id or current.get("task_id"),
                "requested_by": requested_by or current.get("requested_by"),
                "decided_by": decided_by or current.get("decided_by"),
                "decision_note": decision_note or current.get("decision_note"),
            })
            current["state"].update(state or {})

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(approval_id)
            if item is None:
                return None
            return {**item, "state": dict(item.get("state") or {})}

    def claim_for_execution(self, approval_id: str, *, decided_by: str,
                            decision_note: str | None = None) -> bool:
        with self._lock:
            item = self._items.get(approval_id)
            if item is None or item.get("status") != "PENDING_APPROVAL":
                return False
            item.update(status="EXECUTING", decided_by=decided_by,
                        decision_note=decision_note)
            return True


class ExecutionService:
    """负责审批后执行有限的平台动作，不直接绕过平台权限。"""
    ACTIONS: ClassVar[dict[str, tuple[str, ...]]] = {
        "archive_draft": ("draft_id",),
        "send_notification": ("message",),
    }

    def __init__(self, store: ApprovalStore | None = None,
                 platform: PlatformActionAdapter | None = None,
                 *, action_timeout_seconds: float = 60) -> None:
        self.store = store or InMemoryApprovalStore()
        self.platform = platform or TestPlatformActionAdapter()
        self.action_timeout_seconds = max(0.1, action_timeout_seconds)
        self._decision_locks: dict[str, asyncio.Lock] = {}

    async def submit(self, request: ExecutionRequest) -> ExecutionResult:
        self._validate(request)
        approval_id = self._approval_id(request)
        existing = self.store.get(approval_id)
        if existing is not None:
            return self._result(existing)
        self.store.upsert(
            approval_id,
            status="PENDING_APPROVAL",
            requested_by=request.requested_by,
            state={
                "action": request.action,
                "parameters": request.parameters,
                "idempotency_key": request.idempotency_key,
                "thread_id": request.thread_id,
            },
        )
        return self._result(self.store.get(approval_id) or {})

    async def get(self, approval_id: str) -> ExecutionResult | None:
        record = self.store.get(approval_id)
        return self._result(record) if record else None

    async def decide(self, decision: ExecutionDecision) -> ExecutionResult:
        lock = self._decision_locks.setdefault(decision.approval_id, asyncio.Lock())
        if lock.locked():
            record = self.store.get(decision.approval_id)
            if record is not None:
                return self._result(record).model_copy(update={"status": "EXECUTING"})
        async with lock:
            return await self._decide(decision)

    async def _decide(self, decision: ExecutionDecision) -> ExecutionResult:
        record = self.store.get(decision.approval_id)
        if record is None:
            raise KeyError(f"审批请求不存在：{decision.approval_id}")
        if record.get("status") in {"REJECTED", "FAILED"}:
            return self._result(record)
        if record.get("status") == "EXECUTED":
            return self._result(record)
        state = record.get("state") or {}
        if decision.decision == "REJECT":
            self.store.upsert(
                decision.approval_id, status="REJECTED", decided_by=decision.decided_by,
                decision_note=decision.decision_note, state={"decision": "REJECT", "result": {
                    "executed": False, "message": "人工确认拒绝，未执行动作。"
                }},
            )
        else:
            if not self.store.claim_for_execution(
                decision.approval_id, decided_by=decision.decided_by,
                decision_note=decision.decision_note,
            ):
                return self._result(self.store.get(decision.approval_id) or {})
            try:
                result = await asyncio.wait_for(
                    self._execute_action(str(state["action"]), state.get("parameters") or {}),
                    timeout=self.action_timeout_seconds,
                )
                self.store.upsert(
                    decision.approval_id, status="EXECUTED", decided_by=decision.decided_by,
                    decision_note=decision.decision_note, state={"decision": "APPROVE", "result": result},
                )
            except asyncio.TimeoutError:
                self.store.upsert(
                    decision.approval_id, status="FAILED", decided_by=decision.decided_by,
                    decision_note=decision.decision_note, state={"decision": "APPROVE", "result": {
                        "executed": False,
                        "error_code": "ACTION_TIMEOUT",
                        "error": f"动作超过 {self.action_timeout_seconds:g} 秒未完成",
                    }},
                )
            except Exception as exc:  # noqa: BLE001 - 统一转为可审计失败状态
                self.store.upsert(
                    decision.approval_id, status="FAILED", decided_by=decision.decided_by,
                    decision_note=decision.decision_note, state={"decision": "APPROVE", "result": {
                        "executed": False, "error": str(exc)
                    }},
                )
        return self._result(self.store.get(decision.approval_id) or {})

    def _validate(self, request: ExecutionRequest) -> None:
        if request.action not in self.ACTIONS:
            raise ValueError(f"不支持的执行动作：{request.action}")
        missing = [key for key in self.ACTIONS[request.action] if not request.parameters.get(key)]
        if missing:
            raise ValueError("缺少动作参数：" + ", ".join(missing))

    def _approval_id(self, request: ExecutionRequest) -> str:
        if request.idempotency_key:
            digest = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:24]
            return f"approval_{digest}"
        return f"approval_{uuid.uuid4().hex}"

    async def _execute_action(self, action: str, parameters: dict[str, Any]) -> dict[str, object]:
        if action == "archive_draft":
            return await self.platform.archive_draft(parameters)
        if action == "send_notification":
            return await self.platform.send_notification(parameters)
        raise ValueError(f"不支持的执行动作：{action}")

    @staticmethod
    def _result(record: dict[str, Any]) -> ExecutionResult:
        state = record.get("state") or {}
        return ExecutionResult(
            status=str(record.get("status", "FAILED")),
            action=str(state.get("action", "")),
            result=state.get("result") or ({"message": "等待人工确认。"}
                                             if record.get("status") == "PENDING_APPROVAL" else {}),
            approval_id=record.get("approval_id"),
            requested_by=record.get("requested_by"),
            decided_by=record.get("decided_by"),
            decision=state.get("decision"),
            thread_id=state.get("thread_id"),
        )
