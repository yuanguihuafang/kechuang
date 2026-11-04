"""Perception HTTP 适配器；业务 Agent 位于 ``agents.perception_agent``。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, BinaryIO
from urllib.parse import quote, unquote
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from agents.perception_agent.agent import PerceptionAgent
from agents.perception_agent.schemas import (
    PerceptionRequest,
    PerceptionResult,
    TaskSubmitResponse,
    UploadResponse,
)
from runtime.config import get_settings
from runtime.workflows.task_runner import SupervisorTaskRunner
from runtime.workflows.task_store import SupervisorTaskStore
from services.application.document_access import can_delete_attachment
from services.application.document_images import (
    _image_identity_keys,
    _normalize_image_reference,
    hydrate_parse_result_images,
)
from services.document_parsing.mineru_parser import (
    MINERU_OUTPUT_DIR,
    normalize_markdown_image_references,
)
from services.observability.logging import get_logger, log_event, setup_logger, trace_span
from services.observability.metrics import metrics
from services.observability.trace_context import get_trace_context
from services.storage.minio.image_service import perception_image_url
from services.storage.minio.minio_storage import StorageObjectTooLargeError

# 仅使用交付包根目录和当前 Agent 目录；Agent 不依赖总体项目的 backend 包。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parent

# 日志配置
LOG_FILE = PROJECT_ROOT / "logs" / "app.log"
logger = setup_logger("perception", log_path=LOG_FILE)
agent_logger = get_logger("perception", "agent")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_PAGES = 50
STREAM_CHUNK_BYTES = 1024 * 1024
SUPPORTED_EXTENSIONS = {
    "pdf",
    "jpg",
    "jpeg",
    "png",
    "webp",
    "bmp",
    "tif",
    "tiff",
    "docx",
    "pptx",
    "xlsx",
    "md",
    "txt",
}
PPT_UNSUPPORTED_MESSAGE = "暂不支持，请转换为 .pptx 后上传"
UPLOAD_FAILURE_MESSAGE = "文件上传失败，请稍后重试"
PARSE_FAILURE_MESSAGE = "文件解析失败，请稍后重试"


def unsupported_file_message(suffix: str) -> str:
    return PPT_UNSUPPORTED_MESSAGE if suffix.lower() == "ppt" else "不支持的文件类型"


app = APIRouter(tags=["文档解析"])


@dataclass
class _RouteContext:
    agent: PerceptionAgent
    task_store: SupervisorTaskStore
    task_runner: SupervisorTaskRunner | None = None
    storage_service: object | None = None
    identity_resolver: Callable[[str], tuple[str, str, object | None]] | None = None


_route_context: ContextVar[_RouteContext | None] = ContextVar(
    "perception_route_context", default=None
)
_storage_service: object | None = None


def _context() -> _RouteContext:
    context = _route_context.get()
    if context is None:
        context = _RouteContext(PerceptionAgent(), SupervisorTaskStore(service="perception"))
        _route_context.set(context)
    return context


class _ContextProxy:
    def __init__(self, attribute: str) -> None:
        self.attribute = attribute

    def __getattr__(self, name: str):
        return getattr(getattr(_context(), self.attribute), name)


agent = _ContextProxy("agent")
task_store = _ContextProxy("task_store")
MINERU_OUTPUT_ROOT = MINERU_OUTPUT_DIR
HEALTH_TIMEOUT_SECONDS = 4.0
_background_tasks: set[asyncio.Task[object]] = set()
_parse_semaphore: asyncio.Semaphore | None = None
_parse_semaphore_limit: int | None = None


def _parse_limiter() -> asyncio.Semaphore:
    global _parse_semaphore, _parse_semaphore_limit
    limit = get_settings().document_parse_concurrency
    if _parse_semaphore is None or _parse_semaphore_limit != limit:
        # ponytail: 单进程并发上限；多副本部署需改为共享队列限流。
        _parse_semaphore = asyncio.Semaphore(limit)
        _parse_semaphore_limit = limit
    return _parse_semaphore


def configure_task_store(store: object) -> None:
    """由统一网关注入共享任务仓储。"""
    context = _context()
    context.task_store = store


def _create_task(*, operation: str, document_id: str | None = None, status: str = "RUNNING") -> str:
    return _context().task_store.create(
        operation=operation, document_id=document_id, status=status
    )["task_id"]


def configure_task_runner(runner: SupervisorTaskRunner) -> None:
    _context().task_runner = runner


def configure_storage_service(storage: object) -> None:
    """由统一网关注入共享 MinIO 服务。"""
    global _storage_service
    _storage_service = storage


def configure_identity_resolver(resolver: Callable[[str], tuple[str, str, object | None]]) -> None:
    """由统一网关注入身份解析器，供删除接口校验附件归属。"""
    _context().identity_resolver = resolver


def _storage() -> object:
    storage = _storage_service
    if storage is None:
        raise RuntimeError("MinIO 存储服务尚未装配")
    return storage


logger.info(
    "Perception 服务初始化完成",
    extra={
        "structured": {
            "event": "app_init",
            "service": "perception",
        }
    },
)


@dataclass(frozen=True)
class StagedUpload:
    """上传流写入受控临时文件后的摘要。"""

    path: Path
    size: int
    content_hash: str


class UploadTooLargeError(ValueError):
    """分段读取过程中确认上传内容超过限制。"""

    def __init__(self, size: int) -> None:
        super().__init__("文件超过大小限制")
        self.size = size


class DocumentLimitError(ValueError):
    """文件超过解析前置限制。"""

    def __init__(self, message: str, size: int) -> None:
        super().__init__(message)
        self.size = size


def _document_page_count(path: Path) -> int | None:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "pdf":
        import fitz

        with fitz.open(path) as document:
            return int(document.page_count)
    if suffix == "pptx":
        try:
            with ZipFile(path) as archive:
                return sum(
                    name.startswith("ppt/slides/slide") and name.endswith(".xml")
                    for name in archive.namelist()
                )
        except BadZipFile:
            raise ValueError("PPTX 文件损坏") from None
    return None


def validate_document_limits(path: Path, file_size: int) -> None:
    if file_size > MAX_UPLOAD_BYTES:
        raise DocumentLimitError("文件超过限制：单个文件不能超过50MB", file_size)
    page_count = _document_page_count(path)
    if page_count is not None and page_count > MAX_DOCUMENT_PAGES:
        raise DocumentLimitError(
            f"文件超过限制：PDF/PPT最多支持50页，当前文件有{page_count}页",
            file_size,
        )


def _copy_stream_to_path(
    source: BinaryIO,
    target: Path,
    *,
    max_bytes: int,
) -> StagedUpload:
    """使用固定大小缓冲区复制文件流，并同步计算 SHA-256。"""

    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as output:
        while True:
            chunk = source.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise UploadTooLargeError(size)
            output.write(chunk)
            digest.update(chunk)
    return StagedUpload(path=target, size=size, content_hash=digest.hexdigest())


async def _stage_upload(file: UploadFile, directory: Path, file_name: str) -> StagedUpload:
    """把 Starlette 上传对象分段写入指定临时目录。"""

    target = directory / file_name
    await file.seek(0)
    return await asyncio.to_thread(
        _copy_stream_to_path,
        file.file,
        target,
        max_bytes=MAX_UPLOAD_BYTES,
    )


async def _run_agent_path(
    path: Path,
    *,
    file_name: str,
    document_id: str,
    storage_key: str | None,
    knowledge_type: str | None = None,
    extract_draft: bool = False,
) -> PerceptionResult:
    """调用 Perception Agent 的正式路径解析接口。"""

    kwargs = {
        "path": path,
        "file_name": file_name,
        "document_id": document_id,
        "storage_key": storage_key,
        "extract_draft": extract_draft,
    }
    if knowledge_type:
        kwargs["knowledge_type"] = knowledge_type
    async with _parse_limiter():
        return await agent.run_path(**kwargs)


async def persist_image_assets(result: PerceptionResult) -> PerceptionResult:
    """将完整 MinerU 产物与图片关联清单持久化到 MinIO。"""

    if not result.document_id:
        return result
    images = result.metadata.get("images") if result.metadata else None
    if not isinstance(images, list):
        return result
    storage = _storage()
    artifact_keys = await persist_mineru_artifacts(result, storage)
    result.metadata.setdefault("artifact_refs", {}).update(artifact_keys)
    manifest: list[dict[str, object]] = []
    for asset in images:
        if not isinstance(asset, dict) or not asset.get("image_id"):
            continue
        relative_path = str(asset.get("relativePath", "")).replace("\\", "/")
        storage_key = artifact_keys.get(relative_path)
        if not storage_key:
            continue
        image_id = str(asset["image_id"])
        asset["minio_object_key"] = storage_key
        asset["url"] = perception_image_url(result.document_id, image_id)
        manifest.append(
            {
                "image_id": image_id,
                "document_id": result.document_id,
                "page_number": asset.get("pageNumber"),
                "source_locator": asset.get("source_locator"),
                "nearby_block_ids": asset.get("nearby_block_ids", []),
                "caption": asset.get("caption"),
                "image_role": asset.get("imageRole", "candidate"),
                "content_hash": asset.get("contentHash"),
                "minio_object_key": storage_key,
                "content_type": asset.get("contentType", "application/octet-stream"),
            }
        )
    # 重新解析同一 document_id 时也要覆盖为空清单，避免 Retrieval 返回过期图片。
    await storage.save_image_manifest(result.document_id, manifest)
    for field in ("raw_text", "text"):
        text = getattr(result, field, None)
        if isinstance(text, str):
            setattr(
                result,
                field,
                normalize_markdown_image_references(text, images),
            )
    return result


def _contains_image_reference(text: str, reference: str) -> bool:
    """按完整路径匹配，避免同名图片或相似路径被误判为仍在正文中。"""
    if not reference:
        return False
    return (
        re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(reference)}(?![A-Za-z0-9_.-])",
            text,
        )
        is not None
    )


def _prune_removed_parse_images(
    raw_text: str,
    current: dict[str, object],
    image_manifest: list[dict[str, object]],
    artifact_manifest: list[dict[str, object]],
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]], list[str]]:
    """按保存后的 Markdown 图片引用同步清单，返回需要从 MinIO 删除的对象键。"""

    metadata = current.get("metadata")
    if not isinstance(metadata, dict):
        return {}, image_manifest, artifact_manifest, []
    images = metadata.get("images")
    if not isinstance(images, list):
        return dict(metadata), image_manifest, artifact_manifest, []
    try:
        normalized_text = unquote(raw_text).replace("\\", "/")
    except Exception:  # noqa: BLE001 - 文本异常时按原文判断
        normalized_text = raw_text.replace("\\", "/")
    artifact_by_key = {
        str(item.get("minio_object_key")): item
        for item in artifact_manifest
        if isinstance(item, dict) and item.get("minio_object_key")
    }
    artifact_refs = metadata.get("artifact_refs")
    if isinstance(artifact_refs, dict):
        for relative_path, storage_key in artifact_refs.items():
            artifact_by_key.setdefault(
                str(storage_key), {"relative_path": relative_path, "minio_object_key": storage_key}
            )
    manifest_by_id = {
        str(item.get("image_id")): item
        for item in image_manifest
        if isinstance(item, dict) and item.get("image_id")
    }
    retained_ids: set[str] = set()
    retained_object_keys: set[str] = set()
    removed_keys: set[str] = set()
    processed_ids: set[str] = set()

    for image in images:
        if not isinstance(image, dict):
            continue
        image_id = str(image.get("image_id") or "")
        if image_id:
            processed_ids.add(image_id)
        manifest = manifest_by_id.get(image_id, {})
        object_key = str(image.get("minio_object_key") or manifest.get("minio_object_key") or "")
        references = {
            _normalize_image_reference(image.get(field))
            for field in ("relativePath", "relative_path", "path", "url", "image_url")
            if image.get(field)
        }
        artifact = artifact_by_key.get(object_key)
        if artifact and artifact.get("relative_path"):
            references.add(_normalize_image_reference(artifact["relative_path"]))
        if not references or any(
            _contains_image_reference(normalized_text, reference) for reference in references
        ):
            if image_id:
                retained_ids.add(image_id)
            if object_key:
                retained_object_keys.add(object_key)
            continue
        if object_key:
            removed_keys.add(object_key)

    # 同一 MinIO 对象可能对应多个旧 image_id；只要仍有一个正文引用，就不能删除该对象。
    removed_keys.difference_update(retained_object_keys)

    # 兼容旧解析结果：图片元数据缺失时，仍按图片清单和产物路径清理对象。
    for item in image_manifest:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("image_id") or "")
        object_key = str(item.get("minio_object_key") or "")
        if not object_key or image_id in processed_ids:
            continue
        artifact = artifact_by_key.get(object_key)
        reference = _normalize_image_reference(artifact.get("relative_path")) if artifact else ""
        if reference and not _contains_image_reference(normalized_text, reference):
            removed_keys.add(object_key)

    retained_images: list[dict[str, object]] = []
    seen_image_keys: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            continue
        image_id = str(image.get("image_id") or "")
        object_key = str(
            image.get("minio_object_key")
            or manifest_by_id.get(image_id, {}).get("minio_object_key")
            or ""
        )
        if object_key in removed_keys:
            continue
        if image_id and image_id not in retained_ids:
            continue
        identities = _image_identity_keys(image, manifest_by_id.get(image_id))
        if any(identity in seen_image_keys for identity in identities):
            continue
        seen_image_keys.update(identities)
        retained_images.append(image)

    retained_manifest: list[dict[str, object]] = []
    seen_manifest_keys: set[str] = set()
    for item in image_manifest:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("image_id") or "")
        object_key = str(item.get("minio_object_key") or "")
        if image_id not in retained_ids and object_key in removed_keys:
            continue
        identities = _image_identity_keys(item)
        if any(identity in seen_manifest_keys for identity in identities):
            continue
        seen_manifest_keys.update(identities)
        retained_manifest.append(item)
    retained_keys = {
        str(item.get("minio_object_key"))
        for item in retained_manifest
        if isinstance(item, dict) and item.get("minio_object_key")
    }
    removed_keys.difference_update(retained_keys)
    retained_artifacts = [
        item
        for item in artifact_manifest
        if not isinstance(item, dict) or str(item.get("minio_object_key") or "") not in removed_keys
    ]
    updated_metadata = dict(metadata)
    updated_metadata["images"] = retained_images
    if isinstance(artifact_refs, dict):
        updated_metadata["artifact_refs"] = {
            key: value for key, value in artifact_refs.items() if str(value) not in removed_keys
        }
    return updated_metadata, retained_manifest, retained_artifacts, sorted(removed_keys)


async def persist_parse_result(result: PerceptionResult) -> PerceptionResult:
    """保存结构化解析结果，避免页面刷新后只能看到文件而无法恢复 blocks。"""

    if result.document_id and result.status == "SUCCESS":
        storage = _storage()
        save_parse_result = getattr(storage, "save_parse_result", None)
        if save_parse_result is not None:
            parse_result_key = await save_parse_result(
                result.document_id, result.model_dump(mode="json")
            )
            result.metadata["parse_result_key"] = parse_result_key
    return result


async def persist_mineru_artifacts(result: PerceptionResult, storage: object) -> dict[str, str]:
    """上传 MinerU 产物到 MinIO；失败时只回滚本次上传对象。"""

    output_dir = Path(str(result.metadata.get("outputDir", ""))).resolve()
    if not output_dir.is_dir() or MINERU_OUTPUT_ROOT not in output_dir.parents:
        return {}
    attempt_id = uuid.uuid4().hex
    artifact_keys: dict[str, str] = {}
    artifacts: list[dict[str, object]] = []
    uploaded_keys: list[str] = []
    previous_artifacts: list[dict[str, object]] = []
    load_manifest = getattr(storage, "load_mineru_artifact_manifest", None)
    try:
        if callable(load_manifest):
            previous_artifacts = await load_manifest(result.document_id)
        for artifact_path in sorted(path for path in output_dir.rglob("*") if path.is_file()):
            relative_path = artifact_path.relative_to(output_dir).as_posix()
            content_type = mimetypes.guess_type(artifact_path.name)[0] or "application/octet-stream"
            storage_key = await storage.upload_mineru_artifact(
                result.document_id,
                relative_path,
                artifact_path,
                content_type,
                attempt_id=attempt_id,
            )
            artifact_keys[relative_path] = storage_key
            uploaded_keys.append(storage_key)
            artifacts.append(
                {
                    "relative_path": relative_path,
                    "minio_object_key": storage_key,
                    "content_type": content_type,
                    "size_bytes": artifact_path.stat().st_size,
                }
            )
        manifest_key = await storage.save_mineru_artifact_manifest(result.document_id, artifacts)
        result.metadata["artifact_manifest_key"] = manifest_key
        previous_keys = {
            str(item.get("minio_object_key"))
            for item in previous_artifacts
            if isinstance(item, dict) and item.get("minio_object_key")
        }
        obsolete_keys = sorted(previous_keys.difference(uploaded_keys))
        if obsolete_keys:
            try:
                await storage.delete_derived_objects(result.document_id, obsolete_keys)
            except Exception:  # noqa: BLE001 - 新清单已提交，旧产物清理失败交给孤儿清理
                agent_logger.exception(
                    "旧 MinerU 产物清理失败: document_id=%s, count=%s",
                    result.document_id,
                    len(obsolete_keys),
                )
        return artifact_keys
    except Exception:
        if uploaded_keys:
            try:
                await storage.delete_derived_objects(result.document_id, uploaded_keys)
            except Exception:  # noqa: BLE001 - 保留原始上传异常，后续由孤儿清理兜底
                agent_logger.exception(
                    "MinerU 产物回滚失败: document_id=%s, count=%s",
                    result.document_id,
                    len(uploaded_keys),
                )
        raise
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def attach_image_urls(result: PerceptionResult) -> PerceptionResult:
    """保留 MinerU 原始图片路径，同时增加受控的前端访问 URL。"""

    images = result.metadata.get("images") if result.metadata else None
    if not isinstance(images, list):
        return result
    for asset in images:
        if not isinstance(asset, dict):
            continue
        if asset.get("url"):
            continue
        image_path = Path(str(asset.get("path", ""))).resolve()
        if image_path.is_file() and MINERU_OUTPUT_ROOT in image_path.parents:
            relative_path = image_path.relative_to(MINERU_OUTPUT_ROOT).as_posix()
            asset["url"] = f"/api/v1/perception/assets/{quote(relative_path, safe='/')}"
    return result


def finish_parse_task(task_id: str, result: PerceptionResult) -> PerceptionResult:
    task_store.complete(
        task_id,
        status="SUCCEEDED" if result.status == "SUCCESS" else "FAILED",
        stage="completed" if result.status == "SUCCESS" else "failed",
        result_status=result.status,
        source_name=result.source_name,
        result_ref=(result.metadata or {}).get("artifact_manifest_key") or result.storage_key,
        error=result.error,
    )
    return result.model_copy(update={"task_id": task_id})


def _queue(coroutine: object, *, task_id: str, operation: str) -> None:
    if _context().task_runner is not None:
        _context().task_runner.submit(task_id, operation, lambda: coroutine)  # type: ignore[arg-type]
        return
    task = asyncio.create_task(coroutine)  # type: ignore[arg-type]
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_async_parse(task_id: str, operation: str, action: object) -> dict[str, object]:
    # 异步解析必须先写 RUNNING，终态再写入同一个平台任务记录，前端只读任务系统。
    task_store.update(task_id, status="RUNNING", stage="executing", progress=5)
    try:
        result = await action  # type: ignore[misc]
        task = task_store.update(
            task_id,
            status="SUCCEEDED" if result.status == "SUCCESS" else "FAILED",
            progress=100,
            stage="completed" if result.status == "SUCCESS" else "failed",
            result_status=result.status,
            source_name=result.source_name,
            error=result.error,
        )
        return task or {}
    except Exception as exc:  # noqa: BLE001 - 异步任务状态必须持久化失败原因
        task = task_store.update(
            task_id, status="FAILED", progress=100, stage="failed", error=PARSE_FAILURE_MESSAGE
        )
        log_event(
            agent_logger,
            "async_task_failed",
            task_id=task_id,
            operation=operation,
            error_type=type(exc).__name__,
        )
        return task or {}


@app.post(
    "/api/v1/perception/upload",
    response_model=UploadResponse,
    summary="上传解析文件",
    description="上传文件并创建解析任务。",
)
async def upload_document(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI multipart 表单声明
    scope_id: str = Form(default="public"),
    user_id: str = Form(default=""),
    new_version: bool = Form(default=False),
    document_id: str | None = Form(default=None),
) -> UploadResponse:
    """Perception 独立测试入口：保存原始文件，不解析、不建立向量索引。"""

    if type(scope_id).__name__ == "Form":
        scope_id = str(getattr(scope_id, "default", "public") or "public")
    if type(user_id).__name__ == "Form":
        user_id = str(getattr(user_id, "default", "") or "")
    if type(new_version).__name__ == "Form":
        new_version = bool(getattr(new_version, "default", False))
    if type(document_id).__name__ == "Form":
        document_id = getattr(document_id, "default", None)
    file_name = Path(file.filename or "").name
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    user_id = user_id.strip()
    log_event(
        agent_logger,
        "upload_start",
        file_name=file_name,
        scope_id=scope_id,
        user_id=user_id or None,
    )
    if file_name != (file.filename or "") or suffix not in SUPPORTED_EXTENSIONS:
        error = (
            unsupported_file_message(suffix)
            if suffix == "ppt"
            else "不支持的文件类型或文件名不合法"
        )
        return UploadResponse(
            status="FAILED",
            document_id="",
            storage_key="",
            file_name=file_name,
            file_size=0,
            error=error,
        )

    file_size = 0
    try:
        with tempfile.TemporaryDirectory(prefix="kechuang-upload-") as temp_dir:
            staged = await _stage_upload(file, Path(temp_dir), file_name)
            file_size = staged.size
            if not file_size:
                return UploadResponse(
                    status="FAILED",
                    document_id="",
                    storage_key="",
                    file_name=file_name,
                    file_size=0,
                    error="上传文件为空",
                )
            validate_document_limits(staged.path, file_size)

            storage = _storage()
            existing: list[dict[str, object]] = []
            if hasattr(storage, "find_by_content_hash"):
                existing = await storage.find_by_content_hash(
                    staged.content_hash, scope_id=scope_id, user_id=user_id or None
                )
            if existing and not new_version:
                previous = max(existing, key=lambda item: int(item.get("version") or 1))
                return UploadResponse(
                    status="SUCCESS",
                    document_id=str(previous.get("document_id") or ""),
                    storage_key=str(previous.get("storage_key") or ""),
                    file_name=file_name,
                    file_size=int(previous.get("file_size") or file_size),
                    content_hash=staged.content_hash,
                    duplicate=True,
                    version=int(previous.get("version") or 1),
                    version_id=(
                        f"{previous.get('document_id')}:v{int(previous.get('version') or 1)}"
                        if previous.get("document_id")
                        else None
                    ),
                )
            logical_document_id = (
                document_id or f"doc_{uuid.uuid4().hex[:8]}_{staged.content_hash[:16]}"
            )
            # 普通资料不保留历史版本；同一 document_id 始终覆盖同一个原始对象。
            version = 1
            storage_key, _ = await storage.upload_path(
                staged.path,
                file_name,
                file.content_type,
                content_hash=staged.content_hash,
                document_id=logical_document_id,
                scope_id=scope_id,
                user_id=user_id or None,
                version=version,
            )
            log_event(
                agent_logger,
                "upload_complete",
                document_id=logical_document_id,
                storage_key=storage_key,
                size_bytes=file_size,
            )
            version_id = f"{logical_document_id}:v{version}"
            return UploadResponse(
                status="SUCCESS",
                document_id=logical_document_id,
                storage_key=storage_key,
                file_name=file_name,
                file_size=file_size,
                content_hash=staged.content_hash,
                version=version,
                version_id=version_id,
            )
    except (UploadTooLargeError, DocumentLimitError) as exc:
        return UploadResponse(
            status="FAILED",
            document_id="",
            storage_key="",
            file_name=file_name,
            file_size=exc.size,
            error=str(exc),
        )
    except Exception:  # noqa: BLE001 - 上传边界返回统一失败状态
        agent_logger.exception("文件上传失败")
        return UploadResponse(
            status="FAILED",
            document_id="",
            storage_key="",
            file_name=file_name,
            file_size=file_size,
            error=UPLOAD_FAILURE_MESSAGE,
        )


@app.get(
    "/api/v1/perception/files",
    summary="获取已上传文件",
    description="获取当前用户已上传的文件列表。",
)
async def list_uploaded_files(limit: int = Query(default=200, ge=1, le=500)) -> dict[str, object]:
    """从 MinIO 读取真实原始文件清单。"""
    log_event(agent_logger, "file_list_request", limit=limit)
    files = await _storage().list_files(limit)
    log_event(agent_logger, "file_list_complete", count=len(files))
    return {"files": files}


@app.get(
    "/api/v1/perception/files/{document_id}/parse-result",
    summary="获取文件解析结果",
    description="获取指定文件的解析文本和结构化结果。",
)
async def get_parse_result(document_id: str) -> dict[str, object]:
    """读取已完成文档的结构化解析结果，供材料处理页恢复确认预览。"""

    result = await _storage().load_parse_result(document_id)
    if result is None:
        raise HTTPException(status_code=404, detail="当前文档没有已保存的解析结果")
    # 解析结果可能是在图片 URL 规则变更前保存的；以当前图片清单重新补齐受控地址。
    return await hydrate_parse_result_images(document_id, result, _storage())


@app.post(
    "/api/v1/perception/files/{document_id}/parse-result/normalize-images",
    summary="整理解析原文图片引用",
)
async def normalize_parse_result_images(document_id: str) -> dict[str, object]:
    """直接整理已保存原文中的重复图片引用，供历史解析结果修复使用。"""

    storage = _storage()
    current = await storage.load_parse_result(document_id)
    if current is None:
        raise HTTPException(status_code=404, detail="当前文档没有已保存的解析结果")
    raw_text = str(current.get("raw_text") or current.get("text") or "")
    metadata = current.get("metadata")
    images = metadata.get("images", []) if isinstance(metadata, dict) else []
    normalized_text = normalize_markdown_image_references(
        raw_text,
        images if isinstance(images, list) else [],
    )
    updated = {**current, "raw_text": normalized_text, "text": normalized_text}
    await storage.save_parse_result(document_id, updated)
    log_event(
        agent_logger,
        "parse_result_images_normalized",
        document_id=document_id,
        changed=normalized_text != raw_text,
        text_length=len(normalized_text),
    )
    return {
        "document_id": document_id,
        "changed": normalized_text != raw_text,
        "raw_text": normalized_text,
    }


@app.put(
    "/api/v1/perception/files/{document_id}/parse-result",
    summary="保存文件解析结果",
    description="保存指定文件的解析结果。",
)
async def update_parse_result(
    document_id: str, payload: dict[str, object] = Body(...)
) -> dict[str, object]:  # noqa: B008
    """保存用户编辑后的解析原文，并同步清理已删除的解析图片。"""

    raw_text = payload.get("raw_text")
    if not isinstance(raw_text, str):
        raise HTTPException(status_code=400, detail="解析原文格式不正确")
    current = await _storage().load_parse_result(document_id)
    if current is None:
        raise HTTPException(status_code=404, detail="当前文档没有已保存的解析结果")
    storage = _storage()
    load_images = getattr(storage, "load_image_manifest", None)
    load_artifacts = getattr(storage, "load_mineru_artifact_manifest", None)
    image_manifest = await load_images(document_id) if callable(load_images) else []
    artifact_manifest = await load_artifacts(document_id) if callable(load_artifacts) else []
    had_artifact_manifest = bool(artifact_manifest)
    metadata, image_manifest, artifact_manifest, removed_keys = _prune_removed_parse_images(
        raw_text, current, image_manifest, artifact_manifest
    )
    raw_text = normalize_markdown_image_references(
        raw_text,
        metadata.get("images", []) if isinstance(metadata, dict) else [],
    )
    updated = {**current, "raw_text": raw_text, "text": raw_text, "edited": True}
    saved_chunks = payload.get("saved_chunks")
    if isinstance(saved_chunks, list):
        updated["saved_chunks"] = [item for item in saved_chunks if isinstance(item, dict)]
    elif payload.get("clear_saved_chunks"):
        updated.pop("saved_chunks", None)
    if metadata:
        updated["metadata"] = metadata
    delete_objects = getattr(storage, "delete_derived_objects", None)
    deleted_count = (
        await delete_objects(document_id, removed_keys)
        if callable(delete_objects) and removed_keys
        else 0
    )
    if callable(load_images):
        await storage.save_image_manifest(document_id, image_manifest)
    if callable(load_artifacts) and had_artifact_manifest:
        await storage.save_mineru_artifact_manifest(document_id, artifact_manifest)
    await storage.save_parse_result(document_id, updated)
    log_event(
        agent_logger,
        "parse_result_updated",
        document_id=document_id,
        text_length=len(raw_text),
        deleted_image_count=deleted_count,
    )
    return updated


@app.post(
    "/api/v1/perception/parse",
    response_model=PerceptionResult,
    summary="解析文件",
    description="解析已上传的文件内容。",
)
async def parse_document(request: PerceptionRequest) -> PerceptionResult:
    """解析本地文件。"""
    task_id = _create_task(operation="parse", document_id=request.document_id)
    log_event(agent_logger, "parse_request", file_path=request.file_path)
    try:
        source_path = Path(request.file_path)
        validate_document_limits(source_path, source_path.stat().st_size)
        with metrics.stage("perception.parse"):
            result = await agent.run(request)
        log_event(agent_logger, "parse_complete", status=result.status)
        task_store.update(task_id, stage="persist_artifacts", progress=80)
        final_result = attach_image_urls(await persist_image_assets(result))
        return finish_parse_task(task_id, await persist_parse_result(final_result))
    except DocumentLimitError as exc:
        return finish_parse_task(
            task_id,
            PerceptionResult(
                status="FAILED",
                task_id=task_id,
                source_name=Path(request.file_path).name,
                document_id=request.document_id,
                storage_key=request.storage_key,
                error=str(exc),
            ),
        )
    except Exception:
        task_store.update(
            task_id, status="FAILED", progress=100, stage="failed", error=PARSE_FAILURE_MESSAGE
        )
        agent_logger.exception("解析失败")
        raise


@app.post(
    "/api/v1/perception/parse-upload",
    response_model=PerceptionResult,
    summary="上传并解析文件",
    description="上传文件并同步返回解析结果。",
)
async def parse_uploaded_document(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI multipart 表单声明
    document_id: str = Form(...),
    storage_key: str | None = Form(default=None),
    knowledge_type: str = Form(default=""),
) -> PerceptionResult:
    """接收上游编排层提交的文件并调用当前文档解析器。"""
    original_name = file.filename or ""
    file_name = Path(original_name).name
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    task_id = _create_task(operation="parse_upload", document_id=document_id)

    log_event(agent_logger, "file_parse_start", document_id=document_id, file_name=file_name)

    if file_name != original_name or suffix not in SUPPORTED_EXTENSIONS:
        log_event(
            agent_logger, "parse_upload_reject", reason="unsupported_file_type", suffix=suffix
        )
        return finish_parse_task(
            task_id,
            PerceptionResult(
                status="FAILED",
                task_id=task_id,
                source_name=file_name,
                document_id=document_id,
                storage_key=storage_key,
                error=unsupported_file_message(suffix),
            ),
        )

    try:
        with tempfile.TemporaryDirectory(prefix="kechuang-parse-upload-") as temp_dir:
            staged = await _stage_upload(file, Path(temp_dir), file_name)
            if not staged.size:
                return finish_parse_task(
                    task_id,
                    PerceptionResult(
                        status="FAILED",
                        task_id=task_id,
                        source_name=file_name,
                        document_id=document_id,
                        storage_key=storage_key,
                        error="上传文件为空",
                    ),
                )
            validate_document_limits(staged.path, staged.size)
            with metrics.stage("perception.parse"):
                result = await _run_agent_path(
                    staged.path,
                    file_name=file_name,
                    document_id=document_id,
                    storage_key=storage_key,
                    knowledge_type=knowledge_type,
                )
        parser_name = str((result.metadata or {}).get("parser") or "document")
        log_event(agent_logger, "document_parse_complete", status=result.status, parser=parser_name)
        task_store.update(task_id, stage="persist_artifacts", progress=80)
        final_result = attach_image_urls(await persist_image_assets(result))
        return finish_parse_task(task_id, await persist_parse_result(final_result))
    except (UploadTooLargeError, StorageObjectTooLargeError, DocumentLimitError) as exc:
        log_event(agent_logger, "parse_upload_reject", reason="file_too_large", size=exc.size)
        return finish_parse_task(
            task_id,
            PerceptionResult(
                status="FAILED",
                task_id=task_id,
                source_name=file_name,
                document_id=document_id,
                storage_key=storage_key,
                error=str(exc),
            ),
        )
    except Exception:
        task_store.update(
            task_id, status="FAILED", progress=100, stage="failed", error=PARSE_FAILURE_MESSAGE
        )
        agent_logger.exception("解析失败")
        raise


@app.post(
    "/api/v1/perception/parse-stored",
    response_model=PerceptionResult,
    summary="解析已存文件",
    description="解析存储中的指定文件。",
)
async def parse_stored_document(
    document_id: str = Form(...),
    storage_key: str = Form(...),
    file_name: str = Form(default=""),
    knowledge_type: str = Form(default=""),
    extract_draft: Annotated[bool, Form()] = False,
) -> PerceptionResult:
    """读取 MinIO 中已上传的原始文件并重新解析。"""

    task_id = _create_task(operation="parse_stored", document_id=document_id)
    log_event(
        agent_logger,
        "file_parse_start",
        task_id=task_id,
        document_id=document_id,
        storage_key=storage_key,
        file_name=file_name,
    )
    try:
        storage = _storage()
        with tempfile.TemporaryDirectory(prefix="kechuang-parse-stored-") as temp_dir:
            directory = Path(temp_dir)
            local_path, _, stored_name, file_size = await storage.download_file_to_directory(
                storage_key,
                directory,
                MAX_UPLOAD_BYTES,
            )
            resolved_name = Path(file_name or stored_name).name
            suffix = resolved_name.rsplit(".", 1)[-1].lower() if "." in resolved_name else ""
            if local_path.name != resolved_name:
                local_path = local_path.rename(directory / resolved_name)
            log_event(
                agent_logger,
                "file_ready",
                task_id=task_id,
                document_id=document_id,
                storage_key=storage_key,
                file_name=resolved_name,
                stored_file_name=stored_name,
                size_bytes=file_size,
                suffix=suffix,
            )
            if suffix not in SUPPORTED_EXTENSIONS:
                log_event(
                    agent_logger,
                    "parse_stored_reject",
                    task_id=task_id,
                    document_id=document_id,
                    reason="unsupported_file_type",
                    suffix=suffix,
                )
                return finish_parse_task(
                    task_id,
                    PerceptionResult(
                        status="FAILED",
                        task_id=task_id,
                        source_name=resolved_name,
                        document_id=document_id,
                        storage_key=storage_key,
                        error=unsupported_file_message(suffix),
                    ),
                )
            validate_document_limits(local_path, file_size)
            with metrics.stage("perception.parse"):
                result = await _run_agent_path(
                    local_path,
                    file_name=resolved_name,
                    document_id=document_id,
                    storage_key=storage_key,
                    knowledge_type=knowledge_type,
                    extract_draft=extract_draft,
                )
        metadata = result.metadata or {}
        parser_name = str(metadata.get("parser") or "document")
        log_event(
            agent_logger,
            "document_parse_complete",
            task_id=task_id,
            document_id=document_id,
            status=result.status,
            error=result.error,
            block_count=len(result.blocks or []),
            image_count=len(metadata.get("images", []))
            if isinstance(metadata.get("images"), list)
            else 0,
            parser=parser_name,
            extract_draft=extract_draft,
            draft_payload_present=bool(metadata.get("draft_payload")),
        )
        agent_logger.info(
            "正在清洗原文",
            extra={
                "structured": {
                    "event": "source_clean_start",
                    "task_id": task_id,
                    "document_id": document_id,
                }
            },
        )
        task_store.update(task_id, stage="persist_artifacts", progress=80)
        final_result = attach_image_urls(await persist_image_assets(result))
        final_result = finish_parse_task(task_id, await persist_parse_result(final_result))
        agent_logger.info(
            "原文清洗完成",
            extra={
                "structured": {
                    "event": "source_clean_complete",
                    "task_id": task_id,
                    "document_id": document_id,
                }
            },
        )
        log_event(
            agent_logger,
            "file_parse_complete",
            task_id=task_id,
            document_id=document_id,
            status=final_result.status,
            error=final_result.error,
        )
        return final_result
    except (UploadTooLargeError, StorageObjectTooLargeError, DocumentLimitError) as exc:
        return finish_parse_task(
            task_id,
            PerceptionResult(
                status="FAILED",
                task_id=task_id,
                source_name=Path(file_name).name,
                document_id=document_id,
                storage_key=storage_key,
                error=str(exc),
            ),
        )
    except Exception as exc:
        task_store.update(
            task_id, status="FAILED", progress=100, stage="failed", error=PARSE_FAILURE_MESSAGE
        )
        agent_logger.exception(
            "MinIO 文件解析失败",
            extra={
                "structured": {
                    "event": "parse_stored_failed",
                    "task_id": task_id,
                    "document_id": document_id,
                    "storage_key": storage_key,
                    "error_type": type(exc).__name__,
                }
            },
        )
        raise


@app.post(
    "/api/v1/perception/parse-stored/async",
    response_model=TaskSubmitResponse,
    status_code=202,
    summary="异步解析已存文件",
    description="提交存储文件的异步解析任务。",
)
async def parse_stored_document_async(
    document_id: str = Form(...),
    storage_key: str = Form(...),
    file_name: str = Form(default=""),
    knowledge_type: str = Form(default=""),
) -> TaskSubmitResponse:
    """异步读取 MinIO 对象并解析，结果通过任务接口查询。"""
    task_id = _create_task(operation="parse_stored", document_id=document_id, status="QUEUED")
    _queue(
        _run_async_parse(
            task_id,
            "parse_stored",
            parse_stored_document(
                document_id=document_id,
                storage_key=storage_key,
                file_name=file_name,
                knowledge_type=knowledge_type,
            ),
        ),
        task_id=task_id,
        operation="parse_stored",
    )
    return TaskSubmitResponse(
        task_id=task_id,
        operation="parse_stored",
        document_id=document_id,
        storage_key=storage_key,
        status_url=f"/api/v1/perception/tasks/{task_id}",
    )


async def delete_stored_document_internal(
    storage_key: str,
    document_id: str | None = None,
) -> dict[str, str]:
    """供进程内业务流程删除原始文件和解析产物，不经过 HTTP 参数绑定。"""
    await _storage().delete_document(storage_key, document_id)
    return {"status": "DELETED", "storage_key": storage_key}


@app.delete(
    "/api/v1/perception/files",
    summary="删除上传文件",
    description="删除文件原文及其解析产物；外部用户只能删除自己上传的文件。",
)
async def delete_stored_document(
    request: Request,
    storage_key: str = Query(...),
    document_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None, min_length=1, max_length=64),
) -> dict[str, str]:
    """删除 MinIO 原始文件和该文档的解析产物。"""

    requested_user_id = str(getattr(request.state, "user_id", "") or user_id or "").strip()
    if requested_user_id != "admin":
        resolver = _context().identity_resolver
        if resolver is None:
            raise HTTPException(status_code=503, detail="用户身份服务尚未配置")
        requested_user_id, role, _profile = resolver(requested_user_id)
        if role == "external":
            records = _context().task_store.list(limit=10000)
            if not can_delete_attachment(
                user_role=role,
                user_id=requested_user_id,
                document_id=str(document_id or ""),
                storage_key=storage_key,
                records=records,
            ):
                raise HTTPException(status_code=403, detail="外部用户只能删除自己上传的附件")

    log_event(agent_logger, "file_delete_request", storage_key=storage_key, document_id=document_id)
    await delete_stored_document_internal(storage_key, document_id)
    log_event(
        agent_logger, "file_delete_complete", storage_key=storage_key, document_id=document_id
    )
    if requested_user_id:
        try:
            from services.persistence.repositories.observability_repository import PostgresAuditSink

            PostgresAuditSink().record(
                "user_attachment_deleted",
                service="governance",
                user_id=requested_user_id,
                document_id=document_id,
                storage_key=storage_key,
                status="DELETED",
                summary="用户删除附件",
            )
        except Exception:  # noqa: BLE001 - 审计失败不阻断文件删除
            pass
    return {"status": "DELETED", "storage_key": storage_key}


async def delete_original_file(storage_key: str) -> dict[str, str]:
    """替换版本专用：只删旧原文件，不删除最新解析产物。"""
    await _storage().delete_original_file(storage_key)
    return {"status": "DELETED", "storage_key": storage_key}


@app.get(
    "/api/v1/perception/assets/{relative_path:path}",
    summary="获取解析资源",
    description="获取解析过程中生成的图片等资源。",
)
async def get_mineru_asset(relative_path: str) -> FileResponse:
    """只提供项目 MinerU 输出目录中的提取图片。"""

    asset_path = (MINERU_OUTPUT_ROOT / relative_path).resolve()
    if MINERU_OUTPUT_ROOT not in asset_path.parents or not asset_path.is_file():
        raise HTTPException(status_code=404, detail="解析图片不存在")
    media_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
    return FileResponse(asset_path, media_type=media_type, filename=asset_path.name)


@app.get(
    "/api/v1/perception/images/{document_id}/{image_id}",
    summary="获取解析图片",
    description="获取指定文档解析生成的图片。",
)
async def get_persisted_image(document_id: str, image_id: str) -> Response:
    """受控读取 MinIO 中已持久化的 MinerU 图片。"""

    try:
        content, media_type, file_name = await _storage().download_derived_image(
            document_id, image_id
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="解析图片不存在") from None
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{file_name}"'},
    )


@app.get(
    "/api/v1/perception/tasks/{task_id}",
    summary="查询解析任务",
    description="查询文件解析任务状态和结果。",
)
async def get_parse_task(task_id: str) -> dict[str, object]:
    """查询已持久化的解析任务状态。"""

    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="解析任务不存在")
    return task


async def _run_readiness_probe(
    name: str,
    probe: Callable[[], Awaitable[None]],
) -> tuple[str, dict[str, str]]:
    try:
        with trace_span(
            "readiness_probe",
            {"dependency": name},
            agent_logger,
            level=logging.DEBUG,
            error_level=logging.DEBUG,
        ):
            await asyncio.wait_for(probe(), timeout=HEALTH_TIMEOUT_SECONDS)
        return name, {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - 健康端点需要汇总依赖异常
        error_message = str(exc) or type(exc).__name__
        timed_out = isinstance(exc, TimeoutError)
        agent_logger.warning(
            "readiness_probe_failed dependency=%s timeout_seconds=%s timed_out=%s "
            "error_type=%s error_module=%s error=%s",
            name,
            HEALTH_TIMEOUT_SECONDS,
            timed_out,
            type(exc).__name__,
            type(exc).__module__,
            error_message,
            exc_info=True,
            extra={
                "structured": {
                    "event": "readiness_probe_failed",
                    "dependency": name,
                    "timeout_seconds": HEALTH_TIMEOUT_SECONDS,
                    "timed_out": timed_out,
                    "error_type": type(exc).__name__,
                    "error_module": type(exc).__module__,
                    "error": error_message,
                    "traceparent": get_trace_context().traceparent,
                }
            },
        )
        return name, {
            "status": "unavailable",
            "error_type": type(exc).__name__,
            "error": error_message,
        }


async def readiness_checks() -> dict[str, dict[str, str]]:
    """检查有稳定就绪接口的依赖；MinerU 无健康检查接口，不在此主动探测。"""

    checks = await asyncio.gather(
        _run_readiness_probe("minio", _storage().check_ready),
    )
    return dict(checks)
