from __future__ import annotations

from typing import Any

from core.governance.redaction import TextRedactionService
from services.application.chat_response import select_match_results
from services.observability.trace_context import get_trace_context

PUBLIC_CHAT_FAILURE_MESSAGE = "问答处理失败，请稍后重试。"
PUBLIC_CHAT_TIMEOUT_MESSAGE = "问答处理超时，请稍后重试。"
_EXTERNAL_RESTRICTED_MESSAGE = "当前暂不支持处理客户拜访记录，您可以继续整理需求、方案或成果草稿。"


_PUBLIC_FIELDS = {
    "status",
    "answer",
    "intent",
    "evidences",
    "web_results",
    "match_results",
    "images",
    "documents",
    "ui_blocks",
    "actions",
    "draft",
    "drafts",
    "debug",
    "flow_state",
    "processing_elapsed_ms",
    "thread_id",
    "error",
}


def attach_debug_result(result: Any, payload: Any, session_store: Any = None) -> Any:
    """把本次请求的真实 LLM 交换和会话上下文附到测试响应，不改普通响应。"""
    if not getattr(payload, "debug", False):
        return result
    thread_id = getattr(payload, "thread_id", None) or getattr(result, "thread_id", None)
    session = session_store.get(thread_id) if session_store is not None and thread_id else {}
    state = (session or {}).get("state") or {}
    chat_flow = state.get("chat_flow") or {}
    request_context = chat_flow.get("request_context") or {}
    intent = result.intent or {}
    draft = result.draft or (result.drafts if result.drafts else None)
    return result.model_copy(
        update={
            "debug": {
                "context": {
                    "request": payload.model_dump(mode="json"),
                    "rolling_summary": chat_flow.get("rolling_summary"),
                    "draft": draft,
                    "active_draft": chat_flow.get("active_draft"),
                    "draft_context": request_context.get("draft_context") or payload.draft_context,
                    "query": intent.get("query"),
                    "match_results": result.match_results,
                    "retrieval_context": chat_flow.get("retrieval_context"),
                },
                "llm_exchanges": list(get_trace_context().attributes.get("llm_exchanges") or []),
            },
        }
    )


def public_chat_result(result: Any, *, user_role: str | None = None) -> dict[str, object]:
    data = result.model_dump(mode="json")
    public = {key: data.get(key) for key in _PUBLIC_FIELDS if key in data}
    public["match_results"] = [
        _public_match_result(item)
        for item in select_match_results(
            [item for item in data.get("match_results") or [] if isinstance(item, dict)]
        )
    ]
    public["web_results"] = [
        {
            key: item.get(key)
            for key in ("title", "url", "content", "score", "published_date")
            if key in item
        }
        for item in data.get("web_results") or []
        if isinstance(item, dict)
    ]
    public["intent"] = _public_intent(data.get("intent"), data.get("internal_flow"))
    public.pop("evidences", None)
    public["actions"] = [
        action
        for action in data.get("actions") or []
        if isinstance(action, dict) and action.get("id") != "view_evidence"
    ]
    public["ui_blocks"] = [
        block
        for block in data.get("ui_blocks") or []
        if isinstance(block, dict) and block.get("type") != "evidence_list"
    ]
    if user_role == "external":
        if _has_customer_visit_content(data):
            public["answer"] = _EXTERNAL_RESTRICTED_MESSAGE
            public.pop("draft", None)
            public["drafts"] = []
            public["intent"] = None
            public["flow_state"] = None
            public["match_results"] = []
            public["web_results"] = []
            public["documents"] = []
            public["images"] = []
            public["actions"] = []
            public.pop("debug", None)
            public.pop("error", None)
            public["ui_blocks"] = [
                {
                    "type": "answer",
                    "data": {
                        "text": _EXTERNAL_RESTRICTED_MESSAGE,
                        "status": public.get("status"),
                    },
                }
            ]
        redactor = TextRedactionService()
        public["match_results"] = redactor.sanitize_demand_payload(public["match_results"])
        public["ui_blocks"] = redactor.sanitize_demand_payload(public["ui_blocks"])
        if _has_demand_result(public):
            public["answer"] = redactor.sanitize_demand_text(str(public.get("answer") or ""))
            public["ui_blocks"] = [
                {
                    **block,
                    "data": {
                        **block["data"],
                        "text": redactor.sanitize_demand_text(block["data"]["text"]),
                    },
                }
                if (
                    isinstance(block, dict)
                    and block.get("type") == "answer"
                    and isinstance(block.get("data"), dict)
                    and isinstance(block["data"].get("text"), str)
                )
                else block
                for block in public["ui_blocks"]
            ]
    public["thread_id"] = (
        getattr(result, "thread_id", None) or data.get("thread_id") or data.get("session_id")
    )
    public.pop("session_id", None)
    if data.get("error"):
        public["error"] = PUBLIC_CHAT_FAILURE_MESSAGE
    if data.get("status") in {
        "FAILED",
        "DEPENDENCY_TIMEOUT",
        "DEPENDENCY_UNAVAILABLE",
        "INTENT_CLASSIFICATION_UNAVAILABLE",
        "INTENT_CLASSIFICATION_INVALID",
    }:
        message = (
            PUBLIC_CHAT_TIMEOUT_MESSAGE
            if data["status"] == "DEPENDENCY_TIMEOUT"
            else PUBLIC_CHAT_FAILURE_MESSAGE
        )
        public["answer"] = message
        public["error"] = message
        public["ui_blocks"] = [
            {"type": "answer", "data": {"text": message, "status": data["status"]}},
        ]
    return public


def _has_customer_visit_content(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("draft_type", "knowledge_type", "business_intent", "name"):
            if str(value.get(key) or "").strip().lower() == "customer_visit":
                return True
        return any(_has_customer_visit_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_customer_visit_content(item) for item in value)
    return False


def _has_demand_result(public: dict[str, object]) -> bool:
    if any(item.get("knowledge_type") == "demand" for item in public["match_results"]):
        return True
    intent = public.get("intent")
    if not isinstance(intent, dict):
        return False
    if any(
        str(intent.get(key) or "").strip().lower() in {"demand_discovery", "需求发现"}
        for key in ("name", "business_intent")
    ):
        return True
    for key in ("knowledge_type", "knowledge_types", "business_knowledge_types"):
        value = intent.get(key)
        values = value if isinstance(value, list) else [value]
        if any(str(item or "").strip().lower() == "demand" for item in values):
            return True
    return False


def _public_match_result(item: dict[str, Any]) -> dict[str, object]:
    result = {
        key: item.get(key)
        for key in (
            "id",
            "title",
            "summary",
            "details",
            "score",
            "knowledge_type",
            "mysql_id",
            "document_id",
            "chunk_role",
            "chunk_index",
            "detail_available",
            "source",
            "image_ids",
            "image_urls",
        )
        if key in item
    }
    return result


def _public_intent(intent: Any, internal_flow: Any = None) -> dict[str, object]:
    """Expose only the intent fields needed by the live UI."""

    intent_data = intent if isinstance(intent, dict) else {}
    flow_data = internal_flow if isinstance(internal_flow, dict) else {}
    public: dict[str, object] = {}
    for key in (
        "confirmation_required",
        "confirmed",
        "draft_type",
        "business_intent",
        "business_knowledge_types",
        "knowledge_types",
        "source",
        "status",
        "operation",
        "tool",
        "knowledge_type",
        "confidence",
    ):
        value = intent_data.get(key)
        if value is None:
            value = flow_data.get(key)
        if value is not None:
            public[key] = value
    name = str(
        intent_data.get("name") or flow_data.get("intent") or public.get("business_intent") or ""
    ).strip()
    if name == "supply_demand_match":
        name = "supply_demand_match"
    if name:
        public["name"] = name
    query = intent_data.get("query") or flow_data.get("query")
    if query:
        public["query"] = str(query)
    entities = intent_data.get("entities")
    if isinstance(entities, dict):
        safe_entities: dict[str, object] = {}
        next_batch = (
            str(entities.get("next_match_batch") or entities.get("match_batch") or "")
            .strip()
            .lower()
        )
        if next_batch in {"true", "1", "yes", "next", "换一批"}:
            safe_entities["next_match_batch"] = True
        match_direction = entities.get("match_direction")
        if match_direction in {"demand_to_supply", "supply_to_demand"}:
            safe_entities["match_direction"] = match_direction
        if safe_entities:
            public["entities"] = safe_entities
    return public
