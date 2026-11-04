"""使用视觉模型解析单张图片，不依赖 MinerU。"""

from __future__ import annotations

import base64
import io
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ClassVar

import httpx
from PIL import Image

from core.prompts.document_parsing import build_vision_draft_prompt
from runtime.config import get_settings

from .file_ingestion import ParsedDocument


class ArkVisualDocumentParser:
    """逐页渲染文档并调用 Responses API，返回统一解析结果。"""

    supported_extensions: ClassVar[set[str]] = {
        "jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff",
        "pdf", "pptx",
    }
    image_extensions: ClassVar[set[str]] = {
        "jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff",
    }

    async def parse(self, path: Path, *, extract_draft: bool = False, knowledge_type: str = "") -> ParsedDocument:
        pages = await _render_pages(path)
        if not pages:
            raise ValueError("文档没有可解析页面")
        draft_payload = None
        if extract_draft:
            page_texts, draft_payload = await _extract_document_with_draft(
                pages, path.name, knowledge_type
            )
        else:
            page_texts = await _extract_pages(pages, path.name)
        text = "\n\n".join(value for value in page_texts.values() if value).strip()
        return ParsedDocument(
            source_name=path.name,
            text=text,
            raw_text=text,
            page_texts=page_texts,
            page_raw_texts=page_texts,
            metadata={
                "parser": "ark_visual",
                "sourceType": path.suffix.lower().lstrip("."),
                "page_count": len(pages),
                "warnings": [],
                **({"draft_payload": draft_payload} if draft_payload else {}),
            },
        )


async def _render_pages(path: Path) -> list[Image.Image]:
    extension = path.suffix.lower().lstrip(".")
    if extension in ArkVisualDocumentParser.image_extensions:
        return await asyncio.to_thread(_render_image, path)
    if extension == "pdf":
        return await asyncio.to_thread(_render_pdf_pages, path)
    return await asyncio.to_thread(_render_ppt_pages, path)


def _render_image(path: Path) -> list[Image.Image]:
    with Image.open(path) as image:
        return [image.convert("RGB")]


def _render_pdf_pages(path: Path) -> list[Image.Image]:
    import fitz
    document = fitz.open(str(path))
    try:
        return [
            Image.open(io.BytesIO(page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes("png"))).convert("RGB")
            for page in document
        ]
    finally:
        document.close()


def _render_ppt_pages(path: Path) -> list[Image.Image]:
    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError("解析 PPT/PPTX 需要安装 LibreOffice（soffice）")
    with tempfile.TemporaryDirectory(prefix="ark-ppt-") as output_dir:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", output_dir, str(path)],
            check=True, capture_output=True, text=True,
        )
        converted = Path(output_dir) / f"{path.stem}.pdf"
        if not converted.is_file():
            raise RuntimeError("PPT/PPTX 转换为 PDF 失败")
        return _render_pdf_pages(converted)


def _find_soffice() -> str | None:
    project_tools = Path(__file__).resolve().parents[3] / "tools"
    for candidate in (
        project_tools / "LibreOffice" / "program" / "soffice.exe",
        project_tools / "program" / "soffice.exe",
        project_tools / "soffice.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("soffice") or shutil.which("libreoffice")


async def _extract_pages(pages: list[Image.Image], file_name: str) -> dict[int, str]:
    settings = get_settings()
    base_url = str(settings.llm_vision_base_url or "").rstrip("/")
    api_key = str(settings.llm_vision_api_key or "").strip()
    model = str(settings.llm_vision_model or "").strip()
    if not base_url or not api_key or not model:
        raise RuntimeError("LLM_VISION_BASE_URL、LLM_VISION_API_KEY 或 LLM_VISION_MODEL 未配置")
    timeout = httpx.Timeout(float(settings.llm_timeout_seconds), connect=float(settings.llm_connect_timeout_seconds))
    max_retries = max(0, int(settings.llm_max_retries))
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        results: dict[int, str] = {}
        for number, page in enumerate(pages, start=1):
            buffer = io.BytesIO()
            page.save(buffer, format="PNG", optimize=True)
            image_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            for attempt in range(max_retries + 1):
                try:
                    endpoint, payload = _build_vision_request(
                        base_url, model, [
                            {"type": "input_image", "image_url": image_url},
                            {"type": "input_text", "text": f"请完整识别第{number}页（文件：{file_name}）的文字、表格和图片说明，只返回可检索的中文原文。"},
                        ],
                    )
                    response = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    if response.status_code >= 500 and attempt < max_retries:
                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue
                    response.raise_for_status()
                    results[number] = _response_text(response.json())
                    break
                except httpx.TransportError as exc:
                    if attempt >= max_retries:
                        raise RuntimeError(f"Ark 第{number}页请求失败：{exc}") from exc
                    await asyncio.sleep(min(2 ** attempt, 8))
    return results


async def _extract_document_with_draft(
    pages: list[Image.Image], file_name: str, knowledge_type: str,
) -> tuple[dict[int, str], dict[str, Any]]:
    """问答入口使用一次视觉请求，同时返回原文页内容和草稿字段。"""

    settings = get_settings()
    base_url = str(settings.llm_vision_base_url or "").rstrip("/")
    api_key = str(settings.llm_vision_api_key or "").strip()
    model = str(settings.llm_vision_model or "").strip()
    if not base_url or not api_key or not model:
        raise RuntimeError("LLM_VISION_BASE_URL、LLM_VISION_API_KEY 或 LLM_VISION_MODEL 未配置")
    content: list[dict[str, str]] = []
    for number, page in enumerate(pages, start=1):
        buffer = io.BytesIO()
        page.save(buffer, format="PNG", optimize=True)
        content.append({
            "type": "input_text",
            "text": f"下面是文件第 {number} 页，请识别该页全部原文。",
        })
        content.append({
            "type": "input_image",
            "image_url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
        })
    field_requirements = {
        "solution": (
            "fields必须使用这些键：title、pub_dept、product_type、contect、analyse_contect、"
            "link_name、link_tel、deliver_cycle、quote_range。pub_dept填写原文中的发布企业/单位；"
            "contect只填写技术方案本身的原理、组成、功能、实施、参数、场景和交付边界；企业简介、公司规模、主营业务、经营范围、荣誉和企业资质不得写入contect。"
            "资料只有企业介绍而没有明确方案内容时，contect留空，不要用企业介绍代替；analyse_contect只根据方案原文生成方案分析；"
            "deliver_cycle必须保留原文中的完整数值和单位。"
        ),
        "achievement": (
            "fields必须使用这些键：title、category、analyse_contect、tech_maturity、"
            "cooperation_mode、company、link_name、link_tel；analyse_contect填写成果分析，"
            "company填写原文中的发布单位。"
        ),
        "demand": (
            "fields必须使用这些键：title、category、company、contect、analyse_contect、"
            "link_name、link_tel、budget_range、demand_cycle；analyse_contect填写需求分析，"
            "demand_cycle必须保留原文中的完整数值和单位。"
        ),
        "customer_visit": (
            "fields必须使用这些键：enterprise_portrait、potential_demand_list_text、budget_range、"
            "expect_cycle、adapt_solution、supplier_match_dim；从拜访原文提取企业画像、潜在需求、"
            "预算和项目周期，expect_cycle必须保留原文中的完整数值和单位；没有证据的字段留空。"
        ),
    }.get(knowledge_type, "fields只能填写当前资料类型明确出现的字段，数值和单位必须完整保留。")
    prompt = build_vision_draft_prompt(
        file_name=file_name,
        knowledge_type=knowledge_type,
        field_requirements=field_requirements,
    )
    content.append({"type": "input_text", "text": prompt})
    timeout = httpx.Timeout(float(settings.llm_timeout_seconds), connect=float(settings.llm_connect_timeout_seconds))
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        endpoint, request_payload = _build_vision_request(base_url, model, content)
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=request_payload,
        )
        response.raise_for_status()
    payload = _response_json(response.json())
    pages_payload = payload.get("pages") if isinstance(payload, dict) else None
    page_texts = {
        int(item.get("page")): str(item.get("text") or "").strip()
        for item in pages_payload or []
        if isinstance(item, dict) and str(item.get("page") or "").isdigit()
    }
    if not page_texts:
        raise ValueError("视觉模型未返回有效原文页")
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    return page_texts, draft


def _response_text(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            values = [
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if values:
                return "\n".join(values).strip()
    values: list[str] = []
    for item in (payload.get("output", []) if isinstance(payload, dict) else []):
        for content in (item.get("content", []) if isinstance(item, dict) else []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                values.append(content["text"])
    if not values:
        raise ValueError("方舟 Responses 返回中没有文本")
    return "\n".join(values).strip()


def _build_vision_request(
    base_url: str, model: str, content: list[dict[str, str]],
) -> tuple[str, dict[str, Any]]:
    """兼容 Responses API 和项目当前使用的 OpenAI Chat Completions API。"""

    if base_url.endswith("/responses"):
        return base_url, {"model": model, "input": [{"role": "user", "content": content}]}
    chat_content = []
    for item in content:
        if item.get("type") == "input_image":
            chat_content.append({"type": "image_url", "image_url": {"url": item["image_url"]}})
        else:
            chat_content.append({"type": "text", "text": item.get("text", "")})
    return f"{base_url}/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": chat_content}],
        "temperature": 0,
    }


def _response_json(payload: Any) -> dict[str, Any]:
    text = _response_text(payload).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("视觉模型未返回有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("视觉模型 JSON 根节点必须是对象")
    return value
