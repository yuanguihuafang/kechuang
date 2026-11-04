"""检索应用层的文档引用、证据转换和索引预处理辅助逻辑。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from core.contracts.retrieval import Evidence, RelatedDocument, RetrievedRecord, is_source_chunk
from agents.retrieval_agent.retrieval.field_policy import fields_to_text, split_business_fields
from agents.retrieval_agent.schemas import IndexChunk, IndexRequest


def slides_from_source_locator(source_locator: str) -> list[int]:
    match = re.search(r"(?:^|\|)slides:(\d+)-(\d+)(?:\||$)", source_locator)
    if not match:
        return []
    start, end = int(match.group(1)), int(match.group(2))
    return list(range(start, end + 1)) if end >= start else []


def related_documents(evidences: list[Evidence]) -> list[RelatedDocument]:
    documents: list[RelatedDocument] = []
    seen: set[tuple[str, str | None]] = set()
    for evidence in evidences:
        source_locator = evidence.source_locator or ""
        source_name = source_locator.split("|", maxsplit=1)[0].strip()
        refs = (
            []
            if str(evidence.document_id or "").startswith("draft:") or source_name.lower().startswith("mysql:")
            else [(evidence.document_id, source_name)]
        )
        marker = next((part for part in source_locator.split("|") if part.startswith("attachments:")), "")
        if marker:
            try:
                refs.extend(
                    (str(item.get("document_id") or ""), str(item.get("file_name") or "附件"))
                    for item in json.loads(unquote(marker.removeprefix("attachments:")))
                    if isinstance(item, dict) and item.get("document_id")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        for document_id, document_name in refs:
            if not document_id:
                continue
            key = (document_id, evidence.version_id)
            if key in seen:
                continue
            seen.add(key)
            documents.append(RelatedDocument(
                document_id=document_id,
                knowledge_type=evidence.source_knowledge_type,
                version_id=evidence.version_id,
                source_name=document_name,
                file_url=f"/api/v1/knowledge/documents/{document_id}/file",
                preview_url=None,
                file_type=Path(document_name).suffix.lower().removeprefix(".") or "document",
                matched_slides=slides_from_source_locator(source_locator),
            ))
    return documents


def to_evidence(hit: Any) -> Evidence:
    metadata = hit.metadata
    return Evidence(
        record_id=hit.record_id,
        matched_draft_record_id=metadata.get("matched_draft_record_id"),
        matched_draft_mysql_id=metadata.get("matched_draft_mysql_id"),
        text=str(metadata.get("chunk_text", "")),
        score=float(hit.score),
        source=hit.source,
        source_knowledge_type=metadata.get("source_knowledge_type") or metadata.get("knowledge_type"),
        mysql_id=metadata.get("mysql_id"),
        source_id=metadata.get("source_id"),
        chunk_role=metadata.get("chunk_role"),
        status=metadata.get("status"),
        update_time=metadata.get("update_time"),
        scope_id=metadata.get("scope_id") or ("internal" if metadata.get("user_id") else "public"),
        document_id=metadata.get("document_id"),
        version_id=metadata.get("version_id"),
        chunk_index=metadata.get("chunk_index"),
        source_locator=metadata.get("source_locator"),
    )


def group_records(evidences: list[Evidence], documents: list[RelatedDocument]) -> list[RetrievedRecord]:
    groups: dict[tuple[str, str, str], list[Evidence]] = {}
    for evidence in evidences:
        source_id = str(evidence.source_id or "").strip()
        mysql_id = str(evidence.matched_draft_mysql_id or evidence.mysql_id or "").strip()
        key = (
            ("mysql", mysql_id) if mysql_id
            else ("document", str(evidence.document_id)) if evidence.document_id
            else ("source", source_id) if source_id
            else ("document", str(evidence.document_id or evidence.source or evidence.record_id))
        )
        groups.setdefault((evidence.source_knowledge_type or "", *key), []).append(evidence)
    records = []
    for (_, key_type, key_value), items in groups.items():
        selected = max(items, key=lambda item: (is_source_chunk(item.model_dump()), item.score))
        mysql_id = key_value if key_type == "mysql" else next((item.mysql_id for item in items if item.mysql_id), None)
        source_id = selected.source_id
        document_id = selected.document_id
        summary = selected.text
        records.append(RetrievedRecord(
            record_id=mysql_id or source_id or document_id,
            mysql_id=mysql_id or None,
            source_id=source_id,
            document_id=document_id or None,
            title=summary.splitlines()[0].strip()[:120] or f"资料 {mysql_id or document_id}",
            summary=summary[:500],
            score=max(item.score for item in items),
            evidences=items,
            documents=[document for document in documents if (
                document.document_id == document_id
                or any(item.document_id == document.document_id for item in items)
            )],
        ))
    return sorted(records, key=lambda item: item.score, reverse=True)


def step_records(evidences: list[Evidence], documents: list[RelatedDocument]) -> list[RetrievedRecord]:
    documents_by_id: dict[str, list[RelatedDocument]] = {}
    for document in documents:
        documents_by_id.setdefault(document.document_id, []).append(document)
    return [RetrievedRecord(
        record_id=evidence.record_id,
        mysql_id=evidence.mysql_id,
        source_id=evidence.source_id,
        document_id=evidence.document_id,
        title=evidence.text.strip().splitlines()[0].strip()[:120] or "匹配结果",
        summary=evidence.text.strip()[:500],
        score=evidence.score,
        evidences=[evidence],
        documents=documents_by_id.get(evidence.document_id or "", []),
    ) for evidence in evidences]


def normalize_chunk_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


def confirmed_solution_chunks(chunks: list[IndexChunk]) -> list[IndexChunk]:
    result: list[IndexChunk] = []
    for chunk in chunks:
        text = re.sub(r"(^|\n)(#{1,6})[ \t]*\n(?=[^#\s])", r"\1\2 ", chunk.chunk_text.replace("\r\n", "\n"))
        text = re.sub(r"^[ \t]*#{1,6}[ \t]*(?=\n|$)", "", text)
        text = re.sub(r"(?<=\n)[ \t]*#{1,6}[ \t]*\n", "", text)
        text = re.sub(r"\n[ \t]*#{1,6}[ \t]*(?=$)", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            result.append(chunk.model_copy(update={"chunk_index": len(result), "dense_vector": None, "chunk_text": text}))
    return result


def apply_field_policy(request: IndexRequest, chunks: list[IndexChunk]) -> list[IndexChunk]:
    result: list[IndexChunk] = []
    for chunk in chunks:
        metadata = chunk.metadata or {}
        fields = metadata.get("field_values")
        if chunk.visibility != "public" or not metadata.get("field_policy_managed") or not isinstance(fields, dict):
            result.append(chunk)
            continue
        public, internal, known = split_business_fields(request.knowledge_type, fields, metadata.get("field_visibility_overrides"))
        if not known:
            result.append(chunk.model_copy(update={"visibility": "internal_supplement", "metadata": {**metadata, "field_policy_status": "UNMAPPED"}}))
            continue
        for visibility, selected in (("public", public), ("internal_supplement", internal)):
            text = fields_to_text(selected)
            if text:
                result.append(chunk.model_copy(update={
                    "chunk_text": text,
                    "metadata": {**metadata, "field_names": list(selected), "field_visibility": visibility,
                                  "field_policy_status": "OVERRIDDEN" if metadata.get("field_visibility_overrides") else "DEFAULT"},
                    "visibility": visibility,
                }))
    return [chunk.model_copy(update={"chunk_index": index}) for index, chunk in enumerate(result)]


def normalize_for_dedup(text: str) -> str:
    return "".join(char for char in text.lower() if char.isalnum())


def is_highly_contained(text: str, table_text: str) -> bool:
    if not text or not table_text:
        return False
    shorter, longer = sorted((text, table_text), key=len)
    return shorter != longer and shorter in longer and len(shorter) / len(longer) >= 0.7


def deduplicate_source_chunks(source_chunks: list[IndexChunk]) -> list[IndexChunk]:
    groups: dict[tuple[str, str, str, str], list[IndexChunk]] = {}
    for chunk in source_chunks:
        key = (chunk.document_id or "", chunk.source_id or "", chunk.mysql_id or "", chunk.chunk_role)
        groups.setdefault(key, []).append(chunk)
    return [chunk for group in groups.values() for chunk in (
        group if any(item.metadata.get("chunks_are_final") for item in group)
        else _deduplicate_source_group(group)
    )]


def _deduplicate_source_group(source_chunks: list[IndexChunk]) -> list[IndexChunk]:
    tables = [chunk for chunk in source_chunks if "|type:table|" in chunk.source_locator]
    retained: list[IndexChunk] = []
    seen_content: set[str] = set()
    for table in tables:
        normalized = normalize_for_dedup(table.chunk_text)
        if normalized and normalized not in seen_content:
            retained.append(table)
            seen_content.add(normalized)
    table_texts = [normalize_for_dedup(chunk.chunk_text) for chunk in tables]
    table_hashes = {hashlib.sha256(text.encode()).hexdigest() for text in table_texts if text}
    for chunk in source_chunks:
        if "|type:table|" in chunk.source_locator:
            continue
        normalized = normalize_for_dedup(chunk.chunk_text)
        if not normalized:
            continue
        if hashlib.sha256(normalized.encode()).hexdigest() in table_hashes or any(is_highly_contained(normalized, table_text) for table_text in table_texts):
            continue
        if normalized in seen_content:
            continue
        seen_content.add(normalized)
        retained.append(chunk)
    return retained
