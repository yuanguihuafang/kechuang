"""文档版本发布、下架、回收和解析结果读取应用服务。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from agents.retrieval_agent.schemas import IndexResult
from core.contracts.knowledge import DocumentUnpublishRequest, resolve_knowledge_type
from services.application.draft_cleanup import delete_linked_drafts
from services.application.draft_publishing import plan_draft_cleanup
from services.persistence.retention import serialize_storage_references


def _now() -> datetime:
    return datetime.now(timezone.utc)


@serialize_storage_references
async def cleanup_document_version(
    adapter: Any, lifecycle: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    result = await adapter.delete_version(payload)
    if result.get("status") == "SKIPPED":
        return result
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
        self, action: str, version: dict[str, object], user_id: str, *, direct_delete: bool = False
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
            compensation={"status": "PENDING", "action": action, "direct_delete": direct_delete},
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
        direct_delete: bool = False,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        store = self.runtime.require_lifecycle()
        version = store.get_version(version_id)
        if version is None:
            return None, task
        compensation = (task or {}).get("compensation") or {}
        if (action == "restore" and compensation.get("action") == action
                and task.get("version_id") == version_id
                and "postgres" in compensation.get("completed_steps", [])
                and version.get("status") in {"PUBLISHED", "UPLOADED", "WAITING_REVIEW"}):
            task = self.runtime.task_store.complete(
                str(task["task_id"]), status="SUCCEEDED", stage="completed",
                result_ref=f"document:{version['document_id']}",
                compensation={**compensation, "status": "COMPLETED"},
            )
            return {**version, "already_completed": True}, task
        if version.get("status") == "PURGED":
            raise ValueError("该文档版本已彻底删除，不能再次操作")
        if action not in {"publish", "unpublish", "recycle", "restore", "purge"}:
            raise ValueError("不支持的文档生命周期动作")
        direct_delete = direct_delete or bool(compensation.get("direct_delete"))
        document = store.get_document(str(version["document_id"]), include_purged=True)
        if not document or document.get("active_version_id") != version_id:
            raise ValueError("只能操作当前活动版本，请刷新文档目录")
        if direct_delete and (action != "purge" or user_role != "admin"):
            raise ValueError("只有管理员可直接彻底删除文档")
        status = version.get("status")
        if action in {"publish", "unpublish"} and status not in {"PUBLISHED", "UNPUBLISHED", "INDEXED"}:
            raise ValueError("当前状态不能上下架，未发布文档请通过正常确认发布流程")
        if action == "recycle" and status in {"PARSING", "INDEXING"}:
            raise ValueError("文档正在处理中，不能移入回收站")
        has_index = version.get("index_status") == "INDEXED"
        if action in {"publish", "unpublish"} and (
            not has_index or int(version.get("indexed_count") or 0) <= 0
        ):
            raise ValueError("没有有效的已发布索引，请通过正常确认发布流程")
        other_versions = []
        protected_keys: set[str] = set()
        if action == "purge":
            if not direct_delete and version.get("status") != "RECYCLED":
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
            target_ids = {str(row["draft_id"]) for row in linked} if direct_delete or not other_versions else set()
            targets = [version, *other_versions] if direct_delete else [version]
            target_versions = tuple(str(row["version_id"]) for row in targets)
            for row in targets:
                key = str(row.get("storage_key") or "").strip()
                if not key:
                    raise ValueError("文档原件对象键缺失，不能彻底删除")
                if (store.is_storage_key_referenced(key, exclude_version_ids=target_versions)
                        or any(ref.get("status") != "DELETED" and str(ref["draft_id"]) not in target_ids
                               for ref in drafts.list_storage_key_references(key))):
                    protected_keys.add(key)
        if action == "restore" and version.get("status") != "RECYCLED":
            raise ValueError("只有回收站版本才能恢复")
        task = task or self.create_task(action, version, user_id, direct_delete=direct_delete)
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
                        "direct_delete": direct_delete,
                        "completed_steps": completed_steps.copy(),
                    },
                )

        try:
            if task_id:
                self.runtime.task_store.update(
                    task_id, status="RUNNING", stage="milvus", progress=20
                )
            scope_id = version.get("scope_id") or "public"
            vector_user_id = version.get("user_id") if scope_id == "internal" and user_role == "admin" else user_id
            if not vector_user_id:
                raise ValueError("私有文档缺少有效归属，不能操作索引")
            request = DocumentUnpublishRequest.model_validate(
                {
                    "scope_id": scope_id,
                    "version_id": version_id,
                    "knowledge_type": resolve_knowledge_type(
                        version.get("knowledge_type"), (document or {}).get("knowledge_type")
                    ),
                    "remove_vectors": action == "purge",
                    "identity": {"user_id": vector_user_id, "user_role": user_role},
                }
            )
            restore_unpublished = action == "restore" and not has_index
            if action in {"publish", "restore"}:
                key = str(version.get("storage_key") or "").strip()
                if not key or self.runtime.storage_service is None:
                    raise RuntimeError("文档原件不可用，不能恢复或上架")
                original = await self.runtime.storage_service.inspect_object(key)
                if original is None or int(original.get("size") or 0) <= 0:
                    raise RuntimeError("文档原件不存在或为空，不能恢复或上架")
            if (restore_unpublished or (action == "recycle" and not has_index)
                    or action == "purge" and other_versions and not direct_delete and not has_index):
                vector_result = IndexResult(status="UNPUBLISHED", document_id=str(version["document_id"]))
            elif action in {"publish", "restore"}:
                vector_result = await self.runtime.embedded_retrieval.agent.republish_document(
                    str(version["document_id"]), request
                )
            else:
                vector_result = await self.runtime.embedded_retrieval.agent.unpublish_document(
                    str(version["document_id"]), request
                )
            expected_vector_status = "PUBLISHED" if action in {"publish", "restore"} and not restore_unpublished else "UNPUBLISHED"
            if vector_result.status not in {expected_vector_status, "SUCCESS"}:
                raise RuntimeError(vector_result.error or f"{action} 向量操作失败")
            if ((action in {"publish", "unpublish"} or action == "restore" and not restore_unpublished
                 or action == "recycle" and has_index) and vector_result.indexed_count <= 0):
                raise RuntimeError("没有匹配的有效索引，不能报告上下架成功")
            checkpoint("milvus")
            if task_id:
                self.runtime.task_store.update(task_id, stage="postgres", progress=70)
            if action == "purge":
                deleted_drafts = (
                    0
                    if other_versions and not direct_delete
                    else delete_linked_drafts(self.runtime, str(version["document_id"]))
                )
                checkpoint("drafts")
                if direct_delete:
                    keys = {str(row["storage_key"]) for row in [version, *other_versions]}
                    for key in sorted(keys - protected_keys):
                        await self.runtime.embedded_perception.delete_original_file(storage_key=key)
                    if not protected_keys:
                        await self.runtime.embedded_perception.delete_stored_document_internal(
                            storage_key=str(version["storage_key"]),
                            document_id=str(version["document_id"]),
                        )
                elif other_versions or protected_keys:
                    if not any(
                        row.get("storage_key") == version["storage_key"] for row in other_versions
                    ) and version["storage_key"] not in protected_keys:
                        await self.runtime.embedded_perception.delete_original_file(
                            storage_key=str(version["storage_key"])
                        )
                else:
                    await self.runtime.embedded_perception.delete_stored_document_internal(
                        storage_key=str(version["storage_key"]),
                        document_id=str(version["document_id"]),
                    )
                checkpoint("minio")
                if direct_delete:
                    if not store.purge_document(str(version["document_id"])):
                        raise RuntimeError("文档目录彻底删除失败")
                    result = store.get_version(version_id)
                else:
                    result = store.update_version(
                        version_id,
                        status="PURGED",
                        publication_status="UNPUBLISHED",
                        index_status="DELETED",
                    )
            elif action in {"publish", "restore"}:
                result = store.update_version(
                    version_id,
                    status=("WAITING_REVIEW" if version.get("parsed_block_count") else "UPLOADED")
                    if restore_unpublished else "PUBLISHED",
                    publication_status="UNPUBLISHED" if restore_unpublished else "PUBLISHED",
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
                        "direct_delete": direct_delete,
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
                    "direct_delete": direct_delete,
                    "error": str(exc),
                    "completed_steps": completed_steps,
                },
            }
            payload = {
                "action": action,
                "direct_delete": direct_delete,
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
                        "direct_delete": bool(payload.get("direct_delete") or (task.get("compensation") or {}).get("direct_delete")),
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
            direct_delete=bool(payload.get("direct_delete")),
        )
        if result is None:
            raise RuntimeError("生命周期补偿未完成")

    async def with_parse_result(
        self, task: dict[str, object], *,
        load_parse_result: Callable[[str], Awaitable[dict[str, object] | None]] | None = None,
    ) -> dict[str, object]:
        if not task.get("document_id"):
            return task

        def unavailable(code: str, message: str) -> dict[str, object]:
            return {**task, "parsed_result": None,
                    "error_code": task.get("error_code") or code,
                    "error": task.get("error") or message}

        def stale() -> dict[str, object]:
            return unavailable("STALE_SOURCE", "任务引用的文档版本或原件已失效，不能补充解析正文")

        document_id = str(task["document_id"])
        version_id = str(task.get("version_id") or "")
        storage_key = str(task.get("storage_key") or "")
        if not version_id or not storage_key:
            return stale()
        try:
            store = self.runtime.require_lifecycle()

            def current_source() -> bool:
                document = store.get_document(document_id, include_purged=True)
                version = store.get_version(version_id)
                return bool(document and version
                            and document.get("active_version_id") == version_id
                            and version.get("document_id") == document_id
                            and version.get("storage_key") == storage_key
                            and version.get("status") not in {"PURGED", "EXPIRED"})

            if not current_source():
                return stale()
            result = task.get("parsed_result")
            if not result:
                if load_parse_result is not None:
                    result = await load_parse_result(document_id)
                else:
                    storage = self.runtime.storage_service
                    if storage is None:
                        raise RuntimeError("MinIO 存储服务尚未装配")
                    result = await storage.load_parse_result(document_id)
            if result:
                if (result.get("document_id") != document_id
                        or result.get("storage_key") != storage_key
                        or result.get("version_id") not in {None, "", version_id}
                        or not current_source()):
                    return stale()
                return {**task, "parsed_result": result}
        except Exception as exc:  # noqa: BLE001 - 预览失败显式返回，不改任务持久状态
            return unavailable("PARSE_RESULT_UNAVAILABLE", str(exc))
        return task
