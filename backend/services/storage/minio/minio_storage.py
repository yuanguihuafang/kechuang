"""知识库 Agent 共用的 MinIO 对象存储适配层。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from base64 import b64decode, b64encode
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

from services.observability.logging import get_logger, log_event
from services.storage.minio.client import get_minio_bucket, get_minio_client

logger = get_logger("perception", "storage")


class StorageObjectTooLargeError(ValueError):
    """MinIO 对象超过解析链路允许的最大大小。"""

    def __init__(self, size: int) -> None:
        super().__init__("文件超过大小限制")
        self.size = size


class MinioUploadService:
    """负责原始文件和解析产物存储，不负责索引或检索。"""

    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
    ) -> None:
        self.bucket = bucket or get_minio_bucket()
        self.client = get_minio_client()

    async def inspect_object(self, storage_key: str) -> dict[str, object] | None:
        """只读取对象头供预检比较指纹；依赖故障不能冒充对象缺失。"""
        try:
            response = await asyncio.to_thread(
                self.client.head_object, Bucket=self.bucket, Key=storage_key
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"404", "NoSuchKey"}:
                return None
            raise
        return {
            "storage_key": storage_key,
            "etag": response["ETag"],
            "size": response["ContentLength"],
            "last_modified": response["LastModified"].isoformat(),
        }

    async def upload_file(
        self,
        file_data: bytes,
        file_name: str,
        content_type: str | None = None,
        document_id: str | None = None,
        scope_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, str]:
        return await asyncio.to_thread(
            self._upload_file_sync,
            file_data,
            file_name,
            content_type,
            document_id,
            scope_id,
            user_id,
        )

    async def upload_path(
        self,
        path: Path,
        file_name: str,
        content_type: str | None = None,
        *,
        content_hash: str | None = None,
        document_id: str | None = None,
        scope_id: str | None = None,
        user_id: str | None = None,
        version: int = 1,
    ) -> tuple[str, str]:
        """使用 boto3 文件传输器流式上传受控本地文件。"""

        return await asyncio.to_thread(
            self._upload_path_sync,
            path,
            file_name,
            content_type,
            content_hash,
            document_id,
            scope_id,
            user_id,
            version,
        )

    async def list_files(self, limit: int = 200) -> list[dict[str, object]]:
        """列出 MinIO 中的原始上传文件，并返回真实对象地址。"""
        log_event(logger, "file_list_start", limit=limit, bucket=self.bucket, prefix="uploads/")
        try:
            files = await asyncio.to_thread(self._list_files_sync, limit)
            log_event(logger, "file_list_complete", count=len(files), bucket=self.bucket)
            return files
        except Exception as exc:
            logger.exception(
                "MinIO 文件列表读取失败",
                extra={
                    "structured": {
                        "event": "minio_list_failed",
                        "bucket": self.bucket,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                },
            )
            raise

    async def cleanup_objects_older_than(self, prefix: str, *, older_than: datetime) -> int:
        """删除指定前缀下超过宽限期的对象，供临时文件兜底清理使用。"""

        return await asyncio.to_thread(self._cleanup_prefix_objects_sync, prefix, older_than)

    async def cleanup_orphaned_objects(
        self,
        *,
        referenced_document_ids: set[str],
        referenced_storage_keys: set[str],
        older_than: datetime,
        is_referenced: Callable[[str, str | None], bool],
    ) -> dict[str, int]:
        """删除超过宽限期且没有数据库引用的原文和派生对象。"""

        return await asyncio.to_thread(
            self._cleanup_orphaned_objects_sync,
            referenced_document_ids,
            referenced_storage_keys,
            older_than,
            is_referenced,
        )

    async def check_ready(self) -> None:
        """只读检查配置的 Bucket 是否可访问。"""

        await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)

    async def upload_mineru_artifact(
        self,
        document_id: str,
        relative_path: str,
        artifact_path: Path,
        content_type: str | None = None,
        *,
        attempt_id: str | None = None,
    ) -> str:
        """将一个 MinerU 解析产物保存为文档派生产物。"""

        return await asyncio.to_thread(
            self._upload_mineru_artifact_sync,
            document_id,
            relative_path,
            artifact_path,
            content_type,
            attempt_id,
        )

    async def save_mineru_artifact_manifest(
        self, document_id: str, artifacts: list[dict[str, object]]
    ) -> str:
        """持久化 MinerU 完整解析产物清单。"""

        return await asyncio.to_thread(
            self._save_mineru_artifact_manifest_sync, document_id, artifacts
        )

    async def load_mineru_artifact_manifest(self, document_id: str) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._load_mineru_artifact_manifest_sync, document_id)

    async def save_image_manifest(self, document_id: str, images: list[dict[str, object]]) -> str:
        """持久化图片与文本块的关联清单，供 Retrieval 查询时读取。"""

        return await asyncio.to_thread(self._save_image_manifest_sync, document_id, images)

    async def load_image_manifest(self, document_id: str) -> list[dict[str, object]]:
        """读取指定文档的图片清单；文档没有图片时返回空数组。"""

        return await asyncio.to_thread(self._load_image_manifest_sync, document_id)

    async def delete_derived_objects(self, document_id: str, storage_keys: list[str]) -> int:
        """删除指定文档下已从解析结果移除的派生对象。"""

        return await asyncio.to_thread(self._delete_derived_objects_sync, document_id, storage_keys)

    async def save_parse_result(self, document_id: str, result: dict[str, object]) -> str:
        """持久化结构化解析结果，供刷新页面后恢复确认预览。"""

        return await asyncio.to_thread(self._save_parse_result_sync, document_id, result)

    async def load_parse_result(self, document_id: str) -> dict[str, object] | None:
        """读取结构化解析结果；没有完成过解析时返回空值。"""

        return await asyncio.to_thread(self._load_parse_result_sync, document_id)

    async def save_index_request(
        self, document_id: str, task_id: str, request: dict[str, object]
    ) -> str:
        """保存可恢复索引请求，避免把分块正文写入 PostgreSQL 任务状态。"""
        return await asyncio.to_thread(self._save_index_request_sync, document_id, task_id, request)

    async def load_index_request(self, document_id: str, task_id: str) -> dict[str, object] | None:
        return await asyncio.to_thread(self._load_index_request_sync, document_id, task_id)

    async def download_derived_image(
        self, document_id: str, image_id: str
    ) -> tuple[bytes, str, str]:
        """由受控 API 下载已持久化的 MinerU 图片。"""

        return await asyncio.to_thread(self._download_derived_image_sync, document_id, image_id)

    async def download_file(self, storage_key: str) -> tuple[bytes, str, str]:
        """读取已上传的原始文件，供 Perception 重新解析。"""
        log_event(logger, "minio_download_start", bucket=self.bucket, storage_key=storage_key)
        try:
            result = await asyncio.to_thread(self._download_file_sync, storage_key)
            log_event(
                logger,
                "minio_download_complete",
                bucket=self.bucket,
                storage_key=storage_key,
                size_bytes=len(result[0]),
                file_name=result[2],
            )
            return result
        except Exception as exc:
            logger.exception(
                "MinIO 原始文件下载失败",
                extra={
                    "structured": {
                        "event": "minio_download_failed",
                        "bucket": self.bucket,
                        "storage_key": storage_key,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                },
            )
            raise

    async def download_file_to_directory(
        self,
        storage_key: str,
        directory: Path,
        max_bytes: int,
    ) -> tuple[Path, str, str, int]:
        """把 MinIO 原始对象分段下载到受控目录，不创建完整内存副本。"""

        log_event(logger, "file_download_start", bucket=self.bucket, storage_key=storage_key)
        try:
            result = await asyncio.to_thread(
                self._download_file_to_directory_sync,
                storage_key,
                directory,
                max_bytes,
            )
            log_event(
                logger,
                "file_download_complete",
                bucket=self.bucket,
                storage_key=storage_key,
                size_bytes=result[3],
                file_name=result[2],
            )
            return result
        except Exception as exc:
            logger.exception(
                "MinIO 原始文件流式下载失败",
                extra={
                    "structured": {
                        "event": "minio_download_stream_failed",
                        "bucket": self.bucket,
                        "storage_key": storage_key,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                },
            )
            raise

    async def delete_document(self, storage_key: str, document_id: str | None = None) -> None:
        """删除原始文件及指定文档的 MinerU 派生产物。"""
        log_event(
            logger,
            "minio_delete_start",
            bucket=self.bucket,
            storage_key=storage_key,
            document_id=document_id,
        )
        try:
            deleted_count = await asyncio.to_thread(
                self._delete_document_sync, storage_key, document_id
            )
            log_event(
                logger,
                "minio_delete_complete",
                bucket=self.bucket,
                storage_key=storage_key,
                document_id=document_id,
                deleted_count=deleted_count,
            )
        except Exception as exc:
            logger.exception(
                "MinIO 文档删除失败",
                extra={
                    "structured": {
                        "event": "minio_delete_failed",
                        "bucket": self.bucket,
                        "storage_key": storage_key,
                        "document_id": document_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                },
            )
            raise

    async def delete_original_file(self, storage_key: str) -> None:
        """仅删除原始上传对象，避免替换版本误删最新派生产物。"""
        normalized_key = storage_key.replace("\\", "/").lstrip("/")
        if not normalized_key.startswith("uploads/") or ".." in normalized_key.split("/"):
            raise ValueError("原始文件对象键不合法")
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=normalized_key)

    def _upload_file_sync(
        self,
        file_data: bytes,
        file_name: str,
        content_type: str | None,
        document_id: str | None,
        scope_id: str | None,
        user_id: str | None,
    ) -> tuple[str, str]:
        self._ensure_bucket()
        content_hash = hashlib.sha256(file_data).hexdigest()
        now = datetime.now(timezone.utc)
        storage_key = (
            f"uploads/{now.year}/{now.month:02d}/{uuid.uuid4().hex}{Path(file_name).suffix.lower()}"
        )
        metadata = {
            "original-name": b64encode(file_name.encode("utf-8")).decode("ascii"),
            "content-hash": content_hash,
        }
        if document_id:
            metadata["document-id"] = document_id
        if scope_id:
            metadata["scope-id"] = scope_id
        if user_id:
            metadata["user-id"] = user_id
        self.client.put_object(
            Bucket=self.bucket,
            Key=storage_key,
            Body=file_data,
            ContentType=content_type or "application/octet-stream",
            Metadata=metadata,
        )
        return storage_key, content_hash

    def _upload_path_sync(
        self,
        path: Path,
        file_name: str,
        content_type: str | None,
        content_hash: str | None,
        document_id: str | None,
        scope_id: str | None,
        user_id: str | None,
        version: int = 1,
    ) -> tuple[str, str]:
        safe_name = Path(file_name).name
        if safe_name != file_name or safe_name in {"", ".", ".."}:
            raise ValueError("文件名不合法")
        if not path.is_file():
            raise ValueError("待上传文件不存在")
        self._ensure_bucket()
        resolved_hash = content_hash or _sha256_path(path)
        now = datetime.now(timezone.utc)
        storage_key = (
            f"uploads/{now.year}/{now.month:02d}/{uuid.uuid4().hex}{Path(safe_name).suffix.lower()}"
        )
        metadata = _upload_metadata(
            safe_name,
            resolved_hash,
            document_id=document_id,
            scope_id=scope_id,
            user_id=user_id,
            version=version,
        )
        self.client.upload_file(
            Filename=str(path),
            Bucket=self.bucket,
            Key=storage_key,
            ExtraArgs={
                "ContentType": content_type or "application/octet-stream",
                "Metadata": metadata,
            },
        )
        return storage_key, resolved_hash

    def _list_files_sync(self, limit: int) -> list[dict[str, object]]:
        self._ensure_bucket()
        safe_limit = min(max(limit, 1), 500)
        response = self.client.list_objects_v2(
            Bucket=self.bucket, Prefix="uploads/", MaxKeys=safe_limit
        )
        files: list[dict[str, object]] = []
        for item in response.get("Contents", []):
            storage_key = str(item.get("Key") or "")
            if not storage_key:
                continue
            try:
                head = self.client.head_object(Bucket=self.bucket, Key=storage_key)
            except ClientError:
                continue
            metadata = head.get("Metadata") or {}
            encoded_name = metadata.get("original-name", "")
            try:
                file_name = (
                    b64decode(encoded_name).decode("utf-8")
                    if encoded_name
                    else Path(storage_key).name
                )
            except Exception:  # noqa: BLE001 - 元数据异常时回退对象名
                file_name = Path(storage_key).name
            document_id = str(
                metadata.get("document-id")
                or f"storage_{hashlib.sha1(storage_key.encode()).hexdigest()[:16]}"
            )
            files.append(
                {
                    "document_id": document_id,
                    "storage_key": storage_key,
                    "file_name": Path(file_name).name,
                    "file_size": int(item.get("Size") or 0),
                    "scope_id": metadata.get("scope-id") or "public",
                    "user_id": metadata.get("user-id") or None,
                    "content_hash": metadata.get("content-hash") or None,
                    "version": int(metadata.get("document-version") or 1),
                    "updated_at": item.get("LastModified").isoformat()
                    if item.get("LastModified")
                    else None,
                }
            )
        return files

    def _cleanup_prefix_objects_sync(self, prefix: str, older_than: datetime) -> int:
        deleted = 0
        keys = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            if page.get("IsTruncated") and not page.get("NextContinuationToken"):
                raise RuntimeError("Incomplete object cleanup inventory")
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                modified = item.get("LastModified")
                if not key or not isinstance(modified, datetime):
                    continue
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                if modified >= older_than:
                    continue
                keys.append(key)
        for key in keys:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            deleted += 1
        return deleted

    def _cleanup_orphaned_objects_sync(
        self,
        referenced_document_ids: set[str],
        referenced_storage_keys: set[str],
        older_than: datetime,
        is_referenced: Callable[[str, str | None], bool],
    ) -> dict[str, int]:
        counts = {"uploads": 0, "derived": 0, "total": 0}
        candidates = []
        paginator = self.client.get_paginator("list_objects_v2")
        for prefix in ("uploads/", "derived/"):
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                if page.get("IsTruncated") and not page.get("NextContinuationToken"):
                    raise RuntimeError("Incomplete object cleanup inventory")
                for item in page.get("Contents", []):
                    key = str(item.get("Key") or "")
                    modified = item.get("LastModified")
                    if not key or not isinstance(modified, datetime):
                        continue
                    if modified.tzinfo is None:
                        modified = modified.replace(tzinfo=timezone.utc)
                    if modified >= older_than:
                        continue
                    if key in referenced_storage_keys:
                        continue
                    if prefix == "uploads/":
                        metadata = self.client.head_object(Bucket=self.bucket, Key=key).get("Metadata") or {}
                        document_id = str(metadata.get("document-id") or "").strip()
                        if document_id and document_id in referenced_document_ids:
                            continue
                        bucket = "uploads"
                    else:
                        parts = key.split("/", 2)
                        document_id = parts[1] if len(parts) > 1 else ""
                        if document_id and document_id in referenced_document_ids:
                            continue
                        bucket = "derived"
                    candidates.append((key, document_id, bucket))
        # Finish both inventories (including metadata reads) before any deletion.
        for key, document_id, bucket in candidates:
            if is_referenced(key, document_id or None):
                continue
            self.client.delete_object(Bucket=self.bucket, Key=key)
            counts[bucket] += 1
            counts["total"] += 1
        return counts

    async def find_by_content_hash(
        self, content_hash: str, *, scope_id: str = "public", user_id: str | None = None
    ) -> list[dict[str, object]]:
        """查找同一知识范围内的同内容历史对象。"""
        files = await self.list_files(500)
        return [
            item
            for item in files
            if item.get("content_hash") == content_hash
            and item.get("scope_id") == scope_id
            and (scope_id != "internal" or item.get("user_id") == user_id)
        ]

    async def find_by_document_id(self, document_id: str) -> list[dict[str, object]]:
        files = await self.list_files(500)
        return [item for item in files if item.get("document_id") == document_id]

    def _download_file_sync(self, storage_key: str) -> tuple[bytes, str, str]:
        normalized_key = storage_key.replace("\\", "/").lstrip("/")
        if not normalized_key.startswith("uploads/") or ".." in normalized_key.split("/"):
            raise ValueError("原始文件对象键不合法")
        response = self.client.get_object(Bucket=self.bucket, Key=normalized_key)
        metadata = response.get("Metadata") or {}
        encoded_name = metadata.get("original-name", "")
        try:
            file_name = (
                b64decode(encoded_name).decode("utf-8")
                if encoded_name
                else Path(normalized_key).name
            )
        except Exception:  # noqa: BLE001 - 元数据损坏时回退对象名
            file_name = Path(normalized_key).name
        return (
            response["Body"].read(),
            str(response.get("ContentType") or "application/octet-stream"),
            Path(file_name).name,
        )

    def _download_file_to_directory_sync(
        self,
        storage_key: str,
        directory: Path,
        max_bytes: int,
    ) -> tuple[Path, str, str, int]:
        normalized_key = _normalize_upload_key(storage_key)
        if max_bytes <= 0:
            raise ValueError("文件大小限制必须大于 0")
        if not directory.is_dir():
            raise ValueError("下载目标目录不存在")
        response = self.client.get_object(Bucket=self.bucket, Key=normalized_key)
        content_length = int(response.get("ContentLength") or 0)
        if content_length > max_bytes:
            response["Body"].close()
            raise StorageObjectTooLargeError(content_length)
        file_name = _original_file_name(response.get("Metadata") or {}, normalized_key)
        target = directory / file_name
        size = 0
        body = response["Body"]
        try:
            with target.open("wb") as output:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageObjectTooLargeError(size)
                    output.write(chunk)
        finally:
            body.close()
        return (
            target,
            str(response.get("ContentType") or "application/octet-stream"),
            file_name,
            size,
        )

    def _delete_document_sync(self, storage_key: str, document_id: str | None) -> int:
        normalized_key = storage_key.replace("\\", "/").lstrip("/")
        if not normalized_key.startswith("uploads/") or ".." in normalized_key.split("/"):
            raise ValueError("原始文件对象键不合法")
        objects = [{"Key": normalized_key}]
        if document_id:
            _validate_identifier(document_id, "document_id")
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"derived/{document_id}/"):
                objects.extend(
                    {"Key": item["Key"]} for item in page.get("Contents", []) if item.get("Key")
                )
        # 当前 MinIO 配置对批量 DeleteObjects 要求 Content-MD5；逐个删除更兼容，
        # 文档派生产物数量通常较少，也避免因单个对象异常导致整批失败。
        for item in objects:
            self.client.delete_object(Bucket=self.bucket, Key=item["Key"])
        return len(objects)

    def _delete_derived_objects_sync(self, document_id: str, storage_keys: list[str]) -> int:
        _validate_identifier(document_id, "document_id")
        prefix = f"derived/{document_id}/"
        keys: list[str] = []
        for storage_key in storage_keys:
            normalized_key = str(storage_key).replace("\\", "/").lstrip("/")
            if not normalized_key.startswith(prefix) or ".." in normalized_key.split("/"):
                raise ValueError("派生文件对象键不合法")
            if normalized_key not in keys:
                keys.append(normalized_key)
        for key in keys:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        return len(keys)

    def _upload_mineru_artifact_sync(
        self,
        document_id: str,
        relative_path: str,
        artifact_path: Path,
        content_type: str | None,
        attempt_id: str | None,
    ) -> str:
        _validate_identifier(document_id, "document_id")
        if attempt_id:
            _validate_identifier(attempt_id, "attempt_id")
        normalized_relative = _validate_relative_path(relative_path)
        if not artifact_path.is_file():
            raise ValueError("MinerU 解析产物不存在")
        self._ensure_bucket()
        attempt_prefix = f"attempts/{attempt_id}/" if attempt_id else ""
        storage_key = f"derived/{document_id}/mineru/{attempt_prefix}{normalized_relative}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=storage_key,
            Body=artifact_path.read_bytes(),
            ContentType=content_type or "application/octet-stream",
            Metadata={"document-id": document_id, "artifact": "mineru"},
        )
        return storage_key

    def _save_mineru_artifact_manifest_sync(
        self, document_id: str, artifacts: list[dict[str, object]]
    ) -> str:
        _validate_identifier(document_id, "document_id")
        self._ensure_bucket()
        storage_key = f"derived/{document_id}/mineru/artifact-manifest.json"
        body = json.dumps(
            {"document_id": document_id, "artifacts": artifacts}, ensure_ascii=False
        ).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=storage_key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            Metadata={"document-id": document_id, "artifact": "mineru-manifest"},
        )
        return storage_key

    def _load_mineru_artifact_manifest_sync(self, document_id: str) -> list[dict[str, object]]:
        _validate_identifier(document_id, "document_id")
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=f"derived/{document_id}/mineru/artifact-manifest.json"
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}:
                return []
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        artifacts = payload.get("artifacts", []) if isinstance(payload, dict) else []
        return [item for item in artifacts if isinstance(item, dict)]

    def _save_image_manifest_sync(self, document_id: str, images: list[dict[str, object]]) -> str:
        _validate_identifier(document_id, "document_id")
        self._ensure_bucket()
        storage_key = f"derived/{document_id}/image-manifest.json"
        body = json.dumps(
            {"document_id": document_id, "images": images}, ensure_ascii=False
        ).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=storage_key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            Metadata={"document-id": document_id, "artifact": "image-manifest"},
        )
        return storage_key

    def _load_image_manifest_sync(self, document_id: str) -> list[dict[str, object]]:
        _validate_identifier(document_id, "document_id")
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=f"derived/{document_id}/image-manifest.json",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}:
                return []
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        images = payload.get("images", []) if isinstance(payload, dict) else []
        return [image for image in images if isinstance(image, dict)]

    def _save_parse_result_sync(self, document_id: str, result: dict[str, object]) -> str:
        _validate_identifier(document_id, "document_id")
        self._ensure_bucket()
        storage_key = f"derived/{document_id}/mineru/parse-result.json"
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=storage_key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            Metadata={"document-id": document_id, "artifact": "parse-result"},
        )
        return storage_key

    def _load_parse_result_sync(self, document_id: str) -> dict[str, object] | None:
        _validate_identifier(document_id, "document_id")
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=f"derived/{document_id}/mineru/parse-result.json",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}:
                return None
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None

    def _save_index_request_sync(
        self, document_id: str, task_id: str, request: dict[str, object]
    ) -> str:
        _validate_identifier(document_id, "document_id")
        _validate_identifier(task_id, "task_id")
        self._ensure_bucket()
        storage_key = f"derived/{document_id}/tasks/{task_id}/index-request.json"
        self.client.put_object(
            Bucket=self.bucket,
            Key=storage_key,
            Body=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
            Metadata={"document-id": document_id, "task-id": task_id, "artifact": "index-request"},
        )
        return storage_key

    def _load_index_request_sync(self, document_id: str, task_id: str) -> dict[str, object] | None:
        _validate_identifier(document_id, "document_id")
        _validate_identifier(task_id, "task_id")
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=f"derived/{document_id}/tasks/{task_id}/index-request.json",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}:
                return None
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None

    def _download_derived_image_sync(
        self, document_id: str, image_id: str
    ) -> tuple[bytes, str, str]:
        images = self._load_image_manifest_sync(document_id)
        image = next((item for item in images if item.get("image_id") == image_id), None)
        if not image or not isinstance(image.get("minio_object_key"), str):
            raise FileNotFoundError("解析图片不存在")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=image["minio_object_key"])
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError("解析图片对象不存在") from exc
            raise
        return (
            response["Body"].read(),
            str(response.get("ContentType") or "application/octet-stream"),
            Path(image["minio_object_key"]).name,
        )

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status_code == 404 or str(error.get("Code", "")) in {"404", "NoSuchBucket"}:
                self.client.create_bucket(Bucket=self.bucket)
                return
            raise


def _validate_identifier(value: str, label: str) -> None:
    if not value or "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{label} 不合法")


def _validate_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("解析产物路径不合法")
    return "/".join(parts)


def _normalize_upload_key(storage_key: str) -> str:
    normalized = storage_key.replace("\\", "/").lstrip("/")
    if not normalized.startswith("uploads/") or ".." in normalized.split("/"):
        raise ValueError("原始文件对象键不合法")
    return normalized


def _original_file_name(metadata: dict[str, object], storage_key: str) -> str:
    encoded_name = str(metadata.get("original-name") or "")
    try:
        decoded = (
            b64decode(encoded_name).decode("utf-8") if encoded_name else Path(storage_key).name
        )
    except Exception:  # noqa: BLE001 - 元数据损坏时回退对象名
        decoded = Path(storage_key).name
    safe_name = Path(decoded).name
    if safe_name in {"", ".", ".."}:
        raise ValueError("原始文件名不合法")
    return safe_name


def _upload_metadata(
    file_name: str,
    content_hash: str,
    *,
    document_id: str | None,
    scope_id: str | None,
    user_id: str | None,
    version: int = 1,
) -> dict[str, str]:
    file_name = _as_text(file_name, "")
    content_hash = _as_text(content_hash, "")
    document_id = _as_text(document_id)
    scope_id = _as_text(scope_id)
    user_id = _as_text(user_id)
    metadata = {
        "original-name": b64encode(file_name.encode("utf-8")).decode("ascii"),
        "content-hash": content_hash,
        "document-version": str(max(1, version)),
    }
    if document_id:
        metadata["document-id"] = document_id
    if scope_id:
        metadata["scope-id"] = scope_id
    if user_id:
        metadata["user-id"] = user_id
    return metadata


def _as_text(value: object, default: str | None = None) -> str | None:
    """把 FastAPI 参数对象转换成 MinIO 可接受的普通字符串。"""

    if type(value).__name__ == "Form":
        value = getattr(value, "default", default)
    if value is None:
        return default
    return str(value)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
