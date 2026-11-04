"""MySQL 真实表结构契约，只描述远程表及物理字段。"""

from __future__ import annotations

import json
from typing import Final
from urllib.parse import quote


MYSQL_FORM_CONTRACTS: Final[dict[str, dict[str, object]]] = {
    "enterprise": {
        "table": "xp_enterprise_profile",
        "public_fields": frozenset(
            {
                "enterprise_name",
                "description",
                "logo",
                "admin_name",
                "authentication_image",
                "avatar",
                "business_no",
                "reg_capital",
                "reg_location",
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
                "phone",
                "email",
                "address",
                "social_staff_num",
            }
        ),
        "internal_fields": frozenset({
                "created_by",
                "business_scope",
        }),
        "system_fields": frozenset(
            {
                "id",
                "src_id",
                "update_time",
                "updated_time",
                "created_time",
            }
        ),
    },
    "demand": {
        "table": "xp_pub_content_simple",
        "public_fields": frozenset(
            {
                "title",
                "tech_type",
                "product",
                "first_img_url",
                "contect",
                "analyse_contect",
                "remark",
                "application",
                "category",
                "innovation_type",
                "scene_lable",
                "product_names",
                "budget_range",
                "demand_cycle",
                "pub_dept",
                "company",
            }
        ),
        "internal_fields": frozenset({
            "link_name",
            "link_tel",
        }),
        "system_fields": frozenset({
            "id",
            "src_id",
            "update_time",
            "create_time",
        }),
    },
    "achievement": {
        "table": "xp_pub_result",
        "public_fields": frozenset(
            {
                "title",
                "category",
                "tech_type",
                "product",
                "analyse_contect",
                "remark",
                "is_knowledge",
                "application",
                "aplication_field_scenario",
                "main_function",
                "main_advantage",
                "scene_lable",
                "chain_lable",
                "product_names",
                "tech_maturity",
                "cooperation_mode",
            }
        ),
        "internal_fields": frozenset(
            {
                "knowledge_code",
                "invention_name",
                "company",
                "pub_dept",
                "link_name",
                "link_tel",
            }
        ),
        "system_fields": frozenset({
            "temp_same_title",
            "id",
            "src_id",
            "update_time",
            "create_time",
        }),
    },
    "expert": {
        "table": "xp_expert_library",
        "public_fields": frozenset(
            {
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
                "college",
                "professional_title",
                "achievements_honors",
                "application",
                "main_product",
                "technological_achievements",
            }
        ),
        "internal_fields": frozenset({
            "phone",
            "email",
            "office_phone",
        }),
        "system_fields": frozenset({
            "id",
            "src_id",
            "update_time",
            "create_time",
        }),
    },
    "solution": {
        "table": "xp_pub_tech_solution",
        "public_fields": frozenset(
            {
                "title",
                "tech_type",
                "product_type",
                "first_img_url",
                "contect",
                "analyse_contect",
                "deliver_cycle",
                "quote_range",
                "remark",
                "application",
                "scene_lable",
                "product_names",
            }
        ),
        "internal_fields": frozenset({
            "link_name",
            "link_tel",
            "pub_dept",
        }),
        "system_fields": frozenset({
            "id",
            "src_draft_id",
            "update_time",
            "create_time",
        }),
    },
    "customer_visit": {
        "table": "ai_inner_inquiry",
        "public_fields": frozenset(),
        "internal_fields": frozenset({
            "enterprise_name",
            "enterprise_portrait",
            "potential_demand_list_text",
            "budget_range",
            "expect_cycle",
            "adapt_solution",
            "supplier_match_dim",
            "inner_mark_note",
        }),
        "system_fields": frozenset({
            "id",
            "user_card_id",
            "chat_session_id",
            "inquiry_status",
            "is_deleted",
            "create_time",
            "update_time",
        }),
    },
    "user": {
        "table": "xp_user_card_simple",
        "public_fields": frozenset(),
        "internal_fields": frozenset(),
        "system_fields": frozenset(
            {
                "id",
                "telphone",
                "name",
                "linkphone",
                "company",
                "business_no",
                "pcard",
                "ecard",
                "create_time",
                "user_type",
                "is_inner_user",
                "src_id",
                "update_time",
            }
        ),
        "identity_only": True,
    },
}

# 物理字段中文说明，供管理后台、同步文本和开发人员查阅。
MYSQL_FIELD_COMMENTS: Final[dict[str, dict[str, str]]] = {
    "enterprise": {
        "id": "Id",
        "enterprise_name": "企业名称",
        "description": "企业介绍",
        "logo": "Logo",
        "admin_name": "联系人",
        "phone": "联系电话",
        "email": "联系邮箱",
        "address": "通讯地址",
        "authentication_image": "营业执照照片",
        "created_by": "创建人",
        "created_time": "创建时间",
        "update_time": "更新时间",
        "avatar": "用户头像",
        "business_no": "营业执照号",
        "reg_capital": "注册资本",
        "reg_location": "注册地址",
        "business_scope": "经营范围",
        "social_staff_num": "参保人数",
        "company_org_type": "企业类型",
        "estiblish_time": "成立日期",
        "base": "省简称",
        "city": "市",
        "district": "区",
        "category": "国民经济行业分类门类",
        "category_big": "国民经济行业分类大类",
        "category_middle": "国民经济行业分类中类",
        "category_small": "国民经济行业分类小类",
        "reg_status": "企业状态",
        "tags": "企业标签",
        "street": "街道名称",
        "src_id": "原库xp_enterprise_apply主键id",
        "updated_time": "原库xp_enterprise_apply源更新时间",
    },
    "demand": {
        "id": "主键 ID",
        "title": "需求名称",
        "tech_type": "技术类别",
        "product": "应用产品",
        "first_img_url": "需求封面",
        "contect": "需求描述",
        "analyse_contect": "需求分析",
        "remark": "备注",
        "link_name": "联系人",
        "link_tel": "联系电话",
        "pub_dept": "发布部门",
        "create_time": "创建时间",
        "application": "应用领域",
        "category": "需求类型",
        "company": "企业名称",
        "innovation_type": "创新成果类型",
        "scene_lable": "应用场景标签",
        "product_names": "应用产品标签",
        "src_id": "原库xp_pub_content主键id",
        "update_time": "原库xp_pub_content源更新时间",
        "budget_range": "预算区间",
        "demand_cycle": "需求周期",
    },
    "achievement": {
        "id": "主键 ID",
        "title": "成果名称",
        "category": "成果类型",
        "company": "发布单位",
        "tech_type": "技术类别",
        "product": "应用产品",
        "analyse_contect": "成果分析",
        "remark": "备注",
        "is_knowledge": "拥有知识产权",
        "knowledge_code": "专利号",
        "invention_name": "发明人",
        "link_name": "联系人",
        "link_tel": "联系电话",
        "pub_dept": "发布部门",
        "create_time": "创建时间",
        "application": "应用领域",
        "aplication_field_scenario": "应用领域及场景",
        "main_function": "成果主要功能",
        "main_advantage": "成果主要优势",
        "scene_lable": "应用场景标签",
        "chain_lable": "产业链标签",
        "product_names": "应用产品标签",
        "temp_same_title": "临时标记：同标题多份成果，待业务甄别",
        "src_id": "原库xp_pub_content主键id",
        "update_time": "原库xp_pub_content源更新时间",
        "tech_maturity": "技术成熟度",
        "cooperation_mode": "合作模式",
    },
    "expert": {
        "id": "主键 ID",
        "user_name": "专家姓名",
        "image": "专家头像",
        "btype_name": "企业名称",
        "duty": "职务",
        "achievements": "主要成果",
        "research_field": "研究领域",
        "label": "专家标签",
        "brief_intr": "专家简介",
        "introduction": "介绍",
        "create_time": "创建时间",
        "technical_category": "应用领域",
        "phone": "联系电话",
        "email": "邮箱",
        "college": "学院",
        "office_phone": "办公电话",
        "professional_title": "职称",
        "achievements_honors": "成果与荣誉",
        "application": "服务领域",
        "main_product": "应用产品",
        "technological_achievements": "主要技术成果",
        "src_id": "原库xp_expert_scientific主键id",
        "update_time": "原库xp_expert_scientific源更新时间",
    },
    "solution": {
        "id": "主键 ID",
        "title": "方案名称",
        "tech_type": "技术类别",
        "product_type": "产品类型",
        "first_img_url": "方案封面图",
        "contect": "方案描述",
        "analyse_contect": "方案分析",
        "deliver_cycle": "交付周期",
        "quote_range": "报价区间",
        "remark": "备注",
        "link_name": "联系人",
        "link_tel": "联系电话",
        "pub_dept": "企业名称",
        "create_time": "创建时间",
        "application": "适配应用领域",
        "scene_lable": "应用场景标签",
        "product_names": "产品标签",
        "src_draft_id": "来源草稿ai_draft_main.id",
        "update_time": "更新时间",
    },
    "customer_visit": {
        "id": "主键 ID",
        "user_card_id": "创建人：内部市场人员，关联xp_user_card_simple.id",
        "chat_session_id": "关联AI会话ID",
        "enterprise_name": "拜访企业名称",
        "enterprise_portrait": "企业画像",
        "potential_demand_list_text": "潜在需求",
        "budget_range": "预算区间",
        "expect_cycle": "项目周期",
        "adapt_solution": "适配方案",
        "supplier_match_dim": "供应商匹配维度",
        "inner_mark_note": "市场私有拜访手记，永不对外输出",
        "inquiry_status": "已保存；询盘无发布状态，仅内部线索",
        "is_deleted": "逻辑删除标记，0正常 1逻辑删除",
        "create_time": "创建时间",
        "update_time": "更新时间",
    },
    "user": {
        "id": "主键 ID",
        "telphone": "手机号",
        "name": "用户姓名",
        "linkphone": "联系方式",
        "company": "公司名称",
        "business_no": "营业执照号码",
        "pcard": "个人名片",
        "ecard": "企业名片",
        "create_time": "创建时间",
        "user_type": "身份类型(1‑企业 2‑专家)",
        "is_inner_user": "是否内部人员:1内部(檬豆),0外部",
        "src_id": "原库xp_user_card主键id",
        "update_time": "原库xp_user_card源更新时间",
    },
}

# 按 MySQL 表单展示顺序维护；权限分类字段只负责分级，不负责排序。
MYSQL_FIELD_ORDER: Final[dict[str, tuple[str, ...]]] = {
    "enterprise": ("id", "enterprise_name", "description", "logo", "admin_name", "phone", "email", "address", "authentication_image", "created_by", "created_time", "update_time", "avatar", "business_no", "reg_capital", "reg_location", "business_scope", "social_staff_num", "company_org_type", "estiblish_time", "base", "city", "district", "category", "category_big", "category_middle", "category_small", "reg_status", "tags", "street", "src_id", "updated_time"),
    "demand": ("id", "title", "tech_type", "product", "first_img_url", "contect", "analyse_contect", "remark", "link_name", "link_tel", "pub_dept", "create_time", "application", "category", "company", "innovation_type", "scene_lable", "product_names", "src_id", "update_time", "budget_range", "demand_cycle"),
    "achievement": ("id", "title", "category", "company", "tech_type", "product", "analyse_contect", "remark", "is_knowledge", "knowledge_code", "invention_name", "link_name", "link_tel", "pub_dept", "create_time", "application", "aplication_field_scenario", "main_function", "main_advantage", "scene_lable", "chain_lable", "product_names", "temp_same_title", "src_id", "update_time", "tech_maturity", "cooperation_mode"),
    "expert": ("id", "user_name", "image", "btype_name", "duty", "achievements", "research_field", "label", "brief_intr", "introduction", "create_time", "technical_category", "phone", "email", "college", "office_phone", "professional_title", "achievements_honors", "application", "main_product", "technological_achievements", "src_id", "update_time"),
    "solution": ("id", "title", "tech_type", "product_type", "first_img_url", "contect", "analyse_contect", "deliver_cycle", "quote_range", "remark", "link_name", "link_tel", "pub_dept", "create_time", "application", "scene_lable", "product_names", "src_draft_id", "update_time"),
    "customer_visit": ("id", "user_card_id", "chat_session_id", "enterprise_name", "enterprise_portrait", "potential_demand_list_text", "budget_range", "expect_cycle", "adapt_solution", "supplier_match_dim", "inner_mark_note", "inquiry_status", "is_deleted", "create_time", "update_time"),
    "user": ("id", "telphone", "name", "linkphone", "company", "business_no", "pcard", "ecard", "create_time", "user_type", "is_inner_user", "src_id", "update_time"),
}

# 中文映射仍集中维护在 MYSQL_FIELD_COMMENTS；加载后只按表单顺序重排，避免两份顺序定义。
MYSQL_FIELD_COMMENTS = {
    knowledge_type: {
        field: comments[field]
        for field in MYSQL_FIELD_ORDER[knowledge_type]
        if field in comments
    }
    for knowledge_type, comments in MYSQL_FIELD_COMMENTS.items()
}


def form_contract(knowledge_type: str) -> dict[str, object]:
    try:
        return MYSQL_FORM_CONTRACTS[knowledge_type]
    except KeyError as exc:
        raise ValueError(f"不支持的 MySQL 表单类型：{knowledge_type}") from exc


def mysql_record_locator(knowledge_type: str, record_id: object) -> str:
    return f"mysql:{form_contract(knowledge_type)['table']}|type:record|id:{record_id}"


def append_attachment_names(
    locator: str,
    attachments: list[dict[str, object]] | None = None,
) -> str:
    """把关联附件名写入可解析的 locator，并保持 Milvus 512 字节限制。"""

    base = str(locator)
    items = [
        {
            "document_id": str(item.get("document_id") or ""),
            "file_name": str(item.get("file_name") or "附件"),
        }
        for item in attachments or []
        if isinstance(item, dict) and item.get("document_id")
    ]
    if not items:
        return base

    for name_limit in (256, 160, 100, 60, 30, 12):
        limited = [
            {**item, "file_name": item["file_name"][:name_limit]}
            for item in items
        ]
        for count in range(len(limited), 0, -1):
            marker = quote(json.dumps(limited[:count], ensure_ascii=False, separators=(",", ":")), safe="")
            locator = f"{base}|attachments:{marker}"
            if len(locator.encode("utf-8")) <= 512:
                return locator
    return base


def mysql_record_locator_with_attachments(
    knowledge_type: str,
    record_id: object,
    attachments: list[dict[str, object]] | None = None,
) -> str:
    return append_attachment_names(mysql_record_locator(knowledge_type, record_id), attachments)


def business_fields(knowledge_type: str) -> tuple[str, ...]:
    contract = form_contract(knowledge_type)
    allowed = set(contract["public_fields"]) | set(contract["internal_fields"])
    return tuple(field for field in MYSQL_FIELD_ORDER[knowledge_type] if field in allowed)


def all_mysql_fields(knowledge_type: str) -> tuple[str, ...]:
    contract = form_contract(knowledge_type)
    allowed = set(contract["public_fields"]) | set(contract["internal_fields"]) | set(contract["system_fields"])
    return tuple(field for field in MYSQL_FIELD_ORDER[knowledge_type] if field in allowed)


def ordered_fields(knowledge_type: str, *, include_system_fields: bool = False) -> tuple[str, ...]:
    """按 MySQL 表单顺序返回字段。"""
    contract = form_contract(knowledge_type)
    allowed = set(contract["public_fields"]) | set(contract["internal_fields"])
    if include_system_fields:
        allowed |= set(contract["system_fields"])
    return tuple(field for field in MYSQL_FIELD_ORDER[knowledge_type] if field in allowed)


def is_identity_only(knowledge_type: str) -> bool:
    return bool(form_contract(knowledge_type).get("identity_only"))


def mysql_fields_to_text(
    knowledge_type: str,
    fields: dict[str, object],
    column_comments: dict[str, str] | None = None,
) -> str:
    """按字段中文标签或 MySQL 注释转换检索文本。"""
    from .draft_contracts import DRAFT_FIELD_LABELS

    draft_labels = DRAFT_FIELD_LABELS.get(knowledge_type, {})
    comments = column_comments or MYSQL_FIELD_COMMENTS.get(knowledge_type, {})
    return "\n".join(
        f"{draft_labels.get(str(key)) or comments.get(str(key)) or str(key)}：{value}"
        for key, value in fields.items()
        if value is not None and str(value).strip()
    )
