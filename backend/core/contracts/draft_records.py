"""草稿发布前的统一业务记录物化契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .draft_contracts import DRAFT_EXCLUDED_FIELDS, DRAFT_FIELD_LABELS
from .mysql_schema import MYSQL_FIELD_COMMENTS, business_fields, form_contract


_FIELD_LABELS = {
    "title": "标题", "tech_type": "技术类别", "category": "类型", "enterprise_name": "企业名称", "company": "企业名称",
    "contect": "内容描述", "analyse_contect": "内容分析", "link_name": "联系人",
    "link_tel": "联系电话", "budget_range": "预算区间", "demand_cycle": "需求周期",
    "pub_dept": "发布单位", "product_type": "产品类型", "deliver_cycle": "交付周期",
    "quote_range": "报价区间", "tech_maturity": "技术成熟度", "cooperation_mode": "合作模式",
    "enterprise_portrait": "企业画像", "potential_demand_list_text": "潜在需求",
    "expect_cycle": "预计周期", "adapt_solution": "适配方案",
    "supplier_match_dim": "供应商匹配维度", "description": "企业介绍",
    "user_name": "专家姓名", "research_field": "研究领域",
}

_FLOW_FIELD_LABELS = DRAFT_FIELD_LABELS

_FIELD_ALIASES = {
    "provider_name": "pub_dept", "product_category": "product_type",
    "technical_summary": "contect", "delivery_cycle": "deliver_cycle", "price_range": "quote_range",
    "product": "tech_type",
}


@dataclass(frozen=True)
class MaterializedDraftRecord:
    """用户确认后的草稿，转换成可写入知识库的完整父记录。"""

    record_id: str
    text: str
    source_locator: str
    metadata: dict[str, Any]


def materialize_draft_record(
    draft_id: str, draft: dict[str, Any], *, include_internal: bool = True,
) -> MaterializedDraftRecord:
    """把草稿的有效字段合并成一条完整父记录。"""

    summary_key = {
        "enterprise": "enterprise_summary", "demand": "requirement_summary",
        "solution": "technical_summary", "achievement": "achievement_summary",
        "expert": "expert_summary", "customer_visit": "inquiry_summary",
    }.get(str(draft.get("draft_type") or ""), "summary")
    lines = [str(draft.get("title") or "").strip()]
    draft_type = str(draft.get("draft_type") or "")
    summary = "" if draft_type == "customer_visit" else str(draft.get(summary_key) or "").strip()
    if summary:
        lines.append(summary)
    fields = dict(draft.get("mysql_fields") or {})
    # 旧草稿没有 mysql_fields 时，继续使用前端草稿字段发布。
    fields.update(draft.get("fields") or {})
    for old_key, new_key in _FIELD_ALIASES.items():
        if new_key not in fields and old_key in fields:
            fields[new_key] = fields[old_key]
    labels = {**_FIELD_LABELS, **_FLOW_FIELD_LABELS.get(draft_type, {})}
    mysql_comments = MYSQL_FIELD_COMMENTS.get(draft_type, {})
    public_fields = set(form_contract(draft_type).get("public_fields") or ())
    system_fields = set(form_contract(draft_type).get("system_fields") or ())
    excluded_fields = DRAFT_EXCLUDED_FIELDS.get(draft_type, frozenset())
    ordered_keys = list(business_fields(draft_type))
    ordered_keys.extend(key for key in fields if key not in ordered_keys)
    for key in ordered_keys:
        value = fields.get(key)
        if value is None or not str(value).strip():
            continue
        if key in system_fields:
            continue
        if key in excluded_fields:
            continue
        if not include_internal and draft_type != "customer_visit" and key not in public_fields:
            continue
        label = labels.get(key) or mysql_comments.get(key)
        if not label:
            label = key if re.search(r"[\u4e00-\u9fff]", key) else "其他字段"
        lines.append(f"{label}：{str(value).strip()}")
    text = "\n".join(line for line in lines if line)
    if not text:
        raise ValueError("草稿没有可发布的有效内容")
    return MaterializedDraftRecord(
        record_id=draft_id,
        text=text,
        source_locator=f"draft:{draft_id}|type:{draft.get('draft_type') or 'unknown'}",
        metadata={
            "draft_id": draft_id,
            "source": "conversation",
            "source_document_id": (draft.get("attachments") or [{}])[0].get("document_id"),
            "field_names": [key for key in ordered_keys if fields.get(key) not in (None, "") and key not in system_fields and key not in excluded_fields and (include_internal or draft_type == "customer_visit" or key in public_fields)],
        },
    )
