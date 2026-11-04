"""MinerU 运行方式适配层：隔离本地命令与未来 Docker 服务调用。"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image

@dataclass(frozen=True)
class MinerURunResult:
    """一次 MinerU 运行的最小结果；解析产物由 Runner 写入 output_dir。"""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class MinerURunner(Protocol):
    """MinerU 执行协议；本地进程和 Docker HTTP 客户端均实现此接口。"""

    async def run(
        self,
        *,
        input_path: Path,
        output_dir: Path,
        backend: str,
        method: str,
        lang: str,
        table_enabled: bool,
        timeout_seconds: int,
    ) -> MinerURunResult:
        ...


class LocalMinerURunner:
    """调用当前环境中的 mineru 命令；保持现有本地部署行为。"""

    def __init__(self, command: str) -> None:
        self.command = command

    async def run(
        self,
        *,
        input_path: Path,
        output_dir: Path,
        backend: str,
        method: str,
        lang: str,
        table_enabled: bool,
        timeout_seconds: int,
    ) -> MinerURunResult:
        temporary_dir = tempfile.TemporaryDirectory(prefix="mineru-image-")
        try:
            if input_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
                input_path = _convert_image_to_pdf(input_path, Path(temporary_dir.name))
            args = [
                self.command, "-p", str(input_path), "-o", str(output_dir),
                "-b", backend, "-l", lang, "-t", str(table_enabled).lower(),
            ]
            if method:
                args.extend(["-m", method])
            completed = await asyncio.to_thread(
                subprocess.run,
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise
        except OSError as exc:
            return MinerURunResult(returncode=1, stderr=f"MinerU 本地命令调用失败：{exc}")
        finally:
            temporary_dir.cleanup()
        return MinerURunResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


class ImagePdfHttpMinerURunner:
    """把图片转成 PDF 后交给 HTTP MinerU，避免 HTTP 接口直接接收图片。"""

    def __init__(self, http_runner: MinerURunner) -> None:
        self.http_runner = http_runner

    async def run(
        self,
        *,
        input_path: Path,
        output_dir: Path,
        backend: str,
        method: str,
        lang: str,
        table_enabled: bool,
        timeout_seconds: int,
    ) -> MinerURunResult:
        with tempfile.TemporaryDirectory(prefix="mineru-image-http-") as temporary_dir:
            converted = _convert_image_to_pdf(input_path, Path(temporary_dir))
            return await self.http_runner.run(
                input_path=converted,
                output_dir=output_dir,
                backend=backend,
                method=method,
                lang=lang,
                table_enabled=table_enabled,
                timeout_seconds=timeout_seconds,
            )


def _convert_image_to_pdf(input_path: Path, temporary_dir: Path) -> Path:
    converted = temporary_dir / f"{input_path.stem}.pdf"
    with Image.open(input_path) as image:
        image.convert("RGB").save(converted, format="PDF")
    return converted


class HttpMinerURunner:
    """调用当前 Docker MinerU 的 JSON 接口，并物化项目所需的解析产物。"""

    def __init__(self, base_url: str, *, timeout_seconds: int = 900) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def check_ready(self) -> None:
        """探测解析路由；不要求 Docker 部署额外提供健康检查接口。"""

        async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
            # GET 会被 FastAPI 路由返回 405，但不会上传文件或启动解析任务。
            response = await client.get(f"{self.base_url}/file_parse")
            if response.is_success or response.status_code == 405:
                return
            detail = response.text.strip().replace("\n", " ")[-500:]
            raise RuntimeError(
                f"MinerU 解析路由探测失败 HTTP {response.status_code} "
                f"({self.base_url}/file_parse): {detail or '无响应正文'}"
            )

    async def run(self, *, input_path: Path, output_dir: Path, backend: str,
                  method: str, lang: str, table_enabled: bool,
                  timeout_seconds: int) -> MinerURunResult:
        upload_name = input_path.name
        upload_bytes = input_path.read_bytes()
        content_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        data = {
            "backend": backend, "parse_method": method, "lang": lang,
            "formula_enable": "true", "table_enable": str(table_enabled).lower(),
            "return_md": "true", "return_content_list": "true", "return_images": "true",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(max(1, timeout_seconds), connect=30.0), trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}/file_parse", data=data,
                    files={"file": (upload_name, upload_bytes, content_type)},
                )
        except httpx.HTTPError as exc:
            return MinerURunResult(
                returncode=1,
                stderr=(
                    f"MinerU HTTP 请求失败：{type(exc).__name__}: {exc!s} "
                    f"({exc!r})，URL={self.base_url}/file_parse"
                ),
            )
        if response.status_code >= 400:
            return MinerURunResult(returncode=response.status_code,
                                   stderr=f"MinerU HTTP {response.status_code}: {response.text[-2000:]}")
        try:
            payload = response.json()
            result = _first_result(payload)
            markdown = _remove_nul(_first_text(result, "md_content", "markdown", "md"))
            content_list = _remove_nul(result.get("content_list")) if isinstance(result, dict) else None
            if markdown:
                (output_dir / f"{input_path.stem}.md").write_text(markdown, encoding="utf-8")
            if isinstance(content_list, list):
                (output_dir / f"{input_path.stem}_content_list.json").write_text(
                    json.dumps(content_list, ensure_ascii=False), encoding="utf-8"
                )
            _write_images(output_dir, result.get("images") if isinstance(result, dict) else None)
            if not markdown and not isinstance(content_list, list):
                raise RuntimeError("MinerU JSON 未返回 md_content 或 content_list")
        except (ValueError, OSError, RuntimeError) as exc:
            return MinerURunResult(returncode=1, stderr=f"MinerU 返回结果无法转换为解析产物：{exc}")
        return MinerURunResult(returncode=0)


def _first_result(payload: object) -> dict[str, object]:
    """兼容 MinerU 返回 results={filename: {...}} 或直接返回结果对象。"""

    if not isinstance(payload, dict):
        return {}
    results = payload.get("results")
    if isinstance(results, dict):
        item = next(iter(results.values()), {})
        return item if isinstance(item, dict) else {}
    return payload


def _first_text(result: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _remove_nul(value: object) -> object:
    """清理 MinerU 文本中的 NUL，避免 PostgreSQL/JSON 消费方拒绝内容。"""

    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_remove_nul(item) for item in value]
    if isinstance(value, dict):
        return {_remove_nul(key): _remove_nul(item) for key, item in value.items()}
    return value


def _write_images(output_dir: Path, images: object) -> None:
    """保存旧版接口返回的 data URI 图片，使 Markdown images/ 引用可以被关联。"""

    if not isinstance(images, dict):
        return
    image_dir = output_dir / "images"
    for name, value in images.items():
        image_path = Path(str(name))
        safe_name = image_path.name
        if image_path.is_absolute() or ".." in image_path.parts or not isinstance(value, str) or ";base64," not in value:
            continue
        try:
            encoded = value.split(";base64,", 1)[1]
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / safe_name).write_bytes(base64.b64decode(encoded, validate=True))
        except (ValueError, OSError):
            continue
