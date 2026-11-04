"""Perception Agent 的 LLM 字段提取能力。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.context_window import clip_text, limit_draft_context
from core.contracts.draft_contracts import (
    DRAFT_FIELD_DESCRIPTIONS,
    DRAFT_FIELD_LABELS,
    DRAFT_EXCLUDED_FIELDS,
    draft_fields,
    limit_draft_payload,
    limit_draft_text,
)
from core.contracts.mysql_schema import MYSQL_FIELD_COMMENTS, business_fields
from core.prompts.admin_field_extraction import (
    ADMIN_FIELD_EXTRACTION_PROMPT_VERSION,
    build_admin_field_extraction_system,
)
from core.prompts.field_extraction import (
    SMART_QA_FIELD_EXTRACTION_PROMPT_VERSION,
    build_smart_qa_field_extraction_system,
)


FIELD_ALIASES = {
    "provider_name": "pub_dept",
    "product_category": "product_type",
    "technical_summary": "contect",
    "delivery_cycle": "deliver_cycle",
    "price_range": "quote_range",
    "product": "tech_type",
    "demand_type": "category",
    "enterprise_name": "company",
    "企业画像": "enterprise_portrait",
    "enterprise_profile": "enterprise_portrait",
    "潜在需求": "potential_demand_list_text",
    "potential_demand": "potential_demand_list_text",
    "potential_demands": "potential_demand_list_text",
    "预算": "budget_range",
    "预算区间": "budget_range",
    "预计周期": "expect_cycle",
    "项目周期": "expect_cycle",
    "供应商匹配维度": "supplier_match_dim",
    "supplier_matching_dimension": "supplier_match_dim",
}


def _field_aliases(draft_type: str) -> dict[str, str]:
    aliases = dict(FIELD_ALIASES)
    for source in (
        DRAFT_FIELD_LABELS.get(draft_type, {}),
        MYSQL_FIELD_COMMENTS.get(draft_type, {}),
    ):
        aliases.update(
            {str(label).strip(): field for field, label in source.items() if str(label).strip()}
        )
    return aliases


def _validate_admin_fields(payload: dict[str, Any], draft_type: str) -> None:
    aliases = _field_aliases(draft_type)
    allowed = set(business_fields(draft_type)) - DRAFT_EXCLUDED_FIELDS.get(draft_type, frozenset())
    allowed -= {"first_img_url", "image", "avatar", "logo"}
    for item in payload.get("items") or [payload]:
        if "title" in allowed and str(item.get("title") or "").strip():
            continue
        if not any(
            aliases.get(str(key), str(key)) in allowed
            and value is not None and str(value).strip()
            for key, value in (item.get("fields") or {}).items()
        ):
            raise ValueError("DRAFT_FIELDS_EMPTY")


_EXPLICIT_IMAGE_LABELS = ("专家头像", "头像", "照片", "图片")
_IMAGE_URL_RE = re.compile(r"https?://[^\s\]\)；;，,]+", re.IGNORECASE)


def _extract_explicit_image_field(source_text: str, draft_type: str) -> str:
    if draft_type != "expert":
        return ""
    for label in _EXPLICIT_IMAGE_LABELS:
        match = re.search(
            rf"{re.escape(label)}\s*[:：]\s*([^;；\n。！？]*)",
            str(source_text or ""),
            re.IGNORECASE,
        )
        if match:
            url = _IMAGE_URL_RE.search(match.group(1))
            if url:
                return url.group(0).replace(r"\&", "&")
    return ""


_UPLOAD_NOTICE_RE = re.compile(
    r"(?:我上传了(?:一份)?资料|我上传了(?:一个)?文件|已上传(?:一份)?资料|已上传(?:一个)?文件)"
    r"\s*[：:]\s*[^\n。！？]*[。！？]?"
)


def _remove_upload_notice(value: str) -> str:
    cleaned = _UPLOAD_NOTICE_RE.sub("", str(value or ""))
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


FIELD_EXTRACTION_SEGMENT_CHARS = 8000
FIELD_EXTRACTION_MAX_SEGMENTS = 5
FIELD_EXTRACTION_SINGLE_CALL_CHARS = 12000
FIELD_EXTRACTION_MAX_CONCURRENCY = 2
_FIELD_EXTRACTION_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def _split_source_text(value: str, blocks: list[dict[str, Any]] | None = None) -> list[str]:
    text = str(value or "").replace("\x00", " ").strip()
    units = [
        str(block.get("text") or "").replace("\x00", " ").strip()
        for block in blocks or []
        if isinstance(block, dict) and str(block.get("text") or "").strip()
    ]
    if not units:
        if not text:
            return [""]
        units = [
            part.strip()
            for part in re.split(r"(?=^#{1,6}\s|^\d+[.、]\s|\n{2,})", text, flags=re.MULTILINE)
            if part.strip()
        ]
    segments: list[str] = []
    current = ""
    for unit in units:
        if len(unit) <= FIELD_EXTRACTION_SEGMENT_CHARS:
            if current and len(current) + len(unit) + 2 > FIELD_EXTRACTION_SEGMENT_CHARS:
                segments.append(current)
                current = ""
            current = f"{current}\n\n{unit}".strip()
            continue
        if current:
            segments.append(current)
            current = ""
        start = 0
        while start < len(unit):
            end = min(start + FIELD_EXTRACTION_SEGMENT_CHARS, len(unit))
            if end < len(unit):
                boundary = max(
                    unit.rfind("\n", start, end),
                    unit.rfind("。", start, end),
                    unit.rfind("；", start, end),
                    unit.rfind(";", start, end),
                )
                if boundary > start + FIELD_EXTRACTION_SEGMENT_CHARS // 2:
                    end = boundary + 1
            segments.append(unit[start:end].strip())
            start = end
    if current:
        segments.append(current)
    if len(segments) > FIELD_EXTRACTION_MAX_SEGMENTS:
        raise ValueError("SOURCE_TOO_LARGE")
    return segments or [""]


def _prepare_source_segments(value: str, blocks: list[dict[str, Any]] | None = None) -> list[str]:
    """短原文保持完整，超出模型安全长度后才按内容块拆分。"""
    text = str(value or "").replace("\x00", " ").strip()
    if text and len(text) <= FIELD_EXTRACTION_SINGLE_CALL_CHARS:
        return [text]
    return _split_source_text(text, blocks)


def _extraction_cache_key(
    draft_type: str,
    user_message: str,
    source_text: str,
    mysql_fields: dict[str, Any],
    allow_multiple: bool,
    model: str = "",
    extraction_mode: str = "full",
    prompt_profile: str = "smart_qa",
    prompt_version: str = "",
) -> str:
    value = json.dumps(
        {
            "version": prompt_version,
            "draft_type": draft_type,
            "user_message": user_message,
            "source_text": source_text,
            "mysql_fields": mysql_fields,
            "allow_multiple": allow_multiple,
            "extraction_mode": extraction_mode,
            "prompt_profile": prompt_profile,
            "model": model or os.getenv("DRAFT_LLM_MODEL", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cached_payload(key: str) -> dict[str, Any] | None:
    if os.getenv("DRAFT_LLM_CACHE_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
        return None
    item = _FIELD_EXTRACTION_CACHE.get(key)
    if item is None:
        return None
    ttl = max(1, int(os.getenv("DRAFT_LLM_CACHE_TTL_SECONDS", "3600")))
    if time.monotonic() - item[0] > ttl:
        _FIELD_EXTRACTION_CACHE.pop(key, None)
        return None
    _FIELD_EXTRACTION_CACHE.move_to_end(key)
    return json.loads(json.dumps(item[1], ensure_ascii=False))


def _store_cached_payload(key: str, payload: dict[str, Any]) -> None:
    if os.getenv("DRAFT_LLM_CACHE_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
        return
    _FIELD_EXTRACTION_CACHE[key] = (
        time.monotonic(),
        json.loads(json.dumps(payload, ensure_ascii=False)),
    )
    _FIELD_EXTRACTION_CACHE.move_to_end(key)
    limit = max(1, int(os.getenv("DRAFT_LLM_CACHE_SIZE", "128")))
    while len(_FIELD_EXTRACTION_CACHE) > limit:
        _FIELD_EXTRACTION_CACHE.popitem(last=False)


def _merge_review_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "title": "",
        "fields": {},
        "summary": "",
        "missing_fields": [],
        "optional_questions": [],
        "confidence": 0.0,
    }
    summaries: list[str] = []
    item_by_title: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not merged["title"] and payload.get("title"):
            merged["title"] = payload["title"]
        for key, value in (payload.get("fields") or {}).items():
            if value is not None and str(value).strip():
                merged["fields"][key] = limit_draft_text(str(value).strip())
        if str(payload.get("summary") or "").strip() and payload["summary"] not in summaries:
            summaries.append(payload["summary"])
        for key in ("missing_fields", "optional_questions"):
            for value in payload.get(key) or []:
                if value not in merged[key]:
                    merged[key].append(value)
        merged["confidence"] = max(merged["confidence"], float(payload.get("confidence") or 0))
        for item in payload.get("items") or []:
            title = str(item.get("title") or "").strip()
            current = item_by_title.setdefault(
                title or f"item-{len(item_by_title)}",
                {
                    "title": title,
                    "fields": {},
                    "summary": "",
                    "missing_fields": [],
                    "optional_questions": [],
                    "confidence": 0.0,
                },
            )
            current["fields"].update(
                {
                    k: limit_draft_text(str(v).strip())
                    for k, v in (item.get("fields") or {}).items()
                    if v is not None and str(v).strip()
                }
            )
            for key in ("summary",):
                if str(item.get(key) or "").strip():
                    current[key] = item[key]
            for key in ("missing_fields", "optional_questions"):
                current[key].extend(v for v in item.get(key) or [] if v not in current[key])
            current["confidence"] = max(current["confidence"], float(item.get("confidence") or 0))
    merged["summary"] = limit_draft_text("\n\n".join(summaries))
    if item_by_title:
        merged["items"] = list(item_by_title.values())
    merged["missing_fields"] = merged["missing_fields"][:8]
    merged["optional_questions"] = merged["optional_questions"][:3]
    return merged


def _refresh_required_fields(
    result: dict[str, Any],
    required_field_pairs_factory: Callable[[str], tuple[tuple[str, str], ...]] | None,
) -> None:
    draft_type = str(result.get("draft_type") or "")
    if not required_field_pairs_factory or draft_type not in {"demand", "solution", "achievement"}:
        return
    fields = result.setdefault("fields", {})
    values = {"title": str(result.get("title") or ""), **fields}
    result["missing_fields"] = [
        label
        for key, label in required_field_pairs_factory(draft_type)
        if not str(values.get(key) or "").strip()
    ]
    result["optional_questions"] = (
        ["如果方便，请继续用自然语言补充需求的背景、目标、使用场景、预算或周期。"]
        if result["missing_fields"]
        else []
    )


def _order_field_mapping(values: dict[str, Any], draft_type: str) -> dict[str, Any]:
    """按 mysql_schema.py 的 MySQL 表单顺序返回字段，未知字段放在末尾。"""
    ordered = {key: values[key] for key in business_fields(draft_type) if key in values}
    ordered.update({key: value for key, value in values.items() if key not in ordered})
    return ordered


async def enrich_draft_with_llm(
    draft: dict[str, Any],
    *,
    user_message: str = "",
    source_text: str = "",
    reviewer: Any | None = None,
    allow_multiple: bool = False,
    draft_factory: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
    required_field_pairs_factory: Callable[[str], tuple[tuple[str, str], ...]] | None = None,
    reviewer_role: str = "draft",
    extraction_mode: str = "full",
    prompt_profile: str = "smart_qa",
) -> dict[str, Any]:
    """从对话或解析文本提取 MySQL 字段和字段草稿。"""

    extraction_mode = extraction_mode.strip().lower()
    if extraction_mode not in {"draft_only", "full"}:
        raise ValueError("extraction_mode 必须是 draft_only 或 full")
    prompt_profile = prompt_profile.strip().lower()
    if prompt_profile not in {"admin", "smart_qa"}:
        raise ValueError("prompt_profile 必须是 admin 或 smart_qa")
    result = limit_draft_payload(
        limit_draft_context(
            {
                **draft,
                "fields": dict(draft.get("fields") or {}),
                "mysql_fields": dict(draft.get("mysql_fields") or draft.get("fields") or {}),
            }
        )
    )
    draft_type = str(draft.get("draft_type") or "")
    excluded_fields = DRAFT_EXCLUDED_FIELDS.get(draft_type, frozenset())
    allowed_draft_fields = set(draft_fields(draft_type))
    if extraction_mode == "draft_only":
        result["fields"] = {
            key: value for key, value in result["fields"].items() if key in allowed_draft_fields
        }
        result["mysql_fields"] = {
            key: value
            for key, value in result["mysql_fields"].items()
            if key in allowed_draft_fields
        }
    for field in excluded_fields:
        result["fields"].pop(field, None)
        result["mysql_fields"].pop(field, None)
    explicit_image = _extract_explicit_image_field(source_text, draft_type)
    if explicit_image and "image" in allowed_draft_fields:
        result["mysql_fields"]["image"] = explicit_image
        result["fields"]["image"] = explicit_image
    if os.getenv("DRAFT_LLM_REVIEW_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
        if prompt_profile == "admin":
            raise ValueError("DRAFT_EXTRACTION_DISABLED")
        result["draft_review"] = {"status": "DISABLED"}
        _refresh_required_fields(result, required_field_pairs_factory)
        return result
    reviewer = reviewer or _build_draft_reviewer(reviewer_role)
    if reviewer is None:
        if prompt_profile == "admin":
            raise ValueError("DRAFT_LLM_NOT_CONFIGURED")
        result["draft_review"] = {"status": "SKIPPED", "reason": "LLM 未配置"}
        _refresh_required_fields(result, required_field_pairs_factory)
        return result

    draft_type = str(draft.get("draft_type") or "demand")
    summary_key = {
        "enterprise": "enterprise_summary",
        "demand": "requirement_summary",
        "solution": "technical_summary",
        "achievement": "achievement_summary",
        "expert": "expert_summary",
    }.get(draft_type, "summary")
    required_labels = (
        "、".join(label for _, label in required_field_pairs_factory(draft_type))
        if required_field_pairs_factory and draft_type in {"demand", "solution", "achievement"}
        else ""
    )
    fields = (
        draft_fields(draft_type) if extraction_mode == "draft_only" else business_fields(draft_type)
    )
    draft_field_order = list(draft_fields(draft_type))
    guide_fields = (
        draft_field_order + [field for field in fields if field not in draft_field_order]
        if extraction_mode == "full"
        else list(fields)
    )
    priority_hint = (
        "优先提取草稿字段；其它 MySQL 字段有明确依据时一并提取，没有依据时留空。"
        if extraction_mode == "full"
        else ""
    )
    field_guide = clip_text(
        priority_hint
        + "；".join(
            f"{DRAFT_FIELD_LABELS.get(draft_type, {}).get(field, field)}({field}："
            f"{DRAFT_FIELD_DESCRIPTIONS.get(draft_type, {}).get(field) or MYSQL_FIELD_COMMENTS.get(draft_type, {}).get(field, '')})"
            for field in guide_fields
        ),
        8000,
    )
    inquiry_guidance = ""
    try:
        # LLM 客户端自身有连接/请求超时；这里留出模型生成 JSON 的完整时间，
        # 避免外层 60 秒先把仍在生成的草稿误判为兜底。
        timeout_seconds = max(1, int(os.getenv("DRAFT_LLM_REVIEW_TIMEOUT_SECONDS", "300")))
        segments = _prepare_source_segments(source_text, result.get("source_blocks"))
        if prompt_profile == "admin" and not any(segment.strip() for segment in segments):
            raise ValueError("DRAFT_SOURCE_EMPTY")
        prompt_version = (
            ADMIN_FIELD_EXTRACTION_PROMPT_VERSION
            if prompt_profile == "admin"
            else SMART_QA_FIELD_EXTRACTION_PROMPT_VERSION
        )
        cache_key = _extraction_cache_key(
            draft_type,
            str(user_message or ""),
            json.dumps(segments, ensure_ascii=False),
            result["mysql_fields"],
            allow_multiple,
            getattr(getattr(reviewer, "settings", None), "llm_model", ""),
            extraction_mode,
            prompt_profile,
            prompt_version,
        )
        use_cache = prompt_profile != "admin"
        payload = _cached_payload(cache_key) if use_cache else None
        cache_miss = payload is None
        if payload is None:
            prompts = []
            mysql_field_snapshot = {
                key: clip_text(value, 500)
                for key, value in list(result["mysql_fields"].items())[:20]
            }
            mysql_fields_text = json.dumps(mysql_field_snapshot, ensure_ascii=False)
            if prompt_profile == "admin":
                system_prompt = build_admin_field_extraction_system(
                    draft_type=draft_type,
                    allow_multiple=allow_multiple,
                    field_guide=field_guide,
                    required_labels=required_labels,
                )
            else:
                system_prompt = build_smart_qa_field_extraction_system(
                    draft_type=draft_type,
                    allow_multiple=allow_multiple,
                    field_guide=field_guide,
                    required_labels=required_labels,
                    inquiry_guidance=inquiry_guidance,
                    draft_only=extraction_mode == "draft_only",
                )
            for index, segment in enumerate(segments, start=1):
                prompts.append(
                    [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"草稿类型：{draft_type}\nMySQL字段：{mysql_fields_text}\n"
                                f"用户补充：{clip_text(user_message, 4000)}\n"
                                f"这是第 {index}/{len(segments)} 段上传资料：\n{segment}\n"
                                f"摘要字段名：{summary_key}"
                            ),
                        },
                    ]
                )
            semaphore = asyncio.Semaphore(FIELD_EXTRACTION_MAX_CONCURRENCY)

            async def complete_segment(prompt: list[dict[str, str]]) -> dict[str, Any]:
                async with semaphore:
                    return _parse_draft_review(
                        await asyncio.wait_for(reviewer.complete(prompt), timeout=timeout_seconds)
                    )

            completed = await asyncio.gather(
                *(complete_segment(prompt) for prompt in prompts),
                return_exceptions=True,
            )
            errors = [item for item in completed if isinstance(item, Exception)]
            if errors:
                raise errors[0]
            payloads = [item for item in completed if isinstance(item, dict)]
            payload = _merge_review_payloads(payloads)
            has_content = any(
                str(payload.get(key) or "").strip() for key in ("title", "summary")
            ) or any(
                str(value or "").strip()
                for value in (payload.get("fields") or {}).values()
            ) or any(
                any(str(item.get(key) or "").strip() for key in ("title", "summary"))
                or any(str(value or "").strip() for value in (item.get("fields") or {}).values())
                for item in payload.get("items") or []
                if isinstance(item, dict)
            )
        payload = payload or {}
        if prompt_profile == "admin":
            # 摘要或自动封面不能掩盖没有可填写业务字段的空结果。
            try:
                _validate_admin_fields(payload, draft_type)
            except ValueError:
                _FIELD_EXTRACTION_CACHE.pop(cache_key, None)
                raise
        if use_cache and cache_miss and has_content:
            _store_cached_payload(cache_key, payload)
        if allow_multiple and payload.get("items"):
            if draft_factory is None:
                raise ValueError("多条草稿需要 draft_factory")
            return {
                "drafts": [
                    _apply_llm_payload(
                        draft_factory(
                            draft_type,
                            item.get("title") or source_text[:1200],
                            {
                                "text": item.get("summary") or source_text,
                                "parsed_draft": {
                                    "title": item.get("title"),
                                    "fields": item.get("fields") or {},
                                },
                                "source_blocks": result.get("source_blocks") or [],
                                "document_id": (result.get("attachments") or [{}])[0].get(
                                    "document_id"
                                ),
                                "file_name": (result.get("attachments") or [{}])[0].get(
                                    "file_name"
                                ),
                                "company": result.get("fields", {}).get("company")
                                or result.get("mysql_fields", {}).get("company"),
                                "contact_name": result.get("fields", {}).get("link_name")
                                or result.get("mysql_fields", {}).get("link_name"),
                                "contact_phone": result.get("fields", {}).get("link_tel")
                                or result.get("mysql_fields", {}).get("link_tel"),
                            },
                        ),
                        item,
                        required_field_pairs_factory,
                        extraction_mode=extraction_mode,
                    )
                    for item in payload["items"]
                ]
            }
        return _apply_llm_payload(
            result, payload, required_field_pairs_factory, extraction_mode=extraction_mode
        )
    except Exception as exc:  # noqa: BLE001 - 草稿辅助失败不能阻断人工确认
        if prompt_profile == "admin":
            raise
        result["draft_review"] = {"status": "FALLBACK", "reason": type(exc).__name__}
        if str(exc) == "SOURCE_TOO_LARGE":
            result["draft_review"]["message"] = "文件内容过大，请拆分为多个文件后重新上传。"
        _refresh_required_fields(result, required_field_pairs_factory)
        return result


def _apply_llm_payload(
    result: dict[str, Any],
    payload: dict[str, Any],
    required_field_pairs_factory: Callable[[str], tuple[tuple[str, str], ...]] | None,
    *,
    extraction_mode: str = "full",
) -> dict[str, Any]:
    draft_type = str(result.get("draft_type") or "demand")
    summary_key = {
        "demand": "requirement_summary",
        "solution": "technical_summary",
        "achievement": "achievement_summary",
    }.get(draft_type, "summary")
    field_aliases = _field_aliases(draft_type)
    if payload["title"].strip():
        result["title"] = payload["title"].strip()[:160]
    known_fields = (
        set(draft_fields(draft_type))
        if extraction_mode == "draft_only"
        else set(business_fields(draft_type))
    )
    if (
        "title" in known_fields and payload["title"].strip()
        and not str(result["mysql_fields"].get("title") or "").strip()
    ):
        result["mysql_fields"]["title"] = payload["title"].strip()
        result["fields"]["title"] = payload["title"].strip()
    accepted_alias_targets = known_fields | (
        {"category"} if draft_type in {"demand", "achievement"} else set()
    )
    inquiry_aliases = (
        {
            "公司名称": "enterprise_name",
            "企业名称": "enterprise_name",
            "企业画像": "enterprise_portrait",
            "潜在需求": "potential_demand_list_text",
            "预算": "budget_range",
            "预算区间": "budget_range",
            "预计周期": "expect_cycle",
            "项目周期": "expect_cycle",
            "供应商匹配维度": "supplier_match_dim",
        }
        if draft_type == "customer_visit"
        else {}
    )
    for original_key, value in payload["fields"].items():
        key = str(original_key)
        key = inquiry_aliases.get(key, key)
        key = key if key in known_fields else field_aliases.get(key, key)
        if (
            draft_type == "expert"
            and key == "image"
            and not str(result["mysql_fields"].get(key) or "").strip()
        ):
            continue
        if key in DRAFT_EXCLUDED_FIELDS.get(draft_type, frozenset()):
            continue
        # 增量补充只追加空字段；已有草稿字段由后端保留，避免模型重写用户已有内容。
        if str(result["mysql_fields"].get(key) or "").strip():
            continue
        if key in accepted_alias_targets and str(value).strip():
            normalized_value = (
                _remove_upload_notice(value) if key == "contect" else str(value).strip()
            )
            normalized_value = limit_draft_text(normalized_value)
            if not normalized_value:
                continue
            result["mysql_fields"][key] = normalized_value
            result["fields"][key] = normalized_value
    if result["mysql_fields"].get("title"):
        result["title"] = str(result["mysql_fields"]["title"]).strip()[:160]
    if payload["summary"].strip():
        result[summary_key] = limit_draft_text(payload["summary"].strip())
    if payload["missing_fields"]:
        required = set(result.get("missing_fields") or [])
        result["missing_fields"] = [item for item in payload["missing_fields"] if item in required][
            :3
        ]
    result["optional_questions"] = payload["optional_questions"][:3]
    result["draft_review"] = {"status": "COMPLETED", "confidence": payload["confidence"]}
    _refresh_required_fields(result, required_field_pairs_factory)
    if payload["optional_questions"]:
        result["optional_questions"] = payload["optional_questions"][:3]
    result = limit_draft_payload(result)
    result["mysql_fields"] = _order_field_mapping(result["mysql_fields"], draft_type)
    result["fields"] = _order_field_mapping(result["fields"], draft_type)
    return result


def _build_draft_reviewer(role: str = "draft") -> Any | None:
    try:
        from runtime.config import get_settings, settings_for_role
        from services.llm import get_llm_client

        settings = settings_for_role(
            get_settings(), role if role in {"draft", "vision"} else "draft"
        )
        if not all(
            (settings.llm_provider, settings.llm_model, settings.llm_base_url, settings.llm_api_key)
        ):
            return None
        return get_llm_client(settings)
    except Exception:  # noqa: BLE001 - LLM 未配置时允许确定性草稿继续工作
        return None


def _parse_draft_review(raw: Any) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()

    class DraftReviewPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(default="", max_length=160)
        fields: dict[str, Any] = Field(default_factory=dict)
        summary: str = Field(default="", max_length=12000)
        missing_fields: list[str] = Field(default_factory=list)
        optional_questions: list[str] = Field(default_factory=list, max_length=3)
        confidence: float = Field(default=0.0, ge=0.0, le=1.0)

        @field_validator("confidence", mode="before")
        @classmethod
        def normalize_confidence(cls, value: Any) -> Any:
            # 仅兼容模型明确的三级标签，数值不代表校准后的概率。
            levels = {"low": 0.25, "medium": 0.5, "high": 0.75}
            return levels.get(value.strip().lower(), value) if isinstance(value, str) else value

    from services.llm import parse_json_object

    value = parse_json_object(text)
    if value is None:
        raise ValueError("LLM 未返回有效草稿审核 JSON")
    payload = DraftReviewPayload.model_validate(
        {key: item for key, item in value.items() if key != "items"}
    )
    items = [
        DraftReviewPayload.model_validate(item).model_dump()
        for item in value.get("items") or []
        if isinstance(item, dict)
    ]
    return {**payload.model_dump(), "items": items}
