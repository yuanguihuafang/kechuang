"""长期记忆识别；只识别稳定偏好或约束，不把普通问题写入记忆。"""

from __future__ import annotations

import re
from typing import Any

_REMEMBER = re.compile(r"^(?:请)?记住(?P<content>.+)$")
_FUTURE = re.compile(r"^(?:以后|今后)(?P<content>.+)$")


def extract_memory_candidates(message: str) -> list[dict[str, Any]]:
    text = str(message or "").strip()
    match = _REMEMBER.match(text) or _FUTURE.match(text)
    if not match:
        return []
    content = match.group("content").strip()
    if len(content) < 2:
        return []
    memory_type = (
        "preference"
        if any(word in content for word in ("喜欢", "偏好", "希望", "习惯"))
        else "constraint"
    )
    return [{
        "memory_type": memory_type,
        "content": content[:10000],
        "source": "conversation_candidate",
        "importance": 0.6,
        "confirmed": False,
        "status": "PENDING",
    }]
