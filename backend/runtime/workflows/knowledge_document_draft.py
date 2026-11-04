"""需求、技术方案和科研成果的文档草稿流程。"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any, ClassVar

from agents.perception_agent import PerceptionAgent
from core.context_window import POLICY
from core.contracts.flows import required_field_pairs
from services.application.business_records import canonical_source_id
from services.application.draft_builder import build_draft
from services.persistence.retention import storage_reference_guard


def _source_batch_id(task: dict[str, Any]) -> str:
    explicit = canonical_source_id(task.get("source_id"))
    if explicit:
        return explicit
    task_id = str(task.get("task_id") or "").strip()
    batch_key = task_id or uuid.uuid4().hex
    digest = hashlib.sha256(batch_key.encode("utf-8")).hexdigest()[:32]
    return f"source:{digest}"


def persist_drafts(
    draft_store: Any | None,
    task: dict[str, Any],
    enriched: dict[str, Any],
    knowledge_type: str,
) -> dict[str, Any]:
    if draft_store is None:
        return {}
    drafts = enriched.get("drafts") if isinstance(enriched, dict) else None
    drafts = drafts or [enriched]
    saved_items = draft_store.create_many(
        user_id=str(task.get("user_id") or ""),
        user_role=str(task.get("user_role") or ""),
        drafts=drafts,
        thread_id=task.get("thread_id"),
    )
    persisted_drafts = [
        {**(item.get("draft") or draft), "draft_id": item.get("draft_id")}
        for item, draft in zip(saved_items, drafts)
    ]
    return {
        "status": "WAITING_DRAFT_CONFIRM",
        "stage": "draft_confirm",
        "progress": 80,
        "draft_id": saved_items[0].get("draft_id"),
        "draft_preview": persisted_drafts[0],
        "draft_ids": [item.get("draft_id") for item in saved_items],
        "draft_previews": persisted_drafts,
        "intent_knowledge_type": knowledge_type,
    }


class KnowledgeDocumentDraftService:
    """从 Perception 原文生成与问答流程一致的业务草稿。"""

    _TYPES: ClassVar[frozenset[str]] = frozenset({"demand", "solution", "achievement"})
    _ROLES: ClassVar[frozenset[str]] = frozenset({"admin", "internal", "external"})

    def __init__(self, draft_store: Any | None = None) -> None:
        self.draft_store = draft_store

    async def build_task_fields(
        self, task: dict[str, Any], result: dict[str, Any], preview_text: str
    ) -> dict[str, Any]:
        if task.get("user_role") not in self._ROLES:
            return {}
        knowledge_type = str(task.get("knowledge_type") or "").strip()
        if knowledge_type not in self._TYPES:
            return {}
        source_text = str(result.get("raw_text") or preview_text)
        draft = build_draft(
            knowledge_type,
            "",
            {
                "text": source_text[: POLICY.draft_text_chars],
                "document_id": task.get("document_id"),
                "version_id": task.get("version_id"),
                "file_name": task.get("file_name"),
                "source_blocks": result.get("blocks") or [],
                "image_candidates": (result.get("metadata") or {}).get("images") or [],
            },
        )
        enriched = await PerceptionAgent.extract_fields(
            draft,
            source_text=source_text,
            allow_multiple=True,
            draft_factory=build_draft,
            required_field_pairs_factory=required_field_pairs,
            extraction_mode=os.getenv(
                "ADMIN_EXTRACTION_MODE"
                if task.get("user_role") == "admin"
                else "DRAFT_EXTRACTION_MODE",
                "full" if task.get("user_role") == "admin" else "draft_only",
            ),
            prompt_profile="admin" if task.get("user_role") == "admin" else "smart_qa",
        )
        source_id = _source_batch_id(task)
        # 字段提取允许拆成多个草稿；提取器重建草稿时要把版本元数据带回去。
        enriched_drafts = enriched.get("drafts") if isinstance(enriched, dict) else None
        for item in enriched_drafts or [enriched]:
            item["source_id"] = source_id
            item["source_blocks"] = [
                {**block, "document_id": task.get("document_id"), "version_id": task.get("version_id")}
                for block in result.get("blocks") or [] if isinstance(block, dict)
            ]
            for attachment in item.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                if task.get("version_id"):
                    attachment["version_id"] = str(task["version_id"])
                if task.get("storage_key"):
                    attachment["storage_key"] = str(task["storage_key"])
        async with storage_reference_guard():
            return persist_drafts(self.draft_store, task, enriched, knowledge_type)
