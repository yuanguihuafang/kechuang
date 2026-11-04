"""PaddleOCR AI Studio 文档 OCR 适配器。"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar

import httpx

from runtime.config import get_settings

from .file_ingestion import ParsedDocument


class PaddleOCRDocumentParser:
    """通过 PaddleOCR 异步任务接口解析图片。"""

    supported_extensions: ClassVar[set[str]] = {
        "jpg",
        "jpeg",
        "png",
        "webp",
        "bmp",
        "tif",
        "tiff",
    }

    def __init__(
        self,
        *,
        job_url: str | None = None,
        token: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self.job_url = str(job_url or settings.paddleocr_job_url).rstrip("/")
        self.token = str(token or settings.paddleocr_api_token or "").strip()
        self.model = str(model or settings.paddleocr_model).strip() or "PP-OCRv6"
        self.timeout_seconds = int(timeout_seconds or settings.paddleocr_timeout_seconds)
        self.poll_interval_seconds = float(
            poll_interval_seconds or settings.paddleocr_poll_interval_seconds
        )

    async def parse(self, path: Path, **_: object) -> ParsedDocument:
        if not self.token:
            raise RuntimeError("PADDLEOCR_API_TOKEN 未配置，无法解析图片")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        file_bytes = await asyncio.to_thread(path.read_bytes)
        headers = {"Authorization": f"Bearer {self.token}"}
        optional_payload = {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useTextlineOrientation": False,
        }
        timeout = httpx.Timeout(float(self.timeout_seconds), connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            job_id = await self._submit(
                client,
                headers,
                path.name,
                content_type,
                file_bytes,
                optional_payload,
            )
            jsonl = await self._poll_result(client, headers, job_id)
        page_texts = parse_paddleocr_jsonl(jsonl)
        text = "\n\n".join(value for value in page_texts.values() if value).strip()
        extension = path.suffix.lower().lstrip(".")
        metadata: dict[str, Any] = {
            "parser": "paddleocr",
            "sourceType": extension,
            "page_count": len(page_texts),
            "warnings": [],
        }
        return ParsedDocument(
            source_name=path.name,
            text=text,
            raw_text=text,
            page_texts=page_texts,
            page_raw_texts=page_texts,
            metadata=metadata,
        )

    async def _submit(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        file_name: str,
        content_type: str,
        file_bytes: bytes,
        optional_payload: dict[str, bool],
    ) -> str:
        response = await client.post(
            self.job_url,
            headers=headers,
            data={"model": self.model, "optionalPayload": json.dumps(optional_payload)},
            files={"file": (file_name, file_bytes, content_type)},
        )
        response.raise_for_status()
        payload = response.json()
        _raise_for_api_error(payload, "提交")
        try:
            job_id = str(payload["data"]["jobId"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("PaddleOCR 提交任务响应缺少 jobId") from exc
        if not job_id:
            raise RuntimeError("PaddleOCR 提交任务返回空 jobId")
        return job_id

    async def _poll_result(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        job_id: str,
    ) -> str:
        deadline = monotonic() + self.timeout_seconds
        while monotonic() < deadline:
            response = await client.get(f"{self.job_url}/{job_id}", headers=headers)
            response.raise_for_status()
            try:
                payload = response.json()
                _raise_for_api_error(payload, "查询")
                data = payload["data"]
                state = str(data["state"]).lower()
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("PaddleOCR 查询任务响应格式错误") from exc
            if state == "done":
                total_pages = (data.get("extractProgress") or {}).get("totalPages")
                if str(total_pages or "").isdigit() and int(total_pages) > 50:
                    raise RuntimeError("文件超过限制：单个文件不能超过50页")
                result_url = str((data.get("resultUrl") or {}).get("jsonUrl") or "").strip()
                if not result_url:
                    raise RuntimeError("PaddleOCR 任务完成但未返回 jsonUrl")
                result = await client.get(result_url)
                result.raise_for_status()
                return result.text
            if state == "failed":
                reason = str(data.get("errorMsg") or "未知错误")
                raise RuntimeError(f"PaddleOCR 解析失败：{reason[:500]}")
            await asyncio.sleep(min(self.poll_interval_seconds, max(0.1, deadline - monotonic())))
        raise RuntimeError(f"PaddleOCR 解析超时：{self.timeout_seconds} 秒")


def _raise_for_api_error(payload: object, operation: str) -> None:
    if not isinstance(payload, dict):
        return
    code = payload.get("code")
    if code in (None, 0, "0"):
        return
    message = str(payload.get("msg") or "未知错误").strip()[:500]
    raise RuntimeError(f"PaddleOCR{operation}失败，错误码 {code}：{message}")


def parse_paddleocr_jsonl(content: str) -> dict[int, str]:
    """把 PaddleOCR JSONL 的每条结果转换为按页文本。"""

    pages: dict[int, str] = {}
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("PaddleOCR 返回的 JSONL 格式错误") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        records = result.get("ocrResults") if isinstance(result, dict) else None
        if not isinstance(records, list):
            records = result.get("layoutParsingResults") if isinstance(result, dict) else None
        if not isinstance(records, list):
            records = []
        for record in records:
            page_text = "\n".join(_record_texts(record)).strip()
            if page_text:
                pages[len(pages) + 1] = page_text
    return pages


def _record_texts(record: object) -> list[str]:
    if not isinstance(record, dict):
        return []
    markdown = record.get("markdown")
    if isinstance(markdown, dict):
        text = markdown.get("text")
        if isinstance(text, str) and text.strip():
            return [text.strip()]
    value: object = record
    for key in ("prunedResult", "ocrResult"):
        if isinstance(value, dict) and isinstance(value.get(key), dict):
            value = value[key]
            break
    if not isinstance(value, dict):
        return []
    for key in ("rec_texts", "recTexts", "texts"):
        texts = value.get(key)
        if isinstance(texts, list):
            return [str(text).strip() for text in texts if str(text).strip()]
    for key in ("rec_text", "recText", "text"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return [text.strip()]
    return []
