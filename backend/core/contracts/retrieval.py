"""索引输入和检索证据的跨 Agent 契约。"""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field


def _result_candidates(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
    candidates: list[Mapping[str, Any]] = [item, source]
    for key in ("evidence", "record"):
        value = item.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    evidences = item.get("evidences")
    if isinstance(evidences, list) and evidences and isinstance(evidences[0], Mapping):
        candidates.append(evidences[0])
    return candidates


def _first_result_value(item: Mapping[str, Any], *keys: str) -> str:
    for candidate in _result_candidates(item):
        for key in keys:
            value = str(candidate.get(key) or "").strip()
            if value:
                return value
    return ""


def match_result_identity(
    item: Mapping[str, Any], *, default_knowledge_type: str = ""
) -> str:
    source = item.get("source") if isinstance(item.get("source"), Mapping) else {}

    knowledge_type = (
        _first_result_value(item, "knowledge_type", "source_knowledge_type") or default_knowledge_type
    )
    source_id = _first_result_value(item, "source_id")
    mysql_id = _first_result_value(item, "mysql_id")
    if is_source_chunk(item):
        mysql_id = _first_result_value(item, "matched_draft_mysql_id") or mysql_id
    source_locator = _first_result_value(item, "source_locator")
    if mysql_id:
        identifier = f"mysql:{mysql_id}"
    elif is_source_chunk(item) and _first_result_value(item, "document_id"):
        identifier = f"document:{_first_result_value(item, 'document_id')}"
    elif source_id:
        identifier = f"source:{source_id}"
    elif source_locator:
        identifier = f"locator:{source_locator}"
    else:
        identifier = str(
            item.get("id")
            or source.get("record_id")
            or item.get("record_id")
            or item.get("title")
            or item.get("summary")
            or ""
        ).strip()
    return f"{knowledge_type}:{identifier}" if identifier else ""


def is_source_chunk(item: Mapping[str, Any]) -> bool:
    return _first_result_value(item, "chunk_role").lower() in {"agent_doc", "admin_doc"}


def match_batch_identity(item: Mapping[str, Any]) -> str:
    """换批按文档或业务草稿排除，不把整个 source_id 下的草稿都排除。"""
    knowledge_type = _first_result_value(item, "knowledge_type", "source_knowledge_type")
    document_id = _first_result_value(item, "document_id")
    mysql_id = _first_result_value(item, "mysql_id")
    if is_source_chunk(item) and document_id:
        return f"{knowledge_type}:document:{document_id}"
    if mysql_id:
        return f"{knowledge_type}:mysql:{mysql_id}"
    record_id = _first_result_value(item, "record_id", "id")
    if record_id and _first_result_value(item, "chunk_role") in {"agent_draft", "admin_draft", "mysql"}:
        return f"{knowledge_type}:record:{record_id}"
    return match_result_identity(item)


def displayed_match_result_ids(item: Mapping[str, Any]) -> list[str]:
    ids = [match_batch_identity(item)]
    if is_source_chunk(item):
        knowledge_type = _first_result_value(item, "knowledge_type", "source_knowledge_type")
        mysql_id = _first_result_value(item, "matched_draft_mysql_id")
        record_id = _first_result_value(item, "matched_draft_record_id")
        if mysql_id:
            ids.append(f"{knowledge_type}:mysql:{mysql_id}")
        elif record_id:
            ids.append(f"{knowledge_type}:record:{record_id}")
    return [value for value in ids if value]


def is_match_result_excluded(item: Mapping[str, Any], excluded_ids: set[str]) -> bool:
    knowledge_type = _first_result_value(item, "knowledge_type", "source_knowledge_type")
    record_id = _first_result_value(item, "record_id", "id")
    return bool(excluded_ids.intersection({
        match_batch_identity(item), match_result_identity(item),
        f"{knowledge_type}:record:{record_id}" if record_id else "",
    }))


class IndexChunk(BaseModel):
    """Retrieval 建立索引时接收的最小文本块。"""

    chunk_text: str = Field(min_length=1)
    chunk_index: int = Field(default=0, ge=0)
    dense_vector: list[float] | None = None
    source_locator: str = Field(min_length=1, max_length=512)
    visibility: Literal["public", "internal_supplement", "private"] = "public"
    # 正式写入 Milvus 的业务块必须关联已落库的 MySQL 记录。
    mysql_id: str | None = Field(default=None, max_length=128)
    source_id: str | None = Field(default=None, max_length=128)
    document_id: str | None = Field(default=None, max_length=128)
    chunk_role: Literal["admin_draft", "admin_doc", "agent_draft", "agent_doc", "mysql"] = "admin_doc"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChunkPreviewBlock(BaseModel):
    """管理员编辑后的原始块；source_locator 由 Retrieval 统一生成。"""

    text: str = Field(min_length=1)
    block_id: str | None = None
    block_type: str = "text"
    page_number: int | None = None
    source_name: str = "unknown"
    proposed_visibility: Literal["public", "internal_supplement"] = "public"
    source_chunk_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChunkPreviewRequest(BaseModel):
    document_id: str = Field(min_length=1)
    version_id: str | None = Field(default=None, max_length=128)
    scope_id: str = "public"
    knowledge_type: Literal["enterprise", "demand", "achievement", "expert", "solution"]
    blocks: list[ChunkPreviewBlock] = Field(min_length=1)
    chunk_size: int = Field(default=1000, ge=100, le=2000)
    chunk_overlap: int = Field(default=80, ge=0, le=500)
    identity: dict[str, Any] = Field(default_factory=dict)



class Evidence(BaseModel):
    """Retrieval 返回给平台的可追溯文本证据。"""

    record_id: str
    matched_draft_record_id: str | None = None
    matched_draft_mysql_id: str | None = None
    text: str
    score: float
    source: str
    source_knowledge_type: str | None = None
    mysql_id: str | None = None
    source_id: str | None = None
    chunk_role: str | None = None
    status: str | None = None
    update_time: str | None = None
    scope_id: str | None = None
    source_locator: str | None = None
    document_id: str | None = None
    version_id: str | None = None
    publication_status: str | None = None
    index_time: str | None = None
    chunk_index: int | None = None


class RelatedImage(BaseModel):
    """由文本证据关联的图片引用，图片本体不进入向量库。"""

    image_id: str
    document_id: str
    relative_path: str | None = None
    source_locator: str | None = None
    url: str
    relation: str = "same_page"
    caption: str | None = None
    page_number: int | None = None


class RelatedDocument(BaseModel):
    """命中后可直接打开的原始文档引用。"""

    document_id: str
    knowledge_type: str | None = None
    version_id: str | None = None
    source_name: str
    file_url: str
    preview_url: str | None = None
    file_type: str = "document"
    matched_slides: list[int] = Field(default_factory=list)


class RetrievedRecord(BaseModel):
    """按一份资料聚合后的检索结果。"""

    record_id: str
    mysql_id: str | None = None
    source_id: str | None = None
    document_id: str | None = None
    title: str
    summary: str
    score: float
    evidences: list[Evidence] = Field(default_factory=list)
    documents: list[RelatedDocument] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    """Retrieval Agent 的稳定证据包。"""

    agent: str = "knowledge_retrieval"
    status: str
    query: str
    evidences: list[Evidence] = Field(default_factory=list)
    records: list[RetrievedRecord] = Field(default_factory=list)
    images: list[RelatedImage] = Field(default_factory=list)
    documents: list[RelatedDocument] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
