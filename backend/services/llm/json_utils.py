"""LLM JSON 输出的最小容错解析。"""

from __future__ import annotations

import json
import re
from typing import Any


def prompt_version(messages: str | list[dict]) -> str | None:
    """从系统提示词读取可选版本标记，供日志关联，不改变提示词内容。"""

    text = messages if isinstance(messages, str) else "\n".join(
        str(item.get("content", "")) for item in messages if item.get("role") == "system"
    )
    match = re.search(r"prompt[_ -]?version\s*[:=]\s*([\w.-]+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def parse_json_object(text: str) -> dict[str, Any] | None:
    """接受代码块和前后说明，解析失败时返回 None 交给上层降级。"""

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            from json_repair import repair_json

            value = json.loads(repair_json(cleaned[start:end + 1]))
        except (ImportError, TypeError, ValueError, json.JSONDecodeError):
            return None
    return value if isinstance(value, dict) else None
