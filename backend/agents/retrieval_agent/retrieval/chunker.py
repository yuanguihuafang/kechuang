"""文本分块工具。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)\n]+\)")


def split_text_by_utf8_bytes(text: str, max_bytes: int) -> list[str]:
    """按 UTF-8 字节上限拆分文本，并保持 Markdown 图片链接完整。"""

    if not text:
        return []
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    if max_bytes < 8:
        raise ValueError("max_bytes 必须至少为 8")

    pieces: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for token in re.split(f"({_MARKDOWN_IMAGE_RE.pattern})", text):
        if not token:
            continue
        is_image = bool(_MARKDOWN_IMAGE_RE.fullmatch(token))
        units = [token] if is_image else list(token)
        for unit in units:
            unit_bytes = len(unit.encode("utf-8"))
            if unit_bytes > max_bytes:
                if is_image:
                    raise ValueError("单个 Markdown 图片链接超过 Milvus 字节上限")
                raise ValueError("文本字符超过 Milvus 字节上限")
            if current and current_bytes + unit_bytes > max_bytes:
                pieces.append("".join(current))
                current, current_bytes = [], 0
            current.append(unit)
            current_bytes += unit_bytes
    if current:
        pieces.append("".join(current))
    return [piece for piece in pieces if piece]


def _mask_markdown_images(text: str) -> tuple[str, dict[str, str]]:
    """把 Markdown 图片替换成等长占位符，避免图片影响正文分块长度。"""

    replacements: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        marker = chr(0xE000 + len(replacements))
        image = match.group(0)
        placeholder = marker * len(image)
        replacements[placeholder] = image
        return placeholder

    return _MARKDOWN_IMAGE_RE.sub(replace, text), replacements


def _restore_markdown_images(text: str, replacements: dict[str, str]) -> str:
    for marker, image in replacements.items():
        text = text.replace(marker, image)
    return text


def chunk_by_paragraph(
    text: str,
    max_chunk_size: int = 1000,
    min_chunk_size: int = 50,
    overlap: int = 50,
    _image_replacements: dict[str, str] | None = None,
) -> Iterator[str]:
    """
    按段落分块，保留段落完整性。

    策略：
    - 优先保持段落完整
    - 超长段落按句子分割
    - 仅在超长块切分时保留尾部重叠，避免相邻独立段落重复

    Args:
        text: 原始文本
        max_chunk_size: 最大块大小（字符数）
        min_chunk_size: 最小块大小

    Yields:
        文本块
    """
    if not text or not text.strip():
        return

    if _image_replacements is None:
        masked_text, image_replacements = _mask_markdown_images(text)
    else:
        masked_text, image_replacements = text, _image_replacements
    if image_replacements and _image_replacements is None:
        for chunk in chunk_by_paragraph(masked_text, max_chunk_size, min_chunk_size, overlap, image_replacements):
            yield _restore_markdown_images(chunk, image_replacements)
        return
    text = masked_text

    # 保留换行作为段落边界；仅清理行内空白。
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(re.sub(r"[\t \f\v]+", " ", line).strip() for line in text.split("\n"))
    text = text.strip()

    # 按双换行或单换行分割段落
    paragraphs = re.split(r'\n\s*\n|\n', text)

    current_chunks: list[str] = []
    current_size = 0

    for para in paragraphs:
        para = para.strip()
        para_size = len(para)

        if para_size == 0:
            continue

        # 空段落
        if not para:
            continue

        # 单段落过长：按句子分割
        if para_size > max_chunk_size:
            if current_chunks:
                yield _join_chunks(current_chunks, max_chunk_size)
                current_chunks = []
                current_size = 0

            yield from _split_by_sentences(para, max_chunk_size, min_chunk_size, overlap, image_replacements)
            continue

        # 检查加入后是否超限
        if current_size + para_size + 2 > max_chunk_size and current_chunks:
            # 合并前先检查是否需要合并过短的块
            if current_size < min_chunk_size and len(current_chunks) > 1:
                # 和下一个段落合并
                para = _join_chunks(current_chunks) + '\n' + para
                current_chunks = []
                current_size = 0
            else:
                yield _join_chunks(current_chunks, max_chunk_size)
                current_chunks = []
                current_size = 0

        current_chunks.append(para)
        current_size += para_size + 2

    # 尾部短块也保留：文档标题、表格字段和值都可能很短，丢弃会损失证据。
    if current_chunks:
        chunk = _join_chunks(current_chunks, max_chunk_size)
        if chunk:
            yield chunk


def _split_by_sentences(
    text: str,
    max_size: int,
    min_size: int,
    overlap: int,
    image_replacements: dict[str, str] | None = None,
) -> Iterator[str]:
    """按句子分割长文本。"""
    # 分割句子
    sentences = [
        item.strip()
        for item in re.findall(r"[^。！？；.!?;\n]+[。！？；.!?;]?", text)
        if item.strip()
    ]
    if not sentences:
        return

    current = ""
    for sentence in sentences:
        if len(sentence) > max_size:
            if current:
                yield current.strip()
                current = ""
            if image_replacements:
                yield from _split_by_atomic_units(sentence, max_size, image_replacements)
            else:
                yield from chunk_by_fixed_size(sentence, max_size, overlap)
            continue
        if current and len(current) + len(sentence) > max_size:
            yield current.strip()
            current = "" if image_replacements else _semantic_overlap(current, overlap)
        current += sentence

    if current:
        yield current.strip()


def _split_by_atomic_units(text: str, max_size: int, image_replacements: dict[str, str]) -> Iterator[str]:
    """按图片完整性切分，普通文字仍按目标长度切分。"""

    markers = sorted(image_replacements, key=len, reverse=True)
    pattern = re.compile(f"({'|'.join(re.escape(marker) for marker in markers)})")
    atoms = [atom for atom in pattern.split(text) if atom]
    current = ""
    for atom in atoms:
        units = [atom] if atom in image_replacements else [atom[index:index + max_size] for index in range(0, len(atom), max_size)]
        for unit in units:
            if current and len(current) + len(unit) > max_size:
                yield current.strip()
                current = ""
            current += unit
    if current.strip():
        yield current.strip()


def _semantic_overlap(text: str, limit: int) -> str:
    """优先重叠完整句；末句本身过长时才退化为字符尾部。"""

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


def _join_chunks(chunks: list[str], max_size: int | None = None) -> str:
    """合并文本块。"""
    joined = '\n\n'.join(chunks)
    if max_size and len(joined) > max_size:
        return joined[:max_size]
    return joined


def chunk_by_fixed_size(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 50,
) -> Iterator[str]:
    """
    固定大小分块，带重叠。

    Args:
        text: 原始文本
        chunk_size: 每块大小
        overlap: 重叠大小

    Yields:
        文本块
    """
    if not text or not text.strip():
        return

    text = re.sub(r'\s+', ' ', text).strip()
    text_len = len(text)
    start = 0

    while start < text_len:
        end = min(start + chunk_size, text_len)

        # 尝试在句子边界截断
        if end < text_len:
            end = _find_sentence_boundary(text, start, end)

        chunk = text[start:end].strip()
        if chunk:
            yield chunk

        # 到达文本尾部后必须停止，不能从尾部回退 overlap 再次处理同一尾块。
        if end >= text_len:
            break

        # 移动窗口
        start = end - overlap
        if start <= 0:
            start = end
        if start <= 0 or start >= end:
            start = end
        if start >= text_len:
            break


def _find_sentence_boundary(text: str, start: int, end: int) -> int:
    """在指定范围内查找句子边界。"""
    # 向前查找
    for pattern in ['。', '！', '？', '；', '.', '!', '?', ';', '。\n', '！\n', '？\n', '\n']:
        pos = text.rfind(pattern, start, end)
        if pos > start + 50:  # 至少前移50字符
            return pos + len(pattern)

    # 向后查找
    for pattern in ['。', '！', '？', '；', '.', '!', '?', ';', '\n']:
        pos = text.find(pattern, end, min(end + 100, len(text)))
        if pos > end:
            return pos + 1

    return end


def count_chunks(text: str, chunk_size: int = 800) -> int:
    """估算分块数量。"""
    if not text:
        return 0
    text = re.sub(r'\s+', ' ', text).strip()
    return max(1, (len(text) + chunk_size - 1) // chunk_size)


def chunk_document_blocks(
    blocks: list[Any],
    *,
    target_size: int = 1000,
    hard_max_size: int = 1300,
    overlap: int = 50,
) -> list[Any]:
    """按文档语义重组 Perception blocks，返回 Retrieval 最终索引块。

    Excel ``record`` 是稳定的业务行父块，始终保留父子关系；其它文件的普通
    文本、标题和表格按原始顺序合并，不再按页机械切块。只有一个逻辑单元超过
    hard_max_size 时，才给拆出的子块挂 parent_id/parent_text。
    """
    if not blocks:
        return []
    # 草稿字段优先保持为一个整体；只有明显超过约 1200 字才按字段/语义拆分。
    draft_roles = {"agent_draft", "admin_draft"}
    if all(str(getattr(block, "chunk_role", "") or (getattr(block, "metadata", {}) or {}).get("chunk_role") or "") in draft_roles
           for block in blocks):
        output: list[Any] = []
        for block in blocks:
            text = str(getattr(block, "chunk_text", "") or getattr(block, "text", "") or "").strip()
            for index, piece in enumerate(chunk_draft_text(text)):
                output.append(_copy_index_block(block, piece, {}, len(output)))
        return output
    source_name = str(getattr(blocks[0], "source_name", "") or "").lower()
    page_blocks = all(bool((getattr(block, "metadata", {}) or {}).get("page_block")) for block in blocks)
    if page_blocks and source_name.endswith((".pdf", ".docx")):
        return _chunk_page_blocks(blocks, target_size, hard_max_size, overlap)
    output: list[Any] = []
    pending: list[Any] = []

    def flush_pending() -> None:
        nonlocal pending
        if not pending:
            return
        for text, source, meta in _merge_block_group(
            pending, target_size, hard_max_size, overlap
        ):
            output.append(_copy_index_block(source, text, meta, len(output)))
        pending = []

    for block in blocks:
        text = str(getattr(block, "chunk_text", "") or getattr(block, "text", "") or "").strip()
        if not text:
            continue
        if str(getattr(block, "chunk_role", "") or (getattr(block, "metadata", {}) or {}).get("chunk_role") or "") in draft_roles:
            flush_pending()
            for piece in chunk_draft_text(text):
                output.append(_copy_index_block(block, piece, {}, len(output)))
            continue
        block_type = _block_type(block)
        if block_type == "record":
            flush_pending()
            for child_index, piece in enumerate(_chunk_record_text(
                # MySQL 一行先映射成中文字段，约 1200 字以内保持为一个 chunk。
                text, max_size=1200, overlap=min(overlap, 50)
            )):
                metadata = {"parent_id": _parent_id(block), "parent_text": text, "child_index": child_index}
                output.append(_copy_index_block(block, piece, metadata, len(output)))
            continue
        pending.append(block)
    flush_pending()
    return output


def chunk_draft_text(text: str, *, target_size: int = 1200,
                     tolerance: int = 100, overlap: int = 50) -> list[str]:
    """按草稿字段边界切分，长字段再按段落和句子切分。"""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    soft_limit = target_size + max(0, tolerance)
    # 草稿发布器用英文字段名加全角冒号；字段值内部的换行仍属于同一字段。
    fields = [part.strip() for part in re.split(r"\n(?=[A-Za-z_][A-Za-z0-9_]*：)", normalized) if part.strip()]
    if len(fields) == 1 and "\n" in normalized and not re.search(r"^[A-Za-z_][A-Za-z0-9_]*：", normalized):
        fields = [line.strip() for line in normalized.split("\n") if line.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if current:
            chunks.append("\n".join(current))
            current, current_size = [], 0

    for field in fields:
        if len(field) > soft_limit:
            flush()
            # 先估算块数再降低单块目标，避免 1400 字被切成 1200+200。
            count = max(2, (len(field) + target_size - 1) // target_size)
            # 目标长度需把相邻块的语义重叠计入，否则 1400 字可能退化成三块。
            balanced_target = max(1, (len(field) + count - 1) // count) + overlap
            chunks.extend(chunk_by_paragraph(
                field, max_chunk_size=balanced_target, overlap=overlap
            ))
            continue
        candidate_size = current_size + (1 if current else 0) + len(field)
        if current and candidate_size > soft_limit:
            flush()
        current.append(field)
        current_size += (1 if current_size else 0) + len(field)
    flush()
    return chunks


def _chunk_record_text(text: str, *, max_size: int, overlap: int) -> list[str]:
    """业务记录复用草稿的字段边界和语义切分策略。"""
    chunks = chunk_draft_text(text, target_size=max_size, tolerance=100, overlap=overlap)
    if len(chunks) < 2:
        return chunks
    # 同一记录的尾部短字段（如联系电话、邮箱）不单独成块；允许在硬上限内并入相邻块。
    hard_limit = max_size + 300
    merged: list[str] = []
    for piece in chunks:
        if merged and len(piece) < 200 and len(merged[-1]) + 1 + len(piece) <= hard_limit:
            merged[-1] = f"{merged[-1]}\n{piece}"
        else:
            merged.append(piece)
    return merged


def _chunk_page_blocks(
    blocks: list[Any], target: int, hard_max: int, overlap: int
) -> list[Any]:
    """PDF/DOCX 按页合并；超长页只在本页内均衡切分。"""
    output: list[Any] = []
    pending: list[Any] = []

    def flush_pending() -> None:
        nonlocal pending
        if pending:
            for text, source, metadata in _merge_block_group(pending, target, hard_max, overlap):
                output.append(_copy_index_block(source, text, metadata, len(output)))
            pending = []

    for block in blocks:
        text = str(getattr(block, "chunk_text", "") or getattr(block, "text", "") or "").strip()
        if not text:
            continue
        if _block_type(block) in {"table", "presentation_table"}:
            flush_pending()
            for piece in _balanced_table_chunks(text, target, hard_max, overlap):
                output.append(_copy_index_block(block, piece, _page_metadata([block]), len(output)))
            continue
        if len(text) > hard_max:
            flush_pending()
            count = max(2, (len(text) + target - 1) // target)
            balanced_target = max(1, (len(text) + count - 1) // count) + overlap
            for piece in chunk_by_paragraph(text, max_chunk_size=balanced_target, overlap=overlap):
                metadata = {"parent_id": None, "parent_text": None, "child_index": 0,
                            **_page_metadata([block])}
                output.append(_copy_index_block(block, piece, metadata, len(output)))
            continue
        candidate = "\n\n".join([
            *[str(getattr(item, "chunk_text", "") or getattr(item, "text", "") or "").strip() for item in pending],
            text,
        ])
        if pending and len(candidate) > target:
            flush_pending()
        pending.append(block)
    flush_pending()
    return output


def _balanced_text_chunks(text: str, target: int, hard_max: int, overlap: int) -> list[str]:
    """超长页面或表格按语义单元尽量均分，避免尾部出现很短的孤块。"""

    if len(text) <= hard_max:
        return [text]
    count = max(2, (len(text) + target - 1) // target)
    balanced_target = max(1, (len(text) + count - 1) // count) + overlap
    return list(chunk_by_paragraph(text, max_chunk_size=balanced_target, overlap=overlap))


def _balanced_table_chunks(text: str, target: int, hard_max: int, overlap: int) -> list[str]:
    """表格优先按行分组；单行过大时才退回语义切分。"""
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if not rows:
        return []
    if len(text) <= hard_max:
        return ["\n".join(rows)]
    count = max(2, (len(text) + target - 1) // target)
    balanced_target = max(1, (len(text) + count - 1) // count)
    groups: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for index, row in enumerate(rows):
        remaining_rows = len(rows) - index
        remaining_groups = count - len(groups)
        if current and current_size + len(row) + 1 > balanced_target and remaining_rows >= remaining_groups:
            groups.append(current)
            current, current_size = [], 0
        current.append(row)
        current_size += len(row) + 1
    if current:
        groups.append(current)
    chunks: list[str] = []
    for group in groups:
        group_text = "\n".join(group)
        if len(group_text) <= hard_max:
            chunks.append(group_text)
        else:
            chunks.extend(chunk_by_paragraph(group_text, max_chunk_size=hard_max, overlap=overlap))
    return chunks


def _merge_block_group(
    blocks: list[Any], target: int, hard_max: int, overlap: int
) -> list[tuple[str, Any, dict[str, Any]]]:
    """按输入顺序合并相邻普通块；超长单元才产生父子块。"""
    results: list[tuple[str, Any, dict[str, Any]]] = []
    group_parent_ids = {_parent_id(block) for block in blocks if _parent_id(block)}
    if len(group_parent_ids) == 1 and len(blocks) > 1:
        full_text = "\n\n".join(
            str(getattr(block, "chunk_text", "") or getattr(block, "text", "") or "").strip()
            for block in blocks
        ).strip()
        if len(full_text) > hard_max:
            parent_id = next(iter(group_parent_ids))
            parent_text = next(
                (getattr(block, "parent_text", None) for block in blocks if getattr(block, "parent_text", None)),
                full_text,
            )
            for child_index, piece in enumerate(
                chunk_by_paragraph(full_text, max_chunk_size=target, overlap=overlap)
            ):
                results.append((piece, blocks[0], {
                    "parent_id": parent_id,
                    "parent_text": parent_text,
                    "child_index": child_index,
                    **_page_metadata(blocks),
                }))
            return results
    current: list[str] = []
    current_blocks: list[Any] = []
    current_len = 0
    current_source: Any = None

    def emit_current() -> None:
        nonlocal current, current_blocks, current_len, current_source
        if current:
            results.append(("\n\n".join(current), current_source, {
                "parent_id": None, "parent_text": None, "child_index": 0,
                **_page_metadata(current_blocks),
            }))
        current, current_blocks, current_len, current_source = [], [], 0, None

    for block in blocks:
        text = str(getattr(block, "chunk_text", "") or getattr(block, "text", "") or "").strip()
        if not text:
            continue
        # 表格保持在原始流位置；短表格可与同页标题/说明合并，长表格单独分块。
        if _block_type(block) in {"table", "presentation_table"}:
            if len(text) <= hard_max and current:
                candidate = "\n\n".join([*current, text])
                if len(candidate) <= target:
                    current.append(text)
                    current_blocks.append(block)
                    current_len = len(candidate)
                    continue
            emit_current()
            for piece in chunk_by_paragraph(text, max_chunk_size=target, overlap=overlap):
                results.append((piece, block, {
                    "parent_id": None, "parent_text": None, "child_index": 0,
                    **_page_metadata([block]),
                }))
            continue
        if len(text) > hard_max:
            emit_current()
            parent_id = _parent_id(block) or f"derived:{id(block)}"
            parent_text = getattr(block, "parent_text", None) or text
            for child_index, piece in enumerate(
                chunk_by_paragraph(text, max_chunk_size=target, overlap=overlap)
            ):
                results.append((piece, block, {
                    "parent_id": parent_id, "parent_text": parent_text, "child_index": child_index,
                    **_page_metadata([block]),
                }))
            continue
        candidate_len = current_len + (2 if current else 0) + len(text)
        if current and candidate_len > target:
            emit_current()
        if current_source is None:
            current_source = block
        current.append(text)
        current_blocks.append(block)
        current_len = current_len + (2 if current_len else 0) + len(text)
    emit_current()

    # 短尾内容如果仍属于同一页/同一逻辑单元，应回并到前一个块，避免一句话单独成块。
    if len(results) > 1:
        compacted: list[tuple[str, Any, dict[str, Any]]] = []
        non_mergeable_types = {"title", "heading", "header", "table", "presentation_table"}
        for item in results:
            if compacted:
                previous_text, previous_source, previous_metadata = compacted[-1]
                tail_text, tail_source, tail_metadata = item
                same_context = all(
                    previous_metadata.get(key) in (None, tail_metadata.get(key))
                    and tail_metadata.get(key) in (None, previous_metadata.get(key))
                    for key in ("page_start", "page_end", "parent_id")
                )
                previous_name = str(getattr(previous_source, "source_name", "") or "")
                tail_name = str(getattr(tail_source, "source_name", "") or "")
                same_source = not previous_name or not tail_name or previous_name == tail_name
                can_merge = (
                    len(tail_text) < 200
                    and _block_type(previous_source) not in non_mergeable_types
                    and _block_type(tail_source) not in non_mergeable_types
                    and same_context
                    and same_source
                    and len(previous_text) + 2 + len(tail_text) <= hard_max
                )
                if can_merge:
                    compacted[-1] = (
                        f"{previous_text}\n\n{tail_text}", previous_source, previous_metadata
                    )
                    continue
            compacted.append(item)
        results = compacted
    return results


def _copy_index_block(source: Any, text: str, metadata: dict[str, Any], index: int) -> Any:
    """兼容 IndexChunk 与 Perception DocumentBlock，避免 chunker 依赖具体服务模型。"""
    if hasattr(source, "model_copy"):
        updates = {"chunk_text": text, "chunk_index": index}
        if hasattr(source, "source_locator"):
            updates["metadata"] = {**getattr(source, "metadata", {}), **metadata}
            page_start = metadata.get("page_start")
            page_end = metadata.get("page_end")
            if page_start is not None and page_end is not None:
                is_slide_locator = "|slides:" in source.source_locator
                if is_slide_locator:
                    location = f"slides:{page_start}-{page_end}"
                else:
                    location = f"page:{page_start}" if page_start == page_end else f"pages:{page_start}-{page_end}"
                locator = re.sub(r"\|(pages?|slides):\d+(?:-\d+)?", f"|{location}", source.source_locator)
                if locator == source.source_locator and "|page:" not in locator and "|slides:" not in locator:
                    locator = f"{locator}|{location}"
                updates["source_locator"] = locator
            return source.model_copy(update=updates)
        return source.model_copy(update={"text": text, "metadata": {**getattr(source, "metadata", {}), **metadata}})
    return source


def _page_metadata(blocks: list[Any]) -> dict[str, Any]:
    """返回合并块覆盖的页范围；无页码的业务块不添加页元数据。"""

    starts: list[int] = []
    ends: list[int] = []
    slide_starts: list[int] = []
    slide_ends: list[int] = []
    image_ids: list[str] = []
    image_refs: list[dict[str, Any]] = []
    for block in blocks:
        metadata = getattr(block, "metadata", {}) or {}
        start = metadata.get("page_start", getattr(block, "page_number", None))
        end = metadata.get("page_end", getattr(block, "page_number", None))
        if isinstance(start, int) and isinstance(end, int):
            starts.append(start)
            ends.append(end)
        slide_start = metadata.get("slide_start")
        slide_end = metadata.get("slide_end")
        if isinstance(slide_start, int) and isinstance(slide_end, int):
            slide_starts.append(slide_start)
            slide_ends.append(slide_end)
        for image_id in metadata.get("image_ids") or []:
            value = str(image_id).strip()
            if value and value not in image_ids:
                image_ids.append(value)
        for image_ref in metadata.get("image_refs") or []:
            if not isinstance(image_ref, dict):
                continue
            image_id = str(image_ref.get("image_id") or "").strip()
            if not image_id or any(str(item.get("image_id") or "") == image_id for item in image_refs):
                continue
            image_refs.append({
                "image_id": image_id,
                "sources": [
                    str(source).strip()
                    for source in image_ref.get("sources") or []
                    if str(source).strip()
                ],
            })
    result: dict[str, Any] = {}
    if image_ids:
        result["image_ids"] = image_ids
    if image_refs:
        result["image_refs"] = image_refs
    if starts:
        result.update(page_start=min(starts), page_end=max(ends))
    if slide_starts:
        result.update(slide_start=min(slide_starts), slide_end=max(slide_ends))
    return result


def _block_type(block: Any) -> str:
    explicit = str(getattr(block, "block_type", "") or getattr(block, "metadata", {}).get("block_type", ""))
    if explicit:
        return explicit
    locator = str(getattr(block, "source_locator", ""))
    match = re.search(r"\|type:([^|]+)", locator)
    return match.group(1) if match else "text"


def _parent_id(block: Any) -> str | None:
    metadata = getattr(block, "metadata", {}) or {}
    return getattr(block, "parent_id", None) or metadata.get("parent_id")
