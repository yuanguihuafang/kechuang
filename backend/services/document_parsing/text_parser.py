"""Markdown/TXT 轻量解析器。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from services.document_parsing.docx_pagination import extract_docx_page_texts
from services.document_parsing.file_ingestion import ParsedDocument


class PlainTextDocumentParser:
    """直接读取 Markdown 和纯文本文件。"""

    supported_extensions: ClassVar[set[str]] = {"md", "txt"}

    async def parse(self, path: Path, **_: object) -> ParsedDocument:
        text = await asyncio.to_thread(_read_text, path)
        page_texts = {1: text} if text.strip() else {}
        extension = path.suffix.lower().lstrip(".")
        return ParsedDocument(
            source_name=path.name,
            text=text,
            raw_text=text,
            page_texts=page_texts,
            page_raw_texts=page_texts,
            metadata={"parser": "plain_text", "sourceType": extension, "warnings": []},
        )


class HybridDocumentParser:
    """按文件类型选择轻量文本解析器、PaddleOCR、视觉模型或 MinerU。"""

    def __init__(
        self,
        mineru_parser: object | None = None,
        ark_parser: object | None = None,
        preferred_parser: str = "mineru",
        local_mineru_parser: object | None = None,
        image_mineru_parser: object | None = None,
        paddle_parser: object | None = None,
    ) -> None:
        self._plain_text_parser = PlainTextDocumentParser()
        from services.document_parsing.excel_parser import ExcelBusinessParser

        self._excel_parser = ExcelBusinessParser()
        self._mineru_parser = mineru_parser
        self._local_mineru_parser = local_mineru_parser
        self._image_mineru_parser = image_mineru_parser
        self._paddle_parser = paddle_parser
        self._ark_parser = ark_parser
        self._preferred_parser = preferred_parser.strip().lower()
        self.supported_extensions = (
            self._plain_text_parser.supported_extensions
            | self._excel_parser.supported_extensions
            | getattr(mineru_parser, "supported_extensions", set())
            | getattr(local_mineru_parser, "supported_extensions", set())
            | getattr(image_mineru_parser, "supported_extensions", set())
            | getattr(paddle_parser, "supported_extensions", set())
            | getattr(ark_parser, "supported_extensions", set())
        )

    async def parse(self, path: Path, **kwargs: object) -> ParsedDocument:
        extension = path.suffix.lower().lstrip(".")
        if extension in self._plain_text_parser.supported_extensions:
            return await self._plain_text_parser.parse(path)
        if extension in self._excel_parser.supported_extensions:
            return await self._excel_parser.parse(path)
        if extension == "docx":
            page_texts = await asyncio.to_thread(extract_docx_page_texts, path)
            text = "\n\n".join(value for value in page_texts.values() if value.strip())
            return ParsedDocument(
                source_name=path.name,
                text=text,
                raw_text=text,
                page_texts=page_texts,
                page_raw_texts=page_texts,
                metadata={"parser": "python_docx", "sourceType": extension, "warnings": []},
            )
        if extension in {"ppt", "pptx"}:
            if self._local_mineru_parser is None:
                raise RuntimeError("PPT/PPTX 解析需要本地 MinerU")
            return await self._local_mineru_parser.parse(path)
        if self._paddle_parser is not None and extension in getattr(
            self._paddle_parser, "supported_extensions", set()
        ):
            return await self._paddle_parser.parse(path, **kwargs)
        # 图片默认交给视觉模型（LLM_VISION_MODEL）；MinerU 仅保留为显式备用解析器。
        if self._ark_parser is not None and extension in {
            "jpg",
            "jpeg",
            "png",
            "webp",
            "bmp",
            "tif",
            "tiff",
        }:
            return await self._ark_parser.parse(path, **kwargs)
        if self._image_mineru_parser is not None and extension in {
            "jpg",
            "jpeg",
            "png",
            "webp",
            "bmp",
            "tif",
            "tiff",
        }:
            return await self._image_mineru_parser.parse(path)
        if self._mineru_parser is None:
            raise RuntimeError(f"当前解析器不支持文件类型：{extension}")
        return await self._mineru_parser.parse(path)


def _read_text(path: Path) -> str:
    """兼容常见 UTF-8 和 Windows 中文文本编码。"""

    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
