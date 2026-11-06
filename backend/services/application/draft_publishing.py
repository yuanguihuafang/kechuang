"""草稿发布应用服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import unquote

from fastapi import HTTPException
from pydantic import ValidationError

from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
from core.contracts.business_records import ConfirmedBusinessRecordRequest
from core.contracts.draft_contracts import draft_field_length_errors
from core.contracts.draft_records import DRAFT_EXCLUDED_FIELDS, materialize_draft_record
from core.contracts.knowledge import (
    VALID_KNOWLEDGE_TYPES,
    CleanedKnowledgeRecord,
    DocumentKnowledgeRequest,
)
from core.contracts.mysql_schema import (
    form_contract,
    mysql_fields_to_text,
    mysql_record_locator_with_attachments,
)
from services.application.business_records import (
    _source_block_records,
    _source_id,
    canonical_source_id,
    source_group_mysql_record_id,
    stable_source_id,
)
from services.application.temporary_materials import has_expired_attachment
from services.persistence.repositories.observability_repository import PostgresAuditSink
from services.persistence.retention import serialize_storage_references
from services.storage.minio.image_service import normalize_image_references

logger = logging.getLogger(__name__)


def plan_draft_cleanup(
    drafts: Any,
    targets: list[dict[str, Any]],
    *,
    allow_published_targets: bool = False,
) -> dict[str, Any]:
    """Resolve every shared reference before any destructive side effect."""
    target_ids = {str(item["draft_id"]) for item in targets}
    if not allow_published_targets and any(
        item.get("status") == "PUBLISHED" for item in targets
    ):
        raise ValueError("已发布草稿请先下架，不能直接删除")
    plan = {key: set() for key in ("business_keys", "source_keys", "files", "document_ids")}

    def outside(rows):
        return [row for row in rows if row.get("status") != "DELETED"
                and str(row["draft_id"]) not in target_ids]

    for item in targets:
        draft = item.get("draft") or {}
        draft_type = str(item.get("draft_type") or draft.get("draft_type") or "")
        owner = str(item.get("user_id") or "") if draft_type == "customer_visit" else None
        mysql_id = str(draft.get("mysql_record_id") or "").strip()
        if mysql_id and not outside(drafts.list_business_references(draft_type, mysql_id, user_id=owner)):
            plan["business_keys"].add((draft_type, mysql_id, owner))
        attachments = [row for row in draft.get("attachments") or [] if isinstance(row, dict)]
        source_id = canonical_source_id(draft.get("source_id")) or _source_id(attachments)
        document_ids = {str(row["document_id"]) for row in attachments if row.get("document_id")}
        if item.get("source_document_id"):
            document_ids.add(str(item["source_document_id"]))
        other_sources = []
        for document_id in document_ids:
            references = outside(drafts.list_for_document(document_id))
            other_sources.extend(references)
            if not references:
                plan["document_ids"].add(document_id)
        source_shared = any(
            row.get("draft_type") == draft_type
            and (owner is None or row.get("user_id") == owner)
            and (
                canonical_source_id((row.get("draft") or {}).get("source_id"))
                or _source_id((row.get("draft") or {}).get("attachments") or [])
            ) == source_id
            for row in other_sources
        )
        if source_id and not source_shared:
            plan["source_keys"].add((draft_type, source_id, owner))
        for attachment in attachments:
            key = str(attachment.get("storage_key") or "").strip()
            if key and not outside(drafts.list_storage_key_references(key)):
                plan["files"].add((key, str(attachment.get("document_id") or "") or None))
    return plan


def draft_index_fingerprint(draft: dict[str, Any]) -> str:
    content = {key: draft.get(key) for key in (
        "title", "fields", "mysql_record_id", "source_id", "attachments", "source_blocks",
    )}
    return hashlib.sha256(json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def build_published_draft_index_request(draft_record: dict[str, Any]) -> IndexRequest:
    """仅从 PG 完整已发布草稿构建公开索引请求，不读写任何业务存储。"""
    if draft_record.get("status") != "PUBLISHED":
        raise ValueError("草稿索引恢复仅允许 PUBLISHED 状态")
    record_type = draft_record.get("draft_type")
    if record_type not in VALID_KNOWLEDGE_TYPES:
        raise ValueError("草稿索引恢复仅允许五种公开知识类型")
    draft_id = str(draft_record.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("草稿索引恢复缺少 draft_id")
    draft = draft_record["draft"]
    if not str(draft.get("mysql_record_id") or "").strip():
        raise ValueError("草稿索引恢复缺少 mysql_record_id")
    snapshot = draft.get("_index_snapshot")
    saved = None
    if isinstance(snapshot, dict):
        try:
            candidate = IndexRequest.model_validate(snapshot["request"])
            if (
                candidate.identity.user_id == draft_record["user_id"]
                and candidate.knowledge_type == record_type and candidate.scope_id == "public"
                and len(candidate.chunks) == snapshot.get("indexed_count")
            ):
                saved = candidate
        except (ValidationError, KeyError, TypeError):
            logger.warning("草稿 %s 的最终分块快照无效，按已保存来源和既有策略恢复", draft_id)
    try:
        content = _build_draft_knowledge_payload(
            draft_id,
            draft,
            record_type,
            user_id=draft_record["user_id"],
            user_role=saved.identity.user_role.value if saved else draft_record["user_role"],
        )
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
    request = DocumentKnowledgeRequest(**content)
    attachment = next(
        (
            item
            for item in draft.get("attachments") or []
            if isinstance(item, dict) and item.get("document_id")
        ),
        {},
    )
    document_id = str(attachment.get("document_id") or draft_id)
    built = IndexRequest(
        document_id=document_id,
        chunks=[
            IndexChunk(
                chunk_text=record.text.strip(),
                chunk_index=int(record.metadata.get("chunk_index", index)),
                source_locator=record.source_locator,
                chunk_role=record.metadata["chunk_role"],
                mysql_id=record.mysql_id,
                source_id=record.source_id,
                document_id=(
                    None
                    if record.metadata.get("chunk_role") in {"agent_draft", "admin_draft"}
                    else record.document_id or document_id
                ),
                metadata=record.metadata,
            )
            for index, record in enumerate(request.records)
        ],
        **request.model_dump(exclude={"source_name", "records"}),
    )
    if saved is not None and snapshot.get("source_fingerprint") == draft_index_fingerprint(draft):
        if (
                saved.chunks_are_final
                and len(saved.chunks) == snapshot.get("indexed_count")
                and saved.document_id == built.document_id
                and saved.version_id == built.version_id
                and saved.knowledge_type == built.knowledge_type
                and saved.identity == built.identity
                and saved.scope_id == built.scope_id
                and {chunk.mysql_id for chunk in saved.chunks if chunk.chunk_role in {"admin_draft", "agent_draft"}}
                    == {str(draft["mysql_record_id"])}
                and {chunk.document_id for chunk in saved.chunks if chunk.chunk_role in {"admin_doc", "agent_doc"}}
                    == {chunk.document_id for chunk in built.chunks if chunk.chunk_role in {"admin_doc", "agent_doc"}}
                and all(chunk.chunk_text.strip() and chunk.source_id == built.chunks[0].source_id for chunk in saved.chunks)
            ):
            return saved
    return built


def _build_draft_knowledge_payload(
    draft_id: str,
    draft: dict[str, Any],
    record_type: str,
    *,
    user_id: str,
    user_role: str,
) -> dict[str, Any]:
    """正常发布与已发布草稿恢复共用的纯内容构建。"""
    attachments = [item for item in draft.get("attachments") or [] if isinstance(item, dict)]
    attachment = next((item for item in attachments if item.get("document_id")), {})
    attachment_ids = sorted(
        {str(item.get("document_id")) for item in attachments if item.get("document_id")}
    )
    source_name = str(attachment.get("file_name") or draft.get("title") or draft_id)
    mysql_record_id = str(draft.get("mysql_record_id") or "").strip()
    source_id = canonical_source_id(draft.get("source_id")) or (
        _source_id(attachments) if attachment_ids else stable_source_id(mysql_record_id)
    )
    contract = form_contract(record_type)
    public_fields = set(contract.get("public_fields") or ())
    system_fields = set(contract.get("system_fields") or ())
    excluded_fields = DRAFT_EXCLUDED_FIELDS.get(record_type, frozenset())
    draft_lines = []
    draft_field_names = []
    draft_fields = dict(draft.get("fields") or {})
    if draft.get("title"):
        draft_fields["title"] = draft["title"]
    for field_name, value in draft_fields.items():
        if (
            value is None
            or not str(value).strip()
            or field_name in system_fields
            or field_name in excluded_fields
        ):
            continue
        if user_role != "admin" and public_fields and field_name not in public_fields:
            continue
        draft_lines.append(mysql_fields_to_text(record_type, {field_name: str(value).strip()}))
        draft_field_names.append(field_name)
    if not draft_lines:
        raise HTTPException(status_code=409, detail="草稿没有可发布的有效字段")
    draft_chunk_role = "admin_draft" if user_role == "admin" else "agent_draft"
    document_chunk_role = "admin_doc" if user_role == "admin" else "agent_doc"
    records = [
        CleanedKnowledgeRecord(
            record_id=f"{draft_id}:draft",
            mysql_id=mysql_record_id or None,
            source_id=source_id,
            document_id=None,
            text="\n".join(draft_lines),
            source_locator=mysql_record_locator_with_attachments(
                record_type, mysql_record_id or draft_id, attachments
            ),
            metadata={
                "draft_id": draft_id,
                "field_names": draft_field_names,
                "chunk_role": draft_chunk_role,
            },
        )
    ]
    try:
        records.extend(_source_block_records(
            draft.get("source_blocks") or [], attachments, draft_id, source_id,
            chunk_role=document_chunk_role,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "source_name": source_name,
        "version_id": str(attachment.get("version_id") or "") or None,
        "scope_id": "public",
        "knowledge_type": record_type,
        "records": records,
        "identity": {"user_id": user_id, "user_role": user_role},
        "replace_existing": True,
        # 解析块不是最终索引块，由 Retrieval 按统一策略再次分块。
        "chunks_are_final": False,
    }


async def _preflight_attachment_storage(
    attachments: list[dict[str, Any]], versions: dict[str, dict[str, Any]], storage: Any
) -> dict[str, list[dict[str, Any]]]:
    if not attachments:
        return {}
    if storage is None:
        raise HTTPException(status_code=503, detail="附件发布预检不可用：MinIO 存储服务未就绪")
    await storage.check_ready()
    manifests: dict[str, list[dict[str, Any]]] = {}
    for attachment in attachments:
        document_id = str(attachment.get("document_id") or "")
        version = versions.get(document_id) or {}
        storage_key = str(version.get("storage_key") or attachment.get("storage_key") or "")
        if not storage_key or await storage.inspect_object(storage_key) is None:
            raise HTTPException(
                status_code=409,
                detail=f"附件 {document_id or '未知文档'} 的原件在 MinIO 中不可读取，已阻止发布",
            )
        for value in (version.get("artifact_refs") or {}).values():
            key = value.get("storage_key") if isinstance(value, dict) else value
            if isinstance(key, str) and key.startswith(("derived/", "uploads/")):
                if await storage.inspect_object(key) is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"附件 {document_id} 的解析产物缺失：{key}",
                    )
        manifests[document_id] = await storage.load_image_manifest(document_id)
    return manifests


async def _normalize_and_preflight_images(
    request: DocumentKnowledgeRequest,
    manifests: dict[str, list[dict[str, Any]]],
    storage: Any,
    default_document_id: str,
) -> DocumentKnowledgeRequest:
    image_ids_by_document: dict[str, set[str]] = {}
    normalized_records = []
    for record in request.records:
        explicit_document_id = str(record.document_id or record.metadata.get("document_id") or "")
        if explicit_document_id and explicit_document_id not in manifests:
            raise HTTPException(
                status_code=409,
                detail=f"索引内容引用了未关联附件：{explicit_document_id}",
            )
        document_id = explicit_document_id or default_document_id
        metadata_image_ids = record.metadata.get("image_ids") or []
        if isinstance(metadata_image_ids, list):
            image_ids_by_document.setdefault(document_id, set()).update(
                str(image_id).strip() for image_id in metadata_image_ids
                if str(image_id).strip()
            )
        text = record.text
        if text and document_id:
            try:
                text = normalize_image_references(
                    text, document_id=document_id, manifests=manifests
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=f"发布图片引用无效：{exc}") from exc
            for match in re.finditer(
                r"!?\[[^\]]*\]\((?P<target>[^)]+)\)|(?P<url>/api/v1/perception/images/[^\s)\]]+)",
                text,
            ):
                target = str(match.group("target") or match.group("url") or "").strip("<>")
                if target.startswith(("http://", "https://")):
                    continue
                if target.startswith("/api/v1/perception/images/"):
                    parts = target.removeprefix("/api/v1/perception/images/").split("/")
                    if len(parts) != 2 or unquote(parts[0]) != document_id:
                        raise HTTPException(
                            status_code=409,
                            detail=f"附件 {document_id} 引用了其他文档的图片：{target}",
                        )
                    continue
                if re.search(r"(?:^|/)(?:derived|images)/", target):
                    raise HTTPException(
                        status_code=409,
                        detail=f"附件 {document_id} 存在未映射的图片引用：{target}",
                    )
            image_ids_by_document.setdefault(document_id, set()).update(
                unquote(match.group(2))
                for match in re.finditer(
                    r"/api/v1/perception/images/([^/]+)/([^/?#)\s\]]+)", text
                )
                if unquote(match.group(1)) == document_id
            )
        normalized_records.append(record.model_copy(update={"text": text}))

    for document_id, image_ids in image_ids_by_document.items():
        images = manifests.get(document_id, [])
        by_id = {str(item.get("image_id") or ""): item for item in images}
        for image_id in image_ids:
            image = by_id.get(image_id)
            object_key = str((image or {}).get("minio_object_key") or "")
            if not object_key or await storage.inspect_object(object_key) is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"附件 {document_id} 引用的图片 {image_id} 在 MinIO 中不可读取，已阻止发布",
                )
    return request.model_copy(update={"records": normalized_records})


class DraftPublishingApplicationService:
    def __init__(self, runtime: Any, resolve_effective_identity: Any) -> None:
        self.runtime = runtime
        self.resolve_effective_identity = resolve_effective_identity

    @serialize_storage_references
    async def publish(self, draft_id: str, payload: Any) -> dict[str, object]:
        user_id, effective_role, _profile = self.resolve_effective_identity(payload.user_id)
        if effective_role not in {"internal", "external", "admin"}:
            raise HTTPException(status_code=403, detail="当前身份无权提交公开草稿")
        store = self.runtime.require_draft_store()
        draft_record = store.get(draft_id, user_id=user_id)
        if draft_record is None:
            raise HTTPException(status_code=404, detail="草稿不存在或不属于当前用户")
        if draft_record.get("draft_type") == "customer_visit":
            raise HTTPException(
                status_code=409, detail="拜访记录不对外发布，请使用拜访记录保存工具"
            )
        if draft_record.get("status") not in {"CONFIRMED", "PENDING_REVIEW", "WITHDRAWN"}:
            raise HTTPException(status_code=409, detail="当前草稿状态不能发布")
        draft = dict(draft_record.get("draft") or {})
        overlong_fields = draft_field_length_errors(draft)
        if overlong_fields:
            raise HTTPException(
                status_code=422,
                detail=(
                    "字段内容过长，人工编辑后单个字段最多 3060 字：" + "、".join(overlong_fields)
                ),
            )
        # 该标记只控制一次“编辑后重新上架”；不要把内部控制字段写入 MySQL 或向量内容。
        edited_for_republish = bool(draft.pop("_needs_mysql_sync_on_republish", False))
        mysql_clear_fields = list(draft.pop("_explicit_mysql_field_clears", []) or [])
        if has_expired_attachment(draft):
            raise HTTPException(status_code=410, detail="草稿及文档已过期，请重新上传文档")
        materialized = materialize_draft_record(
            draft_id, draft, include_internal=effective_role == "admin"
        )
        attachments = [item for item in draft.get("attachments") or [] if isinstance(item, dict)]
        attachment = next((item for item in attachments if item.get("document_id")), {})
        attachment_id = str(attachment.get("document_id") or "")
        lifecycle = self.runtime.require_lifecycle() if attachments else None
        attachment_versions: dict[str, int | str] = {}
        attachment_version_records: dict[str, dict[str, Any]] = {}
        for attached in attachments:
            document_id = str(attached.get("document_id") or "")
            if not document_id:
                continue
            version_id = str(attached.get("version_id") or "")
            document = lifecycle.get_document(document_id) if document_id and lifecycle else None
            if document and not document.get("knowledge_type"):
                try:
                    lifecycle.bind_draft_attachments(
                        [attached], user_id=user_id, knowledge_type=draft_record["draft_type"],
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                document = lifecycle.get_document(document_id)
            version = lifecycle.get_version(version_id) if version_id and lifecycle else None
            active_version = (document or {}).get("active_version") or {}
            if (
                not document or not version
                or str(version.get("document_id") or "") != document_id
                or str(active_version.get("version_id") or "") != version_id
                or document.get("user_id") != user_id
                or document.get("scope_id") != "public"
                or document.get("knowledge_type") != draft_record.get("draft_type")
                or (
                    attached.get("storage_key")
                    and str(attached["storage_key"]) != str(version.get("storage_key") or "")
                )
                or str(version.get("status") or "") in {"RECYCLED", "PURGED"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"附件 {document_id or '未知文档'} 的归属或版本已变化，请重新选择附件",
                )
            attachment_versions[version_id] = version.get("version")
            attachment_version_records[document_id] = version
        storage = getattr(self.runtime, "storage_service", None)
        manifests = await _preflight_attachment_storage(
            attachments, attachment_version_records, storage
        )
        try:
            draft_input_fields = {
                key: normalize_image_references(value, document_id=attachment_id, manifests=manifests)
                if isinstance(value, str) else value
                for key, value in (draft.get("fields") or {}).items()
            }
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"草稿图片引用无效：{exc}") from exc
        draft = {**draft, "fields": draft_input_fields}
        fields = {
            key: value
            for key, value in draft_input_fields.items()
            if value is not None and str(value).strip()
        }
        if draft.get("title"):
            fields["title"] = draft["title"]
        record_type = str(draft_record.get("draft_type") or "solution")
        flow_thread_id = payload.thread_id or draft_record.get("thread_id")
        sink = self.runtime.require_business_record_sink()
        draft_mysql_id = str(draft.get("mysql_record_id") or "").strip()
        source_id = canonical_source_id(draft.get("source_id")) or _source_id(attachments)
        try:
            existing_mysql_id = source_group_mysql_record_id(
                store,
                source_id=source_id,
                source_id_values={str(draft.get("source_id") or "").strip()},
                draft_id=draft_id,
                user_id=user_id,
                draft_type=record_type,
                current_mysql_id=draft_mysql_id,
                current_draft=draft,
                allow_source_group_rewrite=effective_role == "admin",
            ) or ""
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        preflight_draft = {
            **draft,
            "mysql_record_id": existing_mysql_id or draft_mysql_id or "publish-preflight",
            "source_id": source_id,
        }
        index_payload = _build_draft_knowledge_payload(
            draft_id, preflight_draft, record_type,
            user_id=user_id, user_role=effective_role,
        )
        index_payload["replace_existing"] = payload.replace_existing
        preflight_source_texts: dict[str, str] = {}
        try:
            preflight_request = DocumentKnowledgeRequest(**index_payload)
            preflight_request = await _normalize_and_preflight_images(
                preflight_request, manifests, storage, attachment_id
            )
            preflight_source_texts = {
                record.record_id: record.text
                for record in preflight_request.records
                if record.document_id
            }
            await self.runtime.embedded_retrieval.agent.preflight_publish_document(
                attachment_id or draft_id, preflight_request
            )
        except HTTPException:
            raise
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"发布数据校验失败，未写入 MySQL 或 Milvus：{exc}"
            ) from exc
        except Exception as exc:
            logger.exception("草稿发布预检失败 draft_id=%s", draft_id)
            raise HTTPException(
                status_code=503,
                detail=f"发布预检失败，未写入 MySQL 或 Milvus：{exc}",
            ) from exc
        if edited_for_republish:
            draft_record = (
                store.update(
                    draft_id, user_id=user_id, draft=draft, status=draft_record.get("status")
                )
                or draft_record
            )
        self._mark_chat_flow_stage(
            flow_thread_id,
            draft_id,
            "PUBLISHING",
            draft,
            user_id=user_id,
            user_role=effective_role,
        )
        if existing_mysql_id:
            mysql_record = sink.update(
                record_type=record_type,
                record_id=existing_mysql_id,
                structured_data=fields,
                clear_fields=mysql_clear_fields,
            )
        else:
            mysql_record = sink.save(
                ConfirmedBusinessRecordRequest(
                    user_id=user_id,
                    record_type=record_type,
                    confirmed=True,
                    confirmed_by=user_id,
                    draft_id=draft_id,
                    idempotency_key=(payload.idempotency_key or f"draft:{record_type}:{draft_id}"),
                    structured_data=fields,
                )
            )
        mysql_record_id = str(mysql_record.get("business_record_id") or "").strip()
        if not mysql_record_id:
            raise HTTPException(status_code=502, detail="业务记录保存成功但未返回 mysql_id")
        if source_id:
            source_id = canonical_source_id(source_id)
        elif existing_mysql_id and attachments:
            # 优先从旧草稿 chunk 读取实际关联值；无命中时再用旧附件哈希兼容历史数据。
            reader = getattr(self.runtime.embedded_retrieval.agent, "draft_source_ids", None)
            legacy_ids = (
                await asyncio.to_thread(
                    reader,
                    mysql_record_id,
                    record_type,
                    "admin_draft" if effective_role == "admin" else "agent_draft",
                )
                if callable(reader)
                else []
            )
            if len(legacy_ids) > 1:
                raise HTTPException(
                    status_code=409,
                    detail="该草稿存在多个历史 source_id，已停止更新以避免误删关联文档",
                )
            source_id = canonical_source_id(legacy_ids[0]) if legacy_ids else _source_id(attachments)
        else:
            source_id = stable_source_id(mysql_record_id)
        updated_draft = {
            **draft,
            "mysql_record_id": mysql_record_id,
            "source_id": source_id,
        }
        if (
            mysql_record_id != draft_mysql_id
            or draft.get("source_id") != source_id
            or (draft_record.get("draft") or {}).get("fields") != draft_input_fields
        ):
            status = "CONFIRMED" if mysql_record_id != draft_mysql_id else None
            stored = store.update(
                draft_id, user_id=user_id, draft=updated_draft, status=status
            )
            if stored is None:
                raise HTTPException(status_code=409, detail="草稿关联信息保存失败，尚未写入索引")
            draft_record = stored
            draft = dict(stored.get("draft") or updated_draft)
        index_payload = _build_draft_knowledge_payload(
            draft_id,
            {**draft, "mysql_record_id": mysql_record_id},
            record_type,
            user_id=user_id,
            user_role=effective_role,
        )
        index_payload["records"] = [
            record.model_copy(update={"text": preflight_source_texts[record.record_id]})
            if record.record_id in preflight_source_texts else record
            for record in index_payload["records"]
        ]
        index_payload["replace_existing"] = payload.replace_existing
        try:
            publish_request = DocumentKnowledgeRequest(**index_payload)
            index_result = await self.runtime.embedded_retrieval.agent.publish_document(
                attachment_id or draft_id,
                publish_request,
            )
        except Exception:
            self._mark_chat_flow_stage(
                flow_thread_id,
                draft_id,
                "PUBLISH_FAILED",
                draft,
                user_id=user_id,
                user_role=effective_role,
            )
            raise
        if index_result.status != "SUCCESS":
            error = str(index_result.error or "")
            logger.error(
                "草稿发布索引失败 draft_id=%s document_id=%s error=%s",
                draft_id,
                attachment_id or draft_id,
                error,
            )
            self._mark_chat_flow_stage(
                flow_thread_id,
                draft_id,
                "PUBLISH_FAILED",
                draft,
                user_id=user_id,
                user_role=effective_role,
            )
            raise HTTPException(
                status_code=502,
                detail=_safe_index_failure_detail(error),
            )
        final_chunks = getattr(index_result, "indexed_chunks", [])
        if final_chunks:
            saved_request = IndexRequest(
                document_id=attachment_id or draft_id,
                chunks=final_chunks,
                **publish_request.model_dump(exclude={"source_name", "records", "chunks_are_final"}),
                chunks_are_final=True,
            )
            stored = store.update(
                draft_id, user_id=user_id, expected_version=draft_record.get("version"),
                draft={"_index_snapshot": {
                    "source_fingerprint": draft_index_fingerprint(draft),
                    "indexed_count": len(final_chunks),
                    "request": saved_request.model_dump(mode="json"),
                }},
            )
            if stored is None:
                raise RuntimeError("已发布分块快照保存失败，尚未确认发布")
            draft_record = stored
        for attachment in attachments:
            attachment_version_id = str(attachment.get("version_id") or "").strip()
            if not attachment.get("document_id") or not attachment_version_id:
                continue
            attachment_count = sum(chunk.document_id == attachment.get("document_id") for chunk in final_chunks)
            updated_version = lifecycle.update_version(
                attachment_version_id,
                status="INDEXED",
                publication_status="PUBLISHED",
                index_status="INDEXED",
                chunk_count=attachment_count,
                indexed_count=attachment_count,
                error=None,
                unpublish_reason_code=None,
                unpublish_reason_text=None,
                unpublished_by=None,
                unpublished_at=None,
                expected_version=attachment_versions[attachment_version_id],
            )
            if updated_version is None:
                raise RuntimeError("关联文档版本状态更新失败")
        self._mark_chat_flow_stage(
            flow_thread_id,
            draft_id,
            "INDEXED",
            draft,
            user_id=user_id,
            user_role=effective_role,
        )
        published = store.transition(
            draft_id,
            user_id=user_id,
            target_status="PUBLISHED",
            operator_id=user_id,
            operator_role=effective_role,
            note="用户确认后直接建立公开知识索引",
            expected_version=draft_record.get("version"),
        )
        self._mark_chat_flow_published(
            flow_thread_id,
            draft_id,
            published or draft_record.get("draft") or draft,
            user_id=user_id,
            user_role=effective_role,
        )
        try:
            PostgresAuditSink().record(
                "user_draft_published",
                service="governance",
                user_id=user_id,
                user_role=effective_role,
                draft_id=draft_id,
                draft_title=draft.get("title"),
                document_id=attachment_id or draft_id,
                knowledge_type=draft_record.get("draft_type"),
                indexed_count=index_result.indexed_count,
                summary="用户确认草稿并建立公开知识索引",
            )
        except Exception as exc:  # noqa: BLE001 - 审计故障不得回滚已完成发布
            logger.warning("草稿发布审计失败: %s", exc)
        published_draft = published or draft_record.get("draft") or draft
        is_demand = record_type == "demand"
        match_knowledge_type = "solution" if is_demand else "demand"
        match_message = "请立即进行方案匹配。" if is_demand else "请立即匹配相关需求。"
        next_step = {
            "status": "MATCH_CONFIRM_REQUIRED",
            "answer": (
                "当前需求草稿已经发布到知识库，是否现在立即进行方案匹配？"
                if is_demand
                else "当前技术方案或科研成果草稿已经发布到知识库，是否现在立即匹配相关需求？"
            ),
            "intent": {"name": "supply_demand_match", "confirmed": False},
            "draft_id": draft_id,
            "draft": published_draft,
            "ui_blocks": [
                {
                    "type": "notice",
                    "data": {"title": "可以开始供需匹配", "message": "请确认后再检索知识库。"},
                }
            ],
            "actions": [
                {
                    "id": "start_matching",
                    "type": "START_MATCHING",
                    "label": "立即进行方案匹配" if is_demand else "立即匹配相关需求",
                    "message": match_message,
                    "enabled": True,
                    "draft_id": draft_id,
                    "draft": published_draft,
                    "knowledge_type": match_knowledge_type,
                    "request": {
                        "intent_hint": "supply_demand_match",
                        "intent_confirmation": "confirmed",
                        "draft_context": {
                            "draft_id": draft_id,
                            "draft_type": record_type,
                            "text": materialized.text,
                            "fields": draft.get("fields") or {},
                            "mysql_fields": draft.get("mysql_fields") or {},
                        },
                    },
                }
            ],
        }
        return {
            "draft": published,
            "index_result": index_result.model_dump(),
            "idempotency_key": payload.idempotency_key or f"draft:{record_type}:{draft_id}",
            "next_step": next_step,
        }

    def _mark_chat_flow_published(
        self,
        thread_id: str | None,
        draft_id: str,
        draft: dict[str, Any],
        *,
        user_id: str | None = None,
        user_role: str | None = None,
    ) -> None:
        """把正式发布状态同步到会话，阻止后续附件补充到已发布草稿。"""

        if not thread_id:
            return
        try:
            supervisor = self.runtime.require_conversation()
            session_store = getattr(supervisor, "session_store", None)
            if session_store is None:
                return
            session = session_store.get(thread_id) or {}
            state = dict(session.get("state") or {})
            flow = dict(state.get("chat_flow") or {})
            flow["last_status"] = "PUBLISHED"
            flow["stage"] = "PUBLISHED"
            flow["pending_action"] = ""
            flow["active_draft_id"] = draft_id
            flow["active_draft"] = {
                **dict(draft or {}),
                "draft_id": draft_id,
                "status": "PUBLISHED",
            }
            session_store.upsert(
                thread_id,
                user_id=user_id,
                user_role=user_role,
                state={"chat_flow": flow},
            )
        except Exception as exc:  # noqa: BLE001 - 状态同步失败不回滚已完成发布
            logger.warning(
                "发布状态同步到会话失败 thread_id=%s draft_id=%s error=%s", thread_id, draft_id, exc
            )

    def _mark_chat_flow_stage(
        self,
        thread_id: str | None,
        draft_id: str,
        stage: str,
        draft: dict[str, Any],
        *,
        user_id: str | None = None,
        user_role: str | None = None,
    ) -> None:
        """记录发布节点进度，供会话恢复和重试使用。"""

        if not thread_id:
            return
        try:
            supervisor = self.runtime.require_conversation()
            session_store = getattr(supervisor, "session_store", None)
            if session_store is None:
                return
            session = session_store.get(thread_id) or {}
            state = dict(session.get("state") or {})
            flow = dict(state.get("chat_flow") or {})
            flow["stage"] = stage
            flow["pending_action"] = (
                "retry_publish" if stage == "PUBLISH_FAILED" else "publish_draft"
            )
            flow["active_draft_id"] = draft_id
            flow["active_draft"] = {**dict(draft or {}), "draft_id": draft_id}
            session_store.upsert(
                thread_id,
                user_id=user_id,
                user_role=user_role,
                state={"chat_flow": flow},
            )
        except Exception as exc:  # noqa: BLE001 - 状态同步失败不影响发布本身
            logger.warning(
                "发布阶段同步到会话失败 thread_id=%s draft_id=%s stage=%s error=%s",
                thread_id,
                draft_id,
                stage,
                exc,
            )


def _safe_index_failure_detail(error: str) -> dict[str, str]:
    """把索引失败转换成前端可行动的提示，不把底层异常原文返回给用户。"""
    lowered = error.lower()
    if "schema" in lowered or "collection" in lowered:
        return {
            "message": "草稿已写入 MySQL，但 Milvus 未写入",
            "error_code": "MILVUS_SCHEMA_MISMATCH",
            "action": "请管理员重建对应知识库的 Milvus Collection 后重试发布",
        }
    if "embedding" in lowered or "vector" in lowered:
        return {
            "message": "草稿已写入 MySQL，但向量生成失败",
            "error_code": "EMBEDDING_FAILED",
            "action": "请检查 embedding 服务后重试发布",
        }
    return {
        "message": "草稿已写入 MySQL，但 Milvus 未写入",
        "error_code": "MILVUS_INDEX_FAILED",
        "action": "请检查 Milvus 服务状态后重试发布",
    }
