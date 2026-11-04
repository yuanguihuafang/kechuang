"""通用文件摄取工具：把上传文件转换成后续可抽取的文本或表格片段。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class TableFragment:
    """从文件中解析出来的一段候选表格。

    rows 使用二维字符串数组表达，第一行不一定是表头，表头判断交给上层
    schema / column mapping 流程完成。
    """

    source_name: str
    rows: list[list[str]]
    sheet_name: str | None = None
    page_number: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDocument:
    """文件解析结果，允许同时包含普通文本和多个表格片段。"""

    source_name: str
    text: str = ""  # 过滤后的文本，给 LLM 用
    raw_text: str = ""  # 原始 OCR 文本，用于校对
    tables: list[TableFragment] = field(default_factory=list)
    page_texts: dict[int, str] = field(default_factory=dict)  # {页号: 过滤后文本}
    page_raw_texts: dict[int, str] = field(default_factory=dict)  # {页号: 原始文本}
    metadata: dict[str, Any] = field(default_factory=dict)  # 解析元数据，如页面分类、warnings


class FileParser(Protocol):
    """文件解析器协议。

    具体项目可以分别实现 Excel、CSV、Word、PDF、图片等解析器，并统一返回
    ParsedDocument，避免业务服务直接依赖某个文件库。
    """

    supported_extensions: set[str]

    async def parse(self, path: Path) -> ParsedDocument:
        """解析文件并返回统一文档结构。"""
        ...


class ParserRegistry:
    """按扩展名选择文件解析器。"""

    def __init__(self, parsers: list[FileParser] | None = None) -> None:
        self._parsers = parsers or []

    def register(self, parser: FileParser) -> None:
        self._parsers.append(parser)

    def get_parser(self, filename: str) -> FileParser:
        extension = Path(filename).suffix.lower().lstrip(".")
        for parser in self._parsers:
            if extension in parser.supported_extensions:
                return parser
        raise ValueError(f"不支持的文件类型：{extension or 'unknown'}")

