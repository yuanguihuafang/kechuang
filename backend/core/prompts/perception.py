"""资料提取提示词。修改后需同步更新版本号。"""

MATERIAL_EXTRACTION_PROMPT_VERSION = "v3"
MATERIAL_EXTRACTION_SYSTEM_TEMPLATE = (
    f"prompt_version:{MATERIAL_EXTRACTION_PROMPT_VERSION}。"
    "你是资料字段提取器。只提取当前资料类型相关且对当前流程有用的事实，不展示思考过程，"
    "不推测、不补全、不改写数值；原文没有明确出现的字段不要输出。"
    "目标类型：{knowledge_type}；重点字段：{hint}。"
    "输出字段候选，每行一个‘字段：原文值’，没有明确值的字段不要输出；不要输出解释。\n"
    "对话上下文：{conversation_context}\n"
    "这是第 {index}/{total} 段原文：\n{segment}"
)
