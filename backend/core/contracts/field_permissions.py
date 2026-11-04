"""公开/内部字段权限基线，供 Perception 预览和 Retrieval 索引共同使用。"""

from typing import Any

from core.contracts.mysql_schema import MYSQL_FIELD_COMMENTS, MYSQL_FORM_CONTRACTS, ordered_fields

MILVUS_COLLECTIONS = {
    "enterprise": "enterprise",
    "demand": "demand",
    "achievement": "achievement",
    "expert": "expert",
    "solution": "solution",
    "customer_visit": "inner",
    "user": None,
}

PUBLIC_FIELDS = {
    "enterprise": {"enterprise_name", "description", "logo", "admin_name", "authentication_image", "avatar", "business_no", "reg_capital", "reg_location", "company_org_type", "estiblish_time", "base", "city", "district", "category", "category_big", "category_middle", "category_small", "reg_status", "tags", "street", "企业名称", "企业介绍", "Logo", "联系人", "营业执照照片", "用户头像", "营业执照号", "注册资本", "注册地址", "企业类型", "成立日期", "省简称", "市", "区", "行业分类门类", "行业分类大类", "行业分类中类", "行业分类小类", "企业状态", "企业标签", "街道名称"},
    "achievement": {"title", "tech_type", "product", "analyse_contect", "remark", "is_knowledge", "application", "aplication_field_scenario", "main_function", "main_advantage", "scene_lable", "chain_lable", "product_names", "标题", "技术类别", "应用产品", "成果分析", "备注", "拥有知识产权", "应用领域", "应用领域及场景", "成果主要功能", "成果主要优势", "应用场景标签", "产业链标签", "应用产品标签"},
    "expert": {"user_name", "image", "btype_name", "duty", "achievements", "research_field", "label", "brief_intr", "introduction", "technical_category", "college", "professional_title", "achievements_honors", "application", "main_product", "technological_achievements", "专家姓名", "头像", "企业名称", "职务", "主要成果", "研究领域", "标签", "简介", "介绍", "应用领域", "学院", "职称", "成就与荣誉", "服务领域", "应用产品", "主要技术成果"},
    "demand": {"title", "tech_type", "product", "first_img_url", "contect", "analyse_contect", "remark", "application", "category", "innovation_type", "scene_lable", "product_names", "标题", "技术类别", "应用产品", "需求封面", "成果概述/需求描述", "成果分析", "备注", "应用领域", "需求类型", "创新成果类型", "应用场景标签", "应用产品标签"},
}
INTERNAL_FIELDS = {
    "enterprise": {"created_by", "business_scope", "创建人", "经营范围"},
    "achievement": {"knowledge_code", "invention_name", "company", "pub_dept", "link_name", "link_tel", "create_time", "发布单位", "发布部门", "专利号", "发明人", "联系人", "联系电话", "创建时间"},
    "expert": {"create_time", "phone", "email", "office_phone", "创建时间", "联系电话", "邮箱", "办公电话"},
    "demand": {"link_name", "link_tel", "create_time", "联系人", "联系电话", "创建时间"},
}
NON_PUBLIC_FIELDS = {
    "enterprise": frozenset({"created_by", "business_scope", "创建人", "经营范围"}),
}

# MySQL 物理表字段与权限 Excel 的统一基线。旧的中文字段别名继续保留，
# 用于兼容历史 Excel 解析结果；英文列名以新建表语句为准。
for _knowledge_type, _contract in MYSQL_FORM_CONTRACTS.items():
    PUBLIC_FIELDS.setdefault(_knowledge_type, set()).update(_contract["public_fields"])
    INTERNAL_FIELDS.setdefault(_knowledge_type, set()).update(_contract["internal_fields"])

SYSTEM_FIELDS = {
    _knowledge_type: set(_contract["system_fields"])
    for _knowledge_type, _contract in MYSQL_FORM_CONTRACTS.items()
}

_POLICY_METADATA = {
    "enterprise": ("企业库", "企业业务字段公开；创建人和经营范围仅限内部。"),
    "achievement": ("成果库", "成果能力可公开检索，专利和联系人信息仅限内部。"),
    "expert": ("专家库", "专家能力和成果可公开展示，联系方式和内部时间字段不对外。"),
    "demand": ("需求库", "需求内容可用于匹配；联系人和联系电话仅限内部，发布部门和企业名称对外可见。"),
    "solution": ("技术方案库", "方案能力可公开检索，联系人和同步字段仅限内部。"),
    "customer_visit": ("客户拜访库", "拜访记录仅限创建人和管理员访问，不进入公开检索。"),
    "user": ("用户身份表", "仅用于身份识别和权限判断，不作为知识库内容。"),
}


def list_field_policies() -> list[dict[str, Any]]:
    """返回管理端展示与运行时预览共用的字段分级基线。"""

    policies: list[dict[str, Any]] = []
    for knowledge_type in ("enterprise", "achievement", "expert", "demand", "solution", "customer_visit", "user"):
        label, note = _POLICY_METADATA[knowledge_type]
        contract = MYSQL_FORM_CONTRACTS[knowledge_type]
        unconfigured = not bool(contract.get("table"))
        is_document_solution = knowledge_type == "solution" and unconfigured
        fields = [] if is_document_solution else [
            {"key": field, "label": MYSQL_FIELD_COMMENTS.get(knowledge_type, {}).get(field, field)}
            for field in ordered_fields(knowledge_type)
        ]
        policies.append({
            "type": knowledge_type,
            "label": label,
            "note": note,
            # 管理端展示物理 MySQL 字段；PUBLIC_FIELDS/INTERNAL_FIELDS
            # 仍包含历史中文别名，仅供解析结果兼容，不应参与字段数量统计。
            "public": [] if is_document_solution else [field for field in ordered_fields(knowledge_type) if field in contract["public_fields"]],
            "internal": [] if is_document_solution else [field for field in ordered_fields(knowledge_type) if field in contract["internal_fields"]],
            "system": [] if is_document_solution else [field for field in ordered_fields(knowledge_type, include_system_fields=True) if field in contract["system_fields"]],
            "fields": fields,
            "table": contract.get("table"),
            "collection": MILVUS_COLLECTIONS.get(knowledge_type),
            "identity_only": bool(contract.get("identity_only")),
            "unconfigured": unconfigured,
        })
    return policies


def preview_field_permissions(knowledge_type: str, fields: dict[str, Any]) -> dict[str, str]:
    """返回字段名到 public/internal/unmapped 的预览标记。"""

    known = PUBLIC_FIELDS.get(knowledge_type, set()) | INTERNAL_FIELDS.get(knowledge_type, set())
    return {
        str(name): "public" if str(name) in PUBLIC_FIELDS.get(knowledge_type, set())
        else "internal" if str(name) in INTERNAL_FIELDS.get(knowledge_type, set())
        else "unmapped"
        for name, value in fields.items()
        if value is not None and str(value).strip() and str(name) in known
    } | {
        str(name): "unmapped"
        for name, value in fields.items()
        if value is not None and str(value).strip() and str(name) not in known
    }
