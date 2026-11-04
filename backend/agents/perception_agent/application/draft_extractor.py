"""Perception Agent 的材料字段提取能力。"""

from __future__ import annotations

import re
from typing import Any

from core.prompts.perception import MATERIAL_EXTRACTION_SYSTEM_TEMPLATE
from runtime.config import get_settings, settings_for_role
from services.llm import get_llm_client

MATERIAL_SEGMENT_CHARS = 12000
MAX_MATERIAL_EXTRACTION_CALLS = 5
MATERIAL_SUMMARY_MAX_CHARS = 500
_MATCH_SUMMARY_TYPES = {
    "match", "solution_match", "achievement_match", "demand_discovery",
}


def compact_match_summary(value: str, *, max_chars: int = MATERIAL_SUMMARY_MAX_CHARS) -> str:
    """压缩非草稿匹配摘要，保留段落并限制总长度。"""

    return _compact_summary_paragraphs(value, max_chars=max_chars, complete_sentences_only=False)


def compact_document_summary(value: str, *, max_chars: int = MATERIAL_SUMMARY_MAX_CHARS) -> str:
    """压缩普通资料摘要，保留分段并限制总长度。"""

    return _compact_summary_paragraphs(value, max_chars=max_chars, complete_sentences_only=True)


def _compact_summary_paragraphs(
    value: str, *, max_chars: int, complete_sentences_only: bool
) -> str:
    paragraphs: list[str] = []
    seen: set[str] = set()
    pending_title = ""
    for raw_paragraph in re.split(r"\n\s*\n|\r?\n", str(value or "")):
        paragraph = " ".join(raw_paragraph.strip().split())
        paragraph = re.sub(r"^(?:[-*•]\s*|\d+[、.)]\s*)+", "", paragraph).strip()
        title_match = re.match(r"^名称\s*[:：]\s*(.+)$", paragraph)
        if title_match:
            if pending_title:
                paragraphs.append(pending_title)
            pending_title = title_match.group(1).strip()
            continue
        description_match = re.match(r"^描述\s*[:：]\s*(.+)$", paragraph)
        if description_match and pending_title:
            paragraph = f"{pending_title}：{description_match.group(1).strip()}"
            pending_title = ""
        elif pending_title:
            paragraphs.append(pending_title)
            pending_title = ""
        paragraph = re.sub(
            r"^(?:字段\s*[:：]\s*)?(?:资料|项目|产品|方案|成果)?"
            r"(?:名称|描述|简介|内容|摘要|说明)\s*[:：]\s*",
            "",
            paragraph,
        ).strip()
        if paragraph and paragraph not in seen:
            seen.add(paragraph)
            paragraphs.append(paragraph)
    if pending_title and pending_title not in seen:
        paragraphs.append(pending_title)

    if not paragraphs or max_chars <= 0:
        return ""

    selected: list[str] = []
    remaining = max_chars
    for paragraph in paragraphs:
        separator = 1 if selected else 0
        available = remaining - separator
        if available <= 0:
            break
        if len(paragraph) > available:
            clipped = paragraph[:available]
            if complete_sentences_only:
                boundary = max(
                    clipped.rfind(mark)
                    for mark in ("。", "；", "！", "？", ".", "!", "?")
                )
                if boundary >= max(1, available // 2):
                    selected.append(clipped[: boundary + 1].rstrip())
            else:
                boundary = max(clipped.rfind("。"), clipped.rfind("；"), clipped.rfind("，"))
                if boundary >= max(1, available // 2):
                    clipped = clipped[:boundary]
                clipped = clipped.rstrip("；，。 ")
                if clipped:
                    selected.append(clipped)
            break
        selected.append(paragraph)
        remaining -= separator + len(paragraph)
    return "\n".join(selected)


def split_material(text: str, *, max_chars: int = MATERIAL_SEGMENT_CHARS,
                   max_segments: int = MAX_MATERIAL_EXTRACTION_CALLS) -> list[str]:
    normalized = str(text or "").replace("\x00", " ").strip()
    if not normalized:
        return []
    segments: list[str] = []
    start = 0
    while start < len(normalized) and len(segments) < max_segments:
        end = min(start + max_chars, len(normalized))
        if end < len(normalized):
            boundary = max(normalized.rfind("\n", start, end), normalized.rfind("。", start, end),
                           normalized.rfind("；", start, end), normalized.rfind(";", start, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1
        value = normalized[start:end].strip()
        if value:
            segments.append(value)
        start = end
    if start < len(normalized):
        raise ValueError(
            f"资料内容超过单次解析上限（{max_segments} 段，每段最多 {max_chars} 字）"
        )
    return segments


def material_segments(text: str, blocks: list[dict[str, Any]] | None = None) -> list[str]:
    page_text: dict[str, list[str]] = {}
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        value = str(block.get("text") or "").replace("\x00", " ").strip()
        if value:
            page_text.setdefault(str(block.get("page_number") or block.get("page") or "unknown"), []).append(value)
    if not page_text:
        return split_material(text)
    segments: list[str] = []
    current = ""
    for page in ("\n\n".join(values) for values in page_text.values()):
        for part in split_material(page):
            if current and len(current) + len(part) + 2 > MATERIAL_SEGMENT_CHARS:
                segments.append(current)
                current = ""
            current = f"{current}\n\n{part}".strip()
    if current:
        segments.append(current)
    if len(segments) > MAX_MATERIAL_EXTRACTION_CALLS:
        raise ValueError(
            f"资料内容超过单次解析上限（{MAX_MATERIAL_EXTRACTION_CALLS} 段，每段最多 {MATERIAL_SEGMENT_CHARS} 字）"
        )
    return segments


async def extract_material_facts(text: str, knowledge_type: str,
                                 blocks: list[dict[str, Any]] | None = None,
                                 *, conversation_context: str = "", client: Any | None = None,
                                 on_token: Any = None) -> tuple[str, int]:
    """按页面提取事实；LLM 不可用或提取失败时显式报错。"""
    segments = material_segments(text, blocks)
    if not segments:
        return "", 0
    match_mode = knowledge_type in _MATCH_SUMMARY_TYPES
    document_mode = knowledge_type == "document"
    if client is None:
        client = get_llm_client(settings_for_role(get_settings(), "draft"))
    hint = {
        "solution": "方案名称、企业名称、产品类型、方案描述、方案分析、联系人、联系电话、交付周期、报价区间",
        "achievement": "成果名称、发布单位、成果类型、成果描述、成果分析、联系人、联系电话",
        "demand": "需求标题、企业名称、需求描述、联系人、联系电话、预算、项目周期",
        "demand_discovery": "需求名称、需求描述",
        "solution_match": "方案名称、方案描述",
        "achievement_match": "成果名称、成果描述",
        "match": "名称、描述",
    }.get(knowledge_type, "名称、描述" if document_mode else "标题、企业名称、产品类型、描述、分析、联系人、联系电话、周期、预算")
    extracted: list[str] = []
    for index, segment in enumerate(segments, start=1):
        prompt = MATERIAL_EXTRACTION_SYSTEM_TEMPLATE.format(
            knowledge_type=knowledge_type or "unknown",
            hint=hint,
            conversation_context=str(conversation_context or "")[:6000],
            index=index,
            total=len(segments),
            segment=segment,
        )
        if match_mode:
            prompt += (
                f"\n这是供需检索摘要，不是草稿字段提取。输出分段总结，总计不超过{MATERIAL_SUMMARY_MAX_CHARS}字，"
                "优先保留名称、技术能力、应用场景、目标行业、关键指标和合作方向；"
                "去掉政策背景、目录、联系人、电话、地址、重复内容和泛化宣传语。"
            )
        elif document_mode:
            prompt += (
                f"\n这是普通资料摘要，不是草稿字段提取。请按主题输出分段总结，总计不超过{MATERIAL_SUMMARY_MAX_CHARS}字；"
                "若资料包含多个独立事项，每项单独一行，直接写实际名称和核心内容，不要编号，不要输出“名称：”或“描述：”字段标签，"
                "不要把名称和内容拆成两行；不要留空行，不要复制整篇原文。"
            )
        if on_token is not None and hasattr(client, "stream") and not (match_mode or document_mode):
            parts: list[str] = []
            async for token in client.stream(prompt):
                parts.append(str(token))
                await on_token(str(token))
            value = "".join(parts)
        else:
            value = await client.complete(prompt)
        extracted_value = str(value or "").strip()
        if not extracted_value:
            raise RuntimeError(f"资料字段提取第 {index} 段返回空内容")
        extracted.append(extracted_value)
    result = "\n\n".join(extracted)
    if match_mode:
        result = compact_match_summary(result, max_chars=MATERIAL_SUMMARY_MAX_CHARS)
    elif document_mode:
        result = compact_document_summary(result)
    if on_token is not None and (match_mode or document_mode) and result:
        for start in range(0, len(result), 80):
            await on_token(result[start:start + 80])
    return result, len(segments)
