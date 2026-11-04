"""确认正文属于版本；任务历史仅供显式回填使用。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agents.retrieval_agent.schemas import IndexRequest
from services.persistence.retention import serialize_storage_references

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from runtime.container import GatewayRuntime
    from runtime.contracts import StorageService


class InvalidRecoverySourceError(RuntimeError):
    """持久化来源不满足恢复契约；与存储依赖访问故障区分。"""


async def load_confirmed_version_request(
    storage: StorageService, document: dict[str, Any], version: dict[str, Any]
) -> IndexRequest:
    document_id = document["document_id"]
    version_id = version["version_id"]
    reference = (version.get("artifact_refs") or {}).get("confirmed_index_request")
    error = f"文档 {document_id} 的版本确认来源无效，拒绝恢复"
    if (
        not isinstance(reference, dict)
        or set(reference) != {"storage_key", "task_id", "document_id", "version_id", "identity"}
        or reference["document_id"] != document_id
        or reference["version_id"] != version_id
        or not isinstance(reference["task_id"], str)
        or not reference["task_id"].strip()
        or reference["storage_key"]
        != f"derived/{document_id}/tasks/{reference['task_id']}/index-request.json"
        or version.get("document_id", document_id) != document_id
    ):
        raise InvalidRecoverySourceError(error)
    identity = reference["identity"]
    if (
        not isinstance(identity, dict)
        or set(identity) != {"user_id", "user_role"}
        or identity["user_id"] != document["user_id"]
    ):
        raise InvalidRecoverySourceError(error)
    content = await storage.load_index_request(document_id, reference["task_id"])
    if not content or content.get("identity") != identity:
        raise InvalidRecoverySourceError(error)
    # 旧确认正文只含 chunks/identity；版本归属由显式回填核验的引用提供。
    expected = {
        "document_id": document_id,
        "version_id": version_id,
        "task_id": reference["task_id"],
        "scope_id": document["scope_id"],
        "knowledge_type": document["knowledge_type"],
    }
    if any(key in content and content[key] != value for key, value in expected.items()):
        raise InvalidRecoverySourceError(error)
    try:
        request = IndexRequest(
            document_id=document_id,
            version_id=version_id,
            scope_id=document["scope_id"],
            knowledge_type=document["knowledge_type"],
            chunks=content.get("chunks"),
            chunks_are_final=True,
            identity=identity,
            replace_existing=True,
        )
    except ValidationError as exc:
        raise InvalidRecoverySourceError(error) from exc
    if any(
        not chunk.chunk_text.strip()
        or chunk.document_id not in {None, document_id}
        or any(
            key in chunk.metadata and chunk.metadata[key] != value
            for key, value in expected.items()
            if key != "task_id"
        )
        for chunk in request.chunks
    ):
        raise InvalidRecoverySourceError(error)
    return request


@serialize_storage_references
async def backfill_confirmed_sources(
    runtime: GatewayRuntime, *, apply: bool = False
) -> dict[str, int]:
    """默认只核验；显式 apply 写入版本引用，不启动运行时或修改正文。"""
    storage = runtime.storage_service
    tasks = runtime.task_store
    if storage is None or tasks is None:
        raise RuntimeError("确认来源回填需要 MinIO 和持久化任务存储")
    lifecycle = runtime.require_lifecycle()
    report = {"scanned": 0, "eligible": 0, "updated": 0, "rejected": 0}
    # ponytail: 人工回填先保留完整候选快照；大库应改为稳定主键游标扫描。
    candidates = []
    page = 1
    while True:
        documents = lifecycle.list_documents_page(page=page, page_size=100, include_purged=True)
        for document in documents["documents"]:
            for version in lifecycle.list_versions(document["document_id"]):
                if version.get("status") == "PURGED" or (version.get("artifact_refs") or {}).get(
                    "confirmed_index_request"
                ):
                    continue
                if not (
                    version.get("publication_status") == "PUBLISHED"
                    or version.get("index_status") == "INDEXED"
                    or int(version.get("indexed_count") or 0) > 0
                ):
                    continue
                candidates.append((document, version))
        if page >= documents["total_pages"]:
            break
        page += 1
    for document, version in candidates:
        report["scanned"] += 1
        confirmation = None
        task_page = 1
        while True:
            history = tasks.list_page(
                operation="file_ingestion",
                document_id=document["document_id"],
                page=task_page,
                page_size=100,
            )
            confirmation = next(
                (
                    task
                    for task in history["items"]
                    if task.get("version_id") == version["version_id"]
                    and task.get("status") == "SUCCEEDED"
                ),
                None,
            )
            if confirmation is not None or task_page >= history["total_pages"]:
                break
            task_page += 1
        if confirmation is None:
            report["rejected"] += 1
            logger.warning("版本 %s 缺少成功确认任务，未回填", version["version_id"])
            continue
        reference = {
            "storage_key": confirmation.get("index_request_ref"),
            "task_id": confirmation["task_id"],
            "document_id": confirmation["document_id"],
            "version_id": confirmation["version_id"],
            "identity": {
                "user_id": confirmation.get("user_id"),
                "user_role": confirmation.get("user_role"),
            },
        }
        try:
            if any(
                confirmation.get(key) != document.get(key)
                for key in ("document_id", "scope_id", "knowledge_type", "user_id")
            ):
                raise RuntimeError("成功任务与文档归属不符")
            await load_confirmed_version_request(
                storage,
                document,
                {**version, "artifact_refs": {"confirmed_index_request": reference}},
            )
            if lifecycle.get_version(version["version_id"]) != version:
                raise RuntimeError("版本来源已变化")
            current_task = tasks.get(confirmation["task_id"])
            if current_task is None or any(
                current_task.get(key) != confirmation.get(key)
                for key in (
                    "task_id",
                    "operation",
                    "document_id",
                    "version_id",
                    "status",
                    "scope_id",
                    "knowledge_type",
                    "user_id",
                    "user_role",
                    "index_request_ref",
                    "updated_at",
                )
            ):
                raise RuntimeError("确认任务已变化")
            if (
                apply
                and lifecycle.update_version(
                    version["version_id"],
                    expected_version=version,
                    artifact_refs={"confirmed_index_request": reference},
                )
                is None
            ):
                raise RuntimeError("版本来源写入失败")
        except RuntimeError as exc:
            report["rejected"] += 1
            logger.warning("版本 %s 未回填: %s", version["version_id"], exc)
            continue
        report["eligible"] += 1
        report["updated"] += int(apply)
    return report
