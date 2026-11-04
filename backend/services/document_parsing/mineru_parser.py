"""MinerU 外部解析器适配层。

这个模块只依赖 MinerU 的命令行输出，不直接 import mineru。
好处是：主项目环境不需要安装 MinerU；只有 TABLE_PARSER=mineru 时才要求外部命令可用。
"""

from __future__ import annotations

from .mineru_runner import HttpMinerURunner

import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import ClassVar

from services.observability.logging import get_logger
from services.document_parsing.docx_pagination import extract_docx_page_texts
from services.document_parsing.file_ingestion import ParsedDocument, TableFragment
from services.document_parsing.mineru_runner import LocalMinerURunner, MinerURunner

logger = get_logger("perception", "tool.mineru")
PROJECT_DATA_DIR = Path(
    os.getenv("KECHUANG_DATA_DIR") or (Path(__file__).resolve().parents[3] / "data")
).resolve()
MINERU_OUTPUT_DIR = Path(
    os.getenv("MINERU_OUTPUT_DIR", PROJECT_DATA_DIR / "mineru-output")
).resolve()


class MinerUDocumentParser:
    """通过外部 mineru 命令解析 PDF、图片和 Office 文档。"""

    supported_extensions: ClassVar[set[str]] = {
        "pdf",
        "jpg",
        "jpeg",
        "png",
        "webp",
        "bmp",
        "tif",
        "tiff",
        "docx",
        "ppt",
        "pptx",
        "xlsx",
    }

    def __init__(
        self,
        command: str | None = None,
        output_path: str | Path = MINERU_OUTPUT_DIR,
        backend: str = "pipeline",
        method: str = "auto",
        lang: str = "ch",
        table_enabled: bool = True,
        timeout_seconds: int = 900,
        runner: MinerURunner | None = None,
    ) -> None:
        self.command = command or os.getenv("MINERU_COMMAND", "mineru")
        self.output_path = Path(output_path)
        self.backend = os.getenv("MINERU_BACKEND", backend).strip() or backend
        self.method = method
        self.lang = lang
        self.table_enabled = table_enabled
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if self.runner is None:
            remote_url = os.getenv("MINERU_URL", "").strip()
            self.runner = (
                HttpMinerURunner(remote_url, timeout_seconds=timeout_seconds)
                if remote_url
                else LocalMinerURunner(resolve_mineru_command(self.command))
            )

    async def parse(self, path: Path) -> ParsedDocument:
        """调用 MinerU CLI 并读取 Markdown/JSON 输出。"""
        run_output = self.output_path / f"{path.stem}-{uuid.uuid4().hex[:8]}"
        run_output.mkdir(parents=True, exist_ok=True)

        command = getattr(self.runner, "command", self.command)

        started_at = perf_counter()
        gpu_info = get_torch_gpu_info()
        logger.info(
            "MinerU 解析开始",
            extra={
                "structured": {
                    "event": "mineru_parse_started",
                    "sourceName": path.name,
                    "backend": self.backend,
                    "method": self.method,
                    "lang": self.lang,
                    "tableEnabled": self.table_enabled,
                    "command": command,
                    "outputDir": str(run_output),
                    "torchVersion": gpu_info.get("torchVersion"),
                    "torchCuda": gpu_info.get("torchCuda"),
                    "cudaAvailable": gpu_info.get("cudaAvailable"),
                    "gpuName": gpu_info.get("gpuName"),
                }
            },
        )

        try:
            completed = await self.runner.run(
                input_path=path,
                output_dir=run_output,
                backend=self.backend,
                method=self.method,
                lang=self.lang,
                table_enabled=self.table_enabled,
                timeout_seconds=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"MinerU 解析超时：{self.timeout_seconds} 秒") from error

        duration_ms = int((perf_counter() - started_at) * 1000)
        stdout_text = completed.stdout
        stderr_text = completed.stderr
        if completed.returncode != 0:
            detail = "\n".join(part for part in [stdout_text[-2000:], stderr_text[-2000:]] if part)
            logger.error(
                "MinerU 解析失败",
                extra={
                    "structured": {
                        "event": "mineru_parse_failed",
                        "sourceName": path.name,
                        "backend": self.backend,
                        "method": self.method,
                        "returnCode": completed.returncode,
                        "durationMs": duration_ms,
                        "stderrTail": stderr_text[-500:],
                    }
                },
            )
            raise RuntimeError(f"MinerU 解析失败，退出码 {completed.returncode}：\n{detail}")

        parsed = parse_mineru_output(path.name, run_output)
        if not parsed.text.strip() and not parsed.tables:
            # 扫描件没有有效 OCR 时保留原文件记录，避免重复触发视觉识别。
            parsed.metadata.update(
                {
                    "original_file_only": True,
                    "original_file_reference": path.name,
                    "warnings": [*(parsed.metadata.get("warnings") or []), "未检测到有效 OCR 文本"],
                }
            )
        quality = parsed.metadata.get("quality", {}) if parsed.metadata else {}
        logger.info(
            "MinerU 解析完成",
            extra={
                "structured": {
                    "event": "mineru_parse_finished",
                    "sourceName": path.name,
                    "backend": self.backend,
                    "method": self.method,
                    "durationMs": duration_ms,
                    "textLength": quality.get("textLength", len(parsed.text)),
                    "tableCount": quality.get("tableCount", len(parsed.tables)),
                    "imageCount": quality.get("imageCount"),
                    "warningCount": len(parsed.metadata.get("warnings", []))
                    if parsed.metadata
                    else 0,
                    "outputDir": str(run_output),
                }
            },
        )
        return parsed


def resolve_mineru_command(command: str) -> str:
    """解析 MinerU 命令路径，给出更清晰的错误。"""
    command_path = Path(command)
    if command_path.exists():
        return str(command_path)
    found = shutil.which(command)
    if found:
        return found
    if command_path.name == command:
        executable_name = f"{command}.exe" if os.name == "nt" else command
        environment_candidates = (
            Path(sys.executable).resolve().parent / executable_name,
            Path(sys.executable).resolve().parent / "Scripts" / executable_name,
        )
        for candidate in environment_candidates:
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError(
        "未找到 MinerU 命令，请检查 MINERU_COMMAND 是否指向当前服务器"
        "已安装环境中的 mineru 可执行文件"
    )


def get_torch_gpu_info() -> dict[str, object]:
    """读取当前 Python 环境的 torch/GPU 状态，用于判断 MinerU 环境是否具备 GPU 条件。"""
    try:
        import torch
    except Exception as error:  # noqa: BLE001 - MinerU 环境探测需容错
        return {
            "cudaAvailable": False,
            "torchError": f"{type(error).__name__}: {error}",
        }

    cuda_available = bool(torch.cuda.is_available())
    gpu_name = None
    if cuda_available:
        try:
            gpu_name = torch.cuda.get_device_name(0)
        except Exception:  # noqa: BLE001 - GPU 探测失败按不可用处理
            gpu_name = None
    return {
        "torchVersion": getattr(torch, "__version__", None),
        "torchCuda": getattr(torch.version, "cuda", None),
        "cudaAvailable": cuda_available,
        "gpuName": gpu_name,
    }


def parse_mineru_output(source_name: str, output_dir: Path) -> ParsedDocument:
    """把 MinerU 输出目录转换成项目统一 ParsedDocument。"""
    markdown_files = sorted(output_dir.rglob("*.md"))
    markdown_parts = [path.read_text(encoding="utf-8", errors="ignore") for path in markdown_files]
    text = "\n\n".join(part.strip() for part in markdown_parts if part.strip())
    markdown_text = text

    content_items, content_list_path = load_mineru_content_list(output_dir)
    content_text = content_items_to_text(content_items)
    if not text and content_text:
        text = content_text

    page_texts = build_page_texts(text, content_items)
    # 当 content_list 带有文本页码时，它才是唯一可信的页序来源。Markdown
    # 可能同时包含总文档和分页产物，直接拼接会把前面页面再次追加到末尾。
    has_numbered_text = any(
        str(item.get("type", "")) in {"text", "title", "paragraph"}
        and _page_number_from_item(item) is not None
        for item in content_items
    )
    if has_numbered_text and page_texts:
        page_texts = _deduplicate_page_texts(page_texts)
        text = "\n\n".join(page_text for _, page_text in sorted(page_texts.items()) if page_text)

    tables = extract_tables_from_content_items(source_name, content_items)
    if not tables:
        tables = extract_tables_from_markdown(source_name, markdown_text)
    # MinerU 已经提供 content_list 时优先使用其版面结果。DOCX 原始表格兜底
    # 会把合并单元格按每个网格单元重复展开，容易重新引入重复字段。
    docx_page_texts = {}
    if not content_items and not page_texts:
        docx_page_texts = build_docx_page_texts_fallback(source_name, output_dir, page_texts)
        if docx_page_texts:
            page_texts = docx_page_texts
    image_assets = collect_mineru_images(output_dir, content_items, text)
    page_manifest = build_page_manifest(
        items=content_items,
        page_texts=page_texts,
        tables=tables,
        images=image_assets,
        has_markdown=bool(markdown_files),
    )
    warnings = validate_mineru_output(
        markdown_files=markdown_files,
        content_list_path=content_list_path,
        text=text,
        tables=tables,
        image_assets=image_assets,
    )
    metadata = {
        "parser": "mineru",
        "outputDir": str(output_dir),
        "markdownFiles": [str(path) for path in markdown_files],
        "contentListPath": str(content_list_path) if content_list_path else None,
        "images": image_assets,
        # 页面清单只服务于原文校对；空页也保留，不能作为检索块使用。
        "pages": page_manifest,
        "blocks": summarize_content_items(content_items),
        # PPTX 分块需要完整文本，不能使用 blocks 中为前端摘要而截断到 300 字的内容。
        "semanticUnits": extract_semantic_units(content_items),
        "quality": {
            "textLength": len(text),
            "tableCount": len(tables),
            "imageCount": len(image_assets),
            "pageCount": len(page_manifest),
            "hasMarkdown": bool(markdown_files),
            "hasContentList": content_list_path is not None,
        },
        "docxPageFallback": bool(docx_page_texts),
        "warnings": warnings,
    }

    return ParsedDocument(
        source_name=source_name,
        text=text,
        raw_text=text,
        tables=tables,
        page_texts=page_texts,
        page_raw_texts=page_texts,
        metadata=metadata,
    )


def load_mineru_content_list(output_dir: Path) -> tuple[list[dict], Path | None]:
    """读取 MinerU content_list，优先使用 v2，失败时回退 v1。"""
    candidates = sorted(output_dir.rglob("*_content_list_v2.json"))
    candidates.extend(sorted(output_dir.rglob("*_content_list.json")))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001,S112 - 单个 MinerU 产物损坏不阻断其余文件
            continue
        items = flatten_content_list(data)
        if items:
            return items, path
    return [], None


def flatten_content_list(data: object) -> list[dict]:
    """兼容 MinerU content_list v1/v2 的列表结构。"""
    if isinstance(data, list):
        flattened: list[dict] = []
        for page_index, item in enumerate(data):
            if isinstance(item, dict):
                flattened.append(item)
            elif isinstance(item, list):
                for child in item:
                    if isinstance(child, dict):
                        item_with_page = dict(child)
                        item_with_page.setdefault("page_idx", page_index)
                        flattened.append(item_with_page)
        return flattened
    return []


def content_items_to_text(items: list[dict]) -> str:
    """从 content_list 中提取可读文本，作为 Markdown 缺失时的回退。"""
    lines: list[str] = []
    for item in items:
        item_type = str(item.get("type", ""))
        if item_type in {"text", "title", "paragraph"}:
            text = extract_item_text(item)
            if text:
                lines.append(text)
        elif item_type == "table":
            table_html = extract_item_table_html(item)
            rows = _html_table_to_rows(table_html) if table_html else []
            lines.extend("\t".join(cell for cell in row if cell) for row in rows)
    return "\n".join(line for line in lines if line.strip())


def extract_semantic_units(items: list[dict]) -> list[dict[str, object]]:
    """提取完整文本结构，供 PPTX 按标题、文本框和段落进行确定性语义切分。"""

    units: list[dict[str, object]] = []
    for item in items:
        item_type = str(item.get("type", ""))
        if item_type not in {"text", "title", "paragraph"}:
            continue
        text = extract_item_text(item)
        if not text:
            continue
        units.append(
            {
                "type": item_type,
                "pageNumber": _page_number_from_item(item) or 1,
                "text": text,
                "bbox": item.get("bbox"),
            }
        )
    return units


def extract_item_text(item: dict) -> str:
    """从 MinerU v1/v2 文本块中抽取文字。"""
    if isinstance(item.get("text"), str):
        return item["text"].strip()
    content = item.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, dict):
        return ""

    values: list[str] = []
    for key in ("title_content", "paragraph_content"):
        parts = content.get(key)
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("content"), str):
                    values.append(part["content"].strip())
    return "".join(_unique_text_values(values)).strip()


def _unique_text_values(values: list[str]) -> list[str]:
    """去除同一 MinerU 内容块中的完全重复片段，保留原始顺序。"""

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def extract_item_table_html(item: dict) -> str:
    """从 MinerU v1/v2 表格块中抽取 HTML。"""
    if isinstance(item.get("table_body"), str):
        return item["table_body"]
    content = item.get("content")
    if isinstance(content, dict) and isinstance(content.get("html"), str):
        return content["html"]
    return ""


def extract_tables_from_content_items(source_name: str, items: list[dict]) -> list[TableFragment]:
    """优先从 MinerU content_list 的 table 块提取表格。"""
    tables: list[TableFragment] = []
    for table_index, item in enumerate(
        [item for item in items if str(item.get("type", "")) == "table"],
        start=1,
    ):
        table_html = extract_item_table_html(item)
        rows = _html_table_to_rows(table_html)
        if not rows:
            continue
        page_number = _page_number_from_item(item)
        metadata = {
            "parser": "mineru",
            "tableIndex": str(table_index),
            "format": "content_list_html",
        }
        image_path = extract_item_image_path(item)
        if image_path:
            metadata["imagePath"] = image_path
        tables.append(
            TableFragment(
                source_name=source_name,
                rows=rows,
                page_number=page_number,
                metadata=metadata,
            )
        )
    return tables


def build_page_texts(markdown_text: str, items: list[dict]) -> dict[int, str]:
    """按页组织文本；MinerU v1 有 page_idx，v2 缺页号时回退到第 1 页。"""
    page_lines: dict[int, list[str]] = {}
    for item in items:
        page_number = _page_number_from_item(item) or 1
        if str(item.get("type", "")) in {"text", "title", "paragraph"}:
            text = extract_item_text(item)
            if text:
                page_lines.setdefault(page_number, []).append(text)
        # table 由 extract_tables_from_content_items 单独输出为 table block。
        # 不追加到 page_texts，避免同一张表同时以“页文本”和“表格”进入 Retrieval。
    if page_lines:
        return {
            page: _deduplicate_page_text("\n".join(lines))
            for page, lines in sorted(page_lines.items())
        }
    return {1: markdown_text} if markdown_text else {}


def build_page_manifest(
    *,
    items: list[dict],
    page_texts: dict[int, str],
    tables: list[TableFragment],
    images: list[dict[str, object]],
    has_markdown: bool,
) -> list[dict[str, object]]:
    """构造保真的页面清单，包含无文本但存在于 MinerU 输出中的页面。

    MinerU 的 content_list 通常只为有内容的页面返回项目。这里根据最大页号
    补出中间缺页，并明确标记为未提取内容；这些占位页只用于预览，不参与分块。
    """

    known_pages = set(page_texts)
    known_pages.update(page for item in items if (page := _page_number_from_item(item)) is not None)
    known_pages.update(
        table.page_number
        for table in tables
        if isinstance(table.page_number, int) and table.page_number > 0
    )
    known_pages.update(
        int(page)
        for image in images
        if isinstance((page := image.get("pageNumber")), int) and page > 0
    )
    if not known_pages and (page_texts or has_markdown):
        known_pages.add(1)
    if not known_pages:
        return []

    image_by_page: dict[int, list[str]] = {}
    for image in images:
        page = image.get("pageNumber")
        source = (
            image.get("relativePath") or image.get("relative_path") or image.get("source_locator")
        )
        if isinstance(page, int) and page > 0 and source:
            image_by_page.setdefault(page, []).append(str(source))
    table_pages = {
        table.page_number
        for table in tables
        if isinstance(table.page_number, int) and table.page_number > 0
    }
    last_page = max(known_pages)
    return [
        {
            "page_number": page,
            "text": page_texts.get(page, ""),
            "has_text": bool(page_texts.get(page, "").strip()),
            "has_table": page in table_pages,
            "image_paths": image_by_page.get(page, []),
            "missing_content": page not in known_pages,
        }
        for page in range(1, last_page + 1)
    ]


def _deduplicate_page_text(text: str) -> str:
    """折叠 MinerU 内容列表中重复展开的单元格和值。

    MinerU 对部分 DOCX 表格会同时输出单元格文本和布局文本，导致同一 token
    或同一段 token 序列连续出现。这里只折叠连续重复，不做全局去重，避免误删
    正文中有意义的重复词。
    """

    lines = [_collapse_repeated_tokens(line) for line in text.splitlines()]
    compact_lines: list[str] = []
    for line in lines:
        if line and compact_lines and line == compact_lines[-1]:
            continue
        compact_lines.append(line)
    return "\n".join(compact_lines).strip()


def _deduplicate_page_texts(page_texts: dict[int, str]) -> dict[int, str]:
    """移除 MinerU 把前面整份文档重复写入后续页面的结果。

    只处理与此前累计页面完全相同，或以前累计页面作为大段前缀的情况；
    普通页眉、标题重复不会达到整页匹配条件。
    """
    result: dict[int, str] = {}
    previous_text = ""
    for page, page_text in sorted(page_texts.items()):
        cleaned = _deduplicate_page_text(page_text)
        if not cleaned:
            continue
        normalized = re.sub(r"\s+", "", cleaned)
        previous_normalized = re.sub(r"\s+", "", previous_text)
        if previous_normalized and normalized == previous_normalized:
            continue
        if (
            previous_normalized
            and len(previous_normalized) >= 200
            and normalized.startswith(previous_normalized)
            and len(previous_normalized) / max(len(normalized), 1) >= 0.8
        ):
            previous_lines = [line for line in previous_text.splitlines() if line.strip()]
            current_lines = [line for line in cleaned.splitlines() if line.strip()]
            matched = 0
            while (
                matched < len(previous_lines)
                and matched < len(current_lines)
                and re.sub(r"\s+", "", previous_lines[matched])
                == re.sub(r"\s+", "", current_lines[matched])
            ):
                matched += 1
            if matched == len(previous_lines):
                cleaned = "\n".join(current_lines[matched:]).strip()
                if not cleaned:
                    continue
        result[page] = cleaned
        previous_text = "\n\n".join(result.values())
    return result


def _collapse_repeated_tokens(text: str) -> str:
    """折叠连续重复 token，以及重复至少三次的短 token 序列。"""

    tokens = text.split()
    if len(tokens) < 2:
        return text.strip()

    result: list[str] = []
    index = 0
    while index < len(tokens):
        longest: tuple[int, int] | None = None
        max_width = min(80, (len(tokens) - index) // 2)
        for width in range(max_width, 1, -1):
            unit = tokens[index : index + width]
            repetitions = 1
            while tokens[index + repetitions * width : index + (repetitions + 1) * width] == unit:
                repetitions += 1
            if repetitions >= 3:
                longest = (width, repetitions)
                break

        if longest:
            width, _ = longest
            result.extend(tokens[index : index + width])
            index += width * longest[1]
            continue

        result.append(tokens[index])
        index += 1

    compact: list[str] = []
    for token in result:
        if compact and compact[-1] == token:
            continue
        compact.append(token)
    return " ".join(compact)


def build_docx_page_texts_fallback(
    source_name: str,
    output_dir: Path,
    current_page_texts: dict[int, str],
) -> dict[int, str]:
    """MinerU Office 模式页号不可靠时，使用原始 DOCX 显式分页符兜底。

    MinerU 解析 docx 时可能把 content_list 里的所有块都标成 page_idx=0，
    这会导致校对页只有 1 页。若输出目录里存在 *_origin.docx，
    且该 Word 文件包含显式分页符，则用它重建页级文本。
    """

    if Path(source_name).suffix.lower() != ".docx":
        return {}
    if len(current_page_texts) > 1:
        return {}

    origin_candidates = sorted(output_dir.rglob("*_origin.docx"))
    if not origin_candidates:
        return {}

    page_texts = extract_docx_page_texts(origin_candidates[0])
    if len(page_texts) <= 1:
        return {}
    return page_texts


def collect_mineru_images(
    output_dir: Path, items: list[dict], markdown_text: str
) -> list[dict[str, object]]:
    """收集 MinerU 图片，并用确定性规则标记检索候选或装饰图。"""
    all_image_files = [path for path in output_dir.rglob("images/*") if path.is_file()]
    files_by_relative = {path.relative_to(output_dir).as_posix(): path for path in all_image_files}
    ordered_files: list[Path] = []
    seen_files: set[Path] = set()
    for item in items:
        reference = extract_item_image_path(item)
        normalized = reference.replace("\\", "/").lstrip("./")
        image_path = files_by_relative.get(normalized)
        if image_path is not None and image_path not in seen_files:
            ordered_files.append(image_path)
            seen_files.add(image_path)
    image_files = ordered_files + sorted(
        (path for path in all_image_files if path not in seen_files),
        key=lambda path: path.relative_to(output_dir).as_posix(),
    )
    references = collect_image_references(items, markdown_text)
    page_numbers = collect_image_page_numbers(items)
    image_contexts = collect_image_contexts(items)
    assets: list[dict[str, object]] = []
    for image_path in image_files:
        if not image_path.is_file():
            continue
        relative = image_path.relative_to(output_dir).as_posix()
        image_hash = _file_sha1(image_path)
        context = image_contexts.get(relative) or image_contexts.get(image_path.name) or {}
        width, height = _read_image_size(image_path)
        assets.append(
            {
                "path": str(image_path),
                "relativePath": relative,
                "sizeBytes": image_path.stat().st_size,
                "contentType": mimetypes.guess_type(image_path.name)[0]
                or "application/octet-stream",
                "contentHash": image_hash,
                "referenced": relative in references or image_path.name in references,
                "pageNumber": (
                    context.get("pageNumber")
                    or page_numbers.get(relative)
                    or page_numbers.get(image_path.name)
                ),
                "caption": context.get("caption") or None,
                "width": width,
                "height": height,
            }
        )
    assets.sort(
        key=lambda asset: (
            asset.get("pageNumber") is None,
            asset.get("pageNumber") if isinstance(asset.get("pageNumber"), int) else 0,
        )
    )
    hash_counts = Counter(str(asset["contentHash"]) for asset in assets)
    for asset in assets:
        has_caption = bool(asset.get("caption"))
        repeated_asset = hash_counts[str(asset["contentHash"])] >= 3
        decorative_geometry = _is_decorative_geometry(asset.get("width"), asset.get("height"))
        asset["imageRole"] = (
            "candidate"
            if has_caption or not (repeated_asset or decorative_geometry)
            else "decorative"
        )
    return assets


def normalize_markdown_image_references(
    text: str,
    images: list[dict[str, object]],
) -> str:
    """解析阶段为每个图片源维护一个 Markdown 引用，保留同名不同路径图片。"""

    aliases: dict[str, str] = {}
    canonical_urls: dict[str, str] = {}
    ambiguous_aliases: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            continue
        identity = str(
            image.get("contentHash")
            or image.get("content_hash")
            or image.get("minio_object_key")
            or image.get("image_id")
            or image.get("relativePath")
            or image.get("relative_path")
            or ""
        ).strip()
        if not identity:
            continue
        image_url = str(image.get("url") or image.get("image_url") or "").strip()
        if image_url:
            canonical_urls.setdefault(identity, image_url)
        for field in ("url", "image_url", "relativePath", "relative_path", "path"):
            reference = str(image.get(field) or "")
            normalized = reference.replace("\\", "/").lstrip("./").lstrip("/")
            if not normalized:
                continue
            keys = [normalized]
            marker = normalized.find("images/")
            if marker >= 0:
                keys.append(normalized[marker:])
            keys.append(normalized.rsplit("/", 1)[-1])
            for key in dict.fromkeys(keys):
                if key in ambiguous_aliases:
                    continue
                previous = aliases.get(key)
                if previous and previous != identity:
                    aliases.pop(key, None)
                    ambiguous_aliases.add(key)
                else:
                    aliases[key] = identity

    seen: set[str] = set()
    # Markdown 图片路径可能包含括号，例如 PPT 文件名 ``介绍(1)``。
    pattern = re.compile(
        r"!\[(?P<alt>[\s\S]*?)\]\((?P<source>(?:[^()\r\n]|\([^()\r\n]*\))+?)"
        r"(?P<title>\s+[\"'][^()\r\n]*[\"'])?\)"
    )

    def replace(match: re.Match[str]) -> str:
        source = match.group("source")
        normalized = source.replace("\\", "/").lstrip("./").lstrip("/")
        keys = [normalized]
        marker = normalized.find("images/")
        if marker >= 0:
            keys.append(normalized[marker:])
        keys.append(normalized.rsplit("/", 1)[-1])
        identity = next((aliases[key] for key in dict.fromkeys(keys) if key in aliases), normalized)
        if identity in seen:
            return ""
        seen.add(identity)
        canonical_url = canonical_urls.get(identity)
        if canonical_url:
            return f"![{match.group('alt')}]({canonical_url}{match.group('title') or ''})"
        return match.group(0)

    return pattern.sub(replace, str(text or ""))


def collect_image_contexts(items: list[dict]) -> dict[str, dict[str, object]]:
    """按图片路径收集页码和相邻图注，不进行图片内容理解。"""

    text_items: list[dict[str, object]] = []
    for item in items:
        if str(item.get("type", "")) not in {"text", "title", "paragraph"}:
            continue
        text = extract_item_text(item)
        if text:
            text_items.append(
                {
                    "pageNumber": _page_number_from_item(item),
                    "text": text,
                    "bbox": item.get("bbox"),
                }
            )

    contexts: dict[str, dict[str, object]] = {}
    basename_contexts: dict[str, dict[str, object] | None] = {}
    for item in items:
        image_path = extract_item_image_path(item)
        if not image_path:
            continue
        normalized = image_path.replace("\\", "/")
        page_number = _page_number_from_item(item)
        caption = _extract_direct_caption(item)
        if not caption:
            caption = _nearest_caption(
                image_bbox=item.get("bbox"),
                page_number=page_number,
                text_items=text_items,
            )
        context = {"pageNumber": page_number, "caption": caption or None}
        contexts.setdefault(normalized, context)
        name = Path(normalized).name
        if name not in basename_contexts:
            basename_contexts[name] = context
        elif basename_contexts[name] is not context:
            basename_contexts[name] = None
    contexts.update(
        {name: context for name, context in basename_contexts.items() if context is not None}
    )
    return contexts


def _extract_direct_caption(item: dict) -> str:
    for key in ("caption", "image_caption", "img_caption"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            parts = [
                str(part.get("content", "")).strip()
                if isinstance(part, dict)
                else str(part).strip()
                for part in value
            ]
            text = "".join(part for part in parts if part)
            if text:
                return text
    content = item.get("content")
    if isinstance(content, dict):
        for key in ("caption", "image_caption", "img_caption"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                text = "".join(
                    str(part.get("content", "")).strip()
                    for part in value
                    if isinstance(part, dict) and part.get("content")
                )
                if text:
                    return text
    return ""


def _nearest_caption(
    *,
    image_bbox: object,
    page_number: int | None,
    text_items: list[dict[str, object]],
) -> str:
    if not _valid_bbox(image_bbox) or page_number is None:
        return ""
    image_box = [float(value) for value in image_bbox[:4]]
    candidates: list[tuple[float, str]] = []
    for text_item in text_items:
        if text_item.get("pageNumber") != page_number:
            continue
        text = str(text_item.get("text") or "").strip()
        bbox = text_item.get("bbox")
        if not text or len(text) > 120 or not _valid_bbox(bbox):
            continue
        text_box = [float(value) for value in bbox[:4]]
        vertical_gap = min(abs(text_box[1] - image_box[3]), abs(image_box[1] - text_box[3]))
        horizontal_overlap = min(image_box[2], text_box[2]) - max(image_box[0], text_box[0])
        looks_like_caption = bool(
            re.match(r"^(图|figure|fig\.?)[\s\d一二三四五六七八九十_-]*", text, re.IGNORECASE)
        )
        if (
            horizontal_overlap > 0
            and vertical_gap <= 100
            and (looks_like_caption or len(text) <= 60)
        ):
            candidates.append((vertical_gap, text))
    return min(candidates, default=(0, ""), key=lambda item: item[0])[1]


def _read_image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001 - 图片尺寸是可选元数据，损坏图片不能中断文本解析
        return None, None


def _file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_decorative_geometry(width: object, height: object) -> bool:
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return False
    aspect_ratio = max(width / height, height / width)
    return min(width, height) < 48 or width * height < 8_000 or aspect_ratio > 10


def _valid_bbox(value: object) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 4
        and all(isinstance(item, (int, float)) for item in value[:4])
    )


def collect_image_references(items: list[dict], markdown_text: str) -> set[str]:
    """收集 Markdown/content_list 中引用过的图片路径。"""
    references = set(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', markdown_text, re.IGNORECASE))
    for item in items:
        image_path = extract_item_image_path(item)
        if image_path:
            references.add(image_path)
        table_html = extract_item_table_html(item)
        references.update(
            re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', table_html, re.IGNORECASE)
        )
    normalized = {ref.replace("\\", "/") for ref in references}
    names = {}
    for ref in normalized:
        name = Path(ref).name
        names[name] = names.get(name, 0) + 1
    normalized.update(name for name, count in names.items() if count == 1)
    return normalized


def collect_image_page_numbers(items: list[dict]) -> dict[str, int]:
    """从 content_list 记录图片所在页；MinerU 未提供页号时不伪造关联。"""

    result: dict[str, int] = {}
    basename_pages: dict[str, int | None] = {}
    for item in items:
        image_path = extract_item_image_path(item)
        page_number = _page_number_from_item(item)
        if not image_path or page_number is None:
            continue
        normalized = image_path.replace("\\", "/")
        result.setdefault(normalized, page_number)
        name = Path(normalized).name
        if name not in basename_pages:
            basename_pages[name] = page_number
        elif basename_pages[name] != page_number:
            basename_pages[name] = None
    result.update({name: page for name, page in basename_pages.items() if page is not None})
    return result


def validate_mineru_output(
    markdown_files: list[Path],
    content_list_path: Path | None,
    text: str,
    tables: list[TableFragment],
    image_assets: list[dict[str, object]],
) -> list[str]:
    """生成解析质量警告，供校验页和日志使用。"""
    warnings: list[str] = []
    if not markdown_files:
        warnings.append("MinerU 输出目录未找到 Markdown 文件。")
    if content_list_path is None:
        warnings.append("MinerU 输出目录未找到 content_list JSON。")
    if len(text.strip()) < 50:
        warnings.append("MinerU 解析文本较短，可能需要 OCR/VLM 回退。")
    if not tables:
        warnings.append("MinerU 未解析出表格，产品参数可能需要 PP-StructureV3 或普通 OCR 回退。")
    if image_assets:
        missing_refs = [
            asset["relativePath"] for asset in image_assets if not asset.get("referenced")
        ]
        if missing_refs:
            warnings.append(f"存在 {len(missing_refs)} 张未被 Markdown/content_list 引用的图片。")
    return warnings


def summarize_content_items(items: list[dict]) -> list[dict[str, object]]:
    """保存轻量块摘要，避免把完整 HTML/base64 塞进 metadata。"""
    summaries: list[dict[str, object]] = []
    for index, item in enumerate(items, start=1):
        item_type = str(item.get("type", "unknown"))
        summary: dict[str, object] = {
            "index": index,
            "type": item_type,
            "pageNumber": _page_number_from_item(item),
            "bbox": item.get("bbox"),
        }
        if item_type in {"text", "title", "paragraph"}:
            summary["text"] = extract_item_text(item)[:300]
        elif item_type == "table":
            rows = _html_table_to_rows(extract_item_table_html(item))
            summary["rowCount"] = len(rows)
            summary["columnCount"] = max((len(row) for row in rows), default=0)
            image_path = extract_item_image_path(item)
            if image_path:
                summary["imagePath"] = image_path
        summaries.append(summary)
    return summaries


def extract_item_image_path(item: dict) -> str:
    """获取 content_list 块关联图片路径。"""
    if isinstance(item.get("img_path"), str):
        return item["img_path"].replace("\\", "/")
    content = item.get("content")
    if isinstance(content, dict):
        image_source = content.get("image_source")
        if isinstance(image_source, dict) and isinstance(image_source.get("path"), str):
            return image_source["path"].replace("\\", "/")
    return ""


def _page_number_from_item(item: dict) -> int | None:
    page_idx = item.get("page_idx")
    if isinstance(page_idx, int):
        return page_idx + 1
    return None


def extract_tables_from_markdown(source_name: str, markdown_text: str) -> list[TableFragment]:
    """从 MinerU Markdown 中提取 HTML 表格和 Markdown 管道表格。"""
    tables: list[TableFragment] = []

    for table_index, table_html in enumerate(_find_html_tables(markdown_text), start=1):
        rows = _html_table_to_rows(table_html)
        if rows:
            tables.append(
                TableFragment(
                    source_name=source_name,
                    rows=rows,
                    metadata={"parser": "mineru", "tableIndex": str(table_index), "format": "html"},
                )
            )

    offset = len(tables)
    for table_index, rows in enumerate(_find_markdown_tables(markdown_text), start=1):
        tables.append(
            TableFragment(
                source_name=source_name,
                rows=rows,
                metadata={
                    "parser": "mineru",
                    "tableIndex": str(offset + table_index),
                    "format": "markdown",
                },
            )
        )

    return tables


def _find_html_tables(text: str) -> list[str]:
    return re.findall(r"<table\b.*?</table>", text, flags=re.IGNORECASE | re.DOTALL)


def _html_table_to_rows(table_html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_html in re.findall(r"<tr\b.*?</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(
            r"<t[dh]\b[^>]*>(.*?)</t[dh]>",
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        values = [_strip_html(cell) for cell in cells]
        if any(values):
            rows.append(values)
    return rows


def _strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<.*?>", "", text, flags=re.DOTALL)
    text = html.unescape(text)
    text = re.sub(r"[\r\n\t]+", " ", text.strip())
    return re.sub(r" {2,}", " ", text)


def _find_markdown_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            current.append(stripped)
            continue
        if current:
            _append_markdown_table(tables, current)
            current = []
    if current:
        _append_markdown_table(tables, current)
    return tables


def _append_markdown_table(tables: list[list[list[str]]], lines: list[str]) -> None:
    rows = [_split_markdown_row(line) for line in lines]
    rows = [row for row in rows if not _is_markdown_separator(row)]
    if len(rows) >= 2:
        tables.append(rows)


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_separator(row: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in row if cell.strip())
