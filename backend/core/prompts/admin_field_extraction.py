"""管理后台文档字段提取专用 Prompt。"""

ADMIN_FIELD_EXTRACTION_PROMPT_VERSION = "admin-v3"

ADMIN_FIELD_EXTRACTION_PROFILES = {
    "enterprise": ("企业库", "重点整理企业基本信息、主营业务、经营范围和联系方式。"),
    "demand": ("需求库", "重点整理需求描述、需求类型、企业和联系方式。"),
    "solution": ("方案库", "重点整理技术方案、产品类型、应用场景和联系方式。"),
    "achievement": ("成果库", "重点整理成果内容、技术类别、发布单位和联系方式。"),
    "expert": ("专家库", "重点整理专家信息、所属单位、研究方向和联系方式。"),
    "customer_visit": ("客户拜访记录", "重点整理本次拜访事实及明确提及的潜在需求。"),
}


def build_admin_field_extraction_system(
    *,
    draft_type: str,
    allow_multiple: bool,
    field_guide: str,
    required_labels: str,
) -> str:
    label, focus = ADMIN_FIELD_EXTRACTION_PROFILES.get(
        draft_type, ("当前资料", "按目标 MySQL 表字段整理资料。")
    )
    multiple = (
        "原文包含多条独立记录时，分别放入 items 数组；每项均包含 title、fields、summary、"
        "missing_fields、optional_questions、confidence。"
        if allow_multiple
        else "只生成一条记录。"
    )
    return (
        "你是管理后台的资料字段提取器，负责把已解析原文整理成管理员可校对的 MySQL 记录。"
        f"prompt_version:{ADMIN_FIELD_EXTRACTION_PROMPT_VERSION}。目标资料类型：{label}。"
        f"{focus}必须完整检查原文：优先提取草稿核心字段，"
        "字段清单中的其他 MySQL 业务字段只要有原文依据也要提取，不能只抽草稿字段。"
        "字段清单外的内容不要输出；不根据管理员身份或外部知识补造资料。"
        f"{multiple}只输出 JSON，字段为 title、fields、summary、missing_fields、"
        "optional_questions、confidence。"
        "confidence 必须是 0 到 1 的数字，不要输出 low、medium、high 等文字标签。"
        "fields 必须使用给定的 MySQL 物理字段名；缺失或无法确认的信息留空。"
        "数值、日期、单位、型号和专有名词按原文保留。"
        "各字段按语义填写，不要互相重复；只要原文存在相关线索，"
        "就尽量整理成简洁、宽泛且有原文依据的内容供管理员核对，"
        "不要因为信息不完整或需要归纳就把字段留空；确实完全没有依据时才留空。"
        "描述字段只保留归属该字段的事实，禁止把整篇原文或所有字段拼入单个字段。"
        "analyse_contect 必须基于原文事实归纳，不能复制原文、summary 或其他字段。"
        "summary 仅作整体摘要，不填充业务字段。"
        "每个 fields 字段值和 summary 最多 3060 个字符，超长时压缩为完整摘要。"
        "图片 URL 仅在明确归属图片、头像、照片或 Logo 字段时保留。"
        f"MySQL 字段清单（中文名、物理字段名及说明）：{field_guide}。"
        f"当前类型必填字段：{required_labels}。missing_fields 仅使用这些中文展示名。"
        "管理后台不通过本次提取追问用户，optional_questions 返回空数组。"
    )
