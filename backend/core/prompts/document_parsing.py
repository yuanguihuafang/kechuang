"""文档解析辅助提示词。修改后需同步更新版本号。"""

EXCEL_REVIEW_PROMPT_VERSION = "v2"
EXCEL_REVIEW_SYSTEM = (
    f"prompt_version:{EXCEL_REVIEW_PROMPT_VERSION}。"
    "你是业务资料结构审阅器。只根据给定 Excel 样本快速判断，不展示思考过程，不补造事实。"
    "只输出 JSON：knowledge_type、record_unit、confidence、optional_questions、notes。"
    "knowledge_type 只能是 enterprise、demand、achievement、expert、solution 之一。"
    "record_unit 只能是 row、row_group、section、unknown；无法确认时使用 unknown。"
)
EXCEL_REVIEW_USER_TEMPLATE = (
    "请审阅下面的 Excel 样本。问题只用于给用户提示，不是强制字段；"
    "如果信息已经足够，optional_questions 输出空数组。\n<excel_sample>\n{sample}\n</excel_sample>"
)

VISION_DRAFT_PROMPT_VERSION = "v3"


def build_vision_draft_prompt(
    *, file_name: str, knowledge_type: str | None, field_requirements: str,
) -> str:
    """Build the multimodal extraction prompt without policy in the parser."""
    return (
        f"prompt_version:{VISION_DRAFT_PROMPT_VERSION}。文件名：{file_name}\n"
        f"资料类型：{knowledge_type or '未指定'}\n"
        "你是文档解析和草稿字段提取器。请一次完成解析，只返回 JSON，不要 Markdown，不要输出思考过程。"
        '格式：{"pages":[{"page":1,"text":"该页完整原文"}],'
        '"draft":{"title":"","fields":{},"summary":"",'
        '"missing_fields":[],"optional_questions":[],"confidence":0,'
        '"items":[{"title":"","fields":{},"summary":"",'
        '"missing_fields":[],"optional_questions":[],"confidence":0}]}}。'
        "默认只返回一条 draft；只有确认存在多条彼此独立的需求、方案、成果或拜访记录时，"
        "才在 draft.items 返回多条，不能把同一条记录拆成多条。"
        f"{field_requirements}原文必须完整保留；字段只能依据图片和原文上下文填写，不能猜测。"
        "每个 draft.fields 字段和 draft.summary 最多 3060 个字符；超过时只保留准确的压缩摘要，不要复制整篇原文或图片 URL。"
    )
