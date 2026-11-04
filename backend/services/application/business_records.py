"""业务记录确认应用服务。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from core.contracts.draft_records import materialize_draft_record
from core.contracts.knowledge import (
    CleanedKnowledgeRecord,
    DocumentKnowledgeRequest,
    SessionSummaryRequest,
)
from core.contracts.mysql_schema import mysql_record_locator_with_attachments
from services.application.draft_session import sync_session_draft
from services.persistence.retention import serialize_storage_references


def canonical_source_id(value: object) -> str:
    source_id = str(value or "").strip()
    return "source:" + source_id[len("source:batch:") :] if source_id.startswith("source:batch:") else source_id


def _source_id(attachments: list[dict[str, object]]) -> str | None:
    document_ids = sorted(
        {str(item.get("document_id") or "") for item in attachments if item.get("document_id")}
    )
    if not document_ids:
        return None
    digest = hashlib.sha1("|".join(document_ids).encode("utf-8")).hexdigest()[:24]
    return f"source:{digest}"


def stable_source_id(mysql_id: str) -> str:
    """为新业务记录生成不依赖附件的稳定关联 ID。"""
    value = str(mysql_id or "").strip()
    if not value:
        raise ValueError("生成 source_id 需要 mysql_id")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"source:{digest}"


def source_group_mysql_record_id(
    store: Any,
    *,
    source_id: str | None,
    source_id_values: set[str],
    draft_id: str | None,
    user_id: str,
    draft_type: str,
    current_mysql_id: str | None = None,
    current_draft: dict[str, Any] | None = None,
    allow_source_group_rewrite: bool = False,
) -> str | None:
    """Prefer this draft's row; use the source group only for legacy drafts without a link."""
    current_id = str(current_mysql_id or "").strip()
    if current_id:
        return current_id

    mysql_ids: set[str] = set()
    matching_mysql_ids: dict[str, str] = {}
    current_title = str(
        (current_draft or {}).get("title")
        or ((current_draft or {}).get("fields") or {}).get("title")
        or ""
    ).strip().casefold()
    current_document_ids = {
        str(attachment.get("document_id") or "").strip()
        for attachment in (current_draft or {}).get("attachments") or []
        if isinstance(attachment, dict) and attachment.get("document_id")
    }
    if source_id:
        list_for_source_id = getattr(store, "list_for_source_id", None)
        if not callable(list_for_source_id):
            raise RuntimeError("无法按 source_id 核对已有业务记录")
        lookup_ids = {str(value or "").strip() for value in source_id_values} - {""}
        lookup_ids.add(source_id)
        if source_id.startswith("source:"):
            lookup_ids.add(f"source:batch:{source_id[len('source:'):]}")
        source_drafts = {
            str(item.get("draft_id") or ""): item
            for lookup_id in lookup_ids
            for item in list_for_source_id(lookup_id)
            if isinstance(item, dict) and item.get("draft_id")
        }
        list_for_document = getattr(store, "list_for_document", None)
        if callable(list_for_document):
            for attachment in (current_draft or {}).get("attachments") or []:
                document_id = str(attachment.get("document_id") or "") if isinstance(attachment, dict) else ""
                if document_id:
                    source_drafts.update({
                        str(item.get("draft_id") or ""): item
                        for item in list_for_document(document_id)
                        if isinstance(item, dict) and item.get("draft_id")
                    })
        for item in source_drafts.values():
            item_draft = item.get("draft")
            if not isinstance(item_draft, dict):
                continue
            item_attachments = item_draft.get("attachments")
            if not isinstance(item_attachments, list):
                item_attachments = []
            item_source_id = canonical_source_id(item_draft.get("source_id")) or _source_id(
                [attachment for attachment in item_attachments if isinstance(attachment, dict)]
            )
            if (
                item.get("draft_id") != draft_id
                and item.get("user_id") == user_id
                and item.get("draft_type") == draft_type
                and item.get("status") != "DELETED"
                and (
                    item_source_id == source_id
                    or bool(
                        current_document_ids
                        & (
                            {
                                str(attachment.get("document_id") or "").strip()
                                for attachment in item_attachments
                                if isinstance(attachment, dict) and attachment.get("document_id")
                            }
                            | {str(item.get("source_document_id") or "").strip()}
                        )
                    )
                )
            ):
                mysql_id = str(item_draft.get("mysql_record_id") or "").strip()
                if mysql_id:
                    mysql_ids.add(mysql_id)
                    item_title = str(
                        item_draft.get("title")
                        or (item_draft.get("fields") or {}).get("title")
                        or ""
                    ).strip().casefold()
                    if current_title and item_title == current_title:
                        matching_mysql_ids[mysql_id] = json.dumps(
                            {
                                "title": item_draft.get("title"),
                                "fields": item_draft.get("fields") or {},
                                "mysql_fields": item_draft.get("mysql_fields") or {},
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        )
    if len(matching_mysql_ids) == 1:
        return next(iter(matching_mysql_ids))
    if len(matching_mysql_ids) > 1:
        if len(set(matching_mysql_ids.values())) == 1:
            return sorted(matching_mysql_ids)[0]
        if allow_source_group_rewrite:
            return None
        raise ValueError("同一 source_id 下有多个同名 MySQL 草稿，无法安全确定对应记录")
    if len(mysql_ids) > 1:
        if allow_source_group_rewrite:
            return None
        raise ValueError("同一 source_id 关联多个 MySQL 记录，已停止写入")
    return next(iter(mysql_ids), None)


def _persist_source_link(
    runtime: Any, *, draft_id: str | None, user_id: str, mysql_id: str, source_id: str
) -> None:
    if not draft_id:
        return
    store = runtime.require_draft_store()
    existing = store.get(draft_id, user_id=user_id)
    if existing is None:
        raise RuntimeError("已发布业务草稿不存在，无法持久化 MySQL/source 关联")
    updated = store.update(
        draft_id,
        user_id=user_id,
        draft={
            **(existing.get("draft") or {}),
            "mysql_record_id": mysql_id,
            "source_id": source_id,
        },
    )
    if updated is None:
        raise RuntimeError("已发布业务草稿关联更新失败")


class BusinessRecordApplicationService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @serialize_storage_references
    async def confirm(self, payload: Any) -> dict[str, object]:
        draft_id = payload.draft_id
        if payload.record_type == "customer_visit" and not draft_id:
            draft = {"draft_type": payload.record_type, **payload.structured_data}
            saved_draft = self.runtime.require_draft_store().create(
                user_id=payload.user_id,
                user_role="internal",
                draft=draft,
                thread_id=payload.thread_id,
            )
            draft_id = saved_draft["draft_id"]
        draft = {"draft_type": payload.record_type, **payload.structured_data}
        existing = None
        source_id = None
        existing_mysql_id = ""
        if payload.record_type == "solution":
            store = self.runtime.require_draft_store()
            existing = (
                store.get(draft_id, user_id=payload.user_id) if draft_id else None
            )
            if existing and existing.get("user_id") != payload.user_id:
                raise ValueError("无权确认其他用户的方案草稿")
            existing_draft = (existing or {}).get("draft") or {}
            if not isinstance(existing_draft, dict):
                existing_draft = {}
            if draft_id and existing is None:
                raise ValueError("方案草稿不存在，无法确认")
            attachments = [
                item
                for item in (draft.get("attachments") or existing_draft.get("attachments") or [])
                if isinstance(item, dict)
            ]
            source_id = canonical_source_id(
                existing_draft.get("source_id") or draft.get("source_id")
            ) or _source_id(attachments)
            existing_mysql_id = source_group_mysql_record_id(
                store,
                source_id=source_id,
                source_id_values={str(existing_draft.get("source_id") or "").strip()},
                draft_id=draft_id,
                user_id=payload.user_id,
                draft_type="solution",
                current_mysql_id=existing_draft.get("mysql_record_id"),
                current_draft=draft,
            ) or ""

        sink = self.runtime.require_business_record_sink()
        saved = (
            sink.update(
                record_type="solution",
                record_id=existing_mysql_id,
                structured_data=payload.structured_data,
            )
            if payload.record_type == "solution" and existing_mysql_id
            else sink.save(payload)
        )
        index_result = None
        if payload.record_type not in {"solution", "customer_visit"}:
            return {"business_record": saved, "index_result": None}

        materialized = materialize_draft_record(
            draft_id or f"mysql-{uuid.uuid4().hex}", draft, include_internal=False
        )
        retrieval = self.runtime.embedded_retrieval
        if payload.record_type == "solution":
            parent_id = payload.draft_id or f"solution-{uuid.uuid4().hex}"
            source_blocks = draft.get("source_blocks") or []
            attachments = [
                item for item in draft.get("attachments") or [] if isinstance(item, dict)
            ]
            attachment = next((item for item in attachments if item.get("document_id")), {})
            source_id = source_id or stable_source_id(str(saved.get("business_record_id") or ""))
            _persist_source_link(
                self.runtime,
                draft_id=payload.draft_id,
                user_id=payload.user_id,
                mysql_id=str(saved.get("business_record_id") or ""),
                source_id=source_id,
            )
            source_name = str(attachment.get("file_name") or draft.get("title") or parent_id)
            records = [
                CleanedKnowledgeRecord.model_validate(
                    {
                        "record_id": parent_id,
                        "mysql_id": saved.get("business_record_id"),
                        "source_id": source_id,
                        "document_id": None,
                        "text": materialized.text,
                        "source_locator": mysql_record_locator_with_attachments(
                            "solution", saved.get("business_record_id") or parent_id, attachments
                        ),
                        "metadata": {
                            "parent_id": parent_id,
                            "chunk_role": "agent_draft",
                            "business_record_id": saved.get("business_record_id"),
                        },
                    }
                )
            ]
            records.extend(
                _source_block_records(
                    source_blocks,
                    attachments,
                    parent_id,
                    source_id,
                )
            )
            index_result = await retrieval.agent.publish_document(
                str(attachment.get("document_id") or f"solution:{parent_id}"),
                DocumentKnowledgeRequest(
                    source_name=source_name,
                    version_id=str(attachment.get("version_id") or "") or None,
                    scope_id="public",
                    knowledge_type="solution",
                    records=records,
                    identity={"user_id": payload.user_id, "user_role": "internal"},
                ),
            )
        else:
            attachment = next(
                (item for item in draft.get("attachments") or [] if item.get("document_id")), {}
            )
            attachments = [
                {
                    "document_id": str(item.get("document_id")),
                    "file_name": str(item.get("file_name") or "附件"),
                }
                for item in draft.get("attachments") or []
                if isinstance(item, dict) and item.get("document_id")
            ]
            source_id = stable_source_id(str(saved.get("business_record_id") or ""))
            index_result = await retrieval.agent.store_session_summary(
                SessionSummaryRequest(
                    session_id=payload.thread_id or materialized.record_id,
                    summary=materialized.text,
                    mysql_id=saved.get("business_record_id"),
                    source_id=source_id,
                    source_document_id=str(attachment.get("document_id") or "") or None,
                    source_name=str(attachment.get("file_name") or "") or None,
                    source_attachments=attachments,
                    identity={"user_id": payload.user_id, "user_role": "internal"},
                )
            )
        if index_result.status != "SUCCESS":
            raise RuntimeError(index_result.error or "私密知识索引失败")
        response: dict[str, object] = {
            "business_record": saved,
            "index_result": index_result.model_dump(),
        }
        if payload.record_type == "customer_visit":
            if not draft_id:
                raise RuntimeError("拜访草稿未生成，无法建立记录关联")
            stored_draft = self.runtime.require_draft_store().update(
                draft_id,
                user_id=payload.user_id,
                draft={
                    **draft,
                    "record_id": draft_id,
                    "mysql_record_id": saved.get("business_record_id"),
                    "source_id": source_id,
                },
                status="CONFIRMED",
                **(
                    {"expected_version": payload.expected_draft_version}
                    if payload.expected_draft_version is not None
                    else {}
                ),
            )
            sync_session_draft(
                self.runtime,
                payload.thread_id,
                {
                    **(stored_draft or {}).get("draft", draft),
                    "draft_id": draft_id,
                    "record_id": draft_id,
                    "mysql_record_id": saved.get("business_record_id"),
                },
                user_id=payload.user_id,
                status="CONFIRMED",
            )
            response["visit_record"] = stored_draft or {
                "record_id": draft_id,
                "draft_id": draft_id,
                "mysql_record_id": saved.get("business_record_id"),
                "draft": draft,
            }
            response["record_id"] = draft_id
            response["draft_id"] = draft_id
        return response


def _source_block_records(
    source_blocks: Any,
    attachments: list[dict[str, Any]],
    parent_id: str,
    source_id: str | None = None,
    *,
    chunk_role: str = "agent_doc",
) -> list[Any]:
    if not isinstance(source_blocks, list):
        return []
    records = []
    attachments_by_id = {str(item["document_id"]): item for item in attachments if item.get("document_id")}
    for index, block in enumerate(source_blocks):
        if not isinstance(block, dict) or not attachments_by_id:
            continue
        text = str(block.get("text") or block.get("chunk_text") or "").strip()
        if not text:
            continue
        metadata = dict(block.get("metadata") or {})
        document_ids = {str(value) for value in (
            block.get("document_id"), block.get("attachment_id"),
            metadata.get("document_id"), metadata.get("attachment_id"),
        ) if value}
        if not document_ids and len(attachments_by_id) == 1:
            document_ids = set(attachments_by_id)
        if len(document_ids) != 1 or not document_ids <= attachments_by_id.keys():
            raise ValueError(f"原文块 {index} 的附件归属不明，不能使用第一份附件代替")
        document_id = next(iter(document_ids))
        attachment = attachments_by_id[document_id]
        version_id = str(attachment.get("version_id") or "")
        versions = {str(value) for value in (block.get("version_id"), metadata.get("version_id")) if value}
        if len(versions) > 1 or (versions and versions != {version_id}):
            raise ValueError(f"附件 {document_id} 原文块版本与当前版本不符")
        source_name = str(attachment.get("file_name") or document_id)
        block_type = block.get("block_type") or block.get("type") or metadata.get("block_type") or "text"
        page = block.get("page_number") or metadata.get("page_number")
        location = f"{source_name}|type:{block_type}|attachment:{document_id}"
        if page is not None:
            location += f"|page:{page}"
        if isinstance(metadata.get("slide_start"), int) and isinstance(
            metadata.get("slide_end"), int
        ):
            location += f"|slides:{metadata['slide_start']}-{metadata['slide_end']}"
        location = str(block.get("source_locator") or location)
        metadata.update(
            {
                "document_id": document_id,
                "version_id": version_id or None,
                "attachment_id": document_id,
                "parent_id": parent_id,
                "draft_id": parent_id,
                "chunk_role": chunk_role,
                "chunks_are_final": bool(metadata.get("chunks_are_final")),
                **({"chunk_index": block["chunk_index"]} if "chunk_index" in block else {}),
            }
        )
        records.append(
            CleanedKnowledgeRecord.model_validate(
                {
                    "record_id": f"{parent_id}:attachment:{document_id}:{index}",
                    "source_id": source_id,
                    "document_id": document_id,
                    "text": text,
                    "source_locator": location,
                    "metadata": metadata,
                }
            )
        )
    return records
