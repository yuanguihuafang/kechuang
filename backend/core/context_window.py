"""统一的智能问答上下文边界。

上下文只在进入模型前构建一次边界。数据库、草稿和检索结果都不能把
未限制的原文直接拼进下游 prompt。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


SUMMARY_INPUT_MAX_CHARS = 6_000
SUMMARY_MAX_CHARS = 1_200
DRAFT_TEXT_MAX_CHARS = 8_000
SOURCE_BLOCK_TEXT_MAX_CHARS = 4_000
INTENT_CONTEXT_MAX_CHARS = 16_000
REASONING_CONTEXT_MAX_TOKENS = 12_000
DRAFT_FIELD_MAX_CHARS = 3_060
NESTED_VALUE_MAX_CHARS = 2_000
MAX_CONTEXT_ITEMS = 20


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = max(minimum, int(os.getenv(name, str(default))))
        return min(value, maximum) if maximum is not None else value
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ContextWindowPolicy:
    recent_message_limit: int = 12
    history_message_chars: int = 2_000
    rolling_summary_chars: int = SUMMARY_MAX_CHARS
    summary_input_chars: int = SUMMARY_INPUT_MAX_CHARS
    draft_text_chars: int = DRAFT_TEXT_MAX_CHARS
    source_block_text_chars: int = SOURCE_BLOCK_TEXT_MAX_CHARS
    intent_context_chars: int = INTENT_CONTEXT_MAX_CHARS
    max_tokens: int = REASONING_CONTEXT_MAX_TOKENS

    @classmethod
    def from_env(cls) -> "ContextWindowPolicy":
        return cls(
            recent_message_limit=_env_int("CONTEXT_RECENT_MESSAGE_LIMIT", 12),
            history_message_chars=_env_int("CONTEXT_HISTORY_MESSAGE_CHARS", 2_000),
            rolling_summary_chars=_env_int(
                "CONTEXT_ROLLING_SUMMARY_CHARS", SUMMARY_MAX_CHARS, maximum=SUMMARY_MAX_CHARS
            ),
            summary_input_chars=_env_int(
                "CONTEXT_SUMMARY_INPUT_CHARS", SUMMARY_INPUT_MAX_CHARS, maximum=SUMMARY_INPUT_MAX_CHARS
            ),
            draft_text_chars=_env_int(
                "CONTEXT_DRAFT_TEXT_CHARS", DRAFT_TEXT_MAX_CHARS, maximum=DRAFT_TEXT_MAX_CHARS
            ),
            source_block_text_chars=_env_int(
                "CONTEXT_SOURCE_BLOCK_TEXT_CHARS",
                SOURCE_BLOCK_TEXT_MAX_CHARS,
                maximum=SOURCE_BLOCK_TEXT_MAX_CHARS,
            ),
            intent_context_chars=_env_int(
                "CONTEXT_INTENT_JSON_CHARS", INTENT_CONTEXT_MAX_CHARS, maximum=INTENT_CONTEXT_MAX_CHARS
            ),
            max_tokens=_env_int(
                "REASONING_CONTEXT_MAX_TOKENS",
                REASONING_CONTEXT_MAX_TOKENS,
                minimum=1_000,
                maximum=REASONING_CONTEXT_MAX_TOKENS,
            ),
        )


POLICY = ContextWindowPolicy.from_env()


def clip_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    head = max(1, int(max_chars * 0.7))
    tail = max(1, max_chars - head - 1)
    return f"{text[:head]}…{text[-tail:]}"


def format_history_message(item: Mapping[str, Any]) -> str:
    role = str(item.get("role") or "").strip() or "unknown"
    content = clip_text(item.get("content"), POLICY.history_message_chars)
    return f"{role}: {content}" if content else ""


def user_history_text(messages: list[str] | tuple[str, ...]) -> str:
    """只返回用户事实，绝不把助手引导语当成草稿原文。"""

    return "\n".join(
        message.strip()
        for message in messages
        if str(message or "").strip().lower().startswith("user:")
    )


def build_summary_input(
    previous: str,
    messages: list[str] | tuple[str, ...],
    *,
    max_chars: int = SUMMARY_INPUT_MAX_CHARS,
) -> tuple[str, list[str]]:
    """保留摘要和最新待压缩消息，总输入不超过一个硬上限。"""

    previous_text = clip_text(previous, min(SUMMARY_MAX_CHARS, max_chars))
    remaining = max(0, max_chars - len(previous_text))
    selected: list[str] = []
    for message in reversed(messages):
        value = clip_text(message, min(POLICY.history_message_chars, remaining))
        if not value:
            continue
        separator_cost = 1 if selected else 0
        if len(value) + separator_cost > remaining:
            break
        selected.append(value)
        remaining -= len(value) + separator_cost
    return previous_text, list(reversed(selected))


def _limit_nested(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return clip_text(value, NESTED_VALUE_MAX_CHARS)
    if isinstance(value, Mapping):
        if depth >= 4:
            return {}
        return {
            str(key): _limit_nested(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_CONTEXT_ITEMS]
        }
    if isinstance(value, list):
        return [_limit_nested(item, depth=depth + 1) for item in value[:MAX_CONTEXT_ITEMS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return clip_text(value, NESTED_VALUE_MAX_CHARS)


def limit_draft_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """限制外部草稿上下文，保留字段结构但拒绝无界原文。"""

    if not isinstance(value, Mapping):
        return {}
    result = {
        str(key): _limit_nested(item) for key, item in value.items()
        if key != "_index_snapshot"
    }
    for key in ("text", "summary", "requirement_summary", "technical_summary", "achievement_summary"):
        if key in value:
            result[key] = clip_text(value.get(key), POLICY.draft_text_chars)
    for key in ("fields", "mysql_fields"):
        fields = value.get(key)
        if isinstance(fields, Mapping):
            result[key] = {
                str(field): clip_text(field_value, DRAFT_FIELD_MAX_CHARS)
                for field, field_value in list(fields.items())[:MAX_CONTEXT_ITEMS]
                if field_value not in (None, "")
            }
    blocks = value.get("source_blocks")
    if isinstance(blocks, list):
        limited_blocks: list[Any] = []
        for block in blocks[:MAX_CONTEXT_ITEMS]:
            if not isinstance(block, Mapping):
                limited_blocks.append(_limit_nested(block))
                continue
            limited = {str(key): _limit_nested(item) for key, item in block.items()}
            if "text" in limited:
                limited["text"] = clip_text(limited["text"], POLICY.source_block_text_chars)
            limited_blocks.append(limited)
        result["source_blocks"] = limited_blocks
    return result
