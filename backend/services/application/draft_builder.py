"""当前阶段的草稿预览生成器；只生成候选，不持久化或发布。"""

from __future__ import annotations

import re
from typing import Any

from core.context_window import POLICY
from core.contracts.draft_contracts import limit_draft_payload, limit_draft_text
from core.contracts.draft_records import (
    MaterializedDraftRecord,
    materialize_draft_record,
)
from core.contracts.mysql_schema import business_fields, is_identity_only
from services.storage.minio.image_service import normalize_image_url

__all__ = ["MaterializedDraftRecord", "materialize_draft_record"]

from core.contracts.flows import (
    DEMAND_TYPE_OPTIONS,
    FLOW_META,
    get_flow_definition,
    required_field_pairs,
)

_LABELS = {key: (meta[0], meta[1]) for key, meta in FLOW_META.items()}

_PARSED_FIELD_ALIASES = {
    "enterprise_name": "pub_dept",
    "company": "pub_dept",
    "provider_name": "pub_dept",
    "delivery_cycle": "deliver_cycle",
    "technical_summary": "contect",
    "analysis": "analyse_contect",
}

_CONFIRMATION_ONLY = {
    "确认整理为需求",
    "确认整理为技术方案",
    "确认整理为科研成果",
    "确认整理为技术方案 / 科研成果",
    "确认按当前方向处理",
    "整理生成内部询盘草稿",
    "生成内部询盘草稿",
    "结束拜访并生成草稿",
    "完成拜访记录",
}


def deduplicate_context_items(
    items: list[Any] | None,
    keys: tuple[str, ...],
) -> list[Any]:
    """按稳定业务标识去重，保留第一次出现的原始对象。"""
    result: list[Any] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            identity = next(
                (f"{key}:{item.get(key)}" for key in keys if str(item.get(key) or "").strip()),
                "",
            )
            if not identity:
                identity = repr(sorted((str(key), repr(value)) for key, value in item.items()))
        else:
            identity = repr(item)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


DEMAND_REQUIRED_FIELDS = tuple((key, label) for key, label in required_field_pairs("demand"))

_UPLOAD_NOTICE_RE = re.compile(
    r"(?:我上传了(?:一份)?资料|我上传了(?:一个)?文件|已上传(?:一份)?资料|已上传(?:一个)?文件)"
    r"\s*[：:]\s*[^\n。！？]*[。！？]?"
)

_CONVERSATION_LINE_RE = re.compile(
    r"^\s*(?:user|assistant|human|system|用户|助手)\s*[:：]", re.IGNORECASE
)
_TITLE_CONTROL_MESSAGES = {
    "供需匹配",
    "开始供需匹配",
    "立即匹配",
    "匹配",
    "我有需求",
    "我有技术方案或科研成果",
    "我有技术方案",
    "我有科研成果",
    "立即进入需求草稿完善流程",
}


def _title_source(value: str) -> str:
    """标题只取业务事实，保留完整 combined 给摘要和字段分析。"""

    lines = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        role = re.match(
            r"^(user|assistant|human|system|用户|助手)\s*[:：]\s*(.*)$", line, re.IGNORECASE
        )
        if role:
            if role.group(1).lower() in {"assistant", "system", "助手"}:
                continue
            line = role.group(2).strip()
        if (
            line
            and line not in _CONFIRMATION_ONLY
            and line not in _TITLE_CONTROL_MESSAGES
            and not re.match(r"^(?:需求类型|技术类别|技术领域|资料类型)\s*[:：]", line)
        ):
            lines.append(line)
    return "\n".join(lines)


def _remove_upload_notice(value: str) -> str:
    """移除界面生成的上传提示，避免污染业务字段。"""

    cleaned = _UPLOAD_NOTICE_RE.sub("", str(value or ""))
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _first_image_url(context: dict[str, Any]) -> str:
    images = [item for item in context.get("image_candidates") or [] if isinstance(item, dict)]
    for image in context.get("image_candidates") or []:
        if not isinstance(image, dict):
            continue
        value = image.get("url") or image.get("image_url")
        if value:
            try:
                return normalize_image_url(
                    str(value).strip(),
                    document_id=str(context.get("document_id") or image.get("document_id") or ""),
                    images=images,
                )
            except ValueError:
                continue
    return ""


def build_draft(intent: str, message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """从用户描述和解析文本生成最小结构化草稿。"""

    draft_type = intent if intent in _LABELS else "demand"
    if is_identity_only(draft_type):
        raise ValueError("用户身份表不支持生成知识库草稿")
    definition = get_flow_definition(draft_type)
    _title, label = definition.label, definition.summary_label
    context = context or {}
    parsed_draft = (
        context.get("parsed_draft") if isinstance(context.get("parsed_draft"), dict) else {}
    )
    parsed_fields = (
        parsed_draft.get("fields") if isinstance(parsed_draft.get("fields"), dict) else {}
    )
    source_text = str(context.get("text") or "").strip()
    user_text = str(message or "").strip()
    # 上传资料和用户补充说明都是草稿事实来源，不能因为存在文件就丢掉对话内容。
    # 后续发布时仍由用户确认，平台不会把这段候选内容直接写入 Milvus。
    parts = [part for part in (source_text, user_text) if part and part not in _CONFIRMATION_ONLY]
    combined = "\n\n".join(parts)
    title_combined = _title_source(combined)
    parsed_title = _title_source(str(parsed_draft.get("title") or "")).strip()
    # 名称由明确的标题字段或 LLM 提取结果提供；不能把章节首行当业务名称。
    title = parsed_title[:80].rstrip() if parsed_title else ""
    missing = []
    if draft_type in _LABELS:
        fields = {
            field: str(
                parsed_fields.get(field)
                or next(
                    (
                        parsed_fields.get(alias)
                        for alias, target in _PARSED_FIELD_ALIASES.items()
                        if target == field and parsed_fields.get(alias)
                    ),
                    "",
                )
                or ""
            )
            for field in definition.fields
        }
    mysql_fields = {
        field: str(parsed_fields.get(field) or "")
        for field in business_fields(draft_type)
        if parsed_fields.get(field) not in (None, "")
    }
    if draft_type in {"demand", "solution"}:
        first_image_url = _first_image_url(context)
        if first_image_url:
            fields["first_img_url"] = first_image_url
            mysql_fields["first_img_url"] = first_image_url

    if draft_type == "demand":
        demand_type = str(
            context.get("tech_type")
            or context.get("demand_type")
            or _extract_labeled_value(combined, ("需求类型", "技术类别", "技术领域"))
            or _extract_demand_type(combined)
        ).strip()
        summary_key = "requirement_summary"
    elif draft_type == "enterprise":
        missing = ["enterprise_name", "description"]
        summary_key = "enterprise_summary"
    elif draft_type == "expert":
        missing = ["user_name", "research_field"]
        summary_key = "expert_summary"
    elif draft_type == "solution":
        missing = _solution_missing_fields(combined)
        summary_key = "technical_summary"
    elif draft_type == "customer_visit":
        fields["enterprise_name"] = str(
            context.get("enterprise_name")
            or _extract_labeled_value(combined, ("拜访企业", "企业名称", "公司名称"))
            or parsed_fields.get("enterprise_name")
            or ""
        ).strip()
        fields["enterprise_portrait"] = str(
            context.get("enterprise_portrait") or parsed_fields.get("enterprise_portrait") or ""
        ).strip()
        fields["potential_demand_list_text"] = str(
            context.get("potential_demand_list_text") or _extract_inquiry_demand(combined)
        ).strip()
        fields["potential_demand_list_text"] = _number_potential_demands(
            fields["potential_demand_list_text"]
        )
        for key, labels in {
            "budget_range": ("预算区间", "预算"),
            "expect_cycle": ("预计周期", "项目周期"),
            "adapt_solution": ("适配方案",),
            "supplier_match_dim": ("供应商匹配维度",),
        }.items():
            fields[key] = str(
                context.get(key) or _extract_labeled_value(combined, labels) or ""
            ).strip()
        fields["inner_mark_note"] = str(context.get("inner_mark_note") or "").strip()
        missing = []
        summary_key = "inquiry_summary"
    else:
        missing = _achievement_missing_fields(combined)
        fields.update(
            {
                "tech_maturity": _extract_labeled_value(combined, ("技术成熟度", "成熟度")),
                "cooperation_mode": _extract_labeled_value(combined, ("合作模式", "合作方式")),
            }
        )
        summary_key = "achievement_summary"
    for key, value in fields.items():
        if key in business_fields(draft_type) and value not in (None, ""):
            mysql_fields[key] = value
    if draft_type == "demand":
        # 会话中明确说出的字段优先；没有明确说出时才使用用户表回填值。
        company = (
            _extract_labeled_value(combined, ("企业名称", "公司名称", "所属企业", "单位名称"))
            or parsed_fields.get("company", "")
            or parsed_fields.get("enterprise_name", "")
            or str(context.get("company") or "").strip()
        )
        contact_name = (
            _extract_labeled_value(combined, ("联系人", "联系人姓名"))
            or parsed_fields.get("link_name", "")
            or str(context.get("contact_name") or "").strip()
        )
        contact_phone = (
            _extract_phone(combined)
            or parsed_fields.get("link_tel", "")
            or str(context.get("contact_phone") or context.get("phone") or "").strip()
        )
        title = _extract_labeled_value(title_combined, ("标题", "需求标题")) or parsed_title
        description = _remove_upload_notice(
            context.get("demand_description")
            or _extract_labeled_value(combined, ("需求描述", "当前需求"))
            or parsed_fields.get("contect")
        )
        analysis = str(
            context.get("demand_analysis")
            or context.get("analyse_contect")
            or parsed_fields.get("analyse_contect")
            or ""
        ).strip()

        # 草稿字段使用 MySQL 业务字段名；enterprise_name 是需求草稿的关联主体字段，
        # 后续同步 MySQL 时由业务适配层映射，不参与向量权限字段契约。
        fields["title"] = title
        fields["company"] = company
        fields["category"] = (
            demand_type or parsed_fields.get("category", "") or parsed_fields.get("tech_type", "")
        )
        fields["contect"] = description
        fields["analyse_contect"] = analysis
        fields["link_name"] = contact_name
        fields["link_tel"] = contact_phone
        budget_range = _extract_labeled_value(combined, ("预算区间", "预算"))
        if not budget_range:
            budget_match = re.search(
                r"预算\s*[:：]?\s*([\d,.]+\s*(?:万|万元|元|千)?)", combined, re.IGNORECASE
            )
            budget_range = (
                budget_match.group(1).strip()
                if budget_match
                else parsed_fields.get("budget_range", "")
            )
        fields["budget_range"] = budget_range
        fields["demand_cycle"] = _extract_labeled_value(
            combined, ("需求周期", "周期")
        ) or parsed_fields.get("demand_cycle", "")
        missing = _demand_required_missing(title, fields)
    elif draft_type == "solution":
        fields["title"] = (
            _extract_labeled_value(title_combined, ("方案名称", "标题"))
            or _title_source(fields.get("title"))
            or title
        )
        fields["pub_dept"] = (
            _extract_labeled_value(combined, ("企业名称", "发布企业名称", "发布单位"))
            or fields.get("pub_dept")
            or str(context.get("company") or "").strip()
        )
        fields["product_type"] = (
            _extract_labeled_value(combined, ("产品类型", "产品类别"))
            or fields.get("product_type")
            or ""
        )
        fields["contect"] = (
            _extract_labeled_value(combined, ("方案描述", "方案内容"))
            or fields.get("contect")
            or ""
        )
        fields["analyse_contect"] = fields.get("analyse_contect") or ""
        fields["link_name"] = (
            _extract_labeled_value(combined, ("联系人", "联系人姓名"))
            or str(context.get("contact_name") or "").strip()
        )
        fields["link_tel"] = (
            _extract_phone(combined)
            or str(context.get("contact_phone") or context.get("phone") or "").strip()
        )
        fields["deliver_cycle"] = (
            _extract_labeled_value(combined, ("交付周期", "周期"))
            or fields.get("deliver_cycle")
            or ""
        )
        fields["quote_range"] = (
            _extract_labeled_value(combined, ("报价区间", "报价", "价格"))
            or fields.get("quote_range")
            or ""
        )
        missing = [
            label
            for key, label in (
                ("title", "方案名称"),
                ("contect", "方案描述"),
            )
            if not str(fields.get(key) or "").strip()
        ]
    elif draft_type == "achievement":
        fields["title"] = (
            _extract_labeled_value(title_combined, ("成果名称", "标题"))
            or _title_source(fields.get("title"))
            or title
        )
        fields["category"] = (
            _extract_labeled_value(combined, ("成果类型", "技术类别"))
            or fields.get("category")
            or fields.get("tech_type")
            or ""
        )
        fields["analyse_contect"] = fields.get("analyse_contect") or ""
        fields["company"] = (
            _extract_labeled_value(combined, ("发布单位", "企业名称"))
            or fields.get("company")
            or fields.get("pub_dept")
            or str(context.get("company") or "").strip()
        )
        fields["link_name"] = (
            _extract_labeled_value(combined, ("联系人", "联系人姓名"))
            or str(context.get("contact_name") or "").strip()
        )
        fields["link_tel"] = (
            _extract_phone(combined)
            or str(context.get("contact_phone") or context.get("phone") or "").strip()
        )
        missing = [
            label
            for key, label in (
                ("title", "成果名称"),
                ("company", "发布单位"),
                ("analyse_contect", "成果描述"),
            )
            if not str(fields.get(key) or "").strip()
        ]
    if draft_type == "customer_visit":
        enterprise_name = str(fields.get("enterprise_name") or "").strip()
        if enterprise_name:
            mysql_fields["enterprise_name"] = enterprise_name
    if draft_type == "enterprise":
        title = str(fields.get("enterprise_name") or fields.get("company") or title).strip()
    elif draft_type == "expert":
        title = str(fields.get("user_name") or title).strip()
    elif fields.get("title"):
        title = str(fields["title"]).strip()
    attachments = []
    for item in context.get("attachments") or []:
        if isinstance(item, dict) and (item.get("document_id") or item.get("file_name")):
            attachments.append(
                {
                    key: str(item[key])
                    for key in ("document_id", "version_id", "file_name", "storage_key", "file_url")
                    if item.get(key)
                }
            )
    if context.get("document_id"):
        attachment = {
            "document_id": str(context["document_id"]),
            "file_name": str(context.get("file_name") or "已上传资料"),
        }
        if context.get("version_id"):
            attachment["version_id"] = str(context["version_id"])
        if context.get("storage_key"):
            attachment["storage_key"] = str(context["storage_key"])
        if context.get("file_url"):
            attachment["file_url"] = str(context["file_url"])
        attachments.append(attachment)
    attachments = deduplicate_context_items(
        attachments, ("document_id", "attachment_id", "storage_key", "file_name")
    )
    source_blocks = [
        {
            **{key: value for key, value in block.items() if key not in {"image_urls", "image_url"}},
            **({"document_id": context["document_id"]} if context.get("document_id") and not block.get("document_id") else {}),
            **({"version_id": context["version_id"]} if context.get("version_id") and not block.get("version_id") else {}),
        }
        if isinstance(block, dict)
        else block
        for block in (context.get("source_blocks") or parsed_draft.get("source_blocks") or [])
    ]
    payload = limit_draft_payload(
        {
            "draft_type": draft_type,
            "flow_definition": definition.to_dict(),
            "title": title or f"待完善{label}",
            "fields": fields,
            "mysql_fields": mysql_fields,
            # 拜访原文只用于字段提取，不作为拜访草稿摘要写入内部库。
            summary_key: ""
            if draft_type == "customer_visit"
            else combined[: POLICY.draft_text_chars],
            "source_blocks": source_blocks,
            "attachments": attachments,
            "missing_fields": missing,
            "optional_questions": [
                "如果方便，请继续用自然语言补充需求的背景、目标、使用场景、预算或周期。"
            ]
            if missing
            else [],
            "editable": True,
            "status": "DRAFT_GENERATED",
        }
    )
    # 保留草稿字段结构；上下文压缩器会去掉空值，但草稿编辑页需要完整字段键。
    payload["fields"] = {key: limit_draft_text(value) for key, value in fields.items()}
    payload["mysql_fields"] = {key: limit_draft_text(value) for key, value in mysql_fields.items()}
    # 持久化原文不能使用发给模型的上下文截断结果。
    payload["source_blocks"] = source_blocks
    return payload


def _has_budget(text: str) -> bool:
    return bool(
        re.search(r"预算|金额|价格|报价|\d+(?:\.\d+)?\s*(?:万|万元|元|千)", text, re.IGNORECASE)
    )


def _has_delivery_time(text: str) -> bool:
    return bool(
        re.search(
            r"交付|周期|工期|期限|\d+\s*(?:天|日|周|个月|月|年)|\d{4}[-/.]\d{1,2}",
            text,
            re.IGNORECASE,
        )
    )


def _has_application(text: str) -> bool:
    return bool(re.search(r"应用|场景|用于|用在|适用", text, re.IGNORECASE))


def _extract_demand_type(text: str) -> str:
    for option in DEMAND_TYPE_OPTIONS:
        if option in text:
            return option
    match = re.search(r"(?:需求类型|其他)\s*[:：]\s*([^\n，,。；;]+)", text)
    if match:
        return match.group(1).strip()
    return ""


def _extract_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    if not text.strip():
        return ""
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:：]\s*([^\n，,。；;]+)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    natural_patterns = {
        "企业名称": r"(?:我来自|所属公司是|公司是|企业是)\s*([^\n，,。；;]+)",
        "联系人": r"(?:我叫|联系人是|联系人为)\s*([\u4e00-\u9fa5A-Za-z·]{2,20})",
    }
    for label in labels:
        pattern = natural_patterns.get(label)
        if pattern:
            natural_match = re.search(pattern, text, re.IGNORECASE)
            if natural_match:
                return natural_match.group(1).strip()
    return ""


def _extract_phone(text: str) -> str:
    match = re.search(r"(?<!\d)(1[3-9]\d{9})(?!\d)", text)
    return match.group(1) if match else ""


def _extract_inquiry_demand(text: str) -> str:
    labeled = _extract_labeled_value(text, ("潜在需求", "客户需求", "采购需求"))
    if labeled:
        return labeled
    # 兜底不能把整份企业资料当成潜在需求；没有明确需求字段时交给 LLM 判断。
    return ""


def _number_potential_demands(value: str) -> str:
    """统一拜访草稿中的潜在需求编号，便于后续拆分匹配。"""

    text = str(value or "").strip()
    if not text:
        return ""
    parts = [
        part.strip(" ，,。；;\n")
        for part in re.split(
            r"(?:^|\n)\s*(?:\d+[.、)]|[-*•])\s*|\s+(?=\d+[.、)])|[；;]",
            text,
        )
        if part.strip(" ，,。；;\n")
    ]
    return "\n".join(f"{index}. {part}" for index, part in enumerate(parts, start=1))


def _demand_required_missing(title: str, fields: dict[str, Any]) -> list[str]:
    return _required_missing("demand", title, fields)


def _required_missing(draft_type: str, title: str, fields: dict[str, Any]) -> list[str]:
    """按流程契约重新计算必填项，避免 LLM 返回旧的 missing_fields。"""

    values = {"title": title, **fields}
    return [
        label
        for key, label in required_field_pairs(draft_type)
        if not str(values.get(key) or "").strip()
    ]


def _refresh_required_fields(result: dict[str, Any]) -> None:
    draft_type = str(result.get("draft_type") or "")
    if draft_type not in {"demand", "solution", "achievement"}:
        return
    fields = result.setdefault("fields", {})
    result["missing_fields"] = _required_missing(draft_type, str(result.get("title") or ""), fields)
    result["optional_questions"] = (
        ["如果方便，请继续用自然语言补充内容背景、目标、使用场景、预算或周期。"]
        if result["missing_fields"]
        else []
    )


def _solution_missing_fields(text: str) -> list[str]:
    return [
        label
        for present, label in (
            (len(text.strip()) >= 12, "方案描述"),
            (_has_application(text), "方案分析"),
        )
        if not present
    ][:2]


def _achievement_missing_fields(text: str) -> list[str]:
    missing = []
    if len(text.strip()) < 12:
        missing.append("成果内容")
    if not re.search(r"成熟|阶段|TRL|技术水平|样机|量产|专利", text, re.IGNORECASE):
        missing.append("技术成熟度")
    return missing[:2]
