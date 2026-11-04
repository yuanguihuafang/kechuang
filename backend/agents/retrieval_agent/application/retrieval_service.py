"""Retrieval 知识检索 Agent：LangGraph 编排，按数据类型选择 Milvus 或 MySQL。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from time import perf_counter
from typing import Any, TypedDict
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

from agents.retrieval_agent.application.retrieval_helpers import (
    apply_field_policy,
    confirmed_solution_chunks,
    deduplicate_source_chunks,
    group_records,
    normalize_chunk_text,
    related_documents,
    step_records,
    to_evidence,
)
from agents.retrieval_agent.retrieval.chunker import (
    chunk_document_blocks,
    split_text_by_utf8_bytes,
)
from agents.retrieval_agent.retrieval.embedding_service import EmbeddingService
from agents.retrieval_agent.retrieval.field_policy import fields_to_text
from agents.retrieval_agent.retrieval.search_service import SearchResult, SearchService
from core.contracts.agent import AgentContext, AgentResult, failure, success
from core.contracts.knowledge import (
    BusinessImportRequest,
    DocumentKnowledgeRequest,
    DocumentUnpublishRequest,
    IndexTextRequest,
    SessionSummaryRequest,
)
from core.contracts.mysql_schema import append_attachment_names
from core.contracts.retrieval import ChunkPreviewRequest
from core.governance.access_policy import AccessPolicy
from services.storage.minio.image_service import (
    ImageManifestService,
    normalize_image_references,
    perception_image_url,
)
from services.storage.minio.parent_service import ParentRecordService
from services.vector.milvus.milvus_service import (
    MILVUS_CHUNK_TEXT_MAX_BYTES,
    MilvusService,
    escape_milvus_expr_value,
)

from ..schemas import (
    Evidence,
    IndexChunk,
    IndexRequest,
    IndexResult,
    RelatedImage,
    RetrievalRequest,
    RetrievalResult,
)

BEIJING = ZoneInfo("Asia/Shanghai")


def _milvus_sync_time() -> str:
    return datetime.now(BEIJING).isoformat()


def index_effective_document_id(document_id: str, chunk: IndexChunk) -> str:
    if document_id.startswith("mysql:") or chunk.chunk_role in {"agent_draft", "admin_draft"}:
        return ""
    return chunk.document_id or document_id


def index_record_id(document_id: str, chunk: IndexChunk) -> str:
    """Return the stable Milvus primary key for an already chunked index row."""
    return hashlib.sha1(
        f"{chunk.mysql_id or index_effective_document_id(document_id, chunk) or document_id}:{chunk.source_id or ''}:"
        f"{index_effective_document_id(document_id, chunk)}:{chunk.chunk_role}:"
        f"{chunk.source_locator}:{chunk.chunk_index}".encode()
    ).hexdigest()


class _RetrievalState(TypedDict, total=False):
    request: RetrievalRequest
    result: SearchResult
    response: RetrievalResult
    error: str


class _IndexState(TypedDict, total=False):
    request: IndexRequest
    result: IndexResult
    target_override: MilvusService
    insert_only: bool


class RetrievalAgent:
    """提供检索和向量入库能力；后台按所选范围写入，检索时再执行访问控制。"""

    name = "retrieval"

    def __init__(
        self,
        search_service: SearchService | None = None,
        embedding_service: EmbeddingService | None = None,
        image_manifest_service: ImageManifestService | None = None,
        parent_record_service: ParentRecordService | None = None,
    ) -> None:
        self.search_service = search_service or SearchService()
        self.public_milvus = self.search_service.public_milvus
        self.milvus = self.public_milvus["solution"]
        self.private_milvus = self.search_service.private_milvus
        self.embedding = embedding_service or self.search_service.embedding_service
        self.image_manifest = image_manifest_service or ImageManifestService()
        self.parent_records = parent_record_service or ParentRecordService()
        retrieve_graph = StateGraph(_RetrievalState)
        retrieve_graph.add_node("retrieve", self._retrieve)
        retrieve_graph.add_node("format_evidence", self._format_evidence)
        retrieve_graph.add_edge(START, "retrieve")
        retrieve_graph.add_edge("retrieve", "format_evidence")
        retrieve_graph.add_edge("format_evidence", END)
        self._retrieve_graph = retrieve_graph.compile()

    def close(self) -> None:
        """关闭 SearchService 持有的 worker 级外部连接。"""
        self.search_service.close()

    def initialize_public_collection_aliases(self) -> None:
        """Attach stable public aliases to legacy physical collections at startup."""
        for service in self.public_milvus.values():
            service.ensure_alias()

    async def invoke(self, context: AgentContext, request: Any) -> AgentResult[Any]:
        started_at = perf_counter()
        try:
            if isinstance(request, RetrievalRequest):
                data = await self.retrieve(request)
                operation = "retrieve"
            elif isinstance(request, IndexRequest):
                data = await self.index(request)
                operation = "index"
            else:
                return failure(
                    context,
                    operation="invoke",
                    started_at=started_at,
                    error_code="UNSUPPORTED_REQUEST",
                    error_message=type(request).__name__,
                )
        except Exception as exc:
            return failure(
                context,
                operation="invoke",
                started_at=started_at,
                error_code="RETRIEVAL_FAILED",
                error_message=str(exc),
            )
        status = getattr(data, "status", "FAILED")
        if status == "DEGRADED":
            envelope = success(context, data, operation=operation, started_at=started_at)
            return AgentResult(
                status="FAILED",
                data=data,
                error_code="RETRIEVAL_UNAVAILABLE",
                error_message=getattr(data, "error", None) or "检索服务暂时不可用",
                meta=envelope.meta,
            )
        if status in {"FAILED", "NO_EVIDENCE", "PARTIAL_SUCCESS"}:
            envelope = success(context, data, operation=operation, started_at=started_at)
            return AgentResult(status=status, data=data, meta=envelope.meta)
        return success(context, data, operation=operation, started_at=started_at)

    async def index_text(self, request: IndexTextRequest | dict[str, Any]) -> IndexResult:
        """
        将纯文本分块并索引到 Milvus。

        Args:
            request: 文本索引请求

        Returns:
            索引结果
        """
        if isinstance(request, dict):
            request = IndexTextRequest(**request)

        try:
            index_request = IndexRequest(
                document_id=request.document_id,
                scope_id=request.scope_id,
                knowledge_type=request.knowledge_type,
                chunks=[
                    IndexChunk(
                        chunk_text=request.text,
                        source_locator="text|type:text|ordinal:1",
                    )
                ],
                chunk_size=request.chunk_size,
                chunk_overlap=request.chunk_overlap,
                identity=request.identity,
            )
            return await self.index(index_request)

        except Exception as exc:
            return IndexResult(
                status="FAILED",
                document_id=request.document_id,
                error=str(exc),
            )

    async def store_session_summary(self, request: SessionSummaryRequest) -> IndexResult:
        """只把确认后的拜访草稿按普通草稿规则分块写入私有库。"""
        if not AccessPolicy.can_write_private(request.identity):
            return IndexResult(
                status="FAILED",
                document_id=f"session:{request.session_id}",
                error="当前身份不能写入私有会话知识",
            )
        document_id = (
            request.source_document_id
            or (f"customer_visit:{request.mysql_id}" if request.mysql_id else "")
            or f"session:{request.identity.user_id}:{request.session_id}:"
            f"{hashlib.sha1(request.summary.strip().encode('utf-8')).hexdigest()[:20]}"
        )
        source_name = str(request.source_name or "拜访记录")
        source_locator = (
            f"{source_name}|type:record|record_type:customer_visit|id:{request.mysql_id}"
            if request.source_document_id
            else (
                f"ai_inner_inquiry|record_type:customer_visit|type:record|id:{request.mysql_id}"
                if request.mysql_id
                else f"session:{request.session_id}|record_type:customer_visit|type:summary|user:{request.identity.user_id}"
            )
        )
        attachments = [
            {
                "document_id": str(item.get("document_id") or ""),
                "file_name": str(item.get("file_name") or "附件"),
            }
            for item in request.source_attachments
            if item.get("document_id")
        ]
        source_locator = append_attachment_names(source_locator, attachments)
        # 由统一索引入口按 mysql_id 替换旧草稿，避免重复发布累积旧分块。
        return await self.index(
            IndexRequest(
                document_id=document_id,
                scope_id="internal",
                knowledge_type="solution",
                replace_existing=True,
                chunks=[
                    IndexChunk(
                        chunk_text=request.summary.strip(),
                        chunk_index=0,
                        source_locator=source_locator,
                        visibility="private",
                        mysql_id=request.mysql_id,
                        source_id=request.source_id,
                        document_id=None,
                        chunk_role="agent_draft",
                    )
                ],
                identity=request.identity,
            )
        )

    async def delete_private_record_by_mysql_id(self, mysql_id: str, user_id: str) -> IndexResult:
        """删除一个拜访记录对应的私密向量，不影响同文档的其他草稿。"""
        if not AccessPolicy.can_write_private({"user_id": user_id, "user_role": "internal"}):
            return IndexResult(
                status="FAILED",
                document_id=f"customer_visit:{mysql_id}",
                error="当前身份不能删除私密知识",
            )
        try:
            self.private_milvus.delete_by_mysql_id(mysql_id, user_id=user_id)
            return IndexResult(status="DELETED", document_id=f"customer_visit:{mysql_id}")
        except Exception as exc:
            return IndexResult(
                status="FAILED", document_id=f"customer_visit:{mysql_id}", error=str(exc)
            )

    async def import_business_data(
        self,
        request: BusinessImportRequest | dict[str, Any],
        *,
        target_override: MilvusService | None = None,
        insert_only: bool = False,
    ) -> IndexResult:
        """导入平台已清洗、已发布的业务记录，并替换同文档旧索引。"""

        if isinstance(request, dict):
            request = BusinessImportRequest(**request)
        chunks: list[IndexChunk] = []
        is_mysql_import = request.document_id.startswith("mysql:")
        for index, record in enumerate(request.records):
            text = record.text or fields_to_text(record.fields)
            if not text.strip():
                continue
            source_locator = record.source_locator or (
                f"{request.source_name}|type:record|page:unknown|ordinal:{index + 1}"
            )
            chunk = IndexChunk(
                chunk_text=text.strip(),
                chunk_index=int(record.metadata.get("chunk_index", index)),
                source_locator=source_locator,
                chunk_role=str(
                    record.metadata.get("chunk_role")
                    or ("mysql" if is_mysql_import else "admin_doc")
                ),
                mysql_id=(record.mysql_id or (record.record_id if is_mysql_import else None))
                if str(
                    record.metadata.get("chunk_role")
                    or ("mysql" if is_mysql_import else "admin_doc")
                )
                in {"agent_draft", "admin_draft", "mysql"}
                else None,
                source_id=record.source_id or record.metadata.get("source_id"),
                document_id=(record.document_id or record.metadata.get("document_id"))
                or (None if is_mysql_import else request.document_id),
                metadata=record.metadata,
            )
            # 原文块已经在上传解析阶段完成分块；草稿正文仍按普通规则在超长时拆分。
            if request.chunks_are_final and chunk.chunk_role in {"agent_draft", "admin_draft"}:
                chunks.extend(
                    chunk_document_blocks(
                        [chunk],
                        target_size=request.chunk_size,
                        hard_max_size=max(request.chunk_size, min(request.chunk_size + 300, 1500)),
                        overlap=request.chunk_overlap,
                    )
                )
            else:
                chunks.append(chunk)
        if not chunks:
            return IndexResult(
                status="FAILED",
                document_id=request.document_id,
                error="清洗数据没有可检索文本",
            )
        return await self.index(
            IndexRequest(
                document_id=request.document_id,
                version_id=request.version_id,
                scope_id=request.scope_id,
                knowledge_type=request.knowledge_type,
                chunks=chunks,
                replace_existing=request.replace_existing,
                chunk_size=request.chunk_size,
                chunk_overlap=request.chunk_overlap,
                chunks_are_final=request.chunks_are_final,
                quality_blockers=request.quality_blockers,
                allow_quality_override=request.allow_quality_override,
                identity=request.identity,
            ),
            target_override=target_override,
            insert_only=insert_only,
        )

    async def publish_document(
        self, document_id: str, request: DocumentKnowledgeRequest
    ) -> IndexResult:
        """发布完整清洗数据：写入主库并替换同文档旧向量。"""

        return await self.import_business_data(
            BusinessImportRequest(
                document_id=document_id,
                publication_status="PUBLISHED",
                **request.model_dump(),
            )
        )

    async def preflight_publish_document(
        self, document_id: str, request: DocumentKnowledgeRequest
    ) -> None:
        """在业务记录写入前验证分块、embedding 和目标 Collection，不产生写入。"""
        if request.scope_id not in AccessPolicy.allowed_scopes(request.identity):
            raise ValueError("当前身份无权写入该知识范围")
        if request.quality_blockers and not request.allow_quality_override:
            raise ValueError("解析质量未达到发布门槛：" + "；".join(request.quality_blockers))
        is_mysql_import = document_id.startswith("mysql:")
        chunks = []
        for index, record in enumerate(request.records):
            text = record.text or fields_to_text(record.fields)
            if not text.strip():
                continue
            role = str(record.metadata.get("chunk_role") or (
                "mysql" if is_mysql_import else "admin_doc"
            ))
            chunks.append(IndexChunk(
                chunk_text=text.strip(),
                chunk_index=int(record.metadata.get("chunk_index", index)),
                source_locator=record.source_locator or (
                    f"{request.source_name}|type:record|page:unknown|ordinal:{index + 1}"
                ),
                chunk_role=role,
                mysql_id=(record.mysql_id or (record.record_id if is_mysql_import else None))
                if role in {"agent_draft", "admin_draft", "mysql"} else None,
                source_id=record.source_id or record.metadata.get("source_id"),
                document_id=(record.document_id or record.metadata.get("document_id"))
                or (None if is_mysql_import else document_id),
                metadata=record.metadata,
            ))
        if not chunks:
            raise ValueError("清洗数据没有可检索文本")
        index_request = IndexRequest(
            document_id=document_id,
            version_id=request.version_id,
            scope_id=request.scope_id,
            knowledge_type=request.knowledge_type,
            chunks=chunks,
            replace_existing=request.replace_existing,
            chunk_size=request.chunk_size,
            chunk_overlap=request.chunk_overlap,
            chunks_are_final=request.chunks_are_final,
            quality_blockers=request.quality_blockers,
            allow_quality_override=request.allow_quality_override,
            identity=request.identity,
        )
        prepared = self._prepare_index_chunks(index_request)
        if not prepared:
            raise ValueError("文本为空或分块失败")
        await self.embedding.encode_dense([chunk.chunk_text for chunk in prepared])
        target = self._milvus_for_scope(request.scope_id, request.knowledge_type)
        await target.check_ready()

    async def reindex_document(
        self, document_id: str, request: DocumentKnowledgeRequest
    ) -> IndexResult:
        """使用最新完整清洗数据重建文档索引。"""

        return await self.publish_document(document_id, request)

    async def unpublish_document(
        self, document_id: str, request: DocumentUnpublishRequest
    ) -> IndexResult:
        """下架文档：默认只更新向量可检索状态，彻底删除时才移除向量。"""

        if request.scope_id not in AccessPolicy.allowed_scopes(request.identity):
            return IndexResult(
                status="FAILED",
                document_id=document_id,
                error="测试身份无权下架该知识范围",
            )
        try:
            target = self._milvus_for_scope(request.scope_id, request.knowledge_type)
            if request.remove_vectors and request.scope_id == "internal":
                (
                    target.delete_by_version_id(
                        request.version_id, user_id=request.identity.user_id
                    )
                    if request.version_id
                    else target.delete_by_document_id(document_id, user_id=request.identity.user_id)
                )
            elif request.remove_vectors:
                (
                    target.delete_by_version_id(request.version_id)
                    if request.version_id
                    else target.delete_by_document_id(document_id)
                )
            elif request.scope_id == "internal":
                if request.mysql_id:
                    target.set_publication_status_by_mysql_id(
                        request.mysql_id, "UNPUBLISHED", user_id=request.identity.user_id
                    )
                if request.version_id:
                    target.set_publication_status_by_version_id(
                        request.version_id, "UNPUBLISHED", user_id=request.identity.user_id
                    )
                else:
                    target.set_publication_status_by_document_id(
                        document_id,
                        "UNPUBLISHED",
                        user_id=request.identity.user_id,
                        chunk_role="agent_doc" if request.mysql_id else None,
                    )
            else:
                if request.mysql_id:
                    target.set_publication_status_by_mysql_id(request.mysql_id, "UNPUBLISHED")
                if request.version_id:
                    target.set_publication_status_by_version_id(request.version_id, "UNPUBLISHED")
                else:
                    target.set_publication_status_by_document_id(
                        document_id,
                        "UNPUBLISHED",
                        chunk_role="agent_doc" if request.mysql_id else None,
                    )
            return IndexResult(status="UNPUBLISHED", document_id=document_id)
        except Exception as exc:
            return IndexResult(status="FAILED", document_id=document_id, error=str(exc))

    async def republish_document(
        self, document_id: str, request: DocumentUnpublishRequest
    ) -> IndexResult:
        """重新上架文档：恢复 Milvus 与平台的可检索状态，不重建向量。"""

        if request.scope_id not in AccessPolicy.allowed_scopes(request.identity):
            return IndexResult(
                status="FAILED", document_id=document_id, error="测试身份无权上架该知识范围"
            )
        try:
            target = self._milvus_for_scope(request.scope_id, request.knowledge_type)
            if request.mysql_id:
                updated_count = target.set_publication_status_by_mysql_id(
                    request.mysql_id,
                    "PUBLISHED",
                    user_id=request.identity.user_id if request.scope_id == "internal" else None,
                )
                return IndexResult(
                    status="PUBLISHED", document_id=document_id, indexed_count=updated_count
                )
            if request.version_id:
                if request.scope_id == "internal":
                    target.set_publication_status_by_version_id(
                        request.version_id, "PUBLISHED", user_id=request.identity.user_id
                    )
                else:
                    target.set_publication_status_by_version_id(request.version_id, "PUBLISHED")
            elif request.scope_id == "internal":
                target.set_publication_status_by_document_id(
                    document_id, "PUBLISHED", user_id=request.identity.user_id
                )
            else:
                target.set_publication_status_by_document_id(document_id, "PUBLISHED")
            return IndexResult(status="PUBLISHED", document_id=document_id)
        except Exception as exc:
            return IndexResult(status="FAILED", document_id=document_id, error=str(exc))

    async def _retrieve(self, state: _RetrievalState) -> dict[str, Any]:
        request = state["request"]
        try:
            result = await self.search_service.search_with_trace(
                query=request.query,
                top_k=request.top_k,
                scope_id=request.scope_id,
                knowledge_type=request.knowledge_type,
                knowledge_types=request.knowledge_types,
                document_id=request.document_id,
                use_rerank=request.use_rerank,
                expand_query=request.expand_query,
                identity=request.identity,
                exclude_result_ids=request.exclude_result_ids,
            )
            return {"result": result}
        except Exception as exc:
            return {"error": str(exc)}

    async def _format_evidence(self, state: _RetrievalState) -> dict[str, Any]:
        request = state["request"]
        if "error" in state:
            return {
                "response": RetrievalResult(
                    status="DEGRADED", query=request.query, error=state["error"]
                )
            }
        result = state.get("result")
        evidences = [to_evidence(hit) for hit in (result.hits if result else [])]
        images = await self._related_images(evidences)
        documents = related_documents(evidences)
        records = (
            step_records(evidences, documents)
            if result and result.retrieval_mode == "step"
            else group_records(evidences, documents)
        )
        unavailable_collections = (
            result.unavailable_collections if result else []
        )
        return {
            "response": RetrievalResult(
                status=(
                    "PARTIAL_SUCCESS"
                    if unavailable_collections and evidences
                    else "DEGRADED"
                    if unavailable_collections
                    else "SUCCESS"
                    if evidences
                    else "NO_EVIDENCE"
                ),
                query=request.query,
                evidences=evidences,
                records=records,
                images=images,
                documents=documents,
                trace={
                    "sources": sorted({hit.source for hit in (result.hits if result else [])}),
                    "structured_internal_source": "mysql",
                    # 兼容既有调试调用方；混合来源时该值为 False。
                    "milvus_only": bool(
                        result
                        and result.hits
                        and all(hit.source.startswith("milvus") for hit in result.hits)
                    ),
                    "rerank_applied": result.rerank_applied if result else False,
                    "retrieval_hit_count": result.retrieval_hit_count if result else 0,
                    "retrieval_top_score": result.retrieval_top_score if result else None,
                    "retrieval_mode": result.retrieval_mode if result else "full",
                    "unavailable_collections": unavailable_collections,
                },
            )
        }

    async def _related_images(self, evidences: list[Evidence]) -> list[RelatedImage]:
        """按证据的 document_id 与页码定位图片；同图只返回一次。"""

        related: list[RelatedImage] = []
        seen: set[str] = set()
        # MySQL 同步数据没有 MinIO 原文；只有解析文档的证据允许查图片清单。
        image_evidences = [evidence for evidence in evidences if not _is_mysql_evidence(evidence)]
        locations = [
            (evidence.document_id, evidence.source_locator) for evidence in image_evidences
        ]
        if not locations:
            return []
        try:
            image_groups = await self.image_manifest.find_many(locations)
        except Exception:
            # 图片是证据增强项，不应阻断文本检索；MinIO 暂不可用时降级为空图片集。
            return []
        for evidence, images in zip(image_evidences, image_groups, strict=True):
            for image in images:
                image_id = str(image.get("image_id", ""))
                document_id = str(image.get("document_id") or evidence.document_id or "")
                if not image_id or not document_id or image_id in seen:
                    continue
                seen.add(image_id)
                related.append(
                    RelatedImage(
                        image_id=image_id,
                        document_id=document_id,
                        relative_path=str(
                            image.get("relativePath")
                            or image.get("relative_path")
                            or image.get("path")
                            or ""
                        )
                        or None,
                        source_locator=str(image.get("source_locator") or "") or None,
                        url=perception_image_url(document_id, image_id),
                        relation=(
                            "same_slide_range"
                            if evidence.source_locator and "|slides:" in evidence.source_locator
                            else "same_page"
                        ),
                        caption=str(image.get("caption") or "") or None,
                        page_number=(
                            int(image["page_number"])
                            if image.get("page_number") is not None
                            else None
                        ),
                    )
                )
        return related

    async def retrieve(self, request: RetrievalRequest | str) -> RetrievalResult:
        if isinstance(request, str):
            request = RetrievalRequest(query=request)
        try:
            state = await self._retrieve_graph.ainvoke({"request": request})
            response = state.get("response")
            if isinstance(response, RetrievalResult):
                return response
            return RetrievalResult(
                status="FAILED", query=request.query, error="Retrieval 未返回有效结果"
            )
        except Exception as exc:
            return RetrievalResult(status="FAILED", query=request.query, error=str(exc))

    async def _index(self, state: _IndexState) -> dict[str, Any]:
        request = state["request"]
        try:
            if request.scope_id not in AccessPolicy.allowed_scopes(request.identity):
                return {
                    "result": IndexResult(
                        status="FAILED",
                        document_id=request.document_id,
                        error="测试身份无权写入该知识范围",
                    )
                }
            if request.quality_blockers and not request.allow_quality_override:
                return {
                    "result": IndexResult(
                        status="FAILED",
                        document_id=request.document_id,
                        error="解析质量未达到发布门槛：" + "；".join(request.quality_blockers),
                    )
                }
            # 发布时按已保存图片清单过滤旧分块遗留的 image_ids，避免死链接重新写回 Milvus。
            image_document_ids = set()
            for chunk in request.chunks:
                metadata = chunk.metadata or {}
                if metadata.get("image_ids") or metadata.get("image_refs"):
                    image_document_ids.add(
                        str(chunk.document_id or metadata.get("document_id") or request.document_id)
                    )
                image_document_ids.update(
                    unquote(match.group(1))
                    for match in re.finditer(
                        r"/api/v1/perception/images/([^/]+)/[^/?#)]+", chunk.chunk_text
                    )
                )
                image_document_ids.update(re.findall(r"/?derived/([^/]+)/[^\s)]+", chunk.chunk_text))
                if chunk.document_id and re.search(r"(?:^|[(/])images/", chunk.chunk_text):
                    image_document_ids.add(chunk.document_id)
            image_manifests = {
                document_id: await self.image_manifest.list_for_document(document_id)
                for document_id in image_document_ids
            }
            allowed_image_ids_by_document = {
                document_id: {
                    str(image.get("image_id") or "")
                    for image in images
                }
                for document_id, images in image_manifests.items()
            }
            request = request.model_copy(update={"chunks": [chunk.model_copy(update={
                "chunk_text": normalize_image_references(
                    chunk.chunk_text, document_id=chunk.document_id or request.document_id,
                    manifests=image_manifests,
                ),
            }) for chunk in request.chunks]})
            # 预览和正式入库共用分块逻辑；正式发布再叠加当前图片清单校验。
            index_chunks = self._prepare_index_chunks(
                request, allowed_image_ids_by_document=allowed_image_ids_by_document
            )
            if not index_chunks:
                return {
                    "result": IndexResult(
                        status="FAILED", document_id=request.document_id, error="文本为空或分块失败"
                    )
                }
            # 只有公开字段进入向量化；内部字段在上面已经被过滤。
            texts = [chunk.chunk_text for chunk in index_chunks]
            vectors = [chunk.dense_vector for chunk in index_chunks]
            if any(vector is None for vector in vectors):
                generated = await self.embedding.encode_dense(texts)
                vectors = [vector or generated[index] for index, vector in enumerate(vectors)]
            chunks = []
            accepted_chunks: list[IndexChunk] = []
            chunks_by_visibility: dict[str, list[dict[str, Any]]] = {}
            is_mysql_import = request.document_id.startswith("mysql:")
            public_texts = {
                normalize_chunk_text(chunk.chunk_text)
                for chunk in index_chunks
                if chunk.visibility == "public"
            }
            for index, (chunk, vector) in enumerate(zip(index_chunks, vectors, strict=True)):
                normalized_text = normalize_chunk_text(chunk.chunk_text)
                # 完全相同的内容只保留公开块。
                if (
                    not is_mysql_import
                    and chunk.visibility != "public"
                    and normalized_text in public_texts
                ):
                    continue
                # 草稿按 mysql_id+source_id 定位，原文按 source_id+document_id 定位。
                effective_document_id = index_effective_document_id(request.document_id, chunk)
                record_id = index_record_id(request.document_id, chunk)
                payload = {
                    "id": record_id,
                    "document_id": effective_document_id,
                    "mysql_id": chunk.mysql_id or "",
                    "source_id": chunk.source_id or "",
                    "chunk_role": chunk.chunk_role,
                    "status": "active",
                    "update_time": _milvus_sync_time(),
                    "chunk_index": chunk.chunk_index,
                    "chunk_text": chunk.chunk_text,
                    "dense_vector": vector,
                    "source_locator": chunk.source_locator,
                    "field_names": list((chunk.metadata or {}).get("field_names") or []),
                }
                # 管理后台的身份不是知识可见性。后台已经通过 scope_id 选择目标库，
                # 因此整份资料统一写入所选范围，不再按块权限拦截或丢弃内容。
                # 普通会话摘要仍保留 private 路由和写入权限校验。
                is_admin_ingest = request.identity.user_role.value == "admin"
                visibility = (
                    ("private" if request.scope_id == "internal" else "public")
                    if is_admin_ingest
                    else chunk.visibility
                )
                if visibility == "public" and request.scope_id == "internal":
                    visibility = "private"
                if (
                    visibility in {"internal_supplement", "private"}
                    and not is_admin_ingest
                    and not AccessPolicy.can_write_private(request.identity)
                ):
                    return {
                        "result": IndexResult(
                            status="FAILED",
                            document_id=request.document_id,
                            error="当前身份无权写入内部或私有知识块",
                        )
                    }
                if visibility == "private":
                    payload["user_id"] = request.identity.user_id
                chunks.append(payload)
                accepted_chunks.append(chunk.model_copy(update={
                    "visibility": visibility,
                    "dense_vector": None,
                    "metadata": {
                        **chunk.metadata,
                        "chunks_are_final": True,
                        "version_id": chunk.metadata.get("version_id") or request.version_id,
                    },
                }))
                chunks_by_visibility.setdefault(visibility, []).append(payload)
            # 统一先写新块再清理旧块；MySQL 只替换本批记录。
            count = 0
            target_override = state.get("target_override")
            insert_only = state.get("insert_only", False)
            if insert_only and target_override is None:
                raise ValueError("insert_only 仅允许用于显式指定的影子 Collection")
            if target_override is not None:
                if any(visibility != "public" for visibility in chunks_by_visibility):
                    raise ValueError("影子 Collection 恢复只接受公开知识块")
                count = await asyncio.to_thread(
                    target_override.insert_chunks,
                    chunks_by_visibility.get("public", []),
                    flush=False,
                )
            else:
                reset_targets: list[tuple[str, MilvusService]] = [
                    ("public", self._milvus_for_visibility("public", request.knowledge_type)),
                ]
                if request.scope_id == "internal":
                    reset_targets.append(("private", self.private_milvus))
                seen_targets: set[int] = set()
                replacement_targets: dict[int, tuple[str, MilvusService, str, str | None]] = {}
                if request.replace_existing:
                    for visibility, target in reset_targets:
                        if id(target) in seen_targets:
                            continue
                        seen_targets.add(id(target))
                        mysql_ids = {str(chunk.mysql_id) for chunk in index_chunks if chunk.mysql_id}
                        source_ids = {str(chunk.source_id) for chunk in index_chunks if chunk.source_id}
                        delete_expr: str | None = None
                        if is_mysql_import:
                            if any(not chunk.mysql_id for chunk in index_chunks):
                                raise ValueError("MySQL 替换索引必须提供 mysql_id")
                            literals = ", ".join(
                                f'"{escape_milvus_expr_value(mysql_id)}"'
                                for mysql_id in sorted(mysql_ids)
                            )
                            delete_expr = f'chunk_role == "mysql" && mysql_id in [{literals}]'
                        elif any(
                            chunk.chunk_role in {"agent_draft", "admin_draft"}
                            for chunk in index_chunks
                        ):
                            draft_chunks = [
                                chunk for chunk in index_chunks
                                if chunk.chunk_role in {"agent_draft", "admin_draft"}
                            ]
                            draft_ids = {str(chunk.mysql_id or "") for chunk in draft_chunks}
                            draft_roles = {str(chunk.chunk_role or "") for chunk in draft_chunks}
                            if not draft_ids or "" in draft_ids or len(draft_roles) != 1:
                                raise ValueError("草稿组替换必须提供 mysql_id，且 chunk_role 保持一致")
                            mysql_ids = sorted(draft_ids)
                            chunk_role = next(iter(draft_roles))
                            mysql_filter = (
                                f'mysql_id == "{escape_milvus_expr_value(mysql_ids[0])}"'
                                if len(mysql_ids) == 1
                                else "mysql_id in ["
                                + ", ".join(
                                    f'"{escape_milvus_expr_value(mysql_id)}"'
                                    for mysql_id in mysql_ids
                                )
                                + "]"
                            )
                            delete_expr = (
                                f'chunk_role == "{escape_milvus_expr_value(chunk_role)}" '
                                f"&& {mysql_filter}"
                            )
                            document_scopes = sorted({
                                (chunk.document_id or request.document_id, chunk.chunk_role)
                                for chunk in index_chunks
                                if chunk.chunk_role in {"agent_doc", "admin_doc"}
                                and (chunk.document_id or request.document_id)
                            })
                            if document_scopes:
                                scopes = [delete_expr, *[
                                    f'document_id == "{escape_milvus_expr_value(document)}" '
                                    f'&& chunk_role == "{escape_milvus_expr_value(role)}"'
                                    for document, role in document_scopes
                                ]]
                                delete_expr = " || ".join(f"({scope})" for scope in scopes)
                        elif len(source_ids) == 1:
                            delete_expr = (
                                f'source_id == "{escape_milvus_expr_value(next(iter(source_ids)))}"'
                            )
                        elif len(mysql_ids) == 1:
                            delete_expr = (
                                f'mysql_id == "{escape_milvus_expr_value(next(iter(mysql_ids)))}"'
                            )
                        elif visibility == "private":
                            document_id = (
                                request.version_id.rsplit(":v", 1)[0]
                                if request.version_id
                                else request.document_id
                            )
                            delete_expr = f'document_id == "{escape_milvus_expr_value(document_id)}"'
                        else:
                            document_id = (
                                request.version_id.rsplit(":v", 1)[0]
                                if request.version_id
                                else request.document_id
                            )
                            delete_expr = f'document_id == "{escape_milvus_expr_value(document_id)}"'
                        replacement_targets[id(target)] = (
                            visibility,
                            target,
                            delete_expr,
                            request.identity.user_id if visibility == "private" else None,
                        )
                for visibility, target, delete_expr, user_id in replacement_targets.values():
                    count += await asyncio.to_thread(
                        target.replace_chunks,
                        chunks_by_visibility.get(visibility, []),
                        delete_expr=delete_expr,
                        user_id=user_id,
                    )
                for visibility, visibility_chunks in chunks_by_visibility.items():
                    target = self._milvus_for_visibility(visibility, request.knowledge_type)
                    if id(target) in replacement_targets:
                        continue
                    count += await asyncio.to_thread(target.upsert_chunks, visibility_chunks)
            if count == 0 and insert_only:
                return {
                    "result": IndexResult(
                        status="SUCCESS",
                        document_id=request.document_id,
                        indexed_count=0,
                        indexed_chunks=[],
                    )
                }
            if count == 0 and not chunks:
                return {
                    "result": IndexResult(
                        status="SUCCESS",
                        document_id=request.document_id,
                        indexed_count=0,
                        indexed_chunks=[],
                    )
                }
            if count == 0:
                return {
                    "result": IndexResult(
                        status="FAILED",
                        document_id=request.document_id,
                        error="Milvus 未写入任何有效分块，请检查 Collection 和 embedding 服务",
                    )
                }
            return {
                "result": IndexResult(
                    status="SUCCESS",
                    document_id=request.document_id,
                    indexed_count=count,
                    indexed_chunks=accepted_chunks,
                )
            }
        except Exception as exc:
            return {
                "result": IndexResult(
                    status="FAILED", document_id=request.document_id, error=str(exc)
                )
            }

    @staticmethod
    def _expand_chunks(
        request: IndexRequest,
        chunks: list[IndexChunk] | None = None,
    ) -> list[IndexChunk]:
        source_chunks = chunks if chunks is not None else deduplicate_source_chunks(request.chunks)
        expanded: list[IndexChunk] = []
        pending: list[IndexChunk] = []

        def flush() -> None:
            if pending:
                expanded.extend(chunk_document_blocks(
                    pending, target_size=request.chunk_size,
                    hard_max_size=max(request.chunk_size, min(request.chunk_size + 300, 1500)),
                    overlap=request.chunk_overlap,
                ))
                pending.clear()

        for chunk in source_chunks:
            if chunk.metadata.get("chunks_are_final"):
                flush()
                expanded.append(chunk)
                continue
            if pending and (chunk.document_id, chunk.chunk_role) != (pending[-1].document_id, pending[-1].chunk_role):
                flush()
            pending.append(chunk)
        flush()
        return [chunk.model_copy(update={"dense_vector": None}) for chunk in expanded]

    @classmethod
    def _prepare_index_chunks(
        cls,
        request: IndexRequest,
        *,
        allowed_image_ids_by_document: dict[str, set[str]] | None = None,
    ) -> list[IndexChunk]:
        """统一生成预览与 Milvus 实际使用的最终分块。"""
        is_mysql_import = request.document_id.startswith("mysql:")
        has_draft_chunk = any(
            str(chunk.chunk_role or (chunk.metadata or {}).get("chunk_role") or "")
            in {"agent_draft", "admin_draft"}
            for chunk in request.chunks
        )
        preview_chunking = any(
            (chunk.metadata or {}).get("_preview_chunking") for chunk in request.chunks
        )
        if request.chunks_are_final:
            chunks = []
            for chunk in request.chunks:
                if len(chunk.chunk_text.encode("utf-8")) <= MILVUS_CHUNK_TEXT_MAX_BYTES:
                    chunks.append(chunk)
                else:
                    # 旧最终块超出存储上限才允许沿既有类型策略重切。
                    chunks.extend(cls._expand_chunks(request, [chunk.model_copy(update={
                        "metadata": {**chunk.metadata, "chunks_are_final": False, "rechunk_reason": "MILVUS_TEXT_BYTE_LIMIT"},
                    })]))
        elif not preview_chunking and (
            (not is_mysql_import and request.knowledge_type == "solution" and not has_draft_chunk)
            or (
                not is_mysql_import
                and any((chunk.metadata or {}).get("preview_confirmed") for chunk in request.chunks)
            )
        ):
            chunks = confirmed_solution_chunks(request.chunks)
        else:
            source_chunks = (
                request.chunks if is_mysql_import else deduplicate_source_chunks(request.chunks)
            )
            policy_chunks = (
                source_chunks if is_mysql_import else apply_field_policy(request, source_chunks)
            )
            if not is_mysql_import:
                policy_chunks = [
                    chunk for chunk in policy_chunks if chunk.visibility != "internal_supplement"
                ]
            chunks = cls._expand_chunks(request, policy_chunks)
        chunks = _append_image_urls(
            chunks,
            request.document_id,
            allowed_image_ids_by_document=allowed_image_ids_by_document,
        )
        safe_chunks: list[IndexChunk] = []
        for chunk in chunks:
            for piece in split_text_by_utf8_bytes(chunk.chunk_text, MILVUS_CHUNK_TEXT_MAX_BYTES):
                safe_chunks.append(chunk.model_copy(update={"chunk_text": piece}))
        chunks = [chunk for chunk in safe_chunks if chunk.visibility != "internal_supplement"]
        if not is_mysql_import:
            public_texts = {
                normalize_chunk_text(chunk.chunk_text)
                for chunk in chunks
                if chunk.visibility == "public"
            }
            chunks = [
                chunk
                for chunk in chunks
                if not (
                    chunk.visibility != "public"
                    and normalize_chunk_text(chunk.chunk_text) in public_texts
                )
            ]
        record_indexes: dict[tuple[str, str, str], int] = {}
        normalized_chunks: list[IndexChunk] = []
        for chunk in chunks:
            if request.chunks_are_final or chunk.metadata.get("chunks_are_final"):
                normalized_chunks.append(chunk.model_copy(update={"dense_vector": None}))
                continue
            key = (chunk.document_id or request.document_id, str(chunk.mysql_id or ""), chunk.chunk_role or "")
            chunk_index = record_indexes.get(key, 0)
            record_indexes[key] = chunk_index + 1
            normalized_chunks.append(
                chunk.model_copy(
                    update={
                        "chunk_index": chunk_index,
                        "chunk_text": chunk.chunk_text,
                        "dense_vector": None,
                    }
                )
            )
        return normalized_chunks

    async def index(
        self,
        request: IndexRequest,
        *,
        target_override: MilvusService | None = None,
        insert_only: bool = False,
    ) -> IndexResult:
        state = await self._index(
            {"request": request, "target_override": target_override, "insert_only": insert_only}
        )
        return state["result"]

    @staticmethod
    def preview_chunks(request: IndexRequest) -> list[IndexChunk]:
        """执行正式索引前处理和分块，但不生成 embedding、不写入 Milvus。"""
        return RetrievalAgent._prepare_index_chunks(request)

    @staticmethod
    def preview_chunks_from_blocks(request: ChunkPreviewRequest) -> list[IndexChunk]:
        """把编辑器原始块转换为正式索引输入，再复用正式分块规则。"""
        chunks: list[IndexChunk] = []
        for index, block in enumerate(request.blocks):
            metadata = dict(block.metadata)
            if block.page_number is not None:
                metadata.setdefault("page_start", block.page_number)
                metadata.setdefault("page_end", block.page_number)
            block_type = block.block_type or "text"
            source = block.source_name or request.document_id
            if metadata.get("sheet_name") and metadata.get("row_number") is not None:
                locator = f"{source}|type:{block_type}|sheet:{metadata['sheet_name']}|row:{metadata['row_number']}"
            elif metadata.get("slide_start") is not None and metadata.get("slide_end") is not None:
                locator = f"{source}|type:{block_type}|slides:{metadata['slide_start']}-{metadata['slide_end']}|ordinal:{index + 1}"
            elif metadata.get("page_start") is not None and metadata.get("page_end") is not None:
                page_start, page_end = metadata["page_start"], metadata["page_end"]
                location = (
                    f"page:{page_start}"
                    if page_start == page_end
                    else f"pages:{page_start}-{page_end}"
                )
                locator = f"{source}|type:{block_type}|{location}|ordinal:{index + 1}"
            else:
                locator = f"{source}|type:{block_type}|ordinal:{index + 1}"
            chunks.append(
                IndexChunk(
                    chunk_text=block.text,
                    chunk_index=index,
                    source_locator=locator,
                    visibility=block.proposed_visibility,
                    document_id=request.document_id,
                    chunk_role="admin_doc",
                    metadata={**metadata, "_preview_chunking": True},
                )
            )
        index_request = IndexRequest(
            document_id=request.document_id,
            version_id=request.version_id,
            scope_id=request.scope_id,
            knowledge_type=request.knowledge_type,
            chunks=chunks,
            chunk_size=request.chunk_size,
            chunk_overlap=request.chunk_overlap,
            identity=request.identity,
        )
        prepared = KnowledgeRetrievalAgent._prepare_index_chunks(index_request)
        return [
            chunk.model_copy(
                update={
                    "metadata": {
                        key: value
                        for key, value in (chunk.metadata or {}).items()
                        if key != "_preview_chunking"
                    },
                }
            )
            for chunk in prepared
        ]

    def _milvus_for_scope(self, scope_id: str, knowledge_type: str) -> MilvusService:
        """internal 使用私有库；公开范围按知识类型路由到独立 Collection。"""

        if scope_id == "internal":
            return self.private_milvus
        return self.public_milvus.get(knowledge_type, self.milvus)

    def draft_source_ids(self, mysql_id: str, knowledge_type: str, chunk_role: str) -> list[str]:
        """Resolve legacy association IDs before republishing an existing draft."""
        target = self._milvus_for_visibility("public", knowledge_type)
        return target.source_ids_by_mysql_id(mysql_id, chunk_role)

    def _milvus_for_visibility(self, visibility: str, knowledge_type: str) -> MilvusService:
        if visibility == "private":
            return self.private_milvus
        if visibility == "internal_supplement":
            raise ValueError("internal_supplement 不允许写入 Milvus，应由 MySQL 承载")
        return self.public_milvus.get(knowledge_type, self.milvus)


def _is_mysql_evidence(evidence: Evidence) -> bool:
    """识别 MySQL 同步证据，避免把它当作 MinIO 文档查图片。"""

    document_id = str(evidence.document_id or "")
    source_locator = str(evidence.source_locator or "")
    return (
        evidence.source == "mysql"
        or evidence.chunk_role == "mysql"
        or document_id.startswith("mysql:")
        or source_locator.startswith("mysql:")
    )


KnowledgeRetrievalAgent = RetrievalAgent


def _append_image_urls(
    chunks: list[IndexChunk],
    document_id: str,
    *,
    allowed_image_ids_by_document: dict[str, set[str]] | None = None,
) -> list[IndexChunk]:
    """在原图片引用位置替换受控 URL，预览和正式索引共用。"""
    output: list[IndexChunk] = []
    seen_image_ids: set[tuple[str, str]] = set()
    for chunk in chunks:
        metadata = dict(chunk.metadata or {})
        candidate_image_ids = [
            str(value).strip() for value in metadata.get("image_ids") or [] if str(value).strip()
        ]
        resolved_document_id = str(
            chunk.document_id or metadata.get("document_id") or document_id or ""
        )
        image_ids = []
        for image_id in candidate_image_ids:
            image_key = (resolved_document_id, image_id)
            if image_key not in seen_image_ids:
                image_ids.append(image_id)
                if chunk.visibility != "internal_supplement":
                    seen_image_ids.add(image_key)
        if image_ids:
            metadata["image_ids"] = image_ids
        else:
            metadata.pop("image_ids", None)
        image_refs = {
            str(item.get("image_id") or "").strip(): [
                str(source).strip() for source in item.get("sources") or [] if str(source).strip()
            ]
            for item in metadata.get("image_refs") or []
            if isinstance(item, dict)
            and str(item.get("image_id") or "").strip() in image_ids
        }
        if allowed_image_ids_by_document is not None:
            allowed_image_ids = allowed_image_ids_by_document.get(resolved_document_id, set())
            unavailable_sources = {
                source.replace("\\", "/").lstrip("./").lstrip("/")
                for image_id, sources in image_refs.items()
                if image_id not in allowed_image_ids
                for source in sources
            }
            text = chunk.chunk_text

            def keep_available_image(match: re.Match[str]) -> str:
                target = match.group(1).strip().strip("<>")
                route_match = re.search(
                    r"/api/v1/perception/images/([^/]+)/([^/?#]+)$", target
                )
                if route_match:
                    image_document_id = unquote(route_match.group(1))
                    image_id = unquote(route_match.group(2))
                    if image_id not in allowed_image_ids_by_document.get(image_document_id, set()):
                        return ""
                normalized_target = target.replace("\\", "/").lstrip("./").lstrip("/")
                if any(
                    normalized_target == source or normalized_target.endswith(f"/{source}")
                    for source in unavailable_sources
                ):
                    return ""
                return match.group(0)

            text = re.sub(r"!\[[^\]]*\]\(([^)\s]+)\)", keep_available_image, text)
            image_ids = [image_id for image_id in image_ids if image_id in allowed_image_ids]
            image_refs = {
                image_id: sources
                for image_id, sources in image_refs.items()
                if image_id in allowed_image_ids
            }
            if image_ids:
                metadata["image_ids"] = image_ids
            else:
                metadata.pop("image_ids", None)
            if image_refs:
                metadata["image_refs"] = [
                    {"image_id": image_id, "sources": sources}
                    for image_id, sources in image_refs.items()
                ]
            else:
                metadata.pop("image_refs", None)
        else:
            text = chunk.chunk_text
        urls = [
            perception_image_url(resolved_document_id, image_id)
            for image_id in image_ids
            if resolved_document_id
        ]
        matched_ids: set[str] = set()
        for image_id, url in zip(image_ids, urls, strict=False):
            sources = sorted(image_refs.get(image_id, []), key=len, reverse=True)
            for source in sources:
                normalized = source.replace("\\", "/").lstrip("./").lstrip("/")
                candidates = dict.fromkeys((source, normalized, f"/{normalized}"))
                if any(candidate and candidate in text for candidate in candidates):
                    for candidate in candidates:
                        if candidate:
                            text = text.replace(candidate, url)
                    matched_ids.add(image_id)
                    break
        # 有匹配引用时只替换原位图片，避免把同页未引用的图片追加进来。
        if not matched_ids and urls:
            missing_markdown = [
                f"![图片{index}]({url})"
                for index, url in enumerate(urls, start=1)
                if url not in text
            ]
            if missing_markdown:
                text = f"{text.rstrip()}\n" + "\n".join(missing_markdown)
        if urls:
            metadata["image_urls"] = urls
        else:
            metadata.pop("image_urls", None)
        output.append(
            chunk.model_copy(
                update={
                    "chunk_text": text,
                    "document_id": (
                        resolved_document_id or None
                        if chunk.chunk_role in {"admin_doc", "agent_doc"}
                        else chunk.document_id
                    ),
                    "metadata": metadata,
                }
            )
        )
    return output
