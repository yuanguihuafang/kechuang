"""Persist business conversation state without intent classification or model calls."""

from __future__ import annotations

import uuid
from typing import Any

from core.context_window import clip_text, limit_draft_context
from core.contracts.chat import ChatResponse
from core.contracts.conversation import (
    normalize_conversation_state,
    state_for_response,
)
from core.contracts.retrieval import displayed_match_result_ids
from core.governance.metrics import SupervisorMetrics
from services.application.chat_response import (
    select_match_results,
)
from services.application.draft_builder import deduplicate_context_items
from services.application.draft_session import merge_session_draft_index
from services.observability.metrics import persist_supervisor_metric

_ATTACHMENT_KEYS = ("document_id", "attachment_id", "storage_key", "file_name")
_SOURCE_BLOCK_KEYS = ("block_id", "id")


def _compact_retrieval_item(item: dict[str, Any]) -> dict[str, Any]:
    """会话状态只保留追问所需事实，完整响应仍由消息记录保存。"""

    compact: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, str):
            compact[key] = clip_text(value, 2000)
        elif isinstance(value, (int, float, bool)) or value is None:
            compact[key] = value
        elif isinstance(value, dict):
            compact[key] = _compact_retrieval_item(value)
        elif isinstance(value, list):
            compact[key] = [
                _compact_retrieval_item(entry) if isinstance(entry, dict) else str(entry)[:500]
                for entry in value[:10]
            ]
    return compact


class ConversationStateService:
    """Own only state persistence; DeerFlow owns all conversational execution."""

    def __init__(self, session_store: Any) -> None:
        self.session_store = session_store
        self.metrics = SupervisorMetrics(persist=persist_supervisor_metric)

    def _get_chat_flow_state(self, thread_id: str) -> dict[str, Any]:
        session = self.session_store.get(thread_id) or {}
        return normalize_conversation_state((session.get("state") or {}).get("chat_flow"))

    def _save_chat_flow_state(self, thread_id: str, state: dict[str, Any]) -> None:
        self.session_store.upsert(
            thread_id, state={"chat_flow": normalize_conversation_state(state)}
        )

    def record_chat_result(
        self,
        thread_id: str,
        result: ChatResponse,
        request: Any | None = None,
    ) -> str:
        """把一次响应统一落到会话状态机，并返回当前状态。"""

        flow_state = state_for_response(result.status)
        state = self._get_chat_flow_state(thread_id)
        request_context = state.get("request_context") or {}
        merged_draft_context = dict(request_context.get("draft_context") or {})
        result_match_direction = (
            str(((result.intent or {}).get("entities") or {}).get("match_direction") or "")
            .strip()
            .lower()
        )
        if request is not None:
            request_draft_context = dict(request.draft_context or {})
            saved_draft_context = dict(request_context.get("draft_context") or {})
            previous_direction = (
                str(
                    state.get("match_direction")
                    or (request_context.get("draft_context") or {}).get("match_direction")
                    or ""
                )
                .strip()
                .lower()
            )
            input_direction = (
                str(request_draft_context.get("match_direction") or "").strip().lower()
            )
            if (
                not input_direction
                and previous_direction in {"demand_to_supply", "supply_to_demand"}
                and result_match_direction in {"demand_to_supply", "supply_to_demand"}
                and previous_direction != result_match_direction
            ):
                # 手动输入切换方向时，不把上一方向的查询和匹配队列继续写回会话。
                for key in (
                    "text",
                    "match_items",
                    "match_index",
                    "_precomputed_draft",
                    "_existing_draft",
                    "draft_type",
                    "fields",
                    "mysql_fields",
                ):
                    request_draft_context.pop(key, None)
                saved_draft_context = {}
            merged_draft_context = {
                **saved_draft_context,
                **request_draft_context,
            }
            for key in ("attachments", "source_blocks"):
                merged_values = [
                    *(saved_draft_context.get(key) or []),
                    *(request_draft_context.get(key) or []),
                ]
                if merged_values:
                    keys = _ATTACHMENT_KEYS if key == "attachments" else _SOURCE_BLOCK_KEYS
                    merged_draft_context[key] = deduplicate_context_items(merged_values, keys)
            request_context = {
                **request_context,
                "intent_hint": request.intent_hint,
                "knowledge_type": request.knowledge_type,
                "scope_id": getattr(request, "scope_id", None),
                "top_k": getattr(request, "top_k", None),
                "use_rerank": getattr(request, "use_rerank", None),
                "expand_query": getattr(request, "expand_query", None),
                # 响应阶段可能已从上传文件推断出供需方向；把该方向随会话保存，
                # 下一轮确认“立即匹配”时直接复用，不再重复询问。
                "draft_context": {
                    **merged_draft_context,
                    **(
                        {"match_direction": result_match_direction}
                        if result_match_direction
                        else {}
                    ),
                    **(
                        {"match_direction": result.draft.get("match_direction")}
                        if isinstance(result.draft, dict) and result.draft.get("match_direction")
                        else {}
                    ),
                }
                or None,
            }
        draft_items = [
            item
            for item in (result.drafts or ([result.draft] if result.draft else []))
            if isinstance(item, dict)
        ]
        active_draft_id = state.get("active_draft_id")
        active_draft = state.get("active_draft")
        active_draft_status = (
            str((active_draft or {}).get("status") or state.get("last_status") or "")
            .strip()
            .upper()
        )
        reusable_draft_id = active_draft_id if active_draft_status != "PUBLISHED" else None
        normalized_drafts: list[dict[str, Any]] = []
        for item in draft_items:
            normalized = limit_draft_context(item)
            draft_id = str(
                normalized.get("draft_id")
                or reusable_draft_id
                or f"draft_session_{uuid.uuid4().hex}"
            )
            normalized["draft_id"] = draft_id
            if "attachments" in normalized:
                normalized["attachments"] = deduplicate_context_items(
                    normalized.get("attachments"), _ATTACHMENT_KEYS
                )
            if "source_blocks" in normalized:
                normalized["source_blocks"] = deduplicate_context_items(
                    normalized.get("source_blocks"), _SOURCE_BLOCK_KEYS
                )
            normalized_drafts.append(normalized)
            active_draft_id = draft_id
            active_draft = normalized
        if normalized_drafts:
            result.drafts = normalized_drafts if len(normalized_drafts) > 1 else []
            result.draft = normalized_drafts[0]
        active_draft = limit_draft_context(active_draft)
        if isinstance(active_draft, dict) and active_draft:
            compact_draft_context = {
                "draft_id": active_draft.get("draft_id"),
                "draft_type": active_draft.get("draft_type"),
                "match_direction": active_draft.get("match_direction") or result_match_direction,
            }
            compact_draft_context = {
                key: value
                for key, value in compact_draft_context.items()
                if value not in (None, "")
            }
        else:
            compact_draft_context = limit_draft_context(merged_draft_context)
        if compact_draft_context:
            request_context = {
                **request_context,
                "draft_context": compact_draft_context,
            }
        active_intent = str(
            (result.intent or {}).get("business_intent")
            or (result.intent or {}).get("name")
            or request_context.get("intent_hint")
            or state.get("active_intent")
            or ""
        ).strip()
        state_active = state.get("active")
        match_direction = str(
            request_context.get("match_direction")
            or result_match_direction
            or (active_draft or {}).get("match_direction")
            or ((active_draft or {}).get("fields") or {}).get("match_direction")
            or ""
        ).strip()
        pending_action = next(
            (
                str(action.get("type") or "").strip()
                for action in (result.actions or [])
                if isinstance(action, dict) and action.get("enabled", True) and action.get("type")
            ),
            "",
        )
        if not pending_action:
            pending_action = str(state.get("pending_action") or "")
        document_ids = list(
            dict.fromkeys(
                [str(item) for item in (state.get("document_ids") or []) if str(item).strip()]
                + [
                    str(item.get("document_id"))
                    for item in (result.documents or [])
                    if isinstance(item, dict) and item.get("document_id")
                ]
                + [
                    str(item.get("document_id"))
                    for item in (active_draft or {}).get("attachments", [])
                    if isinstance(item, dict) and item.get("document_id")
                ]
            )
        )
        match_batch = self._next_match_batch_state(
            state,
            result,
            request_context,
            str(getattr(request, "message", "") or ""),
        )
        self._save_chat_flow_state(
            thread_id,
            {
                **state,
                "state": flow_state.value,
                "flow_state": flow_state.value,
                "last_status": result.status,
                "last_answer": result.answer,
                "last_intent": result.intent,
                "last_user_message": str(
                    getattr(request, "message", "") or state.get("last_user_message") or ""
                ),
                "last_assistant_message": result.answer,
                "active_intent": active_intent,
                "active": state_active,
                "stage": flow_state.value,
                "pending_action": pending_action,
                "match_direction": match_direction,
                "draft_id": active_draft_id,
                "draft_type": (active_draft or {}).get("draft_type"),
                "document_ids": document_ids,
                "request_context": request_context,
                "active_draft_id": active_draft_id,
                "active_draft": active_draft,
                "draft_index": merge_session_draft_index(
                    state.get("draft_index"), normalized_drafts
                ),
                "match_batch": match_batch,
                "retrieval_context": (
                    {
                        "status": result.retrieval_status or result.status or "SUCCESS",
                        "business_intent": (result.intent or {}).get("business_intent"),
                        "query": str((result.intent or {}).get("query") or "")[:2000],
                        "match_results": [
                            _compact_retrieval_item(item)
                            for item in (result.match_results or [])[:5]
                        ],
                        "evidences": [
                            _compact_retrieval_item(item) for item in (result.evidences or [])[:10]
                        ],
                        "web_results": [
                            _compact_retrieval_item(item)
                            for item in (result.web_results or [])[:10]
                        ],
                        "images": [
                            _compact_retrieval_item(item) for item in (result.images or [])[:10]
                        ],
                        "documents": [
                            _compact_retrieval_item(item) for item in (result.documents or [])[:10]
                        ],
                        "match_batch": match_batch,
                    }
                    if result.match_results
                    or result.evidences
                    or result.status in {"SUCCESS", "PARTIAL_SUCCESS", "NO_EVIDENCE"}
                    else state.get("retrieval_context")
                ),
            },
        )
        return flow_state.value

    @staticmethod
    def _next_match_batch_state(
        state: dict[str, Any],
        result: ChatResponse,
        request_context: dict[str, Any] | None = None,
        request_message: str = "",
    ) -> dict[str, Any]:
        request_context = request_context or {}
        draft_context = request_context.get("draft_context") or {}
        active_draft = state.get("active_draft") or {}
        intent = result.intent or {}
        internal_flow = result.internal_flow or {}
        batch_action = next(
            (
                action
                for action in (result.actions or [])
                if isinstance(action, dict)
                and action.get("enabled", True)
                and action.get("type") == "NEXT_MATCH_BATCH"
            ),
            {},
        )
        action_intent = str(batch_action.get("intent_hint") or "").strip().lower()
        operation = str(intent.get("operation") or internal_flow.get("operation") or "").strip()
        displayed_items = [
            item
            for item in [*(result.match_results or []), *(result.evidences or [])]
            if isinstance(item, dict)
        ]
        if not displayed_items:
            displayed_items = [
                item
                for block in (result.ui_blocks or [])
                if isinstance(block, dict) and block.get("type") == "recommendation_list"
                for item in ((block.get("data") or {}).get("items") or [])
                if isinstance(item, dict)
            ]
        is_knowledge_batch = operation in {"knowledge_search", "supply_demand_match"} and bool(
            displayed_items
        )
        has_action_batch = bool(displayed_items) and action_intent in {
            "supply_demand_match",
            "demand_discovery",
            "knowledge_search",
        }
        if not (
            (intent.get("business_intent") or internal_flow.get("business_intent"))
            in {"supply_demand_match", "demand_discovery"}
            or (intent.get("name") or intent.get("operation") or internal_flow.get("name"))
            in {"supply_demand_match", "demand_discovery"}
            or request_context.get("intent_hint") in {"supply_demand_match", "demand_discovery"}
            or state.get("active_intent") in {"supply_demand_match", "demand_discovery"}
            or is_knowledge_batch
            or has_action_batch
        ):
            return dict(state.get("match_batch") or {})
        entities = intent.get("entities") or {}
        is_next = str(
            entities.get("next_match_batch") or entities.get("match_batch") or ""
        ).strip().lower() in {"true", "1", "yes", "next", "换一批"}
        previous = dict(state.get("match_batch") or {})
        previous_ids = previous.get("shown_result_ids") if is_next else []
        result_ids = [
            identity
            for item in select_match_results(
                displayed_items
            )
            for identity in displayed_match_result_ids(item)
        ]
        knowledge_types = list(
            dict.fromkeys(
                kind
                for item in displayed_items
                if isinstance(item, dict)
                if (
                    kind := str(
                        item.get("source_knowledge_type") or item.get("knowledge_type") or ""
                    )
                    .strip()
                    .lower()
                )
                in {"demand", "solution", "achievement", "expert", "enterprise"}
            )
        )
        selected_knowledge_types = [
            item
            for item in (intent.get("entities") or {}).get("knowledge_types", [])
            if item in {"demand", "solution", "achievement", "expert", "enterprise"}
        ]
        if selected_knowledge_types:
            knowledge_types = list(dict.fromkeys(selected_knowledge_types))
        business_intent = (
            str(
                intent.get("business_intent")
                or internal_flow.get("business_intent")
                or request_context.get("intent_hint")
                or action_intent
                or state.get("active_intent")
                or operation
                or ""
            )
            .strip()
            .lower()
        )
        requested_knowledge_type = str(request_context.get("knowledge_type") or "").strip().lower()
        if not selected_knowledge_types and business_intent == "knowledge_search" and requested_knowledge_type in {
            "demand",
            "solution",
            "achievement",
            "expert",
            "enterprise",
        }:
            knowledge_types = [requested_knowledge_type]
        shown_ids = list(
            dict.fromkeys(
                [str(item) for item in (previous_ids or []) if str(item).strip()] + result_ids
            )
        )
        active_fields = active_draft.get("fields") or {}
        draft_fields_text = (
            "\n".join(
                str(value).strip()
                for key, value in active_fields.items()
                if key
                not in {"status", "category", "contact_name", "contact_phone", "phone", "email"}
                and value is not None
                and str(value).strip()
            )
            if isinstance(active_fields, dict)
            else ""
        )
        query = str(
            (previous.get("current_query") or previous.get("original_query") if is_next else "")
            or intent.get("query")
            or draft_context.get("text")
            or active_draft.get("text")
            or active_draft.get("summary")
            or draft_fields_text
            or request_message
            or ""
        ).strip()
        business_intent = (
            business_intent or str(previous.get("business_intent") or "").strip().lower()
        )
        direction = (
            str(
                entities.get("match_direction")
                or intent.get("match_direction")
                or draft_context.get("match_direction")
                or active_draft.get("match_direction")
                or (active_draft.get("fields") or {}).get("match_direction")
                or previous.get("direction")
                or state.get("match_direction")
                or ""
            )
            .strip()
            .lower()
        )
        return {
            "original_query": str(previous.get("original_query") or query),
            "current_query": query,
            "user_goal": str(previous.get("user_goal") or request_message or query)[:4000],
            "web_fallback_allowed": (
                previous.get("web_fallback_allowed") is True
                if is_next
                else entities.get("web_fallback_allowed") is True
            ),
            "shown_result_ids": shown_ids,
            "batch_index": (int(previous.get("batch_index") or 0) + 1) if is_next else 1,
            "business_intent": business_intent,
            "direction": direction,
            "knowledge_type": knowledge_types[0] if len(knowledge_types) == 1 else None,
            "knowledge_types": knowledge_types,
        }
