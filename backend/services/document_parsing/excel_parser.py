"""Excel 业务数据解析器。

Excel 在当前知识库中主要承载业务人员清洗后的结构化记录，因此不再只依赖
MinerU 的视觉解析结果，而是保留工作表、表头、数据行和字段名，供 Retrieval
稳定转换为可检索文本。
"""

from __future__ import annotations

import asyncio
import difflib
import html
import json
import math
import os
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, ClassVar

from services.document_parsing.file_ingestion import ParsedDocument, TableFragment
from core.prompts.document_parsing import EXCEL_REVIEW_SYSTEM, EXCEL_REVIEW_USER_TEMPLATE

REVIEW_CELL_MAX_CHARS = 300
REVIEW_SAMPLE_MAX_CHARS = 12000
_URL_PATTERN = re.compile(r"(?:https?://|www\.|oss://)\S+", re.IGNORECASE)


class ExcelBusinessParser:
    """按工作表和字段解析 `.xlsx` 文件。"""

    supported_extensions: ClassVar[set[str]] = {"xlsx"}

    def __init__(self, reviewer: Any | None = None) -> None:
        self.reviewer = reviewer

    async def parse(self, path: Path) -> ParsedDocument:
        parsed = await asyncio.to_thread(_parse_workbook, path)
        parsed.metadata["agent_review"] = await _review_excel_structure(
            parsed, reviewer=self.reviewer
        )
        return parsed


async def _review_excel_structure(parsed: ParsedDocument, reviewer: Any | None = None) -> dict[str, Any]:
    """让 LLM 给出结构建议；失败时只降级，不阻断文件解析。"""

    enabled = os.getenv("EXCEL_AGENTIC_REVIEW_ENABLED", "true").lower()
    if enabled in {"0", "false", "no", "off"}:
        return {"status": "DISABLED", "knowledge_type": None, "confidence": 0.0}
    reviewer = reviewer or _build_excel_reviewer()
    if reviewer is None:
        return {"status": "SKIPPED", "reason": "LLM 未配置", "knowledge_type": None, "confidence": 0.0}
    sample = []
    for table in parsed.tables[:5]:
        sample.append({
            "sheet_name": table.sheet_name,
            "rows": [
                [_sanitize_review_cell(cell) for cell in row]
                for row in table.rows[:12]
            ],
            "metadata": {"headerRow": table.metadata.get("headerRow")},
        })
    sample_text = json.dumps(sample, ensure_ascii=False)
    if len(sample_text) > REVIEW_SAMPLE_MAX_CHARS:
        sample_text = sample_text[:REVIEW_SAMPLE_MAX_CHARS] + "\n[Excel 样本已截断]"
    prompt = [
        {"role": "system", "content": EXCEL_REVIEW_SYSTEM},
        {"role": "user", "content": EXCEL_REVIEW_USER_TEMPLATE.format(sample=sample_text)},
    ]
    try:
        raw = await reviewer.complete(prompt)
        result = _parse_review_json(raw)
        result["status"] = "COMPLETED"
        return result
    except Exception as exc:  # noqa: BLE001 - Agentic 辅助失败必须可降级
        return {
            "status": "FALLBACK",
            "reason": type(exc).__name__,
            "knowledge_type": None,
            "confidence": 0.0,
        }


def _build_excel_reviewer() -> Any | None:
    try:
        from runtime.config import get_settings
        from services.llm import get_llm_client
        settings = get_settings()
        if not settings.llm_provider or not settings.llm_model:
            return None
        return get_llm_client(settings)
    except Exception:  # noqa: BLE001 - 可选 LLM 不应阻断 Excel 解析
        return None


def _sanitize_review_cell(value: Any) -> str:
    """限制审阅样本，只把字段名和业务文本发送给 LLM。"""

    text = html.unescape(str(value or "")).strip()
    text = _URL_PATTERN.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ;；,，")
    return text[:REVIEW_CELL_MAX_CHARS]


def _parse_review_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    payload = json.loads(text)
    allowed_types = {"enterprise", "demand", "achievement", "expert", "solution"}
    allowed_units = {"row", "row_group", "section", "unknown"}
    knowledge_type = payload.get("knowledge_type")
    record_unit = payload.get("record_unit")
    try:
        confidence = max(0.0, min(float(payload.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    questions = payload.get("optional_questions")
    return {
        "knowledge_type": knowledge_type if knowledge_type in allowed_types else None,
        "record_unit": record_unit if record_unit in allowed_units else "unknown",
        "confidence": confidence,
        "optional_questions": [str(item) for item in questions[:3]] if isinstance(questions, list) else [],
        "notes": str(payload.get("notes") or "")[:1000],
    }


def _parse_workbook(path: Path) -> ParsedDocument:
    try:
        from openpyxl import load_workbook
    except ImportError as error:  # pragma: no cover - requirements 通常会保证该依赖已安装
        raise RuntimeError("Excel 业务解析需要 openpyxl，请安装 PerceptionAgent requirements.txt") from error

    workbook = load_workbook(path, read_only=True, data_only=True)
    tables: list[TableFragment] = []
    records: list[str] = []
    sheet_summaries: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        for sheet_index, worksheet in enumerate(workbook.worksheets, start=1):
            rows = _non_empty_rows(worksheet.iter_rows(values_only=True))
            if not rows:
                warnings.append(f"工作表“{worksheet.title}”为空，已跳过")
                continue

            header_index, headers = _find_header_row(rows)
            data_rows: list[list[str]] = []
            for row in rows[header_index + 1:]:
                normalized = _normalize_row(row, len(headers))
                if not any(normalized) or _is_header_row(normalized, headers):
                    continue
                data_rows.append(normalized)

            table_rows = [headers, *data_rows]
            tables.append(TableFragment(
                source_name=path.name,
                rows=table_rows,
                sheet_name=worksheet.title,
                metadata={
                    "parser": "excel_business",
                    "sheetIndex": str(sheet_index),
                    "headerRow": str(header_index + 1),
                    "dataRowCount": str(len(data_rows)),
                    "columnCount": str(len(headers)),
                    "recordUnit": "row_group",
                    "recordGroups": json.dumps(
                        _record_groups(data_rows, header_index + 2), ensure_ascii=False
                    ),
                },
            ))
            for group in _record_groups(data_rows, header_index + 2):
                grouped_rows = data_rows[group["start_index"]:group["end_index"] + 1]
                fields = _group_fields(headers, grouped_rows)
                if fields:
                    # 工作表和行号保存在 table/block 元数据中，不混入业务文本。
                    records.append("；".join(fields))
            sheet_summaries.append({
                "sheet_name": worksheet.title,
                "sheet_index": sheet_index,
                "header_row": header_index + 1,
                "data_row_count": len(data_rows),
                "column_count": len(headers),
            })
    finally:
        workbook.close()

    text = "\n\n".join(records)
    return ParsedDocument(
        source_name=path.name,
        text=text,
        raw_text=text,
        tables=tables,
        # 表格是 Excel 的唯一索引来源，避免再生成重复的页文本块。
        page_texts={},
        page_raw_texts={},
        metadata={
            "parser": "excel_business",
            "sourceType": "xlsx",
            "sheets": sheet_summaries,
            "warnings": warnings,
            "quality": {
                "sheetCount": len(sheet_summaries),
                "tableCount": len(tables),
                "recordCount": len(records),
            },
        },
    )


def _non_empty_rows(rows: Any) -> list[list[str]]:
    normalized_rows: list[list[str]] = []
    for row in rows:
        values = [_cell_to_text(value) for value in row]
        while values and not values[-1]:
            values.pop()
        if any(values):
            normalized_rows.append(values)
    return normalized_rows


def _find_header_row(rows: list[list[str]]) -> tuple[int, list[str]]:
    """在前 20 行中选择最像字段表头的一行，支持标题行在表头之前。"""

    candidates = rows[:20]
    for index, row in enumerate(candidates):
        values = [value for value in row if value]
        if len(values) >= 2 and len(set(values)) == len(values):
            return index, _make_headers(row)
    return 0, _make_headers(rows[0])


def _make_headers(row: list[str]) -> list[str]:
    headers: list[str] = []
    counts: dict[str, int] = {}
    for index, value in enumerate(row, start=1):
        base = value or f"列{index}"
        counts[base] = counts.get(base, 0) + 1
        headers.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return headers


def _normalize_row(row: list[str], width: int) -> list[str]:
    return [_cell_to_text(value) for value in (row[:width] + [""] * width)[:width]]


def _is_header_row(row: list[str], headers: list[str]) -> bool:
    return row == headers or row == [header.split("_")[0] for header in headers]


def _record_groups(rows: list[list[str]], first_data_row: int) -> list[dict[str, int]]:
    """识别候选业务记录组；首列合并导致的空白续行归入上一条记录。"""

    if not rows:
        return []
    # 只有确实存在首列空白续行时才合并，避免普通逐行清单被误合并。
    has_blank_first_cell = any(not row[0] for row in rows if row)
    groups: list[dict[str, int]] = []
    for index, row in enumerate(rows):
        starts_new = not groups or bool(row and row[0]) or not has_blank_first_cell
        if starts_new:
            groups.append({
                "start_index": index,
                "end_index": index,
                "start_row": first_data_row + index,
                "end_row": first_data_row + index,
            })
        else:
            groups[-1]["end_index"] = index
            groups[-1]["end_row"] = first_data_row + index
    return groups


def _group_fields(headers: list[str], rows: list[list[str]]) -> list[str]:
    fields: list[str] = []
    for column, header in enumerate(headers):
        values: list[str] = []
        for row in rows:
            value = row[column] if column < len(row) else ""
            if value and value not in values:
                values.append(value)
        if values:
            fields.append(f"{header}：{'；'.join(values)}")
    return deduplicate_excel_fields(merge_excel_profile_fields(fields))


def clean_excel_text(value: object) -> str:
    """清理 Excel 单元格中的富文本 HTML，但不删除正文内容。"""

    if value is None:
        return ""
    text = html.unescape(str(value))
    # 块级标签和换行标签先转成分隔符，避免两个段落被直接粘在一起。
    text = re.sub(r"<\s*(?:br|/p|/div|/li|/tr)\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return " ".join(text.split()).strip()


def merge_excel_profile_fields(fields: list[str]) -> list[str]:
    """合并“简介/介绍”字段，保留差异内容，避免同一段被向量化两次。"""

    profile_indexes: list[int] = []
    profile_values: list[tuple[str, str]] = []
    for index, field in enumerate(fields):
        header, separator, value = field.partition("：")
        if not separator:
            header, _, value = field.partition(":")
        base_header = header.split("_")[0].strip()
        if base_header in {"简介", "介绍"}:
            profile_indexes.append(index)
            profile_values.append((base_header, clean_excel_text(value)))

    if len(profile_values) < 2:
        return [
            f"{header}：{clean_excel_text(value)}"
            if header.split("_")[0].strip() in {"简介", "介绍"}
            else field
            for field in fields
            for header, _, value in [field.partition("：")]
        ]

    merged = profile_values[0][1]
    for _, value in profile_values[1:]:
        merged = _merge_profile_text(merged, value)
    canonical_header = "简介" if any(header == "简介" for header, _ in profile_values) else "介绍"
    replacement = f"{canonical_header}：{merged}" if merged else ""
    first_index = profile_indexes[0]
    output: list[str] = []
    for index, field in enumerate(fields):
        if index == first_index and replacement:
            output.append(replacement)
        if index not in profile_indexes:
            output.append(field)
    return output


def deduplicate_excel_fields(fields: list[str]) -> list[str]:
    """对任意 Excel 字段做保守去重，只删除完全重复或被完整包含的内容。"""

    kept: list[tuple[str, str]] = []
    for field in fields:
        header, separator, value = field.partition("：")
        if not separator:
            header, separator, value = field.partition(":")
        if not separator:
            kept.append((field, ""))
            continue
        value = clean_excel_text(value)
        if not value:
            continue
        value_key = _comparison_text(value)
        replaced = False
        duplicate = False
        for index, (existing_field, existing_value) in enumerate(kept):
            if not existing_value:
                continue
            existing_key = _comparison_text(existing_value)
            if value_key == existing_key or value_key in existing_key:
                duplicate = True
                break
            if existing_key in value_key:
                kept[index] = (field, value)
                replaced = True
                break
        if not duplicate and not replaced:
            kept.append((f"{header}：{value}", value))
    return [field for field, _ in kept]


def _merge_profile_text(left: str, right: str) -> str:
    left = clean_excel_text(left)
    right = clean_excel_text(right)
    if not left:
        return right
    if not right:
        return left
    left_key = _comparison_text(left)
    right_key = _comparison_text(right)
    if left_key == right_key or left_key in right_key:
        return right if len(right) >= len(left) else left
    if right_key in left_key:
        return left
    similarity = difflib.SequenceMatcher(None, left_key, right_key).ratio()
    # 这类差异通常只是介绍字段多了办公室、成果等补充信息，保留较长版本。
    if similarity >= 0.97:
        return right if len(right) >= len(left) else left

    result = left
    for segment in _profile_segments(right):
        segment = segment.strip()
        if not segment or _already_covered(segment, result):
            continue
        result = f"{result}；{segment}"
    return result


def _comparison_text(value: str) -> str:
    return "".join(char.lower() for char in clean_excel_text(value) if char.isalnum())


def _profile_segments(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？；;.!?])\s*|\n+", clean_excel_text(value)) if part.strip()]


def _already_covered(segment: str, existing: str) -> bool:
    segment_key = _comparison_text(segment)
    existing_key = _comparison_text(existing)
    if not segment_key or segment_key in existing_key:
        return True
    return any(
        difflib.SequenceMatcher(None, segment_key, _comparison_text(item)).ratio() >= 0.92
        for item in _profile_segments(existing)
    )


def _cell_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return clean_excel_text(value)
