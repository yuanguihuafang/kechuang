"""组装面向业务前端的问答展示契约。

这里仅生成声明式展示信息和动作建议，不执行任何业务动作。
真正的草稿、发布、询盘和人工接管接口由后续业务系统接入。
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import quote
from core.contracts.chat import ChatRequest
from core.contracts.flows import get_flow_definition
from core.contracts.retrieval import is_source_chunk, match_result_identity
from services.application.document_access import can_download_document_reference
from services.storage.minio.image_service import perception_image_url

_EVIDENCE_PUBLIC_FIELDS = (
    "matched_draft_record_id",
    "matched_draft_mysql_id",
    "record_id",
    "text",
    "score",
    "source",
    "mysql_id",
    "source_id",
    "chunk_role",
    "status",
    "update_time",
    "scope_id",
    "source_locator",
    "document_id",
    "version_id",
    "publication_status",
    "index_time",
    "chunk_index",
    "source_knowledge_type",
    "source_collection",
    "knowledge_type",
    "title",
    "name",
    "page_number",
)
MATCH_RESULT_LIMIT = 3
_MATCH_DRAFT_TYPES = frozenset({"demand", "solution", "achievement"})
_SUPPLY_DRAFT_TYPES = frozenset({"solution", "achievement"})
def _match_score(item: dict[str, Any]) -> float:
    try:
        value = float(item.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def select_match_results(
    items: list[dict[str, Any]], *, limit: int = MATCH_RESULT_LIMIT
) -> list[dict[str, Any]]:
    """只向问答前端返回高相关的初次匹配结果。"""

    ranked = [item for item in items if isinstance(item, dict)]

    ranked.sort(key=_match_score, reverse=True)
    max_items = min(max(int(limit), 0), MATCH_RESULT_LIMIT)
    if max_items == 0:
        return []
    selected: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(ranked):
        identity = match_result_identity(item) or f"anonymous:{index}"
        current = selected.get(identity)
        if current is None or (is_source_chunk(item) and not is_source_chunk(current)):
            selected[identity] = item
    return list(selected.values())[:max_items]


def resolve_match_draft_type(
    draft_context: Any,
    knowledge_type: Any = None,
) -> str | None:
    """Resolve a safe draft action only when the matching side is clear."""
    context = draft_context if isinstance(draft_context, dict) else {}
    draft_type = str(context.get("draft_type") or "").strip().lower()
    knowledge_type = str(knowledge_type or "").strip().lower()
    direction = str(context.get("match_direction") or "").strip().lower()

    if direction == "demand_to_supply":
        return "demand" if draft_type in {"", "demand"} else None
    if direction == "supply_to_demand":
        supply_type = draft_type or knowledge_type
        return supply_type if supply_type in _SUPPLY_DRAFT_TYPES else None
    if direction:
        return None

    for candidate in (draft_type, knowledge_type):
        if candidate in _MATCH_DRAFT_TYPES:
            return candidate
    return None


def compact_evidences(evidences: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    """同一业务数据优先原文 chunk，避免草稿和原文重复展示。"""

    grouped: dict[str, dict[str, Any]] = {}
    ranked = sorted(
        (item for item in evidences if isinstance(item, dict)),
        key=_match_score,
        reverse=True,
    )
    for evidence in ranked:
        knowledge_type = str(
            evidence.get("source_knowledge_type") or evidence.get("knowledge_type") or ""
        )
        key = match_result_identity(evidence, default_knowledge_type=knowledge_type)
        if not key:
            key = f"{knowledge_type}:{evidence.get('record_id') or evidence.get('source_locator') or evidence.get('text') or evidence.get('chunk_text') or ''}".strip()
        if not key:
            continue
        if key in grouped and not (is_source_chunk(evidence) and not is_source_chunk(grouped[key])):
            continue
        text = " ".join(str(evidence.get("text") or evidence.get("chunk_text") or "").split())
        item = {field: evidence[field] for field in _EVIDENCE_PUBLIC_FIELDS if field in evidence}
        item["text"] = text[:240]
        grouped[key] = item
    return list(grouped.values())[:limit]


def _action(action_id: str, action_type: str, label: str) -> dict[str, Any]:
    return {
        "id": action_id,
        "type": action_type,
        "label": label,
        "enabled": True,
    }


def sanitize_document_references(
    request: ChatRequest,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """给文件引用附加测试身份；外部隐藏需求库附件，保留其他公开库下载。"""

    identity_query = f"user_id={quote(request.user_id, safe='')}" if request.user_id else ""
    output: list[dict[str, Any]] = []
    for document in documents:
        item = dict(document)
        knowledge_type = str(item.get("knowledge_type") or "").strip().lower()
        if request.user_role == "external" and knowledge_type == "demand":
            continue
        can_download = can_download_document_reference(request.user_role, knowledge_type)
        for key in ("file_url",):
            url = item.get(key)
            if url and identity_query:
                separator = "&" if "?" in str(url) else "?"
                item[key] = f"{url}{separator}{identity_query}"
        item["can_download"] = can_download
        item["preview_url"] = None
        if not can_download:
            item["file_url"] = None
        output.append(item)
    return output


def build_recommendation_items(
    evidences: list[dict[str, Any]], *, limit: int = MATCH_RESULT_LIMIT
) -> list[dict[str, Any]]:
    """把检索证据压缩为逐条推荐；不把整份原始记录透传给前端。"""

    max_items = min(max(int(limit), 0), MATCH_RESULT_LIMIT)
    if not max_items:
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    ranked = select_match_results(evidences, limit=max_items)
    for evidence in ranked:
        source_locator = evidence.get("source_locator")
        knowledge_type = (
            evidence.get("source_knowledge_type") or evidence.get("knowledge_type") or ""
        )
        key = match_result_identity(evidence, default_knowledge_type=str(knowledge_type))
        if not key:
            key = f"{knowledge_type}:{evidence.get('record_id') or source_locator or evidence.get('text') or ''}"
        if not key or key in seen:
            continue
        seen.add(key)
        text = str(evidence.get("text") or evidence.get("chunk_text") or "").strip()
        document_id = str(evidence.get("document_id") or "")
        image_ids = [str(image_id) for image_id in evidence.get("image_ids") or [] if image_id]
        image_urls = [perception_image_url(document_id, image_id) for image_id in image_ids] if document_id else []
        image_urls.extend(
            url for value in evidence.get("image_urls") or [] if (url := str(value).strip())
        )
        image_urls = list(dict.fromkeys(image_urls))
        item = {
            "id": evidence.get("record_id") or evidence.get("document_id") or key,
            "title": evidence.get("title") or evidence.get("name") or "匹配结果",
            "summary": evidence.get("summary") or text[:240],
            "details": text,
            "image_ids": image_ids,
            "image_urls": image_urls,
            "knowledge_type": evidence.get("source_knowledge_type")
            or evidence.get("knowledge_type"),
            "mysql_id": evidence.get("mysql_id"),
            "source_id": evidence.get("source_id"),
            "document_id": evidence.get("document_id"),
            "chunk_role": evidence.get("chunk_role"),
            "chunk_index": evidence.get("chunk_index"),
            "matched_draft_record_id": evidence.get("matched_draft_record_id"),
            "matched_draft_mysql_id": evidence.get("matched_draft_mysql_id"),
            "source": {
                "source_locator": source_locator,
                "source": evidence.get("source"),
                "source_collection": evidence.get("source_collection"),
                "knowledge_type": knowledge_type,
                "document_id": evidence.get("document_id"),
                "mysql_id": evidence.get("mysql_id"),
                "source_id": evidence.get("source_id"),
                "chunk_role": evidence.get("chunk_role"),
                "record_id": evidence.get("record_id"),
                "version_id": evidence.get("version_id"),
            },
            "evidence": {
                key: evidence.get(key)
                for key in (
                    "record_id",
                    "text",
                    "score",
                    "source",
                    "source_locator",
                    "mysql_id",
                    "source_id",
                    "document_id",
                    "version_id",
                    "chunk_role",
                    "chunk_index",
                )
                if evidence.get(key) is not None
            },
        }
        if evidence.get("mysql_id") is not None:
            item["mysql_id"] = evidence.get("mysql_id")
            item["source"]["mysql_id"] = evidence.get("mysql_id")
        # 只有具备详情接口所需的块定位信息时才暴露详情字段。
        if (
            evidence.get("chunk_role")
            and evidence.get("chunk_index") is not None
            and (evidence.get("mysql_id") or evidence.get("document_id"))
        ):
            item.update(
                {
                    "mysql_id": evidence.get("mysql_id"),
                    "source_id": evidence.get("source_id"),
                    "document_id": evidence.get("document_id"),
                    "chunk_role": evidence.get("chunk_role"),
                    "chunk_index": evidence.get("chunk_index"),
                    "detail_available": True,
                }
            )
        items.append(item)
        if len(items) >= max_items:
            break
    return items


def compose_business_response(
    request: ChatRequest,
    *,
    answer: str,
    status: str,
    evidences: list[dict[str, Any]],
    images: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    web_results: list[dict[str, Any]] | None = None,
    internal_flow: dict[str, Any] | None = None,
    related_references: list[dict[str, Any]] | None = None,
    match_draft: dict[str, Any] | None = None,
    match_results: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """根据已完成的问答结果生成稳定的 UI blocks 和声明式 actions。"""

    if web_results:
        suffix = "以上结果来自于网络搜索，真伪自辩。"
        if suffix not in str(answer or ""):
            answer = f"{str(answer or '').rstrip()}\n\n{suffix}"
    flow = internal_flow or {}
    match_flow = request.intent_hint in {"supply_demand_match", "demand_discovery"} or flow.get(
        "name"
    ) in {
        "supply_demand_match",
        "demand_discovery",
    }
    card_flow = (
        match_flow
        or request.intent_hint == "enterprise_profile"
        or flow.get("name") == "enterprise_profile"
    )
    demand_discovery = (
        request.intent_hint == "demand_discovery" or flow.get("name") == "demand_discovery"
    )
    contact_offer = "也可以直接联系客服。" if request.user_role == "external" else ""
    draft_match = match_draft or (
        request.draft_context
        if isinstance(request.draft_context, dict)
        and request.draft_context.get("draft_id")
        and request.draft_context.get("draft_type")
        else None
    )
    final_match_results = select_match_results(list(match_results or []))
    if card_flow and evidences and not final_match_results and not web_results:
        final_match_results = build_recommendation_items(evidences)
    follow_up_text = ""
    if match_flow and final_match_results and draft_match:
        draft_label = (
            "需求"
            if draft_match.get("draft_type") == "demand"
            else ("科研成果" if draft_match.get("draft_type") == "achievement" else "技术方案")
        )
        follow_up_text = (
            f"以上是根据{draft_label}草稿匹配到的结果。"
            f"您可以继续补充当前草稿，或编辑草稿后重新匹配。{contact_offer}"
        )
    elif match_flow and final_match_results and "匹配结果不满意" not in answer:
        match_draft_type = resolve_match_draft_type(
            request.draft_context,
            request.knowledge_type,
        )
        if match_draft_type:
            draft_label = {
                "demand": "需求",
                "solution": "技术方案",
                "achievement": "科研成果",
            }[match_draft_type]
            follow_up_text = f"已根据您的描述检索知识库。以上是匹配到的需求和合作机会。如果还需要更精确的匹配，可以补充{draft_label}草稿后重新匹配，是否立即补充？{contact_offer}"
        else:
            direction = (
                str((request.draft_context or {}).get("match_direction") or "").strip().lower()
            )
            follow_up_text = (
                f"已根据您的描述检索知识库。以上是匹配到的结果；请补充您要匹配的技术方案或科研成果类型及具体内容。{contact_offer}"
                if direction == "supply_to_demand"
                else f"已根据您的描述检索知识库。以上是匹配到的结果；如需更精确匹配，请先明确供需方向并补充具体内容。{contact_offer}"
            )

    blocks: list[dict[str, Any]] = [
        {
            "type": "answer",
            "data": {
                "text": answer,
                "status": status,
            },
        }
    ]
    if web_results:
        blocks.append(
            {
                "type": "web_search_results",
                "data": {"title": "联网搜索结果", "count": len(web_results), "items": web_results},
            }
        )
    actions: list[dict[str, Any]] = []

    output_blocks = flow.get("output_blocks") or []
    if output_blocks:
        blocks.append(
            {
                "type": "business_sections",
                "data": {
                    "title": "内部市场分析维度",
                    "items": [
                        {
                            "key": key,
                            "evidence_count": sum(
                                1
                                for evidence in evidences
                                if (
                                    evidence.get("source_knowledge_type")
                                    or evidence.get("knowledge_type")
                                    or evidence.get("source_collection")
                                )
                                in _knowledge_types_for_block(key)
                            ),
                        }
                        for key in output_blocks
                    ],
                },
            }
        )

    if status == "FAILED":
        blocks.append(
            {
                "type": "error_state",
                "data": {"title": "本次请求未完成", "message": answer},
            }
        )
    elif (
        evidences
        and status not in {"NO_EVIDENCE", "FAILED"}
        and (not card_flow or final_match_results)
    ):
        if card_flow:
            recommendations = final_match_results
            if recommendations:
                if draft_match:
                    groups = (
                        (
                            (("solution", "achievement"), "技术方案 / 科研成果"),
                            (("expert",), "相关专家"),
                            (("enterprise",), "相关企业"),
                        )
                        if draft_match.get("draft_type") == "demand"
                        else ((("demand",), "相关需求"),)
                    )
                    for knowledge_type, title in groups:
                        items = [
                            item
                            for item in recommendations
                            if item.get("knowledge_type") in knowledge_type
                        ]
                        if items:
                            blocks.append(
                                {
                                    "type": "recommendation_list",
                                    "data": {"title": title, "count": len(items), "items": items},
                                }
                            )
                else:
                    blocks.append(
                        {
                            "type": "recommendation_list",
                            "data": {
                                "title": "匹配结果",
                                "count": len(recommendations),
                                "items": recommendations,
                            },
                        }
                    )
                if match_flow:
                    actions.append(
                        {
                            "id": "next_match_batch",
                            "type": "NEXT_MATCH_BATCH",
                            "label": "换一批",
                            "message": "换一批匹配结果",
                            "enabled": True,
                            "intent_hint": "demand_discovery"
                            if demand_discovery
                            else "supply_demand_match",
                        }
                    )
                    if draft_match:
                        actions.append(
                            {
                                "id": "edit_published_draft",
                                "type": "EDIT_DRAFT",
                                "label": "编辑草稿",
                                "enabled": True,
                                "draft": draft_match,
                            }
                        )
                    else:
                        action_draft_type = resolve_match_draft_type(
                            request.draft_context,
                            request.knowledge_type,
                        )
                        if action_draft_type:
                            action_draft_label = {
                                "demand": "需求",
                                "solution": "技术方案",
                                "achievement": "科研成果",
                            }[action_draft_type]
                            actions.append(
                                {
                                    "id": "match_result_start_draft",
                                    "type": "MATCH_RESULT_CONFIRM",
                                    "value": "start_draft",
                                    "draft_type": action_draft_type,
                                    "label": "立即补充",
                                    "message": f"立即进入{action_draft_label}草稿完善流程",
                                    "enabled": True,
                                }
                            )
                            actions.append(
                                {
                                    "id": "match_result_finish",
                                    "type": "MATCH_RESULT_CONFIRM",
                                    "value": "finish",
                                    "label": "结束匹配",
                                    "enabled": True,
                                }
                            )
                if match_flow and follow_up_text:
                    blocks.append({"type": "match_follow_up", "data": {"text": follow_up_text}})
        if not match_flow and not card_flow and not web_results:
            blocks.append(
                {
                    "type": "evidence_list",
                    "data": {
                        "title": "检索证据",
                        "count": len(evidences),
                        "items": evidences,
                    },
                }
            )
    else:
        match_draft_type = resolve_match_draft_type(
            match_draft or request.draft_context,
            request.knowledge_type,
        )
        blocks.append(
            {
                "type": "empty_state",
                "data": {
                    "title": "暂未找到匹配证据",
                    "message": (
                        "暂未找到相关需求，您可以补充需求信息后重新匹配。"
                        if demand_discovery
                        else (
                            "暂未检索到与您当前描述相关的内容。"
                            "如果需要更精确的匹配，您可以补充信息，我会整理成需求草稿后重新匹配；"
                            f"草稿发布后会同步到知识库，方便供给方检索。是否立即补充完善需求草稿？{contact_offer}"
                        )
                        if match_flow and match_draft_type == "demand"
                        else (
                            "暂未检索到与您当前描述相关的内容。"
                            f"如果需要更精确的匹配，您可以补充信息，我会整理成{'科研成果' if match_draft_type == 'achievement' else '技术方案'}草稿后重新匹配；"
                            f"草稿发布后会同步到知识库，方便需求方检索。是否立即补充完善{'科研成果' if match_draft_type == 'achievement' else '技术方案'}草稿？{contact_offer}"
                        )
                        if match_flow and match_draft_type in _SUPPLY_DRAFT_TYPES
                        else (
                            "暂未找到匹配需求。请补充技术方案或科研成果类型及具体内容后继续。"
                            if match_flow
                            and str((request.draft_context or {}).get("match_direction") or "")
                            .strip()
                            .lower()
                            == "supply_to_demand"
                            else "当前尚未明确供需方向。请先确认是用需求寻找方案/成果，还是用方案/成果寻找需求，再继续匹配。"
                        )
                        if match_flow
                        else ("可以调整问题描述或扩大知识范围；当前不会生成无来源结论。")
                    ),
                    "reason": "NO_EVIDENCE",
                },
            }
        )
        if demand_discovery:
            actions.extend(
                [
                    _action("supplement_demand", "SUPPLEMENT_DEMAND", "补充需求信息"),
                    _action("retry_demand_search", "RETRY_MATCH", "重新匹配"),
                ]
            )
        elif not match_flow and request.user_role == "internal":
            actions.append(
                _action(
                    "create_customer_inquiry",
                    "CUSTOMER_INQUIRY_CREATE",
                    "整理为客户询盘",
                )
            )
        elif match_flow and draft_match:
            actions.append(
                {
                    "id": "edit_published_draft",
                    "type": "EDIT_DRAFT",
                    "label": "编辑草稿",
                    "enabled": True,
                    "draft": draft_match,
                }
            )
        elif match_flow:
            action_draft_type = resolve_match_draft_type(
                request.draft_context,
                request.knowledge_type,
            )
            if action_draft_type:
                action_draft_label = {
                    "demand": "需求",
                    "solution": "技术方案",
                    "achievement": "科研成果",
                }[action_draft_type]
                actions.append(
                    {
                        "id": "match_result_start_draft",
                        "type": "MATCH_RESULT_CONFIRM",
                        "value": "start_draft",
                        "draft_type": action_draft_type,
                        "label": "立即补充",
                        "message": f"立即进入{action_draft_label}草稿完善流程",
                        "enabled": True,
                    }
                )
                actions.append(
                    {
                        "id": "match_result_finish",
                        "type": "MATCH_RESULT_CONFIRM",
                        "value": "finish",
                        "label": "结束匹配",
                        "enabled": True,
                    }
                )

    if (
        request.user_role == "external"
        and status != "FAILED"
        and (match_flow or (not evidences and not web_results))
    ):
        actions.append(_action("contact_wecom", "CONTACT_WECOM", "联系客服"))

    match_direction = (
        str((request.draft_context or {}).get("match_direction") or "").strip().lower()
    )
    if (
        match_flow
        and not demand_discovery
        and not final_match_results
        and not match_direction
        and status != "FAILED"
    ):
        actions.extend(
            [
                {
                    "id": "match_direction_demand_to_supply",
                    "type": "SELECT_MATCH_DIRECTION",
                    "value": "demand_to_supply",
                    "label": "我有需求，找方案/成果",
                    "enabled": True,
                },
                {
                    "id": "match_direction_supply_to_demand",
                    "type": "SELECT_MATCH_DIRECTION",
                    "value": "supply_to_demand",
                    "label": "我有方案/成果，找需求",
                    "enabled": True,
                },
            ]
        )

    if images:
        blocks.append(
            {
                "type": "image_references",
                "data": {"title": "相关图片", "count": len(images), "items": images},
            }
        )

    documents = sanitize_document_references(request, documents)

    return blocks, actions


def _knowledge_types_for_block(block: str) -> set[str]:
    return {
        "basic_info": {"enterprise"},
        "enterprise_profile": {"enterprise"},
        "technical_capability": {"enterprise"},
        "potential_demand": {"demand", "enterprise"},
        "adapted_solution": {"solution", "achievement", "expert", "enterprise"},
        "supplier_match_dimensions": {"solution", "achievement", "expert", "enterprise"},
    }.get(block, set())


def compose_intent_response(
    *, intent: dict[str, Any], answer: str, status: str, actions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成意图确认或已确认后的下一步展示块。"""

    actions = list(actions)
    if status in {"INTENT_CONFIRM_REQUIRED", "INTERNAL_INTENT_CONFIRM_REQUIRED"}:
        actions.append(
            {"id": "back_flow", "type": "BACK_FLOW", "label": "返回上一步", "enabled": True}
        )

    blocks = [
        {"type": "answer", "data": {"text": answer, "status": status}},
        {"type": "intent_confirmation", "data": {"intent": intent}},
    ]
    internal_flow = intent.get("internal_flow") or {}
    if internal_flow:
        blocks.append(
            {
                "type": "internal_flow",
                "data": {
                    "title": "内部市场处理方向",
                    "intent": internal_flow.get("name"),
                    "output_blocks": internal_flow.get("output_blocks") or [],
                    "knowledge_types": internal_flow.get("knowledge_types") or [],
                },
            }
        )
    return blocks, actions


def compose_conversation_response(
    *,
    answer: str,
    status: str,
    intent: dict[str, Any],
    actions: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """通用对话只展示回复，不展示业务意图确认或业务操作按钮。"""

    return [
        {"type": "answer", "data": {"text": answer, "status": status, "intent": intent}}
    ], actions or []


def compose_draft_information_response(
    *,
    answer: str,
    intent: dict[str, Any],
    missing_fields: list[str],
    questions: list[str],
    options: list[str] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """草稿生成前的必要信息采集提示；此状态不返回草稿。"""

    flow_definition = None
    flow_key = str(intent.get("name") or "")
    if flow_key in {"enterprise", "demand", "solution", "achievement", "customer_visit"}:
        flow_definition = get_flow_definition(flow_key).to_dict()

    actions = list(actions or [])
    actions.append({"id": "back_flow", "type": "BACK_FLOW", "label": "返回上一步", "enabled": True})
    return [
        {
            "type": "draft_information_required",
            "data": {
                "title": "生成草稿前，请先补充以下信息",
                "missing_fields": missing_fields,
                "questions": questions,
                "options": options or [],
                "intent": intent,
                "flow_definition": flow_definition,
            },
        }
    ], actions


def compose_access_denied_response(
    *, intent: dict[str, Any], answer: str, user_role: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """外部用户越权意图的安全响应，不暴露内部工具或资源细节。"""

    actions: list[dict[str, Any]] = []
    if user_role == "external":
        actions.append(_action("contact_wecom", "CONTACT_WECOM", "联系客服"))
    return [
        {
            "type": "access_denied",
            "data": {"title": "当前身份暂不支持此操作", "message": answer, "intent": intent},
        }
    ], actions


def compose_draft_response(
    draft: dict[str, Any], user_role: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    is_publishable_draft = str(draft.get("draft_type") or "") in {
        "demand",
        "solution",
        "achievement",
    }
    actions = [
        {"id": "save_draft", "type": "SAVE_DRAFT", "label": "保存", "enabled": True},
    ]
    if is_publishable_draft:
        actions.append(
            {"id": "publish_draft", "type": "PUBLISH_DRAFT", "label": "发布", "enabled": True}
        )
    elif str(draft.get("draft_type") or "") == "customer_visit":
        actions.append(
            {
                "id": "confirm_inquiry",
                "type": "CONFIRM_DRAFT",
                "label": "确认并保存",
                "enabled": True,
            }
        )
    return [
        {"type": "draft_card", "data": {"draft": draft}},
    ], actions
