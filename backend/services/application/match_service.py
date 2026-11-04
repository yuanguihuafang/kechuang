"""供需匹配的轻量业务规则；不负责调用 Agent 或存储。"""

from __future__ import annotations

import re


class MatchService:
    """整理匹配输入，检索编排仍由 Supervisor 负责。"""

    @staticmethod
    def split_requirements(text: str) -> list[str]:
        """只拆分需求内容，避免把草稿标题、预算等字段误当成匹配项。"""

        raw = str(text or "").strip()
        if not raw:
            return []
        lines: list[str] = []
        for line in raw.splitlines():
            key, separator, value = line.partition("：")
            if not separator:
                key, separator, value = line.partition(":")
            if separator and any(marker in key.lower() for marker in ("requirement", "demand", "需求", "采购")):
                lines.append(value.strip())
            elif re.match(r"^\s*\d+[\.、\)]", line):
                lines.append(line.strip())
        source = "；".join(lines) if lines else raw
        parts = re.split(r"(?:\r?\n|[；;]+|(?<!\d)\d+[\.、\)])", source)
        items = [re.sub(r"^\s*需求\s*[:：]?\s*", "", part).strip(" ，,。") for part in parts]
        return [item for item in items if item]

    @staticmethod
    def build_visit_query(*, enterprise: str, demand: str, budget: str = "", cycle: str = "", supplier_match_dim: str = "") -> str:
        """生成匹配查询；仅在内容过大时按优先级丢弃上下文。"""

        max_length = 2000
        parts = [
            f"企业画像：{enterprise}" if enterprise else "",
            f"潜在需求：{demand}" if demand else "",
            *(
                f"{label}：{value}" if value else ""
                for label, value in (
                    ("预算区间", budget),
                    ("预计周期", cycle),
                    ("供应商匹配维度", supplier_match_dim),
                )
            ),
        ]
        parts = [part for part in parts if part]
        query = "\n".join(parts)
        if len(query) <= max_length:
            return query.strip()

        # 拜访原文过大时，先移除企业画像，再保留需求和能放下的可选条件。
        query = "\n".join(parts[1:2])
        for part in parts[2:]:
            if len(query) + len(part) + 1 <= max_length:
                query = f"{query}\n{part}"
        return query.strip()
