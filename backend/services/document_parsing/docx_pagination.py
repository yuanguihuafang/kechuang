"""DOCX 显式分页符切页工具。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PAGE_BREAK = object()
_W_TYPE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type"


def extract_docx_page_texts(path: Path) -> dict[int, str]:
    """按 DOCX 中的显式分页符提取页级文本。

    说明：
    - Word 的“页”本质上依赖排版引擎，不像 PDF 一样天然固定；
    - 这里只处理文件中真实存在的分页符（w:br type="page" / lastRenderedPageBreak）；
    - 没有显式分页符时返回单页，交给上层正常兜底。
    """

    try:
        from docx import Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        return {}

    try:
        document = Document(str(path))
    except Exception:  # noqa: BLE001 - DOCX 解析失败由上层降级
        return {}

    pages: list[list[str]] = [[]]

    def append_text(text: str) -> None:
        normalized = _normalize_line(text)
        if normalized:
            pages[-1].append(normalized)

    def new_page() -> None:
        if pages[-1]:
            pages.append([])

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            paragraph = Paragraph(child, document)
            for part in _iter_paragraph_parts(paragraph):
                if part is _PAGE_BREAK:
                    new_page()
                else:
                    append_text(str(part))
        elif isinstance(child, CT_Tbl):
            table = Table(child, document)
            table_lines = _table_to_lines(table)
            for line in table_lines:
                append_text(line)

    return {
        page_number: "\n".join(lines)
        for page_number, lines in enumerate(pages, start=1)
        if lines
    }


def _iter_paragraph_parts(paragraph: Any) -> list[str | object]:
    """按文本和分页符拆分段落。"""

    parts: list[str | object] = []
    current: list[str] = []

    for element in paragraph._element.iter():
        tag = _local_name(element.tag)
        if tag == "t":
            current.append(element.text or "")
        elif tag == "tab":
            current.append("\t")
        elif tag == "br" and element.get(_W_TYPE) == "page" or tag == "lastRenderedPageBreak":
            parts.append("".join(current))
            parts.append(_PAGE_BREAK)
            current = []

    parts.append("".join(current))
    return parts


def _table_to_lines(table: Any) -> list[str]:
    """把 Word 表格转成按行文本。"""

    lines: list[str] = []
    for row in table.rows:
        cells = [_normalize_line(cell.text) for cell in row.cells]
        if any(cells):
            lines.append("\t".join(cells))
    return lines


def _normalize_line(value: str) -> str:
    text = re.sub(r"[\r\n\t]+", " ", value.strip())
    return re.sub(r" {2,}", " ", text)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
