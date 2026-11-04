"""检索服务 - Milvus 原生 BM25 + dense / RRF 混合召回 + 重排。"""

import asyncio
import hashlib
import html
import re
from dataclasses import dataclass, field
from time import perf_counter
from urllib.parse import quote, unquote

from agents.retrieval_agent.application.retrieval_helpers import related_documents, to_evidence
from agents.retrieval_agent.retrieval.embedding_service import EmbeddingService
from agents.retrieval_agent.retrieval.query_service import QueryService
from agents.retrieval_agent.retrieval.rerank_service import (
    RerankOutcome,
    RerankService,
    RerankUnavailableError,
)
from agents.retrieval_agent.retrieval.strategy import RetrievalStrategy
from agents.retrieval_agent.schemas import RetrievalHit
from core.contracts.draft_contracts import DRAFT_FIELD_LABELS
from core.contracts.field_permissions import PUBLIC_FIELDS
from core.contracts.mysql_schema import MYSQL_FIELD_COMMENTS, ordered_fields
from core.contracts.retrieval import is_match_result_excluded, is_source_chunk
from core.governance.access_policy import AccessPolicy, ManualIdentity, UserType
from core.prompts.retrieval import DETAIL_SUMMARY_SYSTEM
from runtime.config import get_settings
from services.observability.logging import get_logger
from services.observability.metrics import metrics
from services.observability.trace_context import get_trace_context
from services.storage.minio.image_service import ImageManifestService, perception_image_url
from services.vector.milvus.milvus_service import (
    PRIVATE_COLLECTION_NAME,
    PUBLIC_COLLECTION_ALIASES,
    PUBLIC_COLLECTIONS,
    MilvusConnectionManager,
    MilvusService,
)

DEFAULT_TOP_K = 3
logger = get_logger("retrieval", "search")


def _filter_external_mysql_hits(
    hits: list[RetrievalHit],
    identity: ManualIdentity,
    target_type: str,
    requested_type: str | None = None,
) -> list[RetrievalHit]:
    if identity.user_role != UserType.EXTERNAL:
        return hits

    public_fields = PUBLIC_FIELDS[target_type]
    labels = {
        label: field
        for field in ordered_fields(target_type)
        for label in (
            field,
            MYSQL_FIELD_COMMENTS[target_type].get(field, field),
            DRAFT_FIELD_LABELS.get(target_type, {}).get(field, field),
        )
    }
    visible: list[RetrievalHit] = []
    for hit in hits:
        metadata = hit.metadata
        hit_type = str(
            metadata.get("source_knowledge_type")
            or metadata.get("knowledge_type")
            or requested_type
            or ""
        ).strip().lower()
        is_mysql_record = (
            str(metadata.get("chunk_role") or "").lower() == "mysql"
            or str(metadata.get("storage") or "").lower() == "mysql"
            or hit.source == "mysql"
        )
        if hit_type != target_type or not is_mysql_record:
            visible.append(hit)
            continue

        fields = metadata.get("fields")
        field_names = metadata.get("field_names")
        if not isinstance(field_names, (list, tuple, set)) or not field_names:
            field_names = list(fields) if isinstance(fields, dict) else []
        if not field_names:
            continue

        allowed_names = [str(name) for name in field_names if str(name) in public_fields]
        if not allowed_names:
            continue

        sanitized = dict(metadata)
        sanitized["field_names"] = allowed_names
        if isinstance(fields, dict):
            public_values = {key: value for key, value in fields.items() if str(key) in public_fields}
            sanitized["fields"] = public_values
            sanitized["chunk_text"] = "\n".join(
                f"{key}：{value}" for key, value in public_values.items()
                if value is not None and str(value).strip()
            )
        else:
            current_field_is_public = False
            public_lines: list[str] = []
            for line in str(metadata.get("chunk_text") or "").splitlines():
                normalized_line = line.lstrip()
                separators = [
                    index
                    for index in (normalized_line.find("："), normalized_line.find(":"))
                    if index >= 0
                ]
                separator_index = min(separators) if separators else -1
                label = normalized_line[:separator_index].strip() if separator_index >= 0 else ""
                matched = labels.get(label)
                if matched is None and target_type == "demand" and separator_index < 0:
                    matched = next(
                        (field for alias, field in labels.items() if normalized_line.startswith(alias)),
                        None,
                    )
                if matched is not None:
                    current_field_is_public = matched in public_fields
                elif separator_index >= 0 or target_type == "enterprise":
                    current_field_is_public = False
                if current_field_is_public:
                    public_lines.append(normalized_line)
            sanitized["chunk_text"] = "\n".join(public_lines).strip()

        if sanitized["chunk_text"]:
            visible.append(
                RetrievalHit(
                    record_id=hit.record_id,
                    score=hit.score,
                    source=hit.source,
                    metadata=sanitized,
                )
            )
    return visible


def filter_external_enterprise_hits(
    hits: list[RetrievalHit], identity: ManualIdentity, knowledge_type: str | None = None
) -> list[RetrievalHit]:
    """外部身份保留企业公开字段命中，并剔除非公开字段内容。"""
    return _filter_external_mysql_hits(hits, identity, "enterprise", knowledge_type)


def filter_external_demand_hits(
    hits: list[RetrievalHit], identity: ManualIdentity, knowledge_type: str | None = None
) -> list[RetrievalHit]:
    """在重排前移除外部用户需求命中的联系人字段。"""
    return _filter_external_mysql_hits(hits, identity, "demand", knowledge_type)


def _clean_source_context(value: str) -> str:
    """轻量整理解析原文，不改变事实内容。"""
    text = str(value or "").replace("\r\n", "\r\n").replace("\r", "\r\n")
    text = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b\ufeff]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r\n[ \t]+", "\r\n", text)
    return re.sub(r"\r\n{3,}", "\r\n\r\n", text).strip()


@dataclass(frozen=True)
class SearchResult:
    """检索结果与重排执行状态。"""

    hits: list[RetrievalHit]
    rerank_applied: bool = False
    retrieval_hit_count: int = 0
    retrieval_top_score: float | None = None
    retrieval_mode: str = "full"
    unavailable_collections: list[str] = field(default_factory=list)


class RecallResult(list[list[RetrievalHit]]):
    """List-compatible recall output with per-request collection health."""

    def __init__(
        self,
        hit_lists: list[list[RetrievalHit]],
        unavailable_collections: list[str],
    ) -> None:
        super().__init__(hit_lists)
        self.unavailable_collections = unavailable_collections

    @property
    def hit_lists(self) -> list[list[RetrievalHit]]:
        return self


class SearchService:
    """Milvus 原生 BM25 + dense / RRF 混合检索 + 重排服务。"""

    def __init__(self, strategy: RetrievalStrategy | None = None) -> None:
        self.settings = get_settings()
        self.embedding_service = EmbeddingService()
        self.connection_manager = MilvusConnectionManager(
            uri=self.settings.milvus_uri,
            database=self.settings.milvus_database,
            username=self.settings.milvus_username,
            password=self.settings.milvus_password,
        )
        self.public_milvus = {
            knowledge_type: MilvusService(
                collection_name=collection_name,
                alias_name=PUBLIC_COLLECTION_ALIASES[knowledge_type],
                connection_manager=self.connection_manager,
            )
            for knowledge_type, collection_name in PUBLIC_COLLECTIONS.items()
        }
        # 兼容现有调用方：默认公开库指向资源方技术方案库。
        self.milvus = self.public_milvus["solution"]
        self.private_milvus = MilvusService(
            collection_name=PRIVATE_COLLECTION_NAME,
            requires_user_id=True,
            connection_manager=self.connection_manager,
        )
        self.rerank_service = RerankService()
        self.query_service = QueryService()
        self.rerank_top_k = self.settings.rerank_top_k  # 重排后返回 top_k
        self.strategy = strategy or self

    def close(self) -> None:
        """停止各 Collection 的加载恢复，再关闭共用连接。"""
        services = getattr(self, "public_milvus", {})
        milvus_services = [*services.values(), getattr(self, "private_milvus", None)]
        for milvus in milvus_services:
            stop_recovery = getattr(milvus, "stop_collection_load_recovery", None)
            if callable(stop_recovery):
                stop_recovery()
        self.connection_manager.close()

    async def summarize_chunk_detail(
        self,
        detail: dict[str, object],
        knowledge_type: str | None,
    ) -> dict[str, object]:
        """用推理模型对命中内容生成简短总结。"""

        result = dict(detail)
        draft_chunks = [
            item
            for item in (detail.get("draft_chunks") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        source_chunks = [
            item
            for item in (detail.get("source_chunks") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        chunks = [*draft_chunks, *source_chunks]
        image_urls = list(
            dict.fromkeys(
                str(url)
                for item in chunks
                for url in (item.get("image_urls") or [])
                if str(url).strip()
            )
        )
        result["image_urls"] = image_urls
        if not chunks:
            return result
        try:
            from runtime.config import get_settings, settings_for_role
            from services.llm import get_llm_client

            settings = settings_for_role(get_settings(), "reasoning")
            if not all(
                (
                    settings.llm_provider,
                    settings.llm_model,
                    settings.llm_base_url,
                    settings.llm_api_key,
                )
            ):
                return result
            draft_content = "\r\n\r\n".join(str(item.get("text") or "") for item in draft_chunks)
            source_content = "\r\n\r\n".join(str(item.get("text") or "") for item in source_chunks)
            content = (
                f"【草稿主内容】\r\n{draft_content or '无'}\r\n\r\n"
                f"【文档原文】\r\n{source_content or '无'}"
            )
            prompt = [
                {
                    "role": "system",
                    "content": DETAIL_SUMMARY_SYSTEM,
                },
                {
                    "role": "user",
                    "content": f"资料类型：{knowledge_type or '知识资料'}\r\n\r\n相关内容：\r\n{content}",
                },
            ]
            summary = (await get_llm_client(settings).complete(prompt)).strip()
            if summary:
                result["formatted_text"] = summary
            if source_content:
                result["cleaned_source_text"] = _clean_source_context(source_content)
        except Exception:
            logger.warning("chunk_detail_llm_summary_failed", exc_info=True)
        return result

    def _resolve_search_targets(
        self,
        scopes: list[str],
        document_id: str | None,
        user_id: str,
        knowledge_type: str | None,
        knowledge_types: list[str] | None,
        _requested_scope: str | None,
    ) -> list[tuple[MilvusService, str | None]]:
        """Resolve the Milvus collections visible to the current identity."""

        targets: list[tuple[MilvusService, str | None]] = []
        if "public" in scopes:
            selected_types = knowledge_types or (
                [knowledge_type]
                if knowledge_type in self.public_milvus
                else list(self.public_milvus)
            )
            targets.extend(
                (self.public_milvus[item], None)
                for item in selected_types
                if item in self.public_milvus
            )
        if "internal" in scopes:
            targets.append((self.private_milvus, self._build_private_expr(document_id, user_id)))
        return targets

    async def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        scope_id: str | None = None,
        knowledge_type: str | None = None,
        document_id: str | None = None,
        use_rerank: bool = True,
        expand_query: bool = False,
        knowledge_types: list[str] | None = None,
        identity: ManualIdentity | None = None,
        exclude_result_ids: list[str] | None = None,
    ) -> list[RetrievalHit]:
        """执行检索，仅返回命中结果；需要过程状态时使用 ``search_with_trace``。

        兼容现有调用方。
        """
        result = await self.search_with_trace(
            query=query,
            top_k=top_k,
            scope_id=scope_id,
            knowledge_type=knowledge_type,
            document_id=document_id,
            use_rerank=use_rerank,
            expand_query=expand_query,
            knowledge_types=knowledge_types,
            identity=identity,
            exclude_result_ids=exclude_result_ids,
        )
        return result.hits

    async def search_with_trace(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        scope_id: str | None = None,
        knowledge_type: str | None = None,
        document_id: str | None = None,
        use_rerank: bool = True,
        expand_query: bool = False,
        knowledge_types: list[str] | None = None,
        identity: ManualIdentity | None = None,
        exclude_result_ids: list[str] | None = None,
    ) -> SearchResult:
        """执行 Milvus 原生 hybrid search + 可选 Query 扩展 + 重排。

        1. 可选：QueryService 扩展查询；
        2. Embedding 服务生成查询 dense vector；
        3. Milvus BM25 Function 从查询文本生成 sparse query；
        4. Milvus Hybrid Search 使用 RRFRanker 融合两路结果；
        5. RerankService 对候选进行重排精选。

        Args:
            query: 用户查询文本
            top_k: 最终返回的结果数量
            scope_id: 限定知识范围 ID
            knowledge_type: 限定公开知识库类型；为空时检索全部公开类型
            document_id: 限定文档 ID
            use_rerank: 是否启用重排（默认 True）
            expand_query: 是否启用 Query 扩展（默认 False）
        """
        max_rerank_results = max(int(getattr(self.settings, "rerank_top_k", 10)), 1)
        requested_top_k = max(int(top_k), 1)
        top_k = min(
            requested_top_k,
            max(int(getattr(self.settings, "rag_fusion_candidates", 60)), 1),
        )
        if not query or not query.strip():
            return SearchResult(hits=[])
        search_started = perf_counter()
        logger.info(
            "检索步骤开始: query",
            extra={"structured": {"event": "retrieval_started", "topK": top_k}},
        )

        # Query 扩展
        expanded_queries = [query]
        if expand_query:
            logger.info(
                "检索步骤开始: query_expand",
                extra={"structured": {"event": "retrieval_query_expand_started"}},
            )
            rewrite_started = perf_counter()
            with metrics.stage("retrieval.query_rewrite"):
                expanded = await self.query_service.expand_query(query)
            if expanded:
                expanded_queries = [query] + expanded
            logger.info(
                "检索步骤完成: query_expand",
                extra={
                    "structured": {
                        "event": "retrieval_query_expand_finished",
                        "expandedCount": len(expanded_queries) - 1,
                        "latencyMs": int((perf_counter() - rewrite_started) * 1000),
                    }
                },
            )

        if identity is None:
            raise ValueError("检索必须提供用户身份")
        access_scopes = AccessPolicy.resolve_scopes(identity, scope_id)
        if not access_scopes:
            return SearchResult(hits=[])
        targets = self._resolve_search_targets(
            access_scopes, document_id, identity.user_id, knowledge_type, knowledge_types, scope_id
        )
        if not targets:
            return SearchResult(hits=[])

        excluded_ids = set(exclude_result_ids or [])
        recall_k = min(
            50,
            max(
                max(int(getattr(self.settings, "rag_recall_k", 15)), 1),
                requested_top_k + len(excluded_ids),
            ),
        )

        logger.info(
            "初排开始",
            extra={
                "structured": {
                    "event": "retrieval_initial_rank_started",
                    "queryCount": len(expanded_queries),
                    "targetCount": len(targets),
                    "recallK": recall_k,
                }
            },
        )
        initial_started = perf_counter()
        # 每个 Query 只生成一次向量；每个 Query × Collection 保持独立排名列表。
        recall_result = await self.strategy.recall(
            expanded_queries, targets, recall_k=recall_k
        )
        unavailable_collections = list(
            getattr(recall_result, "unavailable_collections", [])
        )
        query_hit_lists = getattr(recall_result, "hit_lists", recall_result)
        step_mode = str(getattr(self.settings, "rag_retrieval_mode", "full")).lower() == "step"

        # 多 Query、多 Collection 统一排名融合；所有融合候选都交给重排，
        # 不能在重排前按 RRF 顺序截断，否则后面的候选没有机会被模型纠正排序。
        with metrics.stage("retrieval.rrf"):
            # 草稿必须独立参与评分，不能先与原文合并而丢失草稿相关度。
            merged_hits = self.strategy.merge_chunks(query_hit_lists)
        merged_hits = filter_external_enterprise_hits(merged_hits, identity)
        merged_hits = filter_external_demand_hits(merged_hits, identity)
        if excluded_ids:
            merged_hits = [
                hit for hit in merged_hits
                if not is_match_result_excluded({**hit.metadata, "record_id": hit.record_id}, excluded_ids)
            ]
        fusion_limit = max(top_k, int(getattr(self.settings, "rag_fusion_candidates", 60)))
        merged_hits = merged_hits[:fusion_limit]
        logger.info(
            "初排完成",
            extra={
                "structured": {
                    "event": "retrieval_initial_rank_finished",
                    "listCount": len(query_hit_lists),
                    "candidateCount": len(merged_hits),
                    "latencyMs": int((perf_counter() - initial_started) * 1000),
                }
            },
        )
        # 召回结果直接进入选择/重排；不再为每个命中记录二次查询同文档上下文。
        selected_hits: list[RetrievalHit]

        if use_rerank and not self.settings.rerank_enabled:
            raise RerankUnavailableError("当前检索要求重排，但重排服务已禁用")
        if not use_rerank:
            selected_hits = self._select_final_hits(
                merged_hits,
                top_k=top_k,
                rerank_applied=False,
                group_by_business_record=not step_mode,
            )
            rerank_applied = False
        else:
            # 重排阶段
            rerank_output_k = min(len(merged_hits), max(max_rerank_results, top_k))
            logger.info(
                "重排开始",
                extra={
                    "structured": {
                        "event": "retrieval_rerank_started",
                        "candidateCount": len(merged_hits),
                        "topK": rerank_output_k,
                    }
                },
            )
            try:
                with metrics.stage("retrieval.rerank"):
                    reranked, outcome = await self.strategy.rerank(
                        query, merged_hits, top_k=rerank_output_k
                    )
                if merged_hits and not outcome.applied:
                    raise RerankUnavailableError("重排未完成")
                rerank_applied = outcome.applied
            except RerankUnavailableError as exc:
                reranked = merged_hits
                rerank_applied = False
                logger.warning(
                    "远程重排失败，按原始融合分返回候选",
                    extra={
                        "structured": {
                            "event": "retrieval_rerank_fallback",
                            "candidateCount": len(merged_hits),
                            "errorType": type(exc).__name__,
                        }
                    },
                )
            selected_hits = self._select_final_hits(
                reranked,
                top_k=top_k,
                rerank_applied=rerank_applied,
                group_by_business_record=not step_mode,
            )
            logger.info(
                "重排完成"
                if rerank_applied
                else "按原始融合分完成排序"
                if merged_hits
                else "没有召回候选，无需重排",
                extra={
                    "structured": {
                        "event": "retrieval_rerank_finished",
                        "applied": rerank_applied,
                        "resultCount": len(reranked),
                    }
                },
            )
        final_hits = selected_hits
        logger.info(
            "检索步骤完成: search",
            extra={
                "structured": {
                    "event": "retrieval_finished",
                    "resultCount": len(final_hits),
                    "rerankApplied": rerank_applied,
                    "latencyMs": int((perf_counter() - search_started) * 1000),
                }
            },
        )
        return SearchResult(
            hits=final_hits,
            rerank_applied=rerank_applied,
            retrieval_hit_count=len(final_hits),
            retrieval_top_score=max((hit.score for hit in final_hits), default=None),
            retrieval_mode="step" if step_mode else "full",
            unavailable_collections=unavailable_collections,
        )

    async def recall(
        self,
        expanded_queries: list[str],
        targets: list[tuple[MilvusService, str | None]],
        *,
        recall_k: int,
    ) -> RecallResult:
        """默认 Milvus 混合召回策略。"""

        query_hit_lists: list[list[RetrievalHit]] = []
        unavailable_collections: list[str] = []
        recall_k = max(int(recall_k), 1)
        if not targets:
            return RecallResult([], [])
        for query_index, query in enumerate(expanded_queries):
            embedding_started = perf_counter()
            logger.info(
                "检索步骤开始: embedding",
                extra={
                    "structured": {
                        "event": "retrieval_embedding_started",
                        "queryIndex": query_index,
                    }
                },
            )
            with metrics.stage("retrieval.embedding"):
                dense_vecs = await self.embedding_service.encode_dense([query])
            logger.info(
                "检索步骤完成: embedding",
                extra={
                    "structured": {
                        "event": "retrieval_embedding_finished",
                        "queryIndex": query_index,
                        "vectorCount": len(dense_vecs),
                        "latencyMs": int((perf_counter() - embedding_started) * 1000),
                    }
                },
            )
            milvus_tasks = [
                asyncio.to_thread(
                    milvus.search_hybrid,
                    query_text=query,
                    query_vector=dense_vecs[0],
                    top_k=recall_k,
                    expr=expr,
                )
                for milvus, expr in targets
            ]
            milvus_started = perf_counter()
            logger.info(
                "检索步骤开始: milvus",
                extra={
                    "structured": {
                        "event": "retrieval_milvus_started",
                        "queryIndex": query_index,
                        "targetCount": len(milvus_tasks),
                        "recallK": recall_k,
                    }
                },
            )
            with metrics.stage("retrieval.milvus"):
                raw_results = await asyncio.gather(*milvus_tasks, return_exceptions=True)
            milvus_results: list[list[RetrievalHit] | None] = []
            failed_errors: list[Exception] = []
            for (milvus, _), result in zip(targets, raw_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    failed_errors.append(result)
                    collection_name = str(
                        getattr(milvus, "collection_name", "unknown")
                    )
                    if collection_name not in unavailable_collections:
                        unavailable_collections.append(collection_name)
                    logger.error(
                        "Milvus 知识库本次检索不可用",
                        exc_info=(type(result), result, result.__traceback__),
                        extra={
                            "structured": {
                                "event": "milvus_knowledge_unavailable",
                                "traceparent": get_trace_context().traceparent,
                                "collection": collection_name,
                                "operation": "hybrid_search",
                                "query_index": query_index,
                                "error_type": type(result).__name__,
                                "error": str(result),
                            }
                        },
                    )
                    milvus_results.append(None)
                else:
                    milvus_results.append(result)
            if all(result is None for result in milvus_results):
                raise RuntimeError("all Milvus collections unavailable") from failed_errors[0]
            if not any(milvus_results):
                exact_indexes = [
                    index for index, result in enumerate(milvus_results) if result is not None
                ]
                exact_tasks = [
                    asyncio.to_thread(
                        targets[index][0].search_text_prefix,
                        query_text=query,
                        top_k=recall_k,
                        expr=targets[index][1],
                    )
                    for index in exact_indexes
                ]
                with metrics.stage("retrieval.text_prefix_fallback"):
                    exact_results = await asyncio.gather(
                        *exact_tasks, return_exceptions=True
                    )
                for index, result in zip(exact_indexes, exact_results, strict=True):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    if isinstance(result, Exception):
                        milvus, _ = targets[index]
                        failed_errors.append(result)
                        collection_name = str(
                            getattr(milvus, "collection_name", "unknown")
                        )
                        if collection_name not in unavailable_collections:
                            unavailable_collections.append(collection_name)
                        logger.error(
                            "Milvus 知识库文本回退不可用",
                            exc_info=(type(result), result, result.__traceback__),
                            extra={
                                "structured": {
                                    "event": "milvus_knowledge_unavailable",
                                    "traceparent": get_trace_context().traceparent,
                                    "collection": collection_name,
                                    "operation": "text_prefix_fallback",
                                    "query_index": query_index,
                                    "error_type": type(result).__name__,
                                    "error": str(result),
                                }
                            },
                        )
                        milvus_results[index] = None
                    else:
                        milvus_results[index] = result
                if all(result is None for result in milvus_results):
                    raise RuntimeError("all Milvus collections unavailable") from failed_errors[-1]
            completed_results = [
                result if result is not None else [] for result in milvus_results
            ]
            for (milvus, _), hits in zip(targets, completed_results, strict=True):
                knowledge_type = next(
                    (item for item, service in self.public_milvus.items() if service is milvus),
                    None,
                )
                if knowledge_type:
                    for hit in hits:
                        hit.metadata.setdefault("source_knowledge_type", knowledge_type)
            query_hit_lists.extend(completed_results)
            logger.info(
                "检索步骤完成: milvus",
                extra={
                    "structured": {
                        "event": "retrieval_milvus_finished",
                        "queryIndex": query_index,
                        "hitCount": sum(len(items) for items in completed_results),
                        "latencyMs": int((perf_counter() - milvus_started) * 1000),
                    }
                },
            )
        return RecallResult(query_hit_lists, unavailable_collections)

    def merge(self, query_hit_lists: list[list[RetrievalHit]]) -> list[RetrievalHit]:
        return self._merge_query_hits(query_hit_lists)

    def merge_chunks(self, query_hit_lists: list[list[RetrievalHit]]) -> list[RetrievalHit]:
        return self._merge_query_chunks(query_hit_lists)

    @staticmethod
    def _merge_query_chunks(query_hit_lists: list[list[RetrievalHit]]) -> list[RetrievalHit]:
        """分步模式：只按同一子块去重，不合并同一业务记录的不同子块。"""

        unique: dict[str, RetrievalHit] = {}
        for hits in query_hit_lists:
            for hit in hits:
                key = f"{_hit_chunk_key(hit)}:source:{hit.metadata.get('source_id') or ''}"
                current = unique.get(key)
                if current is None or (
                    float(hit.metadata.get("matched_draft_score", float("-inf"))), hit.score
                ) > (
                    float(current.metadata.get("matched_draft_score", float("-inf"))), current.score
                ):
                    unique[key] = hit
        return sorted(unique.values(), key=lambda hit: hit.score, reverse=True)

    @staticmethod
    def _merge_query_hits(query_hit_lists: list[list[RetrievalHit]]) -> list[RetrievalHit]:
        """完整模式：按业务记录合并命中块，并去除 HTML/纯文本重复版本。"""

        grouped: dict[str, RetrievalHit] = {}
        representative_ranks: dict[str, tuple[bool, float]] = {}
        content_keys: dict[str, set[str]] = {}
        counts: dict[str, int] = {}
        for hits in query_hit_lists:
            for hit in hits:
                group_key = _hit_business_key(hit) or f"chunk:{_hit_chunk_key(hit)}"
                item = grouped.get(group_key)
                text = str(hit.metadata.get("chunk_text") or "").strip()
                content_key = _hit_content_key(hit)
                rank = (is_source_chunk(hit.metadata), hit.score)
                if item is None:
                    grouped[group_key] = RetrievalHit(
                        record_id=hit.record_id,
                        score=hit.score,
                        source=hit.source,
                        metadata=dict(hit.metadata),
                    )
                    content_keys[group_key] = {content_key}
                    representative_ranks[group_key] = rank
                    counts[group_key] = 1 if text else 0
                    continue
                item.score = max(item.score, hit.score)
                if rank > representative_ranks[group_key]:
                    previous_text = item.metadata.get("chunk_text", "")
                    item.record_id = hit.record_id
                    item.source = hit.source
                    item.metadata = {**hit.metadata, "chunk_text": previous_text}
                    representative_ranks[group_key] = rank
                if content_key in content_keys[group_key] or not text:
                    continue
                content_keys[group_key].add(content_key)
                counts[group_key] += 1
                previous = str(item.metadata.get("chunk_text") or "").strip()
                item.metadata["chunk_text"] = f"{previous}\r\n\r\n{text}" if previous else text
        result = sorted(grouped.items(), key=lambda item: item[1].score, reverse=True)
        for group_key, hit in result:
            if counts.get(group_key, 0) > 1:
                hit.metadata["merged_chunk_count"] = counts[group_key]
        return [hit for _, hit in result]

    @staticmethod
    def _select_final_hits(
        hits: list[RetrievalHit],
        *,
        top_k: int,
        rerank_applied: bool = False,
        group_by_business_record: bool = False,
    ) -> list[RetrievalHit]:
        """统一执行最终去重和 Top-K 截断。"""

        best_drafts: dict[str, RetrievalHit] = {}
        related_drafts: dict[tuple[str, str], RetrievalHit] = {}
        for hit in hits:
            if hit.metadata.get("chunk_role") not in {"agent_draft", "admin_draft", "mysql"}:
                continue
            key = _hit_business_key(hit) or _hit_chunk_key(hit)
            if key not in best_drafts or hit.score > best_drafts[key].score:
                best_drafts[key] = hit
            source_id = str(hit.metadata.get("source_id") or "").strip()
            source_key = (
                str(hit.metadata.get("source_knowledge_type") or hit.metadata.get("knowledge_type") or "").strip(),
                source_id,
            )
            if source_id and (source_key not in related_drafts or hit.score > related_drafts[source_key].score):
                related_drafts[source_key] = hit
        prepared = []
        for hit in hits:
            draft = best_drafts.get(_hit_business_key(hit) or _hit_chunk_key(hit))
            if is_source_chunk(hit.metadata) and not (
                hit.metadata.get("matched_draft_mysql_id") or hit.metadata.get("mysql_id")
            ):
                draft = related_drafts.get((
                    str(hit.metadata.get("source_knowledge_type") or hit.metadata.get("knowledge_type") or "").strip(),
                    str(hit.metadata.get("source_id") or "").strip(),
                ))
            if draft and is_source_chunk(hit.metadata):
                hit = RetrievalHit(hit.record_id, hit.score, hit.source, {
                    **hit.metadata, "matched_draft_record_id": draft.record_id,
                    "matched_draft_mysql_id": draft.metadata.get("mysql_id"),
                    "matched_draft_score": draft.score,
                })
            elif (
                draft
                and hit.metadata.get("chunk_role") in {"agent_draft", "admin_draft", "mysql"}
                and hit.record_id != draft.record_id
            ):
                continue
            prepared.append(hit)
        # 先选唯一原文，再合并关联业务，避免被淘汰的原文连带吞掉兄弟草稿。
        source_chunks: dict[str, RetrievalHit] = {}
        for hit in prepared:
            if not is_source_chunk(hit.metadata):
                continue
            key = _hit_chunk_key(hit)
            current = source_chunks.get(key)
            if current is None or (
                float(hit.metadata.get("matched_draft_score", float("-inf"))), hit.score
            ) > (
                float(current.metadata.get("matched_draft_score", float("-inf"))), current.score
            ):
                source_chunks[key] = hit
        prepared = [
            hit for hit in prepared
            if not is_source_chunk(hit.metadata) or source_chunks[_hit_chunk_key(hit)] is hit
        ]
        unique: dict[str, RetrievalHit] = {}
        scores: dict[str, float] = {}
        for hit in prepared:
            key = _hit_business_key(hit) or f"chunk:{_hit_chunk_key(hit)}"
            current = unique.get(key)
            scores[key] = max(scores.get(key, hit.score), hit.score)
            if current is None or (is_source_chunk(hit.metadata), hit.score) > (
                is_source_chunk(current.metadata), current.score
            ):
                unique[key] = hit
        candidates = [unique[key] for key in sorted(unique, key=scores.__getitem__, reverse=True)]
        return candidates[: max(int(top_k), 0)]

    async def rerank(
        self,
        query: str,
        hits: list[RetrievalHit],
        top_k: int,
    ) -> tuple[list[RetrievalHit], RerankOutcome]:
        """通过重排服务排序召回结果。"""
        if not hits:
            return [], RerankOutcome(results=[], applied=False)
        outcome = await self.rerank_service.rerank_with_status(
            query=query,
            documents=[str(hit.metadata.get("chunk_text") or "") for hit in hits],
            top_k=top_k,
        )
        reranked = [
            RetrievalHit(
                record_id=hits[item.index].record_id,
                score=item.score,
                source=f"{hits[item.index].source}+rerank",
                metadata=hits[item.index].metadata,
            )
            for item in outcome.results
            if 0 <= item.index < len(hits)
        ]
        return reranked, outcome

    async def get_chunk_detail(
        self,
        *,
        document_id: str | None,
        mysql_id: str | None,
        source_id: str | None = None,
        matched_draft_record_id: str | None = None,
        chunk_role: str,
        chunk_index: int,
        knowledge_type: str | None,
        identity: ManualIdentity,
    ) -> dict[str, object] | None:
        """回查父块关联的草稿、原文和上下文。"""

        scopes = AccessPolicy.resolve_scopes(identity)
        # 详情回查会分别按 mysql_id/document_id 查询；私有库表达式只保留身份条件，
        # 否则按 mysql_id 回查多文档父块时会被首个 document_id 提前截断。
        targets = self._resolve_search_targets(
            scopes, None, identity.user_id, knowledge_type, None, None
        )
        if not targets or not (mysql_id or document_id or source_id):
            return None
        queries = []
        for milvus, expr in targets:
            if source_id:
                queries.append(
                    asyncio.to_thread(
                        milvus.query_document_chunks,
                        source_id=source_id,
                        chunk_role="agent_draft" if chunk_role == "agent_draft" else None,
                        expr=expr,
                        user_id=identity.user_id,
                    )
                )
            if mysql_id:
                queries.append(
                    asyncio.to_thread(
                        milvus.query_document_chunks,
                        mysql_id=mysql_id,
                        expr=expr,
                        user_id=identity.user_id,
                    )
                )
            if document_id:
                queries.append(
                    asyncio.to_thread(
                        milvus.query_document_chunks,
                        document_id=document_id,
                        expr=expr,
                        user_id=identity.user_id,
                    )
                )
        unique: dict[str, RetrievalHit] = {}
        for items in await asyncio.gather(*queries, return_exceptions=True):
            if isinstance(items, Exception):
                logger.warning("chunk_detail_query_failed", exc_info=items)
                continue
            for item in items:
                if item.record_id:
                    unique[item.record_id] = item
        if not unique:
            return None

        chunks = list(unique.values())
        if identity.user_role == UserType.EXTERNAL:
            chunks = filter_external_enterprise_hits(chunks, identity, knowledge_type)
            chunks = filter_external_demand_hits(chunks, identity, knowledge_type)
            if not chunks:
                return None

        def chunk_index_of(item: RetrievalHit) -> int:
            try:
                return int(item.metadata.get("chunk_index"))
            except (TypeError, ValueError):
                return -1

        requested_document_id = str(document_id or "")
        requested_mysql_id = str(mysql_id or "")
        requested_source_id = str(source_id or "")

        def is_requested_chunk(item: RetrievalHit) -> bool:
            metadata = item.metadata
            if str(metadata.get("chunk_role") or "") != chunk_role:
                return False
            if chunk_index_of(item) != chunk_index:
                return False
            if requested_mysql_id and str(metadata.get("mysql_id") or "") != requested_mysql_id:
                return False
            if requested_source_id and str(metadata.get("source_id") or "") != requested_source_id:
                return False
            return (
                not requested_document_id
                or str(metadata.get("document_id") or "") == requested_document_id
            )

        selected = next((item for item in chunks if is_requested_chunk(item)), None)
        if selected is None:
            return None

        selected_metadata = selected.metadata
        selected_document_id = str(selected_metadata.get("document_id") or "")
        selected_mysql_id = str(selected_metadata.get("mysql_id") or "")
        selected_source_id = str(selected_metadata.get("source_id") or "")

        # 按文档定位时，草稿本身可能没有 document_id，需要沿共同来源补查。
        if selected_source_id and not requested_source_id:
            source_queries = [
                asyncio.to_thread(
                    milvus.query_document_chunks, source_id=selected_source_id,
                    expr=expr, user_id=identity.user_id,
                )
                for milvus, expr in targets
            ]
            for items in await asyncio.gather(*source_queries, return_exceptions=True):
                if isinstance(items, Exception):
                    logger.warning("chunk_detail_query_failed", exc_info=items)
                    continue
                for item in items:
                    if item.record_id:
                        unique[item.record_id] = item
            chunks = list(unique.values())
            if identity.user_role == UserType.EXTERNAL:
                chunks = filter_external_enterprise_hits(chunks, identity, knowledge_type)
                chunks = filter_external_demand_hits(chunks, identity, knowledge_type)

        # 历史 Milvus 文本仍可能保存 /images/foo.jpg；实际图片已迁移到
        # MinIO 清单，详情返回时统一改成受控下载地址。
        image_url_by_ref: dict[str, str] = {}
        available_image_ids_by_document: dict[str, set[str]] = {}
        manifest_locations = [
            (
                str(item.metadata.get("document_id") or ""),
                str(item.metadata.get("source_locator") or ""),
            )
            for item in chunks
            if item.metadata.get("document_id")
        ]

        def image_references_for(item: RetrievalHit) -> list[str]:
            text = str(item.metadata.get("chunk_text") or "")
            references = re.findall(
                r"(?:^|[(/])((?:auto/)?images/[^)\s\"']+)",
                text,
                flags=re.IGNORECASE,
            )
            references.extend(
                str(image_id).strip()
                for image_id in item.metadata.get("image_ids") or []
                if str(image_id).strip()
            )
            references.extend(
                unquote(match.group(1))
                for match in re.finditer(r"/api/v1/perception/images/[^/]+/([^/?#)\s]+)", text)
            )
            return list(dict.fromkeys(references))

        image_references = [
            image_references_for(item)
            for item in chunks
            if item.metadata.get("document_id")
        ]
        image_route_pattern = re.compile(
            r"(?:https?://[^/\s<>\"']+)?/+api/v1/perception/images/([^/\s<>\"']+)/([^/?#)\s<>\"']+)"
        )
        # MySQL 草稿自身没有 document_id，封面图片的归属只能从明确的图片地址读取。
        for item in chunks:
            for match in image_route_pattern.finditer(str(item.metadata.get("chunk_text") or "")):
                image_document_id, image_id = (unquote(value) for value in match.groups())
                own_document_id = str(item.metadata.get("document_id") or "")
                location = (
                    image_document_id,
                    str(item.metadata.get("source_locator") or "") if image_document_id == own_document_id else "",
                )
                if location not in manifest_locations:
                    manifest_locations.append(location)
                    image_references.append([])
                references = image_references[manifest_locations.index(location)]
                if image_id not in references:
                    references.append(image_id)
        image_error = None
        if manifest_locations:
            try:
                image_groups = await ImageManifestService().find_many(
                    manifest_locations,
                    references=image_references,
                )
                for (owner_document_id, _), images in zip(
                    manifest_locations, image_groups, strict=True
                ):
                    for image in images:
                        image_id = str(image.get("image_id") or "")
                        image_document_id = str(
                            image.get("document_id") or owner_document_id or ""
                        )
                        if not image_id or not image_document_id:
                            continue
                        available_image_ids_by_document.setdefault(
                            image_document_id, set()
                        ).add(image_id)
                        image_url = perception_image_url(image_document_id, image_id)
                        for ref in (
                            image.get("relativePath"),
                            image.get("relative_path"),
                            image.get("path"),
                        ):
                            normalized = str(ref or "").replace("\\", "/").lstrip("/")
                            if normalized:
                                image_url_by_ref[normalized] = image_url
                                image_url_by_ref[normalized.rsplit("/", 1)[-1]] = image_url
            except Exception:
                logger.warning("chunk_detail_image_manifest_failed", exc_info=True)
                image_error = "图片清单暂时无法读取，文字内容仍可查看，请稍后重新打开详情。"

        def image_urls_for(text: str, metadata: dict[str, object]) -> list[str]:
            urls = []
            document = str(metadata.get("document_id") or "")
            for image_id in metadata.get("image_ids") or []:
                if (
                    document
                    and image_id
                    and str(image_id) in available_image_ids_by_document.get(document, set())
                ):
                    urls.append(perception_image_url(document, str(image_id)))
            for match in re.finditer(
                r"/api/v1/perception/images/([^/]+)/([^/?#)\s]+)", text
            ):
                image_document_id = unquote(match.group(1))
                image_id = unquote(match.group(2))
                if (
                    image_id not in available_image_ids_by_document.get(image_document_id, set())
                ):
                    continue
                url = perception_image_url(image_document_id, image_id)
                if url not in urls:
                    urls.append(url)
            for reference in re.findall(
                r"(?:^|[(/])((?:auto/)?images/[^)\s\"']+)", text, flags=re.IGNORECASE
            ):
                url = image_url_by_ref.get(reference) or image_url_by_ref.get(
                    reference.rsplit("/", 1)[-1]
                )
                if url and url not in urls:
                    urls.append(url)
            return urls

        def normalized_text(item: RetrievalHit) -> str:
            metadata = item.metadata
            text = str(metadata.get("chunk_text") or "")
            for reference in re.findall(
                r"(?:^|[(/])((?:auto/)?images/[^)\s\"']+)", text, flags=re.IGNORECASE
            ):
                url = image_url_by_ref.get(reference) or image_url_by_ref.get(
                    reference.rsplit("/", 1)[-1]
                )
                if url:
                    text = text.replace(f"/{reference}", url).replace(reference, url)

            def keep_available_image(match: re.Match[str]) -> str:
                target = match.group(1).strip().strip("<>")
                route_match = re.search(
                    r"/api/v1/perception/images/([^/]+)/([^/?#]+)", target
                )
                if route_match:
                    image_document_id = unquote(route_match.group(1))
                    image_id = unquote(route_match.group(2))
                    if image_id not in available_image_ids_by_document.get(
                        image_document_id, set()
                    ):
                        return ""
                    canonical_url = perception_image_url(image_document_id, image_id)
                    return match.group(0).replace(target, canonical_url)
                elif re.search(r"(?:^|/)(?:auto/)?images/", target, re.IGNORECASE):
                    reference = re.search(
                        r"(?:^|/)((?:auto/)?images/[^/?#]+)", target, re.IGNORECASE
                    )
                    normalized = reference.group(1) if reference else ""
                    if not normalized or not (
                        image_url_by_ref.get(normalized)
                        or image_url_by_ref.get(normalized.rsplit("/", 1)[-1])
                    ):
                        return ""
                return match.group(0)

            text = re.sub(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)", keep_available_image, text)

            def normalize_image_route(match: re.Match[str]) -> str:
                image_document_id, image_id = (unquote(value) for value in match.groups())
                if image_id not in available_image_ids_by_document.get(image_document_id, set()):
                    return ""
                return perception_image_url(image_document_id, image_id)

            text = image_route_pattern.sub(normalize_image_route, text)
            return text

        def belongs_to_draft(item: RetrievalHit) -> bool:
            metadata = item.metadata
            item_role = str(metadata.get("chunk_role") or "")
            draft_roles = {"agent_draft", "admin_draft", "mysql"}
            if selected_source_id:
                return (
                    str(metadata.get("source_id") or "") == selected_source_id
                    and item_role in draft_roles
                    and (chunk_role not in draft_roles or item_role == chunk_role)
                )
            if selected_mysql_id:
                return (
                    str(metadata.get("mysql_id") or "") == selected_mysql_id
                    and item_role in draft_roles
                    and (chunk_role not in draft_roles or item_role == chunk_role)
                )
            item_document_id = str(metadata.get("document_id") or "")
            return bool(
                selected_document_id and item_document_id == selected_document_id
                and item_role in draft_roles
            )

        def belongs_to_source(item: RetrievalHit) -> bool:
            metadata = item.metadata
            if selected_source_id and str(metadata.get("source_id") or "") != selected_source_id:
                return False
            item_document_id = str(metadata.get("document_id") or "")
            item_mysql_id = str(metadata.get("mysql_id") or "")
            if not selected_document_id or item_document_id != selected_document_id:
                return False
            if not selected_mysql_id:
                return True
            # MySQL 同步记录只有草稿，没有可继承的解析原文；只有明确绑定
            # 到同一 mysql_id 的真实文档块才能作为其原文上下文。
            if chunk_role == "mysql":
                return item_mysql_id == selected_mysql_id
            # 上传文档的原文可能没有 mysql_id；有值时仍必须属于当前草稿。
            return not item_mysql_id or item_mysql_id == selected_mysql_id

        draft_chunks = sorted(
            [
                item
                for item in chunks
                if (
                    str(item.metadata.get("chunk_role") or "")
                    in {"agent_draft", "admin_draft", "mysql"}
                    and belongs_to_draft(item)
                )
            ],
            key=lambda item: (chunk_index_of(item), item.record_id),
        )
        draft_selection_error = None
        draft_groups: dict[str, list[RetrievalHit]] = {}
        for item in draft_chunks:
            key = str(item.metadata.get("mysql_id") or item.record_id)
            draft_groups.setdefault(key, []).append(item)
        if matched_draft_record_id:
            chosen_draft = next(
                (item for item in draft_chunks if item.record_id == matched_draft_record_id), None
            )
            if chosen_draft is None:
                return None
            draft_chunks = draft_groups[str(chosen_draft.metadata.get("mysql_id") or chosen_draft.record_id)]
        elif is_source_chunk(selected_metadata):
            if len(draft_groups) > 1:
                candidates = [RetrievalHit(
                    items[0].record_id, 0, items[0].source,
                    {"chunk_text": "\n\n".join(normalized_text(item) for item in items)},
                ) for items in draft_groups.values()]
                chosen = None
                try:
                    ranked, outcome = await self.rerank(normalized_text(selected), candidates, top_k=1)
                    if outcome.applied and ranked:
                        chosen = next((items for items in draft_groups.values()
                                       if items[0].record_id == ranked[0].record_id), None)
                except RerankUnavailableError:
                    logger.warning("chunk_detail_draft_rerank_unavailable", exc_info=True)
                if chosen:
                    draft_chunks = chosen
                    matched_draft_record_id = chosen[0].record_id
                else:
                    draft_chunks = []
                    draft_selection_error = "未能确定关联草稿，仅返回原文详情"
                    logger.warning("chunk_detail_draft_not_selected", extra={"structured": {
                        "document_id": selected_document_id, "source_id": selected_source_id,
                        "draft_count": len(draft_groups),
                    }})
        source_candidates = sorted(
            [
                item
                for item in chunks
                if (
                    chunk_role in {"agent_doc", "admin_doc"}
                    and str(item.metadata.get("chunk_role") or "") in {"agent_doc", "admin_doc"}
                    and belongs_to_source(item)
                )
            ],
            key=lambda item: (chunk_index_of(item), item.record_id),
        )
        source_by_index: dict[int, RetrievalHit] = {}
        for item in source_candidates:
            index = chunk_index_of(item)
            if chunk_index - 1 <= index <= chunk_index + 1:
                source_by_index.setdefault(index, item)
        if chunk_role in {"agent_doc", "admin_doc"}:
            source_by_index[chunk_index] = selected
        source_chunks = [
            source_by_index[index]
            for index in range(chunk_index - 1, chunk_index + 2)
            if index in source_by_index
        ]

        def unique_detail_chunks(items: list[RetrievalHit]) -> list[RetrievalHit]:
            seen: set[str] = set()
            result: list[RetrievalHit] = []
            for item in items:
                key = " ".join(normalized_text(item).split()).casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                result.append(item)
            return result

        draft_chunks = unique_detail_chunks(draft_chunks)

        def serialize(item: RetrievalHit) -> dict[str, object]:
            metadata = item.metadata
            text = normalized_text(item)
            return {
                "id": item.record_id,
                "text": text,
                "chunk_role": metadata.get("chunk_role"),
                "chunk_index": metadata.get("chunk_index"),
                "mysql_id": metadata.get("mysql_id"),
                "document_id": metadata.get("document_id"),
                "source_locator": metadata.get("source_locator"),
                "image_ids": [
                    str(image_id)
                    for image_id in metadata.get("image_ids") or []
                    if str(image_id) in available_image_ids_by_document.get(
                        str(metadata.get("document_id") or ""), set()
                    )
                ],
                "image_urls": image_urls_for(text, metadata),
            }

        selected_text = normalized_text(selected)
        image_urls = list(
            dict.fromkeys(
                url
                for item in [*draft_chunks, *source_chunks]
                for url in image_urls_for(normalized_text(item), item.metadata)
            )
        )
        document_knowledge_type = (
            str(
                selected_metadata.get("source_knowledge_type")
                or selected_metadata.get("knowledge_type")
                or knowledge_type
                or ""
            )
            .strip()
            .lower()
        )
        documents = []
        if not (identity.user_role == "external" and document_knowledge_type == "demand"):
            document_refs = []
            seen_documents: set[tuple[str, str | None]] = set()
            for item in [*draft_chunks, *source_chunks] or [selected]:
                for document in related_documents([to_evidence(item)]):
                    key = (document.document_id, document.source_name)
                    if key in seen_documents:
                        continue
                    seen_documents.add(key)
                    document_refs.append(document)
            documents = [
                {**document.model_dump(), "can_download": True} for document in document_refs
            ]
        return {
            "text": selected_text,
            "source": selected.source,
            "mysql_id": selected_mysql_id or None,
            "source_id": requested_source_id or selected_metadata.get("source_id") or None,
            "document_id": selected_document_id or None,
            "chunk_count": len(chunks),
            "draft": "\r\n\r\n".join(normalized_text(item) for item in draft_chunks).strip(),
            "draft_selection_required": False,
            "draft_selection_error": draft_selection_error,
            "matched_draft_record_id": matched_draft_record_id,
            "draft_chunks": [serialize(item) for item in draft_chunks],
            "source_chunks": [
                serialize(item)
                for item in sorted(
                    source_chunks, key=lambda item: int(item.metadata.get("chunk_index") or 0)
                )
            ],
            "image_urls": image_urls,
            "image_error_code": "IMAGE_MANIFEST_UNAVAILABLE" if image_error else None,
            "image_error": image_error,
            "documents": documents,
        }

    @staticmethod
    def _build_private_expr(document_id: str | None, user_id: str) -> str:
        """私有库必须带 user_id 条件，不能由前端 scope 单独决定访问范围。"""

        safe_user_id = _escape_expr_value(user_id)
        parts = [f'user_id == "{safe_user_id}"']
        if document_id:
            parts.append(f'document_id == "{_escape_expr_value(document_id)}"')
        return " && ".join(parts)


def _hit_business_key(hit: RetrievalHit) -> str | None:
    metadata = hit.metadata
    source_id = str(metadata.get("source_id") or "").strip()
    mysql_id = str(metadata.get("mysql_id") or "").strip()
    if is_source_chunk(metadata):
        mysql_id = str(metadata.get("matched_draft_mysql_id") or "").strip() or mysql_id
    knowledge_type = str(
        metadata.get("source_knowledge_type") or metadata.get("knowledge_type") or ""
    ).strip()
    if mysql_id:
        return f"business:{knowledge_type}:mysql:{mysql_id}"
    if is_source_chunk(metadata) and metadata.get("document_id"):
        return f"business:{knowledge_type}:document:{metadata['document_id']}"
    if source_id:
        return f"business:{knowledge_type}:{source_id}"
    return None


def _hit_chunk_key(hit: RetrievalHit) -> str:
    """同一业务记录内区分草稿块和原文块。"""

    metadata = hit.metadata
    if is_source_chunk(metadata) and metadata.get("document_id"):
        text = " ".join(str(metadata.get("chunk_text") or "").split())
        return ":".join((
            "source_chunk", str(metadata.get("source_knowledge_type") or metadata.get("knowledge_type") or ""),
            str(metadata["document_id"]), str(metadata.get("version_id") or ""),
            str(metadata.get("chunk_index", "")), hashlib.sha256(text.encode()).hexdigest(),
        ))
    return ":".join(
        (
            "chunk",
            str(metadata.get("source_knowledge_type") or metadata.get("knowledge_type") or ""),
            str(metadata.get("document_id") or ""),
            str(metadata.get("mysql_id") or ""),
            str(metadata.get("chunk_role") or ""),
            str(
                metadata.get("chunk_index")
                if metadata.get("chunk_index") is not None
                else hit.record_id
            ),
        )
    )


def _hit_content_key(hit: RetrievalHit) -> str:
    document_id = str(hit.metadata.get("document_id") or "")
    mysql_id = str(hit.metadata.get("mysql_id") or "")
    chunk_index = str(hit.metadata.get("chunk_index") or "")
    text = str(hit.metadata.get("chunk_text") or "")
    text = re.sub(r"<[^>]*>", " ", html.unescape(text))
    normalized = re.sub(r"\W+", "", text, flags=re.UNICODE).casefold()
    if normalized:
        return f"{mysql_id}:{document_id}:{chunk_index}:{normalized}"
    return f"record:{hit.record_id}"


def _escape_expr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
