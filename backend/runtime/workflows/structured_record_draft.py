"""企业和专家资料的 MySQL 字段草稿流程。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from agents.perception_agent import PerceptionAgent
from core.context_window import POLICY
from core.contracts.draft_contracts import DRAFT_EXCLUDED_FIELDS
from core.contracts.flows import required_field_pairs
from core.contracts.mysql_schema import MYSQL_FIELD_COMMENTS, business_fields
from services.application.draft_builder import build_draft

from .knowledge_document_draft import persist_drafts


def _excel_record_texts(
    result: dict[str, Any], knowledge_type: str
) -> list[tuple[str, dict[str, str]]]:
    metadata = result.get("metadata") or {}
    if str(metadata.get("parser") or result.get("parser") or "") != "excel_business":
        return []

    labels = MYSQL_FIELD_COMMENTS.get(knowledge_type, {})
    label_to_field = {str(label).strip(): field for field, label in labels.items()}
    allowed = set(business_fields(knowledge_type)) - set(
        DRAFT_EXCLUDED_FIELDS.get(knowledge_type, frozenset())
    )
    records: list[tuple[str, dict[str, str]]] = []
    for table in result.get("tables") or []:
        if not isinstance(table, dict):
            continue
        rows = table.get("rows") or []
        if not isinstance(rows, list) or len(rows) < 2:
            continue
        headers = [
            str(value or "").strip() or f"第{index + 1}列" for index, value in enumerate(rows[0])
        ]
        for row in rows[1:]:
            if not isinstance(row, list):
                continue
            fields = {
                label_to_field[header]: str(row[index] or "").strip()
                for index, header in enumerate(headers)
                if header in label_to_field
                and label_to_field[header] in allowed
                and index < len(row)
                and str(row[index] or "").strip()
            }
            if fields:
                records.append(
                    (
                        "\n".join(
                            f"{labels.get(field, field)}：{value}"
                            for field, value in fields.items()
                        ),
                        fields,
                    )
                )
    return records


class StructuredRecordDraftService:
    """仅按企业表或专家表字段提取草稿，不生成文档分块。"""

    _TYPES = {"enterprise", "expert"}

    def __init__(self, draft_store: Any | None = None) -> None:
        self.draft_store = draft_store

    async def build_task_fields(
        self, task: dict[str, Any], result: dict[str, Any], preview_text: str
    ) -> dict[str, Any]:
        if task.get("user_role") != "admin":
            return {}
        knowledge_type = str(task.get("knowledge_type") or "").strip()
        if knowledge_type not in self._TYPES:
            return {}
        source_text = str(result.get("raw_text") or preview_text)
        excel_records = _excel_record_texts(result, knowledge_type)
        if excel_records:

            async def extract_one(record: tuple[str, dict[str, str]]) -> dict[str, Any]:
                record_text, fields = record
                record_draft = build_draft(
                    knowledge_type,
                    "",
                    {
                        "text": record_text,
                        "parsed_draft": {"fields": fields},
                        "document_id": task.get("document_id"),
                        "file_name": task.get("file_name"),
                        "source_blocks": [],
                    },
                )
                return await PerceptionAgent.extract_fields(
                    record_draft,
                    source_text=record_text,
                    allow_multiple=False,
                    draft_factory=build_draft,
                    required_field_pairs_factory=required_field_pairs,
                    extraction_mode=os.getenv("ADMIN_EXTRACTION_MODE", "full"),
                    prompt_profile="admin",
                )

            # ponytail: 当前按行并行调用 LLM；只有大型表格触发供应商限流时，
            # 才增加有界并发控制。
            extracted = await asyncio.gather(*(extract_one(item) for item in excel_records))
            for item in extracted:
                for attachment in item.get("attachments") or []:
                    if isinstance(attachment, dict):
                        attachment["version_id"] = str(task.get("version_id") or "")
                        attachment["storage_key"] = str(task.get("storage_key") or "")
            return persist_drafts(
                self.draft_store, task, {"drafts": list(extracted)}, knowledge_type
            )
        draft = build_draft(
            knowledge_type,
            "",
            {
                "text": source_text[: POLICY.draft_text_chars],
                "document_id": task.get("document_id"),
                "file_name": task.get("file_name"),
                "source_blocks": result.get("blocks") or [],
            },
        )
        enriched = await PerceptionAgent.extract_fields(
            draft,
            source_text=source_text,
            allow_multiple=True,
            draft_factory=build_draft,
            required_field_pairs_factory=required_field_pairs,
            extraction_mode=os.getenv("ADMIN_EXTRACTION_MODE", "full"),
            prompt_profile="admin",
        )
        for attachment in enriched.get("attachments") or []:
            if isinstance(attachment, dict):
                attachment["version_id"] = str(task.get("version_id") or "")
                attachment["storage_key"] = str(task.get("storage_key") or "")
        return persist_drafts(self.draft_store, task, enriched, knowledge_type)
