"""Supervisor 输出脱敏。

底层知识库保留完整原文；本模块只处理进入 LLM 和返回前端的文本。
Presidio 可以作为后续识别器接入，但当前业务规则先使用确定性规则，避免
把企业名称、报价和合作条款交给不可控的模型判断。
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

REDACTED = "[信息已屏蔽]"


@dataclass(frozen=True)
class RedactionResult:
    text: str
    replacements: int


class TextRedactionService:
    """对问答上下文、答案和证据执行统一的文本脱敏。"""

    _PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
    _EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
    _PRICE = re.compile(
        r"(?:报价|价格|单价|总价|含税价|不含税价)\s*[:：]?\s*"
        r"(?:人民币|RMB|¥|￥)?\s*[\d,]+(?:\.\d+)?\s*(?:元|万元|万|块)?"
        r"|(?:人民币|RMB|¥|￥)\s*[\d,]+(?:\.\d+)?\s*(?:元|万元|万|块)?",
        re.IGNORECASE,
    )
    _CAPACITY = re.compile(
        r"(?:产能|年产|月产|日产|生产能力)\s*[:：]?\s*"
        r"[\d,]+(?:\.\d+)?\s*(?:万|千|百)?\s*[\w一-龥/%³²·~至-]+"
    )
    _TERM = re.compile(
        r"[^。！？\n]*(?:内部合作条款|内部合作|独家合作|保密条款|合同条款|返点|佣金)[^。！？\n]*"
    )
    _COMPANY = re.compile(r"[一-龥A-Za-z0-9（）()·-]{2,50}(?:股份有限公司|有限责任公司|有限公司|集团公司|集团|公司)")
    _DEMAND_SENSITIVE_LABELS = (
        r"link_name|link_tel|company_short_name|short_name|address|"
        r"company_address|registered_address|reg_location|business_no|credit_code|"
        r"social_credit_code|unified_social_credit_code|email|phone|mobile|wechat|"
        r"联系人|联系电话|公司简称|地址|公司地址|注册地址|"
        r"统一社会信用代码|营业执照号|邮箱|微信号|简称"
    )
    _DEMAND_SENSITIVE_VALUE = re.compile(
        rf"(?i)(?P<label>{_DEMAND_SENSITIVE_LABELS})\s*[:：]\s*[^；;。,，\n]+"
    )
    _DEMAND_CREDIT_CODE = re.compile(r"(?i)(?<![a-z0-9])[0-9a-z]{18}(?![a-z0-9])")
    _DEMAND_SENSITIVE_KEYS = {
        "link_name", "link_tel", "company_short_name", "short_name",
        "address", "company_address", "registered_address", "reg_location", "business_no",
        "credit_code", "social_credit_code", "unified_social_credit_code", "email", "phone",
        "mobile", "wechat", "联系人", "联系电话", "公司简称",
        "地址", "公司地址", "注册地址", "统一社会信用代码", "营业执照号", "邮箱", "微信号", "简称",
    }
    _DEMAND_PRESERVED_KEYS = {
        "first_img_url", "需求封面", "attachments", "attachment", "images", "image", "image_url",
        "file_url", "original_file_url", "attachment_url", "url",
    }

    def redact(self, text: str) -> RedactionResult:
        value = str(text or "")
        matches: list[tuple[int, int, str, int]] = []
        self._collect(matches, self._PHONE, value, REDACTED, 100)
        self._collect(matches, self._EMAIL, value, REDACTED, 100)
        self._collect(matches, self._PRICE, value, REDACTED, 90)
        self._collect(matches, self._CAPACITY, value, REDACTED, 90)
        self._collect(matches, self._TERM, value, REDACTED, 80)
        for match in self._COMPANY.finditer(value):
            matches.append((match.start(), match.end(), self._mask_company(match.group()), 70))

        selected: list[tuple[int, int, str]] = []
        for start, end, replacement, _priority in sorted(
            matches, key=lambda item: (item[0], -(item[1] - item[0]), -item[3])
        ):
            if any(start < existing_end and end > existing_start for existing_start, existing_end, _ in selected):
                continue
            selected.append((start, end, replacement))
        for start, end, replacement in sorted(selected, reverse=True):
            value = value[:start] + replacement + value[end:]
        return RedactionResult(text=value, replacements=len(selected))

    @staticmethod
    def _collect(
        matches: list[tuple[int, int, str, int]],
        pattern: re.Pattern[str],
        text: str,
        replacement: str,
        priority: int,
    ) -> None:
        matches.extend((m.start(), m.end(), replacement, priority) for m in pattern.finditer(text))

    @staticmethod
    def _mask_company(name: str) -> str:
        length = len(name)
        if length <= 4:
            keep = 1
        elif length <= 8:
            keep = 2
        else:
            keep = 3
        return name[:keep] + ("*" * (length - keep * 2)) + name[-keep:]

    def sanitize_evidences(self, evidences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """复制并脱敏证据，保留来源定位等非正文元数据。"""

        text_fields = {"text", "chunk_text", "parent_text", "content", "answer"}
        sanitized: list[dict[str, Any]] = []
        for evidence in evidences:
            item = dict(evidence)
            for field in text_fields:
                if field in item and isinstance(item[field], str):
                    item[field] = self.redact(item[field]).text
            sanitized.append(item)
        return sanitized

    def sanitize_demand_text(self, text: str) -> str:
        """外部展示需求文本时保留发布部门和企业名称，屏蔽联系人及其他隐私信息。"""

        value = str(text or "")
        value = self._DEMAND_SENSITIVE_VALUE.sub("", value)
        value = re.sub(r"([；;、,，])[ \t]*(?:[；;、,，][ \t]*)+", r"\1", value)
        value = re.sub(r"[ \t]*\n(?:[ \t]*\n)+", "\n", value)
        value = value.strip(" \t\n；;、,，")
        value = self._DEMAND_CREDIT_CODE.sub(REDACTED, value)
        value = self._EMAIL.sub(REDACTED, value)
        return self._PHONE.sub(REDACTED, value)

    def sanitize_demand_payload(self, value: Any) -> Any:
        """递归处理需求匹配卡片，避免嵌套 evidence/record 绕过脱敏。"""
        return self._sanitize_demand_payload(value, "")

    def _sanitize_demand_payload(self, value: Any, inherited_type: str) -> Any:
        if isinstance(value, list):
            return [self._sanitize_demand_payload(item, inherited_type) for item in value]
        if not isinstance(value, dict):
            return value
        knowledge_type = str(
            value.get("source_knowledge_type") or value.get("knowledge_type") or inherited_type
        ).strip().lower()
        if knowledge_type != "demand":
            return {
                key: self._sanitize_demand_payload(item, knowledge_type)
                for key, item in value.items()
            }
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in self._DEMAND_PRESERVED_KEYS:
                result[key] = deepcopy(item)
            elif key in {"pub_dept", "company"}:
                result[key] = deepcopy(item)
            elif key in self._DEMAND_SENSITIVE_KEYS:
                continue
            elif isinstance(item, str):
                result[key] = self.sanitize_demand_text(item)
            else:
                result[key] = self._sanitize_demand_payload(item, knowledge_type)
        return result

    def sanitize_demand_detail(self, detail: dict[str, Any]) -> dict[str, Any]:
        """脱敏外部用户查看需求详情时的草稿和原文块，保留定位与图片地址。"""

        result = deepcopy(detail)
        for field in ("text", "draft", "formatted_text", "cleaned_source_text"):
            if isinstance(result.get(field), str):
                result[field] = self.sanitize_demand_text(result[field])
        for field in ("draft_chunks", "source_chunks"):
            chunks = result.get(field)
            if not isinstance(chunks, list):
                continue
            for chunk in chunks:
                if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                    chunk["text"] = self.sanitize_demand_text(chunk["text"])
        return result

    def sanitize_retrieval(self, retrieval: dict[str, Any]) -> dict[str, Any]:
        result = dict(retrieval)
        result["evidences"] = self.sanitize_evidences(list(retrieval.get("evidences") or []))
        return result

    def sanitize_tool_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """用于 Supervisor 对前端暴露的工具结果，避免原始证据直接出网。"""

        sanitized = dict(result)
        if isinstance(sanitized.get("evidences"), list):
            sanitized["evidences"] = self.sanitize_evidences(sanitized["evidences"])
        if isinstance(sanitized.get("answer"), str):
            sanitized["answer"] = self.redact(sanitized["answer"]).text
        return sanitized


class PromptGuard:
    """LLM 调用前的上下文保护边界。"""

    def __init__(self, redactor: TextRedactionService | None = None) -> None:
        self.redactor = redactor or TextRedactionService()

    def prepare(
        self, query: str, evidences: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        return self.redactor.redact(query).text, self.redactor.sanitize_evidences(evidences)


class OutputRedactor:
    """LLM 和工具结果出站前的独立文本保护边界。"""

    def __init__(self, redactor: TextRedactionService | None = None) -> None:
        self.redactor = redactor or TextRedactionService()

    def answer(self, answer: str) -> str:
        return self.redactor.redact(answer).text

    def tool_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return self.redactor.sanitize_tool_result(result)
