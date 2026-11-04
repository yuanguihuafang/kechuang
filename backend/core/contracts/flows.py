"""可替换的业务流程/草稿字段定义。"""

from __future__ import annotations

from dataclasses import dataclass

from core.contracts.draft_contracts import DRAFT_FIELD_LABELS, DRAFT_REQUIRED_FIELDS, draft_fields
from core.contracts.mysql_schema import MYSQL_FIELD_COMMENTS, business_fields, is_identity_only

FIELD_LABELS = {
    "title": "标题",
    "tech_type": "技术类别",
    "category": "类型",
    "enterprise_name": "企业名称",
    "company": "企业名称",
    "contect": "内容描述",
    "analyse_contect": "内容分析",
    "link_name": "联系人",
    "link_tel": "联系电话",
    "budget_range": "预算区间",
    "demand_cycle": "需求周期",
    "pub_dept": "发布单位",
    "product_type": "产品类型",
    "deliver_cycle": "交付周期",
    "quote_range": "报价区间",
    "tech_maturity": "技术成熟度",
    "cooperation_mode": "合作模式",
    "enterprise_portrait": "企业画像",
    "potential_demand_list_text": "潜在需求",
    "expect_cycle": "预计周期",
    "adapt_solution": "适配方案",
    "supplier_match_dim": "供应商匹配维度",
    "description": "企业介绍",
    "user_name": "专家姓名",
    "research_field": "研究领域",
}

FLOW_FIELD_LABELS = {
    **DRAFT_FIELD_LABELS,
    "enterprise": MYSQL_FIELD_COMMENTS.get("enterprise", {}),
    "expert": MYSQL_FIELD_COMMENTS.get("expert", {}),
}

FLOW_META = {
    "enterprise": ("企业草稿", "企业信息", "enterprise_summary"),
    "demand": ("需求草稿", "需求", "requirement_summary"),
    "solution": ("技术方案草稿", "技术方案", "technical_summary"),
    "achievement": ("科研成果草稿", "科研成果", "achievement_summary"),
    "expert": ("专家草稿", "专家信息", "expert_summary"),
    "customer_visit": ("客户拜访记录草稿", "客户拜访记录", "inquiry_summary"),
}

REQUIRED_FIELDS = {
    **DRAFT_REQUIRED_FIELDS,
    "enterprise": ("enterprise_name", "description"),
    "expert": ("user_name", "research_field"),
}

REQUIRED_FIELD_LABELS = {
    "demand": DRAFT_FIELD_LABELS["demand"],
}

# 需求清洗表中的“需求类型”存在组合值，交互选项使用其去重后的基础分类。
DEMAND_TYPE_OPTIONS = ("物料采购", "智能制造", "创新研发", "软件服务", "数字化管理", "其他")
SUPPLY_DRAFT_TYPE_OPTIONS = ("技术方案", "科研成果")


@dataclass(frozen=True)
class IntentFlowDefinition:
    """业务流程的声明式元数据；DeerFlow 负责执行。"""

    key: str
    label: str
    operations: tuple[str, ...]
    knowledge_types: tuple[str, ...]
    query_field: str | None = None
    query_prompt: str | None = None
    allowed_roles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "operations": list(self.operations),
            "knowledge_types": list(self.knowledge_types),
            "query_field": self.query_field,
            "query_prompt": self.query_prompt,
            "allowed_roles": list(self.allowed_roles),
        }


INTENT_FLOW_REGISTRY: dict[str, IntentFlowDefinition] = {
    "enterprise_profile": IntentFlowDefinition(
        "enterprise_profile",
        "企业画像",
        ("knowledge_search",),
        ("enterprise",),
        "公司名称、技术方向或业务领域",
        "请告诉我企业名称、简称，或具体技术方向和业务领域。例如：查询青岛某公司的技术能力，或寻找工业机器人研发企业。",
    ),
    "demand_discovery": IntentFlowDefinition(
        "demand_discovery",
        "需求发现",
        ("knowledge_search",),
        ("demand",),
        "具体需求或技术方向",
        "请描述具体需求、产品或技术内容即可，例如寻找适合汽车产线的视觉质检方案。",
    ),
    "supply_demand_match": IntentFlowDefinition(
        "supply_demand_match",
        "供需匹配",
        ("knowledge_search", "supply_demand_match"),
        ("enterprise", "expert", "solution", "achievement"),
        "需求、技术方向或合作目标",
        "请描述具体需求或方案成果内容即可，例如名称、技术方向、产品或应用用途。也可以上传相关文档，我会据此进行匹配。",
    ),
    "expert_search": IntentFlowDefinition(
        "expert_search",
        "专家匹配",
        ("knowledge_search",),
        ("expert",),
        "技术领域或专业方向",
        "请告诉我需要解决的技术问题、研究方向或专业领域。例如：寻找工业污水处理、机器视觉或新材料方向的专家。",
    ),
    "customer_visit": IntentFlowDefinition(
        "customer_visit",
        "客户拜访",
        ("draft_create", "draft_update", "supply_demand_match"),
        ("enterprise", "solution", "expert", "achievement"),
        allowed_roles=("internal",),
    ),
}


def get_intent_flow(intent: str | None) -> IntentFlowDefinition | None:
    return INTENT_FLOW_REGISTRY.get(str(intent or "").strip())


def intent_flow_definitions() -> list[dict[str, object]]:
    return [flow.to_dict() for flow in INTENT_FLOW_REGISTRY.values()]


@dataclass(frozen=True)
class FlowDefinition:
    key: str
    label: str
    summary_label: str
    summary_field: str
    fields: tuple[str, ...]
    required_fields: tuple[str, ...]

    @property
    def field_labels(self) -> dict[str, str]:
        labels = {**FIELD_LABELS, **FLOW_FIELD_LABELS.get(self.key, {})}
        return {key: labels.get(key, key) for key in self.fields}

    def to_dict(self) -> dict[str, object]:
        labels = self.field_labels
        return {
            "key": self.key,
            "label": self.label,
            "summary_field": self.summary_field,
            "fields": [
                {
                    "key": key,
                    "label": labels[key],
                    "required": key in self.required_fields,
                    "input_type": "text",
                }
                for key in self.fields
            ],
        }


def get_flow_definition(flow_key: str) -> FlowDefinition:
    if flow_key not in FLOW_META or is_identity_only(flow_key):
        raise ValueError(f"不支持的草稿流程：{flow_key}")
    label, summary_label, summary_field = FLOW_META[flow_key]
    fields = draft_fields(flow_key) or business_fields(flow_key)
    return FlowDefinition(
        key=flow_key,
        label=label,
        summary_label=summary_label,
        summary_field=summary_field,
        fields=tuple(fields),
        required_fields=REQUIRED_FIELDS.get(flow_key, ()),
    )


def flow_definitions() -> list[dict[str, object]]:
    return [get_flow_definition(key).to_dict() for key in FLOW_META]


def required_field_pairs(flow_key: str) -> tuple[tuple[str, str], ...]:
    definition = get_flow_definition(flow_key)
    labels = {**definition.field_labels, **REQUIRED_FIELD_LABELS.get(flow_key, {})}
    return tuple((key, labels.get(key, key)) for key in definition.required_fields)
