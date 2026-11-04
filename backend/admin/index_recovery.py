"""读取有效发布来源，构建仅写向量的恢复请求；不修改业务发布状态。"""

import hashlib
import json
import logging
import re
from typing import Any, cast
from urllib.parse import unquote

from agents.retrieval_agent.schemas import IndexRequest
from runtime.container import GatewayRuntime
from runtime.contracts import StorageService
from services.application.business_records import canonical_source_id
from services.application.draft_publishing import (
    build_published_draft_index_request,
    draft_index_fingerprint,
)
from services.application.index_recovery_sources import (
    InvalidRecoverySourceError,
    load_confirmed_version_request,
)
from services.persistence.retention import serialize_storage_references
from services.storage.minio.image_service import normalize_image_references

logger = logging.getLogger(__name__)


def _strip_missing_image_urls(text: str, document_id: str, missing_ids: set[str]) -> str:
    if not missing_ids:
        return text
    document = re.escape(document_id)
    image_ids = "|".join(re.escape(value) for value in sorted(missing_ids) if value)
    if not image_ids:
        return text
    route = rf"/api/v1/perception/images/{document}/(?:{image_ids})"
    text = re.sub(rf"!?\[[^\]]*\]\([^)]*{route}[^)]*\)", "", text)
    return re.sub(rf"{route}(?=[\s)\]]|$)", "", text)


def _drop_missing_image_metadata(metadata: dict[str, Any], document_id: str, missing_ids: set[str]) -> dict[str, Any]:
    if not missing_ids:
        return metadata
    result = dict(metadata)
    result["image_ids"] = [
        value for value in result.get("image_ids") or [] if str(value) not in missing_ids
    ]
    result["image_refs"] = [
        value for value in result.get("image_refs") or []
        if not isinstance(value, dict) or str(value.get("image_id") or "") not in missing_ids
    ]
    result["image_urls"] = [
        value for value in result.get("image_urls") or []
        if not any(
            re.search(
                rf"/api/v1/perception/images/{re.escape(document_id)}/{re.escape(image_id)}(?:[?#]|$)",
                str(value),
            )
            for image_id in missing_ids
        )
    ]
    return result


def _strip_unmapped_derived_images(text: str, document_id: str) -> tuple[str, list[str]]:
    route = rf"/?derived/{re.escape(document_id)}/[^\s)\]]+\.(?:png|jpe?g|gif|webp|svg)"
    matches = list(dict.fromkeys(re.findall(route, text, flags=re.IGNORECASE)))
    text = re.sub(rf"!?\[[^\]]*\]\([^)]*{route}[^)]*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(route, "", text, flags=re.IGNORECASE)
    return text, matches


def canonicalize_recovery_request(request: IndexRequest) -> IndexRequest:
    chunks = [
        chunk.model_copy(update={"source_id": canonical_source_id(chunk.source_id) or None})
        for chunk in request.chunks
    ]
    return request.model_copy(update={"chunks": chunks})


def recovery_snapshot_fingerprint(
    requests: list[IndexRequest],
    references: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    mysql_count: int,
    mysql_fingerprint: str | None = None,
) -> tuple[str, str]:
    paired_sources = [
        {"reference": reference, "request": request.model_dump(mode="json")}
        for request, reference in zip(requests, references[: len(requests)], strict=True)
    ]
    paired_sources.sort(
        key=lambda item: (
            str(item["reference"].get("draft_id") or item["reference"].get("document_id") or ""),
            str(item["reference"].get("version_id") or ""),
        )
    )
    additional_references = sorted(
        references[len(requests) :],
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
    )
    ordered_skipped = sorted(skipped, key=lambda item: (item["source_type"], item["source_id"]))
    snapshot = {
        "sources": paired_sources,
        "additional_references": additional_references,
        "skipped": ordered_skipped,
        "mysql_count": mysql_count,
        "mysql_fingerprint": mysql_fingerprint,
    }
    serialized = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    missing = json.dumps(
        snapshot["skipped"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return (
        hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        hashlib.sha256(missing.encode("utf-8")).hexdigest(),
    )


def mysql_source_snapshot(source: Any, knowledge_type: str) -> tuple[int, str]:
    """Fingerprint the actual sync rows so same-count edits invalidate preflight."""
    list_rows = getattr(source, "list_for_sync", None)
    digest = hashlib.sha256()
    if not callable(list_rows):
        count = int(source.count_for_sync(knowledge_type))
        digest.update(str(count).encode("ascii"))
        return count, digest.hexdigest()

    count = 0
    after_id = 0
    while True:
        rows = list_rows(knowledge_type, after_id=after_id, limit=500)
        if not rows:
            return count, digest.hexdigest()
        rows = sorted(rows, key=lambda row: int(row.get("id") or 0))
        next_after_id = after_id
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id <= next_after_id:
                raise RuntimeError("MySQL 恢复快照分页没有前进，拒绝生成不完整指纹")
            serialized = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
            digest.update(len(serialized).to_bytes(8, "big"))
            digest.update(serialized)
            next_after_id = row_id
            count += 1
        if next_after_id <= after_id:
            raise RuntimeError("MySQL 恢复快照分页没有前进，拒绝生成不完整指纹")
        after_id = next_after_id


def published_source_ids_by_mysql_id(requests: list[IndexRequest]) -> dict[str, str]:
    grouped: dict[str, set[str]] = {}
    for request in requests:
        for chunk in request.chunks:
            if chunk.chunk_role not in {"agent_draft", "admin_draft"}:
                continue
            mysql_id = str(chunk.mysql_id or "").strip()
            source_id = canonical_source_id(chunk.source_id)
            if mysql_id and source_id:
                grouped.setdefault(mysql_id, set()).add(source_id)
    conflicts = {key: sorted(values) for key, values in grouped.items() if len(values) > 1}
    if conflicts:
        raise ValueError(f"索引恢复 source_id 关联冲突：{conflicts}")
    return {mysql_id: next(iter(source_ids)) for mysql_id, source_ids in grouped.items()}


def validate_recovery_source(
    runtime: GatewayRuntime, request: IndexRequest, reference: dict[str, Any]
) -> None:
    """写入前和完成前核验来源；维护窗口仍需暂停跨进程业务变更。"""
    if "draft_id" in reference:
        draft = runtime.require_draft_store().get_admin(reference["draft_id"])
        if (
            not draft
            or draft.get("status") != "PUBLISHED"
            or draft.get("updated_at") != reference["updated_at"]
            or (
                draft_index_fingerprint(draft.get("draft") or {}) != reference["source_fingerprint"]
                if "source_fingerprint" in reference
                else canonicalize_recovery_request(build_published_draft_index_request(draft)) != request
            )
        ):
            raise RuntimeError(f"草稿 {reference['draft_id']} 的发布来源已变化，请重新恢复")
        for attached in reference.get("documents") or []:
            document = runtime.require_lifecycle().get_document(attached["document_id"])
            version = (document or {}).get("active_version") or {}
            if (
                not document or document.get("scope_id") != request.scope_id
                or document.get("knowledge_type") != request.knowledge_type
                or document.get("user_id") != request.identity.user_id
                or version.get("publication_status") != "PUBLISHED"
                or version.get("version_id") != attached["version_id"]
            ):
                raise RuntimeError(f"附件 {attached['document_id']} 的发布来源已变化，请重新恢复")
        return
    document = runtime.require_lifecycle().get_document(request.document_id)
    version = (document or {}).get("active_version")
    if (
        not document
        or document.get("scope_id") != request.scope_id
        or document.get("knowledge_type") != request.knowledge_type
        or document.get("user_id") != request.identity.user_id
        or not version
        or version.get("publication_status") != "PUBLISHED"
        or version.get("version_id") != request.version_id
        or (
            "document_id" in reference
            and (version.get("artifact_refs") or {}).get("confirmed_index_request")
            != reference.get("confirmed_index_request")
        )
    ):
        raise RuntimeError(f"文档 {request.document_id} 的发布来源已变化，请重新恢复")


@serialize_storage_references
async def prepare_collection_recovery(
    runtime: GatewayRuntime, knowledge_type: str
) -> tuple[list[IndexRequest], list[dict[str, Any]], list[dict[str, Any]]]:
    storage = cast(StorageService | None, runtime.storage_service)
    if storage is None:
        raise RuntimeError("索引恢复需要 MinIO 存储")
    # ponytail: 人工维护时一次核验全部正文；库规模很大时改为分批核验/读取持久化引用。
    draft_candidates: list[tuple[IndexRequest, dict[str, Any], set[str], dict[str, Any]]] = []
    document_requests: list[IndexRequest] = []
    document_references: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    related_sources_by_document: dict[str, list[dict[str, str]]] = {}
    covered_documents: set[str] = set()
    covered_versions: set[tuple[str, str]] = set()
    invalid_documents: dict[str, str] = {}
    documents_by_id: dict[str, dict[str, Any]] = {}
    published_mysql_ids: set[str] = set()
    unpublished_mysql_ids: set[str] = set()

    def skip(source_type: str, source_id: str, reason: str, **details: Any) -> None:
        entry = {"source_type": source_type, "source_id": source_id, "reason": reason, **details}
        if entry in skipped:
            return
        skipped.append(entry)
        logger.warning(
            "索引恢复跳过无效来源 source_type=%s source_id=%s reason=%s",
            source_type,
            source_id,
            reason,
        )

    drafts = runtime.require_draft_store()
    page = 1
    while True:
        result = drafts.list_page(draft_types=[knowledge_type], page=page, page_size=100)
        for draft in result["items"]:
            mysql_id = str((draft.get("draft") or {}).get("mysql_record_id") or "").strip()
            if draft.get("status") != "PUBLISHED":
                if mysql_id:
                    unpublished_mysql_ids.add(mysql_id)
                continue
            published_mysql_ids.add(mysql_id)
            draft_body = draft.get("draft") or {}
            fields = draft_body.get("fields") or {}
            draft_title = str(
                draft.get("draft_title") or draft_body.get("title") or fields.get("title")
                or fields.get("enterprise_name") or fields.get("user_name") or draft.get("draft_id") or "未知草稿"
            )
            for attachment in draft_body.get("attachments") or []:
                if not isinstance(attachment, dict) or not attachment.get("document_id"):
                    continue
                document_id = str(attachment["document_id"])
                related = {
                    "draft_id": str(draft.get("draft_id") or ""),
                    "draft_title": draft_title,
                    "attachment_name": str(attachment.get("file_name") or ""),
                    "document_id": document_id,
                }
                entries = related_sources_by_document.setdefault(document_id, [])
                if related not in entries:
                    entries.append(related)
            try:
                request = canonicalize_recovery_request(
                    build_published_draft_index_request(draft)
                )
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                # 附件失败不能抹掉已经发布、可独立恢复的业务字段。
                original = draft.get("draft") or {}
                body = {**original, "source_blocks": [], "attachments": []}
                body.setdefault("source_id", canonical_source_id(original.get("source_id")))
                body.pop("_index_snapshot", None)
                try:
                    request = canonicalize_recovery_request(build_published_draft_index_request({**draft, "draft": body}))
                except (RuntimeError, ValueError, KeyError, TypeError):
                    skip("draft", str(draft.get("draft_id") or "unknown"), str(exc))
                    continue
                skip("attachment_source", str(draft["draft_id"]), str(exc))
            document_ids = {
                str(attachment["document_id"])
                for attachment in (draft.get("draft") or {}).get("attachments") or []
                if isinstance(attachment, dict) and attachment.get("document_id")
            }
            covered_versions.update(
                (str(item["document_id"]), str(item.get("version_id") or ""))
                for item in (draft.get("draft") or {}).get("attachments") or []
                if isinstance(item, dict) and item.get("document_id")
            )
            draft_candidates.append(
                (
                    request,
                    {
                        "draft_id": draft["draft_id"], "updated_at": draft.get("updated_at"),
                        "source_id": canonical_source_id(
                            (draft.get("draft") or {}).get("source_id")
                        ),
                        "source_fingerprint": draft_index_fingerprint(draft["draft"]),
                        "restore_mode": "saved_final_chunks" if request.chunks_are_final else "existing_type_policy",
                    },
                    document_ids,
                    draft,
                )
            )
            covered_documents.update(document_ids)
        if page >= result["total_pages"]:
            break
        page += 1

    lifecycle = runtime.require_lifecycle()
    unseen_documents = set(covered_documents)
    page = 1
    while True:
        result = lifecycle.list_documents_page(
            scope_id="public",
            knowledge_type=knowledge_type,
            page=page,
            page_size=100,
            include_purged=True,
        )
        for document in result["documents"]:
            version = document.get("active_version")
            document_id = str(document["document_id"])
            documents_by_id[document_id] = document
            unseen_documents.discard(document_id)
            version_id = str((version or {}).get("version_id") or "")
            if (document_id, version_id) in covered_versions:
                if not version or version.get("publication_status") != "PUBLISHED":
                    invalid_documents[document_id] = (
                        f"文档 {document_id} 不是有效发布来源，拒绝恢复草稿原文"
                    )
                continue
            if not version or version.get("publication_status") != "PUBLISHED":
                continue
            version_id = str(version["version_id"])
            try:
                request = await load_confirmed_version_request(storage, document, version)
            except InvalidRecoverySourceError as exc:
                invalid_documents[document_id] = str(exc)
                skip("document", document_id, str(exc))
                continue
            document_requests.append(request)
            document_references.append(
                {
                    "document_id": document_id,
                    "version_id": version_id,
                    "confirmed_index_request": version["artifact_refs"]["confirmed_index_request"],
                }
            )
        if page >= result["total_pages"]:
            break
        page += 1
    if unseen_documents:
        for document_id in sorted(unseen_documents):
            invalid_documents[document_id] = f"已发布草稿的原文缺少有效发布文档记录：{document_id}"

    drafts_by_mysql_id: dict[str, list[tuple[IndexRequest, dict[str, Any], set[str], dict[str, Any]]]] = {}
    for candidate in draft_candidates:
        mysql_id = str((candidate[3].get("draft") or {}).get("mysql_record_id") or "").strip()
        if mysql_id:
            drafts_by_mysql_id.setdefault(mysql_id, []).append(candidate)
    selected_candidates = []
    for mysql_id, candidates in drafts_by_mysql_id.items():
        if len(candidates) == 1:
            selected_candidates.extend(candidates)
            continue
        source_ids = {
            canonical_source_id(chunk.source_id)
            for request, *_ in candidates
            for chunk in request.chunks
            if chunk.chunk_role in {"admin_draft", "agent_draft"}
        }
        latest_time = max(str(candidate[1].get("updated_at") or "") for candidate in candidates)
        latest = [candidate for candidate in candidates if str(candidate[1].get("updated_at") or "") == latest_time]
        fingerprints = {candidate[1]["source_fingerprint"] for candidate in latest}
        if len(source_ids) != 1 or len(fingerprints) != 1:
            details = []
            for _, reference, _, record in candidates:
                body = record.get("draft") or {}
                attachment = next(
                    (item for item in body.get("attachments") or [] if isinstance(item, dict)), {}
                )
                details.append({
                    "draft_id": reference["draft_id"],
                    "draft_title": str(record.get("draft_title") or body.get("title") or ""),
                    "attachment_name": str(attachment.get("file_name") or ""),
                    "document_id": str(attachment.get("document_id") or ""),
                })
            skip(
                "draft_conflict",
                mysql_id,
                "同一 mysql_id 关联多个内容不一致的已发布草稿，未猜测选用版本",
                mysql_id=mysql_id,
                draft_ids=sorted(item["draft_id"] for item in details),
                source_ids=sorted(source_ids),
                related_sources=details,
            )
            continue
        selected = sorted(latest, key=lambda candidate: str(candidate[1]["draft_id"]))[-1]
        selected[1]["superseded_draft_ids"] = sorted(
            candidate[1]["draft_id"] for candidate in candidates if candidate is not selected
        )
        selected_candidates.append(selected)
    draft_candidates = selected_candidates

    requests: list[IndexRequest] = []
    references: list[dict[str, Any]] = []
    source_groups: dict[tuple[str, str, str], tuple[Any, list[Any]]] = {}
    source_group_draft_ids: dict[tuple[str, str, str], set[str]] = {}
    conflicted_source_groups: set[tuple[str, str, str]] = set()

    def same_source_chunks(left: list[Any], right: list[Any]) -> bool:
        def content(chunks: list[Any]) -> list[Any]:
            return [
                (
                    chunk.chunk_text,
                    chunk.chunk_index,
                    chunk.document_id,
                    chunk.source_id,
                    chunk.mysql_id,
                    chunk.chunk_role,
                    chunk.source_locator,
                    {
                        key: value
                        for key, value in (chunk.metadata or {}).items()
                        if key not in {"draft_id", "parent_id"}
                    },
                )
                for chunk in chunks
            ]
        return content(left) == content(right)

    for request, reference, document_ids, draft_record in draft_candidates:
        content = draft_record.get("draft") or {}
        attachments = {str(item["document_id"]): item for item in content.get("attachments") or [] if isinstance(item, dict) and item.get("document_id")}
        chunks = [chunk.model_copy(update={"metadata": {**chunk.metadata, "chunks_are_final": request.chunks_are_final or bool(chunk.metadata.get("chunks_are_final"))}})
                  for chunk in request.chunks if chunk.chunk_role in {"admin_draft", "agent_draft"}]
        reference["documents"] = []
        for document_id in sorted(document_ids):
            document = documents_by_id.get(document_id) or {}
            version = document.get("active_version") or {}
            attachment = attachments[document_id]
            version_id = str(attachment.get("version_id") or "")
            reason = invalid_documents.get(document_id)
            if not reason and (
                version_id != str(version.get("version_id") or "")
                or document.get("user_id") != request.identity.user_id
                or document.get("scope_id") != request.scope_id
                or document.get("knowledge_type") != request.knowledge_type
            ):
                reason = f"文档 {document_id} 的草稿版本或归属与当前发布来源不符"
            if reason:
                skip("attachment", document_id, reason, draft_id=reference["draft_id"], version_id=version_id)
                continue
            source_chunks = [chunk for chunk in request.chunks if chunk.document_id == document_id]
            if request.chunks_are_final:
                source_chunks = [chunk.model_copy(update={"metadata": {**chunk.metadata, "chunks_are_final": True}}) for chunk in source_chunks]
            if not source_chunks:
                try:
                    confirmed = await load_confirmed_version_request(storage, document, version)
                    source_chunks = [chunk.model_copy(update={
                        "source_id": request.chunks[0].source_id,
                        "chunk_role": "admin_doc" if request.chunks[0].chunk_role == "admin_draft" else "agent_doc",
                        "document_id": document_id,
                        "mysql_id": None,
                        "metadata": {**chunk.metadata, "chunks_are_final": True, "version_id": version_id},
                    }) for chunk in confirmed.chunks if chunk.chunk_role in {"admin_doc", "agent_doc"}]
                except InvalidRecoverySourceError as exc:
                    skip("attachment_source", document_id, str(exc), version_id=version_id)
                    continue
            if not source_chunks:
                skip("attachment_source", document_id, "附件没有可恢复的原文分块", version_id=version_id)
                continue
            key = (str(request.chunks[0].source_id or ""), document_id, version_id)
            source_group_draft_ids.setdefault(key, set()).add(str(reference["draft_id"]))
            source_groups.setdefault(key, (reference.get("updated_at"), source_chunks))
            prior_time, prior_chunks = source_groups[key]
            if str(reference.get("updated_at") or "") > str(prior_time or ""):
                source_groups[key] = (reference.get("updated_at"), source_chunks)
            elif (
                str(reference.get("updated_at") or "") == str(prior_time or "")
                and not same_source_chunks(source_chunks, prior_chunks)
            ):
                conflicted_source_groups.add(key)
                skip(
                    "attachment_source",
                    document_id,
                    "同一附件版本在多个已发布草稿中存在不同分块，未猜测选用内容",
                    document_id=document_id,
                    version_id=version_id,
                    draft_ids=sorted(source_group_draft_ids[key]),
                    mysql_ids=sorted({
                        str(chunk.mysql_id)
                        for item_request, *_ in draft_candidates
                        if (str(item_request.chunks[0].source_id or ""), document_id, version_id) == key
                        for chunk in item_request.chunks
                        if chunk.chunk_role in {"admin_draft", "agent_draft"} and chunk.mysql_id
                    }),
                )
            chunks.extend(source_chunks)
            reference["documents"].append({"document_id": document_id, "version_id": version_id})
        request = request.model_copy(update={"chunks": chunks, "chunks_are_final": all(chunk.metadata.get("chunks_are_final") for chunk in chunks)})
        requests.append(canonicalize_recovery_request(request))
        references.append(reference)
    for reference in references:
        reference["documents"] = [
            attached
            for attached in reference["documents"]
            if (
                str(reference.get("source_id") or ""),
                attached["document_id"],
                attached["version_id"],
            ) not in conflicted_source_groups
        ]
    written_sources: set[tuple[str, str, str]] = set()
    for index, request in enumerate(requests):
        chunks = [chunk for chunk in request.chunks if chunk.chunk_role in {"admin_draft", "agent_draft"}]
        for attached in references[index]["documents"]:
            key = (str(request.chunks[0].source_id or ""), attached["document_id"], attached["version_id"])
            if key not in written_sources:
                chunks.extend(source_groups[key][1])
                written_sources.add(key)
        requests[index] = request.model_copy(update={"chunks": chunks, "chunks_are_final": all(chunk.metadata.get("chunks_are_final") for chunk in chunks)})
    requests.extend(document_requests)
    # 重建前发现冲突，避免清空线上集合后才在 MySQL 回灌阶段失败。
    published_source_ids_by_mysql_id(requests)
    references.extend(document_references)
    manifests = {}
    referenced_images: dict[str, set[str]] = {}
    for request in requests:
        for chunk in request.chunks:
            owner = str(chunk.document_id or request.document_id or "")
            if not owner:
                continue
            metadata = chunk.metadata or {}
            referenced_images.setdefault(owner, set()).update(
                str(image_id).strip() for image_id in metadata.get("image_ids") or []
                if str(image_id).strip()
            )
            referenced_images[owner].update(
                str(item.get("image_id") or "").strip()
                for item in metadata.get("image_refs") or []
                if isinstance(item, dict) and item.get("image_id")
            )
            image_reference_text = re.sub(r"%5d", "]", chunk.chunk_text, flags=re.IGNORECASE)
            referenced_images[owner].update(
                unquote(image_id)
                for image_id in re.findall(
                    rf"/api/v1/perception/images/{re.escape(owner)}/([^/\s)\]]+)",
                    image_reference_text,
                )
            )
    missing_image_ids: dict[str, set[str]] = {}
    for document_id, document in sorted(documents_by_id.items()):
        version = document.get("active_version") or {}
        if version.get("publication_status") != "PUBLISHED":
            continue
        checks = []
        original = str(version.get("storage_key") or document.get("storage_key") or "")
        object_keys = {original} if original else set()
        if not original:
            skip("original_file", document_id, "未保存原附件对象键，原件无法下载", document_id=document_id)
        for value in (version.get("artifact_refs") or {}).values():
            key = value.get("storage_key") if isinstance(value, dict) else value
            if isinstance(key, str) and key.startswith(("derived/", "uploads/")):
                object_keys.add(key)
        images = await storage.load_image_manifest(document_id)
        manifests[document_id] = images
        manifest_key = f"derived/{document_id}/image-manifest.json"
        manifest_object = await storage.inspect_object(manifest_key)
        if manifest_object is None and referenced_images.get(document_id):
            skip(
                "image_manifest", document_id,
                "原文引用图片但 MinIO 图片清单不存在，无法确认图片对应对象",
                document_id=document_id, storage_key=manifest_key,
            )
            missing_image_ids[document_id] = set(referenced_images[document_id])
        listed_image_ids = {str(image.get("image_id") or "") for image in images}
        for image_id in sorted(referenced_images.get(document_id, set()) - listed_image_ids):
            skip(
                "image", image_id, "原文引用的图片 ID 不在 MinIO 图片清单中",
                document_id=document_id, version_id=str(version.get("version_id") or ""),
            )
            missing_image_ids.setdefault(document_id, set()).add(image_id)
        image_ids_by_key = {}
        for image in images:
            key = str(image.get("minio_object_key") or "")
            if key:
                object_keys.add(key)
                image_ids_by_key[key] = str(image.get("image_id") or "")
            else:
                skip("image", str(image.get("image_id") or document_id), "图片清单缺少 MinIO 对象键", document_id=document_id)
                missing_image_ids.setdefault(document_id, set()).add(str(image.get("image_id") or ""))
        available_ids = set()
        for key in sorted(object_keys):
            info = await storage.inspect_object(key)
            checks.append({"storage_key": key, "object": info})
            if info is None:
                image_id = image_ids_by_key.get(key)
                if image_id:
                    missing_image_ids.setdefault(document_id, set()).add(image_id)
                    skip("image", image_id, "图片清单存在但对应 MinIO 图片对象缺失",
                         document_id=document_id, version_id=str(version.get("version_id") or ""), storage_key=key)
                else:
                    skip("original_file" if key == original else "artifact", key,
                         "原附件对象缺失，正文可恢复但原件无法下载" if key == original else "引用的解析产物对象缺失",
                         document_id=document_id, version_id=str(version.get("version_id") or ""), storage_key=key)
            elif key in image_ids_by_key:
                available_ids.add(image_ids_by_key[key])
        manifests[document_id] = [image for image in images if str(image.get("image_id") or "") in available_ids]
        references.append({"document_id": document_id, "version_id": version.get("version_id"), "object_checks": checks})
    normalized_requests = []
    for request in requests:
        chunks = []
        for chunk in request.chunks:
            document_id = str(chunk.document_id or request.document_id or "")
            missing = missing_image_ids.get(document_id, set())
            chunk_text = _strip_missing_image_urls(chunk.chunk_text, document_id, missing)
            try:
                chunk_text = normalize_image_references(
                    chunk_text, document_id=document_id, manifests=manifests
                )
            except ValueError as exc:
                chunk_text, unresolved = _strip_unmapped_derived_images(chunk_text, document_id)
                for reference in unresolved:
                    skip("image", reference, str(exc), document_id=document_id)
                if not unresolved:
                    raise
            chunks.append(chunk.model_copy(update={
                "chunk_text": chunk_text,
                "metadata": _drop_missing_image_metadata(dict(chunk.metadata or {}), document_id, missing),
            }))
        normalized_requests.append(request.model_copy(update={"chunks": chunks}))
    requests = normalized_requests
    references.extend(
        {"unpublished_mysql_id": mysql_id}
        for mysql_id in sorted(unpublished_mysql_ids - published_mysql_ids)
    )
    for item in skipped:
        document_id = str(item.get("document_id") or "")
        if not document_id:
            continue
        related_sources = related_sources_by_document.get(document_id, [])
        document_name = str(
            (documents_by_id.get(document_id) or {}).get("source_name")
            or (documents_by_id.get(document_id) or {}).get("file_name")
            or ""
        )
        item["related_sources"] = [
            {**source, "attachment_name": source["attachment_name"] or document_name}
            for source in related_sources
        ]
        if not item["related_sources"] and document_name:
            item["attachment_name"] = document_name
    skipped.sort(key=lambda item: (item["source_type"], item["source_id"]))
    return requests, references, skipped
