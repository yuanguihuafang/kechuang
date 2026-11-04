"""业务草稿契约，与 MySQL 物理表结构独立。"""

from __future__ import annotations

from typing import Any, Final

from core.context_window import limit_draft_context


DRAFT_FIELD_MAX_CHARS: Final = 3060
DRAFT_MANUAL_FIELD_MAX_CHARS: Final = 3060


def limit_draft_text(value: Any) -> Any:
    """限制草稿字段文本；原文 source_blocks 不经过这里。"""
    return value[:DRAFT_FIELD_MAX_CHARS] if isinstance(value, str) else value


def limit_draft_payload(draft: dict[str, Any]) -> dict[str, Any]:
    """限制草稿字段、摘要和来源块；所有进入模型的草稿使用同一边界。"""
    result = limit_draft_context(draft)
    nested_drafts = draft.get("drafts")
    if isinstance(nested_drafts, list):
        result["drafts"] = [
            limit_draft_payload(item) for item in nested_drafts if isinstance(item, dict)
        ]
    for key in (
        "title",
        "summary",
        "requirement_summary",
        "technical_summary",
        "achievement_summary",
        "enterprise_summary",
        "expert_summary",
    ):
        if isinstance(result.get(key), str):
            result[key] = limit_draft_text(result[key])
    for key in ("fields", "mysql_fields"):
        values = result.get(key)
        if isinstance(values, dict):
            result[key] = {field: limit_draft_text(value) for field, value in values.items()}
    return result


DRAFT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "demand": (
        "title",
        "category",
        "company",
        "contect",
        "analyse_contect",
        "link_name",
        "link_tel",
        "budget_range",
        "demand_cycle",
    ),
    "solution": (
        "title",
        "pub_dept",
        "product_type",
        "contect",
        "analyse_contect",
        "link_name",
        "link_tel",
        "deliver_cycle",
        "quote_range",
    ),
    "achievement": (
        "title",
        "category",
        "analyse_contect",
        "tech_maturity",
        "cooperation_mode",
        "company",
        "link_name",
        "link_tel",
    ),
    "enterprise": (
        "enterprise_name",
        "description",
        "admin_name",
        "phone",
        "email",
        "address",
        "business_no",
        "reg_capital",
        "reg_location",
        "business_scope",
        "company_org_type",
        "estiblish_time",
        "base",
        "city",
        "district",
        "category",
        "category_big",
        "category_middle",
        "category_small",
        "reg_status",
        "tags",
        "street",
    ),
    "expert": (
        "user_name",
        "image",
        "btype_name",
        "duty",
        "achievements",
        "research_field",
        "label",
        "brief_intr",
        "introduction",
        "technical_category",
        "phone",
        "email",
        "college",
        "office_phone",
        "professional_title",
        "achievements_honors",
        "application",
        "main_product",
        "technological_achievements",
    ),
    "customer_visit": (
        "enterprise_name",
        "enterprise_portrait",
        "potential_demand_list_text",
        "budget_range",
        "expect_cycle",
        "adapt_solution",
        "supplier_match_dim",
        "inner_mark_note",
    ),
}

DRAFT_FIELD_LABELS: Final[dict[str, dict[str, str]]] = {
    "demand": {
        "title": "需求名称",
        "category": "需求类型",
        "company": "企业名称",
        "contect": "需求描述",
        "analyse_contect": "需求分析",
        "link_name": "联系人",
        "link_tel": "联系电话",
        "budget_range": "预算区间",
        "demand_cycle": "需求周期",
    },
    "solution": {
        "title": "方案名称",
        "pub_dept": "发布企业名称",
        "product_type": "产品类型",
        "contect": "方案描述",
        "analyse_contect": "方案分析",
        "link_name": "联系人",
        "link_tel": "联系电话",
        "deliver_cycle": "交付周期",
        "quote_range": "报价区间",
    },
    "achievement": {
        "title": "成果名称",
        "category": "成果类型",
        "analyse_contect": "成果分析",
        "tech_maturity": "技术成熟度",
        "cooperation_mode": "合作模式",
        "company": "发布单位",
        "link_name": "联系人",
        "link_tel": "联系电话",
    },
    "customer_visit": {
        "enterprise_name": "公司名称",
        "enterprise_portrait": "企业画像",
        "potential_demand_list_text": "潜在需求",
        "budget_range": "预算区间",
        "expect_cycle": "预计周期",
        "adapt_solution": "适配方案",
        "supplier_match_dim": "供应商匹配维度",
        "inner_mark_note": "补充记录",
    },
    "expert": {"image": "头像"},
}


def draft_field_length_errors(draft: dict[str, Any]) -> list[str]:
    """返回超过人工发布上限的草稿字段展示名。"""
    draft_type = str(draft.get("draft_type") or "")
    labels = DRAFT_FIELD_LABELS.get(draft_type, {})
    errors: list[str] = []
    seen: set[str] = set()

    def check(field_name: str, value: Any) -> None:
        if (
            isinstance(value, str)
            and len(value) > DRAFT_MANUAL_FIELD_MAX_CHARS
            and field_name not in seen
        ):
            seen.add(field_name)
            errors.append(labels.get(field_name, field_name))

    for container_name in ("fields", "mysql_fields"):
        values = draft.get(container_name)
        if isinstance(values, dict):
            for field_name, value in values.items():
                check(str(field_name), value)
    for field_name in (
        "title",
        "summary",
        "requirement_summary",
        "technical_summary",
        "achievement_summary",
        "enterprise_summary",
        "expert_summary",
    ):
        check(field_name, draft.get(field_name))
    return errors


# 草稿字段业务说明；required 字段由 DRAFT_REQUIRED_FIELDS 统一判定。
DRAFT_FIELD_DESCRIPTIONS: Final[dict[str, dict[str, str]]] = {
    "demand": {
        "title": "需求标题或名称",
        "category": "客户需求所属类型",
        "company": "从用户身份表读取的所属企业",
        "contect": "客户提出的采购痛点、技术指标等需求描述",
        "analyse_contect": "对需求背景、难点和可行性的分析",
        "link_name": "客户联系人姓名",
        "link_tel": "客户联系电话",
        "budget_range": "客户整体预算范围",
        "demand_cycle": "客户期望的需求交付周期",
    },
    "solution": {
        "title": "技术方案名称",
        "pub_dept": "发布方案的企业或单位",
        "product_type": "方案对应的产品类型",
        "contect": "方案内容和能力描述",
        "analyse_contect": "AI 生成的方案分析",
        "link_name": "方案联系人姓名",
        "link_tel": "方案联系人电话",
        "deliver_cycle": "方案交付周期",
        "quote_range": "方案报价范围",
    },
    "achievement": {
        "title": "科研成果名称",
        "category": "成果所属类型",
        "analyse_contect": "科研成果分析和能力说明",
        "tech_maturity": "成果技术成熟度",
        "cooperation_mode": "成果合作方式",
        "company": "从用户身份表读取的发布单位",
        "link_name": "成果联系人姓名",
        "link_tel": "成果联系电话",
    },
    "customer_visit": {
        "enterprise_name": "本次拜访的公司名称",
        "enterprise_portrait": "本次拜访企业整体画像",
        "potential_demand_list_text": "本次拜访识别出的全部潜在需求，逐条记录",
        "budget_range": "本次拜访涉及的整体预算",
        "expect_cycle": "本次拜访涉及的整体项目周期",
        "adapt_solution": "根据需求检索匹配出的适配方案方向",
        "supplier_match_dim": "供应商筛选关注点和匹配维度",
        "inner_mark_note": "市场私有拜访手记，默认留空，供用户手动补充",
    },
}

DRAFT_REQUIRED_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "demand": (
        "title",
        "category",
        "company",
        "contect",
        "analyse_contect",
        "link_name",
        "link_tel",
    ),
    "solution": ("title", "contect"),
    "achievement": ("title", "company", "analyse_contect"),
    "customer_visit": (),
}

# 附件由对象存储管理；专家头像是明确标注时可提取的 MySQL 字段。
DRAFT_EXCLUDED_FIELDS: Final[dict[str, frozenset[str]]] = {
    "achievement": frozenset({"temp_same_title"}),
    "enterprise": frozenset({"logo", "authentication_image", "avatar"}),
    "customer_visit": frozenset({"link_name", "link_tel"}),
}


def draft_fields(knowledge_type: str) -> tuple[str, ...]:
    return DRAFT_FIELDS.get(knowledge_type, ())


def draft_required_fields(knowledge_type: str) -> tuple[str, ...]:
    return DRAFT_REQUIRED_FIELDS.get(knowledge_type, ())


def draft_field_labels(knowledge_type: str) -> dict[str, str]:
    return dict(DRAFT_FIELD_LABELS.get(knowledge_type, {}))
