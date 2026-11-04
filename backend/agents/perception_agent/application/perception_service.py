"""Perception 多模态感知 Agent"""

from __future__ import annotations

import hashlib
import json
import re
from time import perf_counter
from pathlib import Path
from typing import Any

from services.document_parsing.excel_parser import (
    deduplicate_excel_fields,
    merge_excel_profile_fields,
)
from services.document_parsing.mineru_parser import normalize_markdown_image_references
from core.contracts import DocumentBlock
from core.contracts.field_permissions import preview_field_permissions
from core.contracts.agent import AgentContext, AgentResult, failure, success

from ..schemas import PerceptionRequest, PerceptionResult


def _clean_chunk_markdown(value: str) -> str:
    """清理解析器生成的空标题标记，保留有效 Markdown 标题。"""

    text = str(value or "").replace("\r\n", "\n")
    text = re.sub(r"(^|\n)(#{1,6})[ \t]*\n(?=[^#\s])", r"\1\2 ", text)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*(?=\n|$)", "", text)
    text = re.sub(r"(?<=\n)[ \t]*#{1,6}[ \t]*\n", "", text)
    text = re.sub(r"\n[ \t]*#{1,6}[ \t]*(?=$)", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _create_mineru_parser() -> Any:
    from services.document_parsing.text_parser import HybridDocumentParser
    from services.document_parsing.ark_visual_parser import ArkVisualDocumentParser
    from services.document_parsing.mineru_parser import MinerUDocumentParser
    from services.document_parsing.paddleocr_parser import PaddleOCRDocumentParser
    from services.document_parsing.mineru_runner import (
        HttpMinerURunner,
        ImagePdfHttpMinerURunner,
        LocalMinerURunner,
    )

    settings = __import__("runtime.config", fromlist=["get_settings"]).get_settings()
    mineru_url = str(getattr(settings, "mineru_url", "") or "").strip()
    mineru_command = str(getattr(settings, "mineru_command", "") or "").strip() or "mineru"
    document_parser = (
        str(getattr(settings, "document_parser", "mineru") or "mineru").strip().lower()
    )
    runner = HttpMinerURunner(mineru_url) if mineru_url else LocalMinerURunner(mineru_command)
    local_mineru_parser = MinerUDocumentParser(
        runner=LocalMinerURunner(mineru_command),
    )
    image_runner = (
        ImagePdfHttpMinerURunner(HttpMinerURunner(mineru_url))
        if mineru_url
        else LocalMinerURunner(mineru_command)
    )
    return HybridDocumentParser(
        MinerUDocumentParser(runner=runner),
        ark_parser=ArkVisualDocumentParser(),
        preferred_parser=document_parser,
        local_mineru_parser=local_mineru_parser,
        image_mineru_parser=MinerUDocumentParser(runner=image_runner),
        paddle_parser=PaddleOCRDocumentParser(),
    )


class PerceptionAgent:
    """将原始文件转换成统一的文本、页文本和表格结构。

    DOCX 使用 python-docx；PPT/PPTX 使用本地 MinerU；图片通过 PaddleOCR API 解析。
    """

    def __init__(self, parser: Any | None = None) -> None:
        self.parser = parser or _create_mineru_parser()

    name = "perception"

    @staticmethod
    async def extract_fields(
        draft: dict[str, Any],
        *,
        user_message: str = "",
        source_text: str = "",
        reviewer: Any | None = None,
        allow_multiple: bool = False,
        draft_factory: Any | None = None,
        required_field_pairs_factory: Any | None = None,
        reviewer_role: str = "draft",
        extraction_mode: str = "full",
        prompt_profile: str = "smart_qa",
    ) -> dict[str, Any]:
        """从已解析内容提取结构化字段；流程层负责保存和发布草稿。"""

        from .field_extraction import enrich_draft_with_llm

        return await enrich_draft_with_llm(
            draft,
            user_message=user_message,
            source_text=source_text,
            reviewer=reviewer,
            allow_multiple=allow_multiple,
            draft_factory=draft_factory,
            required_field_pairs_factory=required_field_pairs_factory,
            reviewer_role=reviewer_role,
            extraction_mode=extraction_mode,
            prompt_profile=prompt_profile,
        )

    async def extract_drafts(self, draft: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """统一的草稿提取门面，聊天和管理控制台共用。"""
        return await self.extract_fields(draft, **kwargs)

    async def extract_material_facts(
        self,
        text: str,
        knowledge_type: str,
        blocks: list[dict[str, Any]] | None = None,
        *,
        conversation_context: str = "",
        client: Any | None = None,
        on_token: Any = None,
    ) -> tuple[str, int]:
        from .draft_extractor import extract_material_facts

        return await extract_material_facts(
            text,
            knowledge_type,
            blocks,
            conversation_context=conversation_context,
            client=client,
            on_token=on_token,
        )

    async def parse_and_extract(
        self, request: PerceptionRequest, draft: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """先解析文件，再把解析原文作为草稿提取输入。"""
        parsed = await self.run(request)
        if parsed.status != "SUCCESS":
            return {
                **draft,
                "draft_review": {"status": "FALLBACK", "reason": parsed.error or "解析失败"},
            }
        reviewer_role = "vision" if parsed.metadata.get("parser") == "ark_visual" else "draft"
        return await self.extract_drafts(
            {
                **draft,
                "source_text": parsed.raw_text or parsed.text,
                "source_blocks": [block.model_dump(mode="json") for block in parsed.blocks],
            },
            source_text=parsed.raw_text or parsed.text,
            reviewer_role=reviewer_role,
            **kwargs,
        )

    async def invoke(
        self, context: AgentContext, request: PerceptionRequest
    ) -> AgentResult[PerceptionResult]:
        started_at = perf_counter()
        try:
            result = await self.run(request)
        except Exception as exc:  # noqa: BLE001 - Agent 边界统一错误
            return failure(
                context,
                operation="parse",
                started_at=started_at,
                error_code="PERCEPTION_FAILED",
                error_message=str(exc),
            )
        if result.status != "SUCCESS":
            return failure(
                context,
                operation="parse",
                started_at=started_at,
                error_code="PERCEPTION_FAILED",
                error_message=result.error or "文件解析失败",
            )
        return success(context, result, operation="parse", started_at=started_at)

    async def _parse(self, state: dict[str, Any]) -> dict[str, Any]:
        path = Path(state["file_path"])
        if not path.exists() or not path.is_file():
            return {"error": f"文件不存在：{path}"}
        try:
            parse_kwargs = (
                {
                    "extract_draft": bool(state.get("extract_draft")),
                    "knowledge_type": str(state.get("knowledge_type") or ""),
                }
                if state.get("extract_draft")
                else {}
            )
            parsed = await self.parser.parse(path, **parse_kwargs)
            return {"parsed": parsed}
        except Exception as exc:  # noqa: BLE001 - Agent 边界需要统一解析器异常
            return {"error": str(exc)}

    async def run(self, request: PerceptionRequest | str) -> PerceptionResult:
        if isinstance(request, str):
            request = PerceptionRequest(file_path=request)
        state = await self._parse(
            {
                "file_path": request.file_path,
                "extract_draft": request.extract_draft,
                "knowledge_type": request.knowledge_type,
            }
        )
        path = Path(request.file_path)
        parsed = state.get("parsed")
        if parsed is None:
            return PerceptionResult(
                status="FAILED",
                source_name=path.name,
                error=state.get("error", "文件解析失败"),
            )
        tables = [
            {
                "source_name": table.source_name,
                "rows": table.rows,
                "sheet_name": table.sheet_name,
                "page_number": table.page_number,
                "metadata": table.metadata,
            }
            for table in parsed.tables
        ]
        cleaned_page_texts: dict[int, str] = {}
        for page, page_text in parsed.page_texts.items():
            cleaned = _clean_chunk_markdown(page_text)
            if cleaned:
                cleaned_page_texts[page] = cleaned
        blocks = _build_document_blocks(
            source_name=parsed.source_name,
            text=parsed.text,
            page_texts=cleaned_page_texts,
            tables=tables,
            metadata={
                **parsed.metadata,
                "knowledge_type": request.knowledge_type
                or parsed.metadata.get("knowledge_type", ""),
            },
        )
        public_metadata = dict(parsed.metadata)
        # semanticUnits 只用于服务内构造 PPTX blocks；对外继续保留 content_list
        # 的 MinIO 审计地址，避免把完整文本在 metadata 和 blocks 中重复传输。
        public_metadata.pop("semanticUnits", None)
        metadata = _attach_image_context(
            metadata=public_metadata,
            document_id=request.document_id,
            source_name=parsed.source_name,
            blocks=blocks,
        )
        blocks = _attach_block_image_ids(blocks, metadata.get("images"))
        audit_text = "\n\n".join(
            page_text for _, page_text in sorted(cleaned_page_texts.items()) if page_text
        )
        return PerceptionResult(
            status="SUCCESS",
            source_name=parsed.source_name,
            document_id=request.document_id,
            storage_key=request.storage_key,
            text=audit_text,
            raw_text=normalize_markdown_image_references(
                _clean_chunk_markdown(parsed.raw_text), metadata.get("images") or []
            ),
            page_texts=cleaned_page_texts,
            tables=tables,
            blocks=blocks,
            metadata={**metadata, "parser": metadata.get("parser", "mineru")},
        )

    async def run_path(
        self,
        *,
        path: Path,
        file_name: str,
        document_id: str | None = None,
        storage_key: str | None = None,
        knowledge_type: str | None = None,
        extract_draft: bool = False,
    ) -> PerceptionResult:
        """解析受控本地文件；正式上传链路通过该方法避免完整内存副本。"""

        safe_name = Path(file_name).name
        if safe_name != file_name or safe_name in {"", ".", ".."}:
            return PerceptionResult(
                status="FAILED",
                source_name=safe_name,
                document_id=document_id,
                storage_key=storage_key,
                error="文件名不合法",
            )
        if not path.is_file() or path.stat().st_size == 0:
            return PerceptionResult(
                status="FAILED",
                source_name=safe_name,
                document_id=document_id,
                storage_key=storage_key,
                error="上传文件为空",
            )
        result = await self.run(
            PerceptionRequest(
                file_path=str(path),
                document_id=document_id,
                storage_key=storage_key,
                knowledge_type=knowledge_type,
                extract_draft=extract_draft,
            )
        )
        # 临时路径的文件名与原始安全文件名一致；显式保留跨 Agent 身份字段。
        return result.model_copy(
            update={
                "source_name": safe_name,
                "document_id": document_id,
                "storage_key": storage_key,
            }
        )


def _build_document_blocks(
    *,
    source_name: str,
    text: str,
    page_texts: dict[int, str],
    tables: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> list[DocumentBlock]:
    """把页文本和表格转换为 Retrieval 可直接消费的统一文档块。"""

    if Path(source_name).suffix.lower() == ".pptx":
        from services.document_parsing.presentation_chunking import build_presentation_blocks

        semantic_units = (metadata or {}).get("semanticUnits", [])
        return build_presentation_blocks(
            source_name=source_name,
            page_texts=page_texts,
            tables=tables,
            semantic_units=semantic_units if isinstance(semantic_units, list) else [],
        )

    blocks: list[DocumentBlock] = []
    tables_by_page: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for table_index, table in enumerate(tables, start=1):
        page = table.get("page_number")
        if isinstance(page, int) and page > 0:
            tables_by_page.setdefault(page, []).append((table_index, table))
    # 按页生成文本块，表格单独生成 table 块，避免页面级切分把正文和大表格混在一起。
    page_numbers = sorted({*page_texts.keys(), *tables_by_page.keys()})
    for page_number in page_numbers:
        page_text = page_texts.get(page_number, "")
        if page_text.strip():
            blocks.append(
                DocumentBlock(
                    block_id=f"{source_name}:page:{page_number}:text",
                    block_type="text",
                    text=page_text.strip(),
                    page_number=page_number,
                    source_name=source_name,
                    metadata={"page_block": True, "contains_text": True, "contains_table": False},
                )
            )
        for table_index, table in tables_by_page.get(page_number, []):
            if table.get("metadata", {}).get("parser") == "excel_business":
                continue
            rows = table.get("rows", [])
            table_text = "\n".join("\t".join(str(cell) for cell in row) for row in rows)
            if table_text.strip():
                blocks.append(
                    DocumentBlock(
                        block_id=f"{source_name}:page:{page_number}:table:{table_index}",
                        block_type="table",
                        text=table_text.strip(),
                        page_number=page_number,
                        source_name=source_name,
                        metadata={
                            "page_block": True,
                            "contains_text": False,
                            "contains_table": True,
                            "table_indexes": [table_index],
                        },
                    )
                )

    # Excel 的结构化记录由“每行一个父块”承载；只有没有任何表格时，
    # 才用整段文本生成兜底 block，避免同一内容重复进入 Retrieval。
    if not blocks and not tables and text.strip():
        blocks.append(
            DocumentBlock(
                block_id=f"{source_name}:text:1",
                text=text.strip(),
                source_name=source_name,
            )
        )

    for table_index, table in enumerate(tables, start=1):
        rows = table.get("rows", [])
        table_metadata = table.get("metadata") or {}
        if table_metadata.get("parser") == "excel_business":
            if len(rows) < 2:
                continue
            headers = [str(cell).strip() for cell in rows[0]]
            try:
                header_row = int(table_metadata.get("headerRow", "1"))
            except (TypeError, ValueError):
                header_row = 1
            sheet_name = str(table.get("sheet_name") or "Sheet1")
            data_rows = rows[1:]
            groups = _excel_groups_from_metadata(table_metadata, len(data_rows), header_row + 1)
            for group in groups:
                grouped_rows = data_rows[group["start_index"] : group["end_index"] + 1]
                field_values = _merge_excel_field_values(headers, grouped_rows)
                fields = [f"{key}：{value}" for key, value in field_values.items()]
                if not field_values:
                    continue
                actual_row = group["start_row"]
                actual_end_row = group["end_row"]
                parent_id = f"{source_name}:sheet:{sheet_name}:row:{actual_row}"
                if actual_end_row != actual_row:
                    parent_id += f"-{actual_end_row}"
                # 工作表和行号是来源定位元数据，不参与向量化文本。
                parent_text = "\n".join(fields)
                blocks.append(
                    DocumentBlock(
                        block_id=f"{source_name}:record:{sheet_name}:{actual_row}",
                        block_type="record",
                        text=parent_text,
                        source_name=source_name,
                        metadata={
                            "table_index": table_index,
                            "parser": "excel_business",
                            "sheet_name": sheet_name,
                            "row_number": actual_row,
                            "row_end_number": actual_end_row,
                            "parent_id": parent_id,
                            "parent_text": parent_text,
                            "field_values": field_values,
                            "field_visibility_preview": preview_field_permissions(
                                str(
                                    (metadata or {}).get("knowledge_type")
                                    or table_metadata.get("knowledge_type")
                                    or ""
                                ),
                                field_values,
                            ),
                            "field_policy_managed": True,
                            **table_metadata,
                        },
                    )
                )
            continue
        if isinstance(table.get("page_number"), int) and table.get("page_number") in tables_by_page:
            continue
        table_text = "\n".join("\t".join(str(cell) for cell in row) for row in rows)
        if not table_text.strip():
            continue
        blocks.append(
            DocumentBlock(
                block_id=f"{source_name}:table:{table_index}",
                block_type="table",
                text=table_text,
                page_number=table.get("page_number"),
                source_name=source_name,
                metadata={
                    "table_index": table_index,
                    "sheet_name": table.get("sheet_name"),
                    **table_metadata,
                },
            )
        )
    return blocks


def _excel_groups_from_metadata(
    metadata: dict[str, Any], row_count: int, first_data_row: int
) -> list[dict[str, int]]:
    try:
        groups = json.loads(str(metadata.get("recordGroups") or ""))
        if isinstance(groups, list) and groups:
            return [
                {
                    "start_index": max(0, int(item["start_index"])),
                    "end_index": min(row_count - 1, int(item["end_index"])),
                    "start_row": int(item["start_row"]),
                    "end_row": int(item["end_row"]),
                }
                for item in groups
                if isinstance(item, dict) and int(item.get("start_index", -1)) < row_count
            ]
    except (TypeError, ValueError, json.JSONDecodeError, KeyError):
        pass
    return [
        {
            "start_index": index,
            "end_index": index,
            "start_row": first_data_row + index,
            "end_row": first_data_row + index,
        }
        for index in range(row_count)
    ]


def _merge_excel_fields(headers: list[str], rows: list[list[str]]) -> list[str]:
    fields: list[str] = []
    for column, header in enumerate(headers):
        values: list[str] = []
        for row in rows:
            value = str(row[column]).strip() if column < len(row) else ""
            if value and value not in values:
                values.append(value)
        if header and values:
            fields.append(f"{header}：{'；'.join(values)}")
    return deduplicate_excel_fields(merge_excel_profile_fields(fields))


def _merge_excel_field_values(headers: list[str], rows: list[list[str]]) -> dict[str, str]:
    """保留列名到值的结构，供 Retrieval 按字段权限拆分向量块。"""

    result: dict[str, str] = {}
    for column, header in enumerate(headers):
        key = str(header or "").strip()
        if not key:
            continue
        values: list[str] = []
        for row in rows:
            value = str(row[column]).strip() if column < len(row) else ""
            if value and value not in values:
                values.append(value)
        if values:
            result[key] = "；".join(values)
    return result


def _attach_image_context(
    *,
    metadata: dict[str, Any],
    document_id: str | None,
    source_name: str,
    blocks: list[DocumentBlock],
) -> dict[str, Any]:
    """为每张 MinerU 图片补足页码、相邻文本块和稳定标识。"""

    output = dict(metadata)
    images = output.get("images")
    if not isinstance(images, list):
        return output
    contextual_images: list[dict[str, Any]] = []
    for ordinal, raw_asset in enumerate(images, start=1):
        if not isinstance(raw_asset, dict):
            continue
        asset = dict(raw_asset)
        page_number = asset.get("pageNumber")
        nearby = [
            block.block_id
            for block in blocks
            if page_number is not None and _block_contains_page(block, page_number)
        ]
        relative_path = str(asset.get("relativePath", ""))
        if document_id and relative_path:
            asset["image_id"] = hashlib.sha1(f"{document_id}:{relative_path}".encode()).hexdigest()
        page_part = page_number if page_number is not None else "unknown"
        asset["source_locator"] = f"{source_name}|type:image|page:{page_part}|ordinal:{ordinal}"
        asset["nearby_block_ids"] = nearby
        contextual_images.append(asset)
    output["images"] = contextual_images
    return output


def _attach_block_image_ids(blocks: list[DocumentBlock], images: object) -> list[DocumentBlock]:
    """把页面图片关系复制到文档块，供技术方案库确认后原样入库。"""

    if not isinstance(images, list):
        return blocks
    image_ids_by_block: dict[str, list[str]] = {}
    image_refs_by_block: dict[str, list[dict[str, object]]] = {}
    for image in images:
        if not isinstance(image, dict) or not image.get("image_id"):
            continue
        image_ref = {
            "image_id": str(image["image_id"]),
            "sources": [
                str(image[field]).replace("\\", "/")
                for field in ("url", "relativePath", "relative_path", "path")
                if image.get(field)
            ],
        }
        for block_id in image.get("nearby_block_ids") or []:
            image_ids_by_block.setdefault(str(block_id), []).append(str(image["image_id"]))
            refs = image_refs_by_block.setdefault(str(block_id), [])
            if not any(item["image_id"] == image_ref["image_id"] for item in refs):
                refs.append(image_ref)
    return [
        block.model_copy(
            update={
                "metadata": {
                    **block.metadata,
                    "image_ids": image_ids_by_block.get(block.block_id, []),
                    "image_refs": image_refs_by_block.get(block.block_id, []),
                }
            }
        )
        for block in blocks
    ]


def _block_contains_page(block: DocumentBlock, page_number: int) -> bool:
    """兼容普通页块和跨多张幻灯片的 PPTX 子块。"""

    slide_start = block.metadata.get("slide_start")
    slide_end = block.metadata.get("slide_end")
    if isinstance(slide_start, int) and isinstance(slide_end, int):
        return slide_start <= page_number <= slide_end
    return block.page_number == page_number
