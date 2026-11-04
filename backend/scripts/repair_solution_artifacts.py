"""Restore missing parse artifacts for published solution documents only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from agents.perception_agent.application.perception_service import (
    PerceptionAgent,
    _attach_block_image_ids,
)
from agents.perception_agent.schemas import PerceptionResult
from services.document_parsing.mineru_parser import (
    MinerUDocumentParser,
    normalize_markdown_image_references,
)
from services.storage.minio.image_service import perception_image_url

CONFIRMATION = "REPAIR-SOLUTION-PARSE-ARTIFACTS"
MAX_SOURCE_BYTES = 50 * 1024 * 1024


def _progress(message: str) -> None:
    print(f"[solution-artifact-repair] {message}", file=sys.stderr, flush=True)


def _load_project_environment() -> None:
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    load_dotenv(root / ".env.local", override=True)


def _build_runtime() -> Any:
    _load_project_environment()
    from services.persistence.document_lifecycle import DocumentLifecycleStore
    from services.storage.minio.minio_storage import MinioUploadService

    return type(
        "Runtime",
        (),
        {"lifecycle": DocumentLifecycleStore(), "storage": MinioUploadService()},
    )()


def _image_path(image: dict[str, Any]) -> str:
    return str(image.get("relativePath") or image.get("relative_path") or "")


def _file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_entries_from_parse_result(
    document_id: str, storage_key: str, parse_result: Any
) -> list[dict[str, Any]] | None:
    if (
        not isinstance(parse_result, dict)
        or parse_result.get("document_id") != document_id
        or parse_result.get("storage_key") != storage_key
    ):
        return None
    metadata = parse_result.get("metadata")
    if not isinstance(metadata, dict):
        return None
    refs = metadata.get("artifact_refs")
    if not isinstance(refs, dict):
        return [] if str(metadata.get("parser") or "").lower() not in {"mineru", "hybrid"} else None
    prefix = f"derived/{document_id}/mineru/"
    entries = []
    for relative_path, object_key in refs.items():
        relative = PurePosixPath(str(relative_path).replace("\\", "/"))
        key = str(object_key).replace("\\", "/").lstrip("/")
        if (
            not relative_path
            or relative.is_absolute()
            or ".." in relative.parts
            or "." in relative.parts
            or not key.startswith(prefix)
            or ".." in key.split("/")
        ):
            return None
        entries.append({"relative_path": relative.as_posix(), "minio_object_key": key})
    return entries


async def _prepare(
    runtime: Any, temporary_root: Path, target_document_id: str | None = None
) -> list[dict[str, Any]]:
    agent = None
    documents: list[dict[str, Any]] = []
    page = 1
    while True:
        result = runtime.lifecycle.list_documents_page(
            knowledge_type="solution", page=page, page_size=100
        )
        _progress(f"扫描已发布方案文档，第 {page} 页，共 {result['total']} 份")
        for document in result["documents"]:
            version = document.get("active_version") or {}
            if str(version.get("publication_status") or "").upper() != "PUBLISHED":
                continue
            document_id = str(document.get("document_id") or "")
            if not document_id:
                continue
            if target_document_id and target_document_id != document_id:
                continue
            manifest_key = f"derived/{document_id}/mineru/artifact-manifest.json"
            parse_key = f"derived/{document_id}/mineru/parse-result.json"
            manifest_info = await runtime.storage.inspect_object(manifest_key)
            parse_info = await runtime.storage.inspect_object(parse_key)
            if manifest_info and parse_info:
                continue
            documents.append(
                {
                    "document_id": document_id,
                    "source_name": str(document.get("source_name") or ""),
                    "version_id": version.get("version_id"),
                    "storage_key": str(version.get("storage_key") or ""),
                    "source_info": None,
                    "manifest_missing": not manifest_info,
                    "parse_result_missing": not parse_info,
                    "existing_artifacts": await runtime.storage.load_mineru_artifact_manifest(
                        document_id
                    ),
                    "stored_parse_result": (
                        await runtime.storage.load_parse_result(document_id)
                        if parse_info
                        else None
                    ),
                    "image_manifest": await runtime.storage.load_image_manifest(document_id),
                    "result": None,
                    "image_mapping": {},
                    "artifact_files": [],
                    "status": "pending",
                }
            )
        if page * result["page_size"] >= result["total"]:
            break
        page += 1

    for index, group in enumerate(documents, start=1):
        document_id = group["document_id"]
        _progress(f"预检 {index}/{len(documents)}：{group['source_name']} [{document_id}]")
        if not group["storage_key"]:
            group.update(status="skipped", reason="活动版本没有原附件对象键")
            continue
        source_info = await runtime.storage.inspect_object(group["storage_key"])
        if not source_info:
            group.update(status="skipped", reason="活动原附件对象缺失")
            continue
        if int(source_info.get("size") or 0) > MAX_SOURCE_BYTES:
            group.update(status="skipped", reason="活动原附件超过解析大小限制")
            continue
        group["source_info"] = source_info
        if group["manifest_missing"] and not group["parse_result_missing"]:
            entries = _manifest_entries_from_parse_result(
                document_id, group["storage_key"], group["stored_parse_result"]
            )
            if entries is not None:
                verified_entries = []
                for entry in entries:
                    key_info = await runtime.storage.inspect_object(entry["minio_object_key"])
                    if not key_info:
                        verified_entries = []
                        break
                    verified_entries.append(
                        {
                            **entry,
                            "content_type": key_info.get("content_type")
                            or mimetypes.guess_type(entry["relative_path"])[0]
                            or "application/octet-stream",
                            "size_bytes": int(key_info.get("size") or 0),
                        }
                    )
                if entries == [] or verified_entries:
                    group.update(
                        status="ready",
                        manifest_only_repair=True,
                        existing_artifacts=verified_entries,
                        artifact_files=[],
                        image_count=len(group["image_manifest"]),
                    )
                    continue
        input_dir = temporary_root / "sources"
        input_dir.mkdir(parents=True, exist_ok=True)
        path, _, file_name, _ = await runtime.storage.download_file_to_directory(
            group["storage_key"], input_dir, MAX_SOURCE_BYTES
        )
        _progress(f"重新解析原附件：{group['source_name']} [{document_id}]")
        if agent is None:
            agent = PerceptionAgent()
            for name in ("_mineru_parser", "_local_mineru_parser", "_image_mineru_parser"):
                parser = getattr(getattr(agent, "parser", None), name, None)
                if isinstance(parser, MinerUDocumentParser):
                    parser.output_path = temporary_root / "mineru-output"
        parsed = await agent.run_path(
            path=path,
            file_name=file_name,
            document_id=document_id,
            storage_key=group["storage_key"],
            knowledge_type="solution",
            extract_draft=False,
        )
        if parsed.status != "SUCCESS":
            group.update(status="failed", reason=parsed.error or "文档解析失败")
            continue
        _progress(f"解析完成，核对图片与产物：{group['source_name']} [{document_id}]")
        if parsed.metadata.get("parser") != "mineru":
            images = parsed.metadata.get("images") or []
            if images:
                group.update(status="blocked", reason="非 MinerU 解析器返回图片资产，无法安全恢复旧图片映射")
                continue
            group.update(
                status="ready",
                result=parsed,
                image_mapping={},
                mapping_modes={},
                artifact_files=[],
                output_dir=None,
                image_count=0,
            )
            continue
        output_dir = Path(str(parsed.metadata.get("outputDir") or "")).resolve()
        if temporary_root.resolve() not in output_dir.parents or not output_dir.is_dir():
            group.update(status="failed", reason="解析产物目录不在本次临时目录中")
            continue
        artifact_files = sorted(item for item in output_dir.rglob("*") if item.is_file())
        if not artifact_files:
            group.update(status="failed", reason="解析器未生成任何产物文件")
            continue
        images = [item for item in parsed.metadata.get("images", []) if isinstance(item, dict)]
        image_mapping, unresolved, mapping_modes = map_reparsed_images(
            images, group["image_manifest"]
        )
        if unresolved:
            group.update(
                status="blocked",
                reason="解析图片无法与现有图片 ID 唯一匹配，未写入以保护草稿图片引用",
                unresolved_images=unresolved,
            )
            continue
        verified_mapping = {}
        for relative_path, entry in image_mapping.items():
            image = next((item for item in images if _image_path(item) == relative_path), None)
            local_path = Path(str((image or {}).get("path") or ""))
            if not local_path.is_file():
                unresolved.append(relative_path)
                continue
            try:
                image_data, _, _ = await runtime.storage.download_derived_image(
                    document_id, str(entry["image_id"])
                )
            except Exception as exc:  # noqa: BLE001 - report the exact image blocking this doc
                group.update(
                    status="blocked",
                    reason=f"原图片对象不可读：{relative_path}: {type(exc).__name__}",
                )
                break
            if mapping_modes.get(relative_path) != "position" and hashlib.sha1(
                image_data
            ).hexdigest() != _file_sha1(local_path):
                unresolved.append(relative_path)
                continue
            verified_mapping[relative_path] = entry
        else:
            if unresolved:
                group.update(
                    status="blocked",
                    reason="重解析图片与已存图片内容不一致，未写入以保护图片映射",
                    unresolved_images=unresolved,
                )
                continue
            group.update(
                status="ready",
                result=parsed,
                image_mapping=verified_mapping,
                mapping_modes=mapping_modes,
                artifact_files=artifact_files,
                output_dir=output_dir,
                image_count=len(images),
            )
    return documents


async def _apply_group(runtime: Any, group: dict[str, Any], run_id: str) -> dict[str, Any]:
    document_id = group["document_id"]
    current = runtime.lifecycle.get_document(document_id)
    version = (current or {}).get("active_version") or {}
    source_info = await runtime.storage.inspect_object(group["storage_key"])
    if (
        not current
        or current.get("knowledge_type") != "solution"
        or str(version.get("publication_status") or "").upper() != "PUBLISHED"
        or version.get("version_id") != group["version_id"]
        or not source_info
        or source_info.get("etag") != group["source_info"].get("etag")
    ):
        raise RuntimeError("活动文档版本或原附件在预检后发生变化")
    manifest_key = f"derived/{document_id}/mineru/artifact-manifest.json"
    parse_key = f"derived/{document_id}/mineru/parse-result.json"
    if group["manifest_missing"] and await runtime.storage.inspect_object(manifest_key):
        raise RuntimeError("解析产物清单在预检后已被其他操作写入，拒绝覆盖")
    if group["parse_result_missing"] and await runtime.storage.inspect_object(parse_key):
        raise RuntimeError("解析结果在预检后已被其他操作写入，拒绝覆盖")
    if await runtime.storage.load_image_manifest(document_id) != group["image_manifest"]:
        raise RuntimeError("图片清单在预检后发生变化，拒绝写入")

    artifacts = list(group["existing_artifacts"])
    document_prefix = f"derived/{document_id}/"
    for item in artifacts:
        object_key = str(item.get("minio_object_key") or "")
        if not object_key.startswith(document_prefix):
            raise RuntimeError("现有解析产物清单包含文档目录之外的对象键")
        if not await runtime.storage.inspect_object(object_key):
            raise RuntimeError(f"现有解析产物对象缺失：{object_key}")
    new_keys: list[str] = []
    written_keys: list[str] = []
    try:
        if group["manifest_missing"]:
            artifacts = (
                list(group["existing_artifacts"])
                if group.get("manifest_only_repair")
                else []
            )
            for path in ([] if group.get("manifest_only_repair") else group["artifact_files"]):
                relative_path = path.relative_to(group["output_dir"]).as_posix()
                image_entry = group["image_mapping"].get(relative_path)
                if image_entry:
                    object_key = str(image_entry["minio_object_key"])
                    if not object_key.startswith(document_prefix):
                        raise RuntimeError(f"图片对象键不属于当前文档：{relative_path}")
                else:
                    object_key = await runtime.storage.upload_mineru_artifact(
                        document_id,
                        relative_path,
                        path,
                        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        attempt_id=run_id,
                    )
                    new_keys.append(object_key)
                info = await runtime.storage.inspect_object(object_key)
                if not info or int(info.get("size") or -1) != path.stat().st_size:
                    raise RuntimeError(f"解析产物对象写入后校验失败：{relative_path}")
                artifacts.append(
                    {
                        "relative_path": relative_path,
                        "minio_object_key": object_key,
                        "content_type": mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream",
                        "size_bytes": path.stat().st_size,
                    }
                )
            written_keys.append(manifest_key)
            await runtime.storage.save_mineru_artifact_manifest(document_id, artifacts)
            if await runtime.storage.load_mineru_artifact_manifest(document_id) != artifacts:
                raise RuntimeError("解析产物清单回读校验失败")

        if group["parse_result_missing"]:
            result = group["result"].model_copy(deep=True)
            rebind_result_images(result, document_id, group["image_mapping"])
            artifact_refs = {
                str(item.get("relative_path")): str(item.get("minio_object_key"))
                for item in artifacts
                if item.get("relative_path") and item.get("minio_object_key")
            }
            result.metadata["artifact_refs"] = artifact_refs
            result.metadata["artifact_manifest_key"] = manifest_key
            result.metadata["parse_result_key"] = parse_key
            payload = result.model_dump(mode="json")
            written_keys.append(parse_key)
            await runtime.storage.save_parse_result(document_id, payload)
            if await runtime.storage.load_parse_result(document_id) != payload:
                raise RuntimeError("解析结果回读校验失败")
        return {
            "document_id": document_id,
            "status": "SUCCESS",
            "artifact_count": len(artifacts),
            "image_count": group["image_count"],
            "positional_image_count": sum(
                mode == "position" for mode in group.get("mapping_modes", {}).values()
            ),
            "restored_manifest": group["manifest_missing"],
            "restored_parse_result": group["parse_result_missing"],
        }
    except Exception:
        if written_keys:
            await runtime.storage.delete_derived_objects(document_id, written_keys)
        if new_keys:
            await runtime.storage.delete_derived_objects(document_id, new_keys)
        raise


async def run(
    *, apply: bool, backup_file: Path | None, document_id: str | None = None
) -> dict[str, Any]:
    runtime = _build_runtime()
    with tempfile.TemporaryDirectory(prefix="solution-artifact-repair-") as directory:
        try:
            groups = await _prepare(runtime, Path(directory), document_id)
            report = [
                {
                    key: value
                    for key, value in group.items()
                    if key
                    not in {
                        "result",
                        "image_mapping",
                        "image_manifest",
                        "existing_artifacts",
                        "artifact_files",
                        "output_dir",
                        "source_info",
                    }
                }
                for group in groups
            ]
            ready = [group for group in groups if group["status"] == "ready"]
            if not apply:
                return {"status": "dry_run", "groups": report}
            if backup_file is None:
                raise ValueError("--apply 必须同时提供 --backup-file")
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            backup = {
                group["document_id"]: {
                    "version_id": group["version_id"],
                    "manifest_was_missing": group["manifest_missing"],
                    "parse_result_was_missing": group["parse_result_missing"],
                    "image_manifest": group["image_manifest"],
                    "existing_artifacts": group["existing_artifacts"],
                }
                for group in ready
            }
            fd = os.open(backup_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(backup, stream, ensure_ascii=False, indent=2)
            results = []
            run_id = uuid.uuid4().hex
            for index, group in enumerate(ready, start=1):
                _progress(
                    f"写入 {index}/{len(ready)}：{group['source_name']} [{group['document_id']}]"
                )
                try:
                    results.append(await _apply_group(runtime, group, run_id))
                    _progress(f"写入完成 {group['document_id']}")
                except Exception as exc:  # noqa: BLE001 - keep later independent docs repairable
                    results.append(
                        {
                            "document_id": group["document_id"],
                            "status": "FAILED",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    _progress(f"写入失败 {group['document_id']}：{type(exc).__name__}: {exc}")
            complete = all(group["status"] == "ready" for group in groups) and all(
                item["status"] == "SUCCESS" for item in results
            )
            return {
                "status": "applied" if complete else "partial",
                "backup_file": str(backup_file),
                "groups": report,
                "results": results,
            }
        finally:
            runtime.lifecycle.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="仅恢复已发布方案文档缺失的解析产物；默认只预检"
    )
    parser.add_argument("--apply", action="store_true", help="执行 MinIO 写入")
    parser.add_argument("--document-id", help="仅处理指定的方案文档 ID")
    parser.add_argument("--confirm", help=f"执行确认词：{CONFIRMATION}")
    parser.add_argument("--backup-file", type=Path, help="独占创建的执行前清单备份 JSON")
    args = parser.parse_args(argv)
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
    if args.backup_file and not args.apply:
        parser.error("--backup-file 仅可与 --apply 一起使用")
    report = asyncio.run(
        run(apply=args.apply, backup_file=args.backup_file, document_id=args.document_id)
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] in {"partial", "preflight_failed"} else 0


def map_reparsed_images(
    images: list[dict[str, Any]], manifest: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, str]]:
    """Map parsed images to existing IDs, using order only when counts match."""

    mapped: dict[str, dict[str, Any]] = {}
    mapping_modes: dict[str, str] = {}
    unresolved: list[tuple[str, dict[str, Any]]] = []
    used: set[int] = set()
    for image in images:
        relative_path = str(image.get("relativePath") or image.get("relative_path") or "")
        candidates: list[dict[str, Any]] = []
        matched_mode = ""
        for image_value, manifest_field in (
            (image.get("image_id"), "image_id"),
            (image.get("source_locator"), "source_locator"),
            (image.get("contentHash") or image.get("content_hash"), "content_hash"),
        ):
            if not image_value:
                continue
            matches = [
                item
                for item in manifest
                if item.get(manifest_field) == image_value
                and id(item) not in used
                and item.get("image_id")
                and item.get("minio_object_key")
            ]
            if len(matches) == 1:
                candidates = matches
                matched_mode = {
                    "image_id": "image_id",
                    "source_locator": "source_locator",
                    "content_hash": "content_hash",
                }[manifest_field]
                break
        if relative_path and len(candidates) == 1:
            mapped[relative_path] = candidates[0]
            mapping_modes[relative_path] = matched_mode
            used.add(id(candidates[0]))
        else:
            unresolved.append((relative_path or str(image.get("image_id") or "<unknown>"), image))

    available = [
        item
        for item in manifest
        if id(item) not in used and item.get("image_id") and item.get("minio_object_key")
    ]
    if len(images) == len(manifest) and len(unresolved) == len(available):
        for (relative_path, _), entry in zip(unresolved, available, strict=True):
            if relative_path and relative_path != "<unknown>":
                mapped[relative_path] = entry
                mapping_modes[relative_path] = "position"
        unresolved = [item for item in unresolved if item[0] in {"", "<unknown>"}]

    unresolved_paths = [path for path, _ in unresolved]
    return mapped, unresolved_paths, mapping_modes


def rebind_result_images(
    result: PerceptionResult,
    document_id: str,
    mapping: dict[str, dict[str, Any]],
) -> None:
    """Use already-persisted image IDs in the regenerated parse result."""

    images = result.metadata.get("images")
    if not isinstance(images, list):
        images = []
    for image in images:
        if not isinstance(image, dict):
            continue
        relative_path = str(image.get("relativePath") or image.get("relative_path") or "")
        entry = mapping.get(relative_path)
        if not entry:
            continue
        image_id = str(entry["image_id"])
        image["image_id"] = image_id
        image["minio_object_key"] = entry["minio_object_key"]
        image["url"] = perception_image_url(document_id, image_id)
    result.blocks = _attach_block_image_ids(result.blocks, images)
    for field in ("raw_text", "text"):
        value = getattr(result, field)
        setattr(result, field, normalize_markdown_image_references(value, images))


if __name__ == "__main__":
    raise SystemExit(main())
