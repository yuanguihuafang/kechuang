"""PPTX 的结构父块与语义检索子块。

MinerU 负责识别幻灯片中的标题、正文、表格和图片；本模块只做确定性的
结构分组与文本切分，不调用 LLM/VLM。父块用于回查完整上下文，子块用于向量检索。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from typing import Any

from core.contracts import DocumentBlock


def group_slide_ranges(total_slides: int, target_group_size: int = 5) -> list[tuple[int, int]]:
    """生成父块页范围：短文档整体保留，长文档按约五页分组。"""

    if total_slides <= 0:
        return []
    if total_slides <= 10:
        return [(1, total_slides)]

    ranges = [
        (start, min(start + target_group_size - 1, total_slides))
        for start in range(1, total_slides + 1, target_group_size)
    ]
    if len(ranges) > 1:
        last_start, last_end = ranges[-1]
        if last_end - last_start + 1 < 3:
            previous_start, _ = ranges[-2]
            ranges[-2:] = [(previous_start, last_end)]
    return ranges


def build_presentation_blocks(
    *,
    source_name: str,
    page_texts: dict[int, str],
    tables: list[dict[str, Any]],
    semantic_units: list[dict[str, Any]],
    target_chars: int = 1000,
    hard_max_chars: int = 1300,
    overlap_chars: int = 50,
) -> list[DocumentBlock]:
    """把 PPTX 解析结构转换为 Retrieval 可消费的父子块。"""

    if target_chars <= 0 or hard_max_chars < target_chars:
        raise ValueError("PPTX 分块阈值不合法")

    slide_numbers = {
        int(page) for page in page_texts if isinstance(page, int) and page > 0
    }
    slide_numbers.update(
        page
        for unit in semantic_units
        if (page := _positive_int(unit.get("pageNumber"))) is not None
    )
    slide_numbers.update(
        page
        for table in tables
        if (page := _positive_int(table.get("page_number"))) is not None
    )
    if not slide_numbers:
        return []

    total_slides = max(slide_numbers)
    repeated_texts = _find_repeated_boilerplate(
        semantic_units=semantic_units,
        page_texts=page_texts,
        total_slides=total_slides,
    )
    pages = _build_slide_payloads(page_texts, semantic_units, repeated_texts, total_slides)
    tables_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for table in tables:
        page = _positive_int(table.get("page_number")) or 1
        tables_by_page[page].append(table)

    blocks: list[DocumentBlock] = []
    for parent_start, parent_end in group_slide_ranges(total_slides):
        parent_id = _presentation_parent_id(source_name, parent_start, parent_end)
        parent_text = _build_parent_text(
            pages=pages,
            tables_by_page=tables_by_page,
            start=parent_start,
            end=parent_end,
        )
        child_specs: list[dict[str, Any]] = []

        slide_chunks: list[tuple[str, int, int]] = []
        for slide in range(parent_start, parent_end + 1):
            payload = pages.get(slide)
            if not payload:
                continue
            for text in _split_slide_payload(
                slide=slide,
                title=payload["title"],
                units=payload["units"],
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
                overlap_chars=overlap_chars,
            ):
                slide_chunks.append((text, slide, slide))

        for text, slide_start, slide_end in _combine_short_slide_chunks(slide_chunks, target_chars):
            child_specs.append({
                "block_type": "presentation_text",
                "text": text,
                "slide_start": slide_start,
                "slide_end": slide_end,
                "semantic_role": "slide_text",
            })

        for slide in range(parent_start, parent_end + 1):
            for table in tables_by_page.get(slide, []):
                for table_text in _split_table(
                    slide=slide,
                    rows=table.get("rows") or [],
                    target_chars=target_chars,
                    hard_max_chars=hard_max_chars,
                    overlap_chars=overlap_chars,
                ):
                    child_specs.append({
                        "block_type": "presentation_table",
                        "text": table_text,
                        "slide_start": slide,
                        "slide_end": slide,
                        "semantic_role": "table",
                    })

        for child_index, child in enumerate(child_specs):
            slide_start = child["slide_start"]
            slide_end = child["slide_end"]
            blocks.append(DocumentBlock(
                block_id=(
                    f"{source_name}:slides:{slide_start}-{slide_end}:"
                    f"{child['block_type']}:{child_index + 1}"
                ),
                block_type=child["block_type"],
                text=child["text"],
                page_number=slide_start,
                source_name=source_name,
                metadata={
                    "parser": "mineru",
                    "parent_id": parent_id,
                    # 父块正文只随首个子块跨服务传输一次，后续子块只引用 parent_id。
                    "parent_text": parent_text if child_index == 0 else None,
                    "parent_slide_start": parent_start,
                    "parent_slide_end": parent_end,
                    "slide_start": slide_start,
                    "slide_end": slide_end,
                    "child_index": child_index,
                    "semantic_role": child["semantic_role"],
                },
            ))
    return blocks


def _build_slide_payloads(
    page_texts: dict[int, str],
    semantic_units: list[dict[str, Any]],
    repeated_texts: set[str],
    total_slides: int,
) -> dict[int, dict[str, Any]]:
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, unit in enumerate(semantic_units):
        page = _positive_int(unit.get("pageNumber")) or 1
        text = _clean_text(unit.get("text"))
        if not text or _normalize_text(text) in repeated_texts:
            continue
        by_page[page].append({**unit, "text": text, "_index": index})

    pages: dict[int, dict[str, Any]] = {}
    for page in range(1, total_slides + 1):
        units = sorted(by_page.get(page, []), key=_unit_sort_key)
        title = next((unit["text"] for unit in units if unit.get("type") == "title"), "")
        body = [unit["text"] for unit in units if unit["text"] != title]
        if not units:
            lines = [
                line.strip()
                for line in str(page_texts.get(page, "")).splitlines()
                if line.strip()
                and not re.fullmatch(r"#{1,6}", line.strip())
                and _normalize_text(line) not in repeated_texts
            ]
            if lines:
                if len(lines[0]) <= 80:
                    title = lines[0]
                    body = lines[1:]
                else:
                    body = lines
        if title or body:
            pages[page] = {"title": title, "units": body}
    return pages


def _build_parent_text(
    *,
    pages: dict[int, dict[str, Any]],
    tables_by_page: dict[int, list[dict[str, Any]]],
    start: int,
    end: int,
) -> str:
    parts: list[str] = []
    for slide in range(start, end + 1):
        payload = pages.get(slide)
        if payload:
            header = _slide_header(slide, payload["title"])
            body = "\n".join(payload["units"])
            parts.append(f"{header}\n{body}".strip())
        for table in tables_by_page.get(slide, []):
            table_text = "\n".join(
                "\t".join(_clean_text(cell) for cell in row)
                for row in (table.get("rows") or [])
            ).strip()
            if table_text:
                parts.append(f"【第{slide}页表格】\n{table_text}")
    return "\n\n".join(parts)


def _split_slide_payload(
    *,
    slide: int,
    title: str,
    units: list[str],
    target_chars: int,
    hard_max_chars: int,
    overlap_chars: int,
) -> list[str]:
    header = _slide_header(slide, title)
    if not units:
        return [header]

    target_body = max(1, target_chars - len(header) - 1)
    hard_body = max(1, hard_max_chars - len(header) - 1)
    # 长页面按预计块数均分，避免最后一块明显偏短（例如 1200 + 800）。
    raw_size = sum(len(unit) for unit in units)
    total_size = len(header) + 1 + raw_size
    if total_size > target_chars:
        chunk_count = math.ceil(total_size / target_chars)
        overlap_total = max(0, chunk_count - 1) * min(overlap_chars, target_body)
        target_body = max(1, math.ceil((raw_size + overlap_total) / chunk_count))
    atoms: list[str] = []
    for unit in units:
        if len(unit) <= hard_body:
            atoms.append(unit)
        else:
            atoms.extend(_semantic_split(unit, target_body, hard_body, overlap_chars))

    chunks: list[str] = []
    current = ""
    for atom in atoms:
        separator = "\n" if current else ""
        if current and len(current) + len(separator) + len(atom) > target_body:
            chunks.append(f"{header}\n{current}".strip())
            current = ""
        if len(atom) > hard_body:
            split_atoms = _semantic_split(atom, target_body, hard_body, overlap_chars)
            chunks.extend(f"{header}\n{part}".strip() for part in split_atoms[:-1])
            atom = split_atoms[-1]
        current = f"{current}{separator}{atom}" if current else atom
    if current:
        chunks.append(f"{header}\n{current}".strip())
    return chunks


def _semantic_split(text: str, target: int, hard_max: int, overlap: int) -> list[str]:
    sentences = [
        item.strip()
        for item in re.findall(r"[^。！？；.!?;\n]+[。！？；.!?;]?", text)
        if item.strip()
    ]
    if not sentences:
        sentences = [text.strip()]

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > hard_max:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_fixed_split(sentence, target, hard_max, overlap))
            continue
        if current and len(current) + len(sentence) > target:
            chunks.append(current)
            carry = _semantic_overlap(current, overlap)
            if carry and len(carry) + len(sentence) <= hard_max:
                current = carry
            else:
                current = ""
        if current and len(current) + len(sentence) > hard_max:
            chunks.append(current)
            current = ""
        current += sentence
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def _fixed_split(text: str, target: int, hard_max: int, overlap: int) -> list[str]:
    size = min(target, hard_max)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - min(overlap, size - 1))
    return chunks


def _semantic_overlap(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    sentences = [
        item.strip()
        for item in re.findall(r"[^。！？；.!?;\n]+[。！？；.!?;]?", text)
        if item.strip()
    ]
    selected: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        if len(sentence) > limit:
            return sentence[-limit:]
        if total + len(sentence) > limit:
            break
        selected.append(sentence)
        total += len(sentence)
    return "".join(reversed(selected))


def _combine_short_slide_chunks(
    chunks: list[tuple[str, int, int]], target_chars: int
) -> list[tuple[str, int, int]]:
    combined: list[tuple[str, int, int]] = []
    current_text = ""
    current_start = 0
    current_end = 0
    for text, slide_start, slide_end in chunks:
        candidate = f"{current_text}\n\n{text}" if current_text else text
        if current_text and len(candidate) > target_chars:
            combined.append((current_text, current_start, current_end))
            current_text = text
            current_start = slide_start
            current_end = slide_end
            continue
        if not current_text:
            current_start = slide_start
        current_text = candidate
        current_end = slide_end
    if current_text:
        combined.append((current_text, current_start, current_end))
    return combined


def _split_table(
    *,
    slide: int,
    rows: list[list[Any]],
    target_chars: int,
    hard_max_chars: int,
    overlap_chars: int,
) -> list[str]:
    normalized_rows = ["\t".join(_clean_text(cell) for cell in row).strip() for row in rows]
    normalized_rows = [row for row in normalized_rows if row]
    if not normalized_rows:
        return []

    marker = f"【第{slide}页表格】"
    header = normalized_rows[0]
    data_rows = normalized_rows[1:]
    prefix = f"{marker}\n{header}"
    if not data_rows:
        return [prefix[:hard_max_chars]]

    chunks: list[str] = []
    current_rows: list[str] = []
    for row in data_rows:
        candidate = "\n".join([prefix, *current_rows, row])
        if current_rows and len(candidate) > target_chars:
            chunks.append("\n".join([prefix, *current_rows]))
            current_rows = []
        if len(f"{prefix}\n{row}") > hard_max_chars:
            capacity = max(1, hard_max_chars - len(prefix) - 1)
            parts = _semantic_split(row, capacity, capacity, min(overlap_chars, capacity - 1))
            chunks.extend(f"{prefix}\n{part}" for part in parts)
        else:
            current_rows.append(row)
    if current_rows:
        chunks.append("\n".join([prefix, *current_rows]))
    return chunks


def _find_repeated_boilerplate(
    *,
    semantic_units: list[dict[str, Any]],
    page_texts: dict[int, str],
    total_slides: int,
) -> set[str]:
    threshold = max(3, math.ceil(total_slides * 0.3))
    occurrences: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for unit in semantic_units:
        text = _clean_text(unit.get("text"))
        bbox = unit.get("bbox")
        page = _positive_int(unit.get("pageNumber"))
        if not text or len(text) > 80 or page is None or not _valid_bbox(bbox):
            continue
        occurrences[_normalize_text(text)].append((page, float(bbox[1])))

    repeated = {
        text
        for text, values in occurrences.items()
        if len({page for page, _ in values}) >= threshold
        and max(y for _, y in values) - min(y for _, y in values) <= 20
    }

    boundary_occurrences: dict[str, set[int]] = defaultdict(set)
    for page, raw_text in page_texts.items():
        lines = [line.strip() for line in str(raw_text).splitlines() if line.strip()]
        for line in [*lines[:2], *lines[-2:]]:
            if len(line) <= 80:
                boundary_occurrences[_normalize_text(line)].add(page)
    repeated.update(
        text for text, pages in boundary_occurrences.items() if len(pages) >= threshold
    )
    return repeated


def _unit_sort_key(unit: dict[str, Any]) -> tuple[float, float, int]:
    bbox = unit.get("bbox")
    if _valid_bbox(bbox):
        return float(bbox[1]), float(bbox[0]), int(unit.get("_index", 0))
    return float("inf"), float("inf"), int(unit.get("_index", 0))


def _slide_header(slide: int, title: str) -> str:
    return f"【第{slide}页：{title}】" if title else f"【第{slide}页】"


def _presentation_parent_id(source_name: str, start: int, end: int) -> str:
    readable = f"{source_name}:slides:{start}-{end}"
    if len(readable) <= 128:
        return readable
    source_hash = hashlib.sha1(source_name.encode("utf-8")).hexdigest()[:20]
    return f"pptx:{source_hash}:slides:{start}-{end}"


def _clean_text(value: Any) -> str:
    text = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]*$", "", str(value or ""))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[\t \f\v]+", " ", text)).strip()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _valid_bbox(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) >= 4 and all(
        isinstance(item, (int, float)) for item in value[:4]
    )


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None
