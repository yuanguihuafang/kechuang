"""文档版本发布、下架、回收和解析结果读取应用服务。"""

from __future__ import annotations
from services.persistence.retention import serialize_storage_references

from datetime import datetime, timezone
from typing import Any

from core.contracts.knowledge import DocumentUnpublishRequest, resolve_knowledge_type
from services.application.draft_cleanup import delete_linked_drafts
from services.application.draft_publishing import plan_draft_cleanup


def _now() -> datetime:
    return datetime.now(timezone.utc)


@serialize_storage_references
async def cleanup_document_version(
    adapter: Any, lifecycle: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    result = await adapter.delete_version(payload)
    if result.get("status") not in {"DELETED", "SUCCESS", "UNPUBLISHED"}:
        raise RuntimeError(result.get("error") or "旧版本清理失败")
    if lifecycle is not None and payload.get("version_id"):
        if (
            lifecycle.update_version(
                str(payload["version_id"]),
                status="PURGED",
                publication_status="UNPUBLISHED",
                index_status="DELETED",
            )
            is None
        ):
            raise RuntimeError("旧版本更新失败")
    return result


async def _recover_lifecycle(runtime: Any, event: dict[str, Any]) -> None:
    await DocumentLifecycleApplicationService(runtime).recover(event)


class DocumentLifecycleApplicationService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def create_task(
        self, action: str, version: dict[str, object], user_id: str
    ) -> dict[str, object] | None:
        if self.runtime.task_store is None:
            return None
        return self.runtime.task_store.create(
            operation=f"document_{action}",
            status="RUNNING",
            document_id=str(version["document_id"]),
            version_id=str(version["version_id"]),
            lifecycle_action=action,
            scope_id=str(version.get("scope_id") or "public"),
            user_id=user_id,
            stage="milvus",
            progress=20,
            compensation={"status": "PENDING", "action": action},
            idempotency_key=f"document:{action}:{version['version_id']}:{user_id}",
        )

    @serialize_storage_references
    async def run_action(
        self,
        action: str,
        version_id: str,
        user_id: str,
        task: dict[str, object] | None = None,
        reason_code: str | None = None,
        reason_text: str | None = None,
        *,
        user_role: str = "internal",
        recovering: bool = False,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        store = self.runtime.require_lifecycle()
        version = store.get_version(version_id)
        if version is None:
            return None, task
        other_versions = []
        if action == "purge":
            if version.get("status") != "RECYCLED":
                raise ValueError("只有回收站版本才能彻底清理")
            drafts = self.runtime.require_draft_store()
            linked = [
                row
                for row in drafts.list_for_document(str(version["document_id"]))
                if row.get("status") != "DELETED"
            ]
            plan_draft_cleanup(drafts, linked, allow_published_targets=user_role == "admin")
            other_versions = [
                row
                for row in store.list_versions(str(version["document_id"]))
                if row["version_id"] != version_id and row.get("status") != "PURGED"
            ]
        if action == "restore" and version.get("status") != "RECYCLED":
            raise ValueError("只有回收站版本才能恢复")
        task = task or self.create_task(action, version, user_id)
        task_id = str(task["task_id"]) if task else None
        completed_steps: list[str] = []

        def checkpoint(step: str) -> None:
            completed_steps.append(step)
            if task_id:
                self.runtime.task_store.update(
                    task_id,
                    compensation={
                        "status": "PENDING",
                        "action": action,
                        "completed_steps": completed_steps.copy(),
                    },
                )

        try:
            if task_id:
                self.runtime.task_store.update(
                    task_id, status="RUNNING", stage="milvus", progress=20
                )
            document = store.get_document(str(version["document_id"]), include_purged=True)
            request = DocumentUnpublishRequest.model_validate(
                {
                    "scope_id": version.get("scope_id") or "public",
                    "version_id": version_id,
                    "knowledge_type": resolve_knowledge_type(
                        version.get("knowledge_type"), (document or {}).get("knowledge_type")
                    ),
                    "remove_vectors": action == "purge",
                    "identity": {"user_id": user_id, "user_role": user_role},
                }
            )
            if action in {"publish", "restore"}:
                vector_result = await self.runtime.embedded_retrieval.agent.republish_document(
                    str(version["document_id"]), request
                )
            else:
                vector_result = await self.runtime.embedded_retrieval.agent.unpublish_document(
                    str(version["document_id"]), request
                )
            if vector_result.status == "FAILED":
                raise RuntimeError(vector_result.error or f"{action} 向量操作失败")
            checkpoint("milvus")
            if task_id:
                self.runtime.task_store.update(task_id, stage="postgres", progress=70)
            if action == "purge":
                deleted_drafts = (
                    0
                    if other_versions
                    else delete_linked_drafts(self.runtime, str(version["document_id"]))
                )
                checkpoint("drafts")
                if other_versions:
                    if not any(
                        row.get("storage_key") == version["storage_key"] for row in other_versions
                    ):
                        await self.runtime.embedded_perception.delete_original_file(
                            storage_key=str(version["storage_key"])
                        )
                else:
                    await self.runtime.embedded_perception.delete_stored_document_internal(
                        storage_key=str(version["storage_key"]),
                        document_id=str(version["document_id"]),
                    )
                checkpoint("minio")
                result = store.update_version(
                    version_id,
                    status="PURGED",
                    publication_status="UNPUBLISHED",
                    index_status="DELETED",
                )
            elif action in {"publish", "restore"}:
                result = store.update_version(
                    version_id,
                    status="PUBLISHED",
                    publication_status="PUBLISHED",
                    error=None,
                    unpublish_reason_code=None,
                    unpublish_reason_text=None,
                    unpublished_by=None,
                    unpublished_at=None,
                )
            elif action == "recycle":
                result = store.update_version(
                    version_id, status="RECYCLED", publication_status="UNPUBLISHED"
                )
            else:
                result = store.update_version(
                    version_id,
                    status="UNPUBLISHED",
                    publication_status="UNPUBLISHED",
                    unpublish_reason_code=reason_code,
                    unpublish_reason_text=reason_text,
                    unpublished_by=user_id,
                    unpublished_at=_now(),
                )
            if result is None:
                raise RuntimeError("文档版本更新失败")
            checkpoint("postgres")
            if task_id:
                task = self.runtime.task_store.complete(
                    task_id,
                    status="SUCCEEDED",
                    result_ref=f"document:{version['document_id']}",
                    stage="completed",
                    compensation={
                        "status": "COMPLETED",
                        "action": action,
                        "completed_steps": completed_steps,
                    },
                )
            response = {**result, "vector_result": vector_result.model_dump()}
            if user_role == "admin":
                response["deleted_draft_count"] = deleted_drafts if action == "purge" else 0
            return response, task
        except Exception as exc:  # noqa: BLE001 - 生命周期失败必须进入补偿态
            task_id = task_id or f"document:{action}:{version_id}"
            task_fields = {
                "status": "FAILED",
                "stage": "compensation",
                "error_code": "LIFECYCLE_PARTIAL_FAILURE",
                "error": str(exc),
                "compensation": {
                    "status": "WAITING_RECOVERY",
                    "action": action,
                    "error": str(exc),
                    "completed_steps": completed_steps,
                },
            }
            payload = {
                "action": action,
                "version_id": version_id,
                "user_id": user_id,
                "completed_steps": completed_steps,
                "error": str(exc),
            }
            if recovering:
                if task and self.runtime.task_store is not None:
                    self.runtime.task_store.complete(task_id, result_ref=None, **task_fields)
                raise
            postgres_task_store = getattr(self.runtime.task_store, "_postgres", None)
            if self.runtime.outbox_repository is not None and postgres_task_store is not None:
                # PostgreSQL 内部采用事务性 Outbox；外部 MinIO/Milvus 由 Worker 补偿。
                with postgres_task_store.engine.begin() as connection:
                    self.runtime.outbox_repository.append(
                        task_id, "LIFECYCLE_RECOVERY", payload, connection=connection
                    )
                    if task_id.startswith("task_"):
                        task = self.runtime.task_store.complete(
                            task_id, result_ref=None, connection=connection, **task_fields
                        )
            else:
                if self.runtime.outbox_repository is not None:
                    self.runtime.outbox_repository.append(task_id, "LIFECYCLE_RECOVERY", payload)
                if task_id.startswith("task_"):
                    task = self.runtime.task_store.complete(task_id, result_ref=None, **task_fields)
            return None, task

    async def recover(self, event: dict[str, object]) -> None:
        payload = dict(event.get("payload") or {})
        action = str(payload.get("action") or "")
        version_id = str(payload.get("version_id") or "")
        if action not in {"publish", "unpublish", "purge", "recycle", "restore"} or not version_id:
            raise RuntimeError("生命周期补偿事件缺少有效版本或动作")
        version = self.runtime.require_lifecycle().get_version(version_id)
        if version is None:
            raise RuntimeError("生命周期补偿版本不存在")
        task = None
        if self.runtime.task_store is not None:
            operation_id = str(event.get("operation_id") or "")
            task = self.runtime.task_store.get(operation_id)
            if task is None:
                raise RuntimeError("生命周期补偿任务不存在")
        if action == "purge" and version.get("status") == "PURGED":
            if task is not None:
                self.runtime.task_store.complete(
                    str(task["task_id"]),
                    status="SUCCEEDED",
                    result_ref=f"document:{version['document_id']}",
                    stage="completed",
                    compensation={
                        "status": "COMPLETED",
                        "action": action,
                        "completed_steps": ["milvus", "drafts", "minio", "postgres"],
                    },
                )
            return
        result, _ = await self.run_action(
            action,
            version_id,
            str(payload.get("user_id") or ""),
            task=task,
            user_role="admin",
            recovering=True,
        )
        if result is None:
            raise RuntimeError("生命周期补偿未完成")

    async def with_parse_result(self, task: dict[str, object]) -> dict[str, object]:
        if task.get("document_id") and not task.get("parsed_result"):
            try:
                storage = self.runtime.storage_service
                if storage is None:
                    raise RuntimeError("MinIO 存储服务尚未装配")
                result = await storage.load_parse_result(str(task["document_id"]))
            except Exception:  # noqa: BLE001 - 任务列表补充失败不阻断主查询
                result = None
            if result:
                return {**task, "parsed_result": result}
        return task
