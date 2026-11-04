"""Restore solution images referenced by published solution documents and drafts.

Run from ``backend``. Default mode parses and reports only; applying requires a
fresh exclusive backup file and the printed confirmation token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from services.document_parsing.mineru_parser import MinerUDocumentParser

CONFIRMATION = "REPAIR-SOLUTION-IMAGE-OBJECTS"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
IMAGE_URL = re.compile(r"/api/v1/perception/images/([^/]+)/([^/\s)\]]+)")


def _progress(message: str) -> None:
    print(f"[solution-image-repair] {message}", file=sys.stderr, flush=True)


def _candidate_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        image
        for image in images
        if str(image.get("imageRole") or image.get("image_role") or "candidate").lower()
        != "decorative"
    ]


def match_reparsed_image(sources: list[str], images: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match one old ref only when its filename identifies one parsed asset."""
    names = {
        PurePosixPath(source.replace("\\", "/").split("?", 1)[0]).name
        for source in sources
        if PurePosixPath(source.replace("\\", "/").split("?", 1)[0]).suffix.lower()
        in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
    }
    if len(names) != 1:
        return None
    matches = [
        image
        for image in images
        if PurePosixPath(
            str(image.get("relativePath") or image.get("relative_path") or "").replace("\\", "/")
        ).name
        == next(iter(names))
    ]
    return matches[0] if len(matches) == 1 else None


def plan_image_mappings(
    missing: dict[str, list[str]], images: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, str]]:
    """Use unique exact names first, then best-effort document order."""
    mapped: dict[str, dict[str, Any]] = {}
    modes: dict[str, str] = {}
    used: set[int] = set()
    unresolved: list[str] = []
    for image_id, sources in missing.items():
        image = match_reparsed_image(sources, images)
        if image is None or id(image) in used:
            unresolved.append(image_id)
        else:
            mapped[image_id] = image
            modes[image_id] = "exact_filename"
            used.add(id(image))
    # MinerU already returns assets in source-document order; preserve it for the
    # explicitly accepted positional fallback rather than sorting by filename.
    candidates = [image for image in images if id(image) not in used]
    if unresolved:
        for image_id, image in zip(unresolved, candidates):
            mapped[image_id] = image
            modes[image_id] = (
                "document_order" if len(unresolved) == len(candidates) else "document_order_partial"
            )
        return mapped, unresolved[len(candidates) :], modes
    return mapped, unresolved, modes


def _load_project_environment() -> None:
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    load_dotenv(root / ".env.local", override=True)


def _collect_refs(draft: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    attachments = [
        item
        for item in draft.get("attachments") or []
        if isinstance(item, dict) and item.get("document_id")
    ]
    attached_ids = [str(item["document_id"]) for item in attachments]
    refs: dict[str, dict[str, list[str]]] = {}
    for block in draft.get("source_blocks") or []:
        if not isinstance(block, dict):
            continue
        metadata = block.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        values = metadata.get("image_refs") or []
        ids = [str(value) for value in metadata.get("image_ids") or [] if value]
        for item in values:
            if not isinstance(item, dict) or not item.get("image_id"):
                continue
            image_id = str(item["image_id"])
            sources = [str(value) for value in item.get("sources") or [] if value]
            owners = {match.group(1) for source in sources if (match := IMAGE_URL.search(source))}
            if not owners and len(attached_ids) == 1:
                owners = {attached_ids[0]}
            for document_id in owners.intersection(attached_ids):
                current = refs.setdefault(document_id, {}).setdefault(image_id, [])
                current.extend(source for source in sources if source not in current)
            if image_id in ids:
                ids.remove(image_id)
        if ids and len(attached_ids) == 1:
            for image_id in ids:
                refs.setdefault(attached_ids[0], {}).setdefault(image_id, [])
    # Cover and other image fields can be outside source_blocks; use their explicit
    # document owner so multi-attachment drafts are not cross-associated.
    for value in _string_values(draft):
        match = IMAGE_URL.search(value)
        if match and match.group(1) in attached_ids:
            refs.setdefault(match.group(1), {}).setdefault(match.group(2), []).append(value)
    return refs


def _string_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)


def _collect_parse_result_refs(document_id: str, result: dict[str, Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}

    def add(image_id: str, source: str = "") -> None:
        sources = refs.setdefault(image_id, [])
        if source and source not in sources:
            sources.append(source)

    for field in ("raw_text", "text"):
        content = result.get(field)
        if isinstance(content, str):
            for match in IMAGE_URL.finditer(content):
                if match.group(1) == document_id:
                    add(match.group(2), match.group(0))

    metadata = result.get("metadata")
    images = metadata.get("images") if isinstance(metadata, dict) else None
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict) or not image.get("image_id"):
                continue
            image_id = str(image["image_id"])
            add(image_id)
            for field in ("relativePath", "relative_path", "path", "url", "image_url"):
                source = image.get(field)
                if not isinstance(source, str) or not source:
                    continue
                match = IMAGE_URL.search(source)
                if not match or match.group(1) == document_id:
                    add(image_id, source)
    return refs


def _manifest_entry(
    document_id: str, image_id: str, asset: dict[str, Any], key: str
) -> dict[str, Any]:
    return {
        "image_id": image_id,
        "document_id": document_id,
        "page_number": asset.get("pageNumber"),
        "source_locator": asset.get("source_locator"),
        "nearby_block_ids": asset.get("nearby_block_ids", []),
        "caption": asset.get("caption"),
        "image_role": asset.get("imageRole", "candidate"),
        "content_hash": asset.get("contentHash"),
        "minio_object_key": key,
        "content_type": asset.get("contentType")
        or mimetypes.guess_type(str(asset.get("path") or ""))[0]
        or "application/octet-stream",
    }


def _build_runtime() -> Any:
    _load_project_environment()
    from services.persistence.document_lifecycle import DocumentLifecycleStore
    from services.persistence.repositories.workflow_repository import PostgresDraftStore
    from services.storage.minio.minio_storage import MinioUploadService

    return type(
        "Runtime",
        (),
        {
            "lifecycle": DocumentLifecycleStore(),
            "drafts": PostgresDraftStore(),
            "storage": MinioUploadService(),
        },
    )()


async def _prepare(
    runtime: Any, temporary_root: Path, target_document_id: str | None = None
) -> list[dict[str, Any]]:
    parser = MinerUDocumentParser(output_path=temporary_root / "mineru-output")
    groups: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        result = runtime.drafts.list_page(
            status="PUBLISHED", draft_types=["solution"], page=page, page_size=100
        )
        _progress(f"扫描已发布草稿，第 {page} 页，共 {result['total']} 条")
        for record in result["items"]:
            draft = record.get("draft") or {}
            for document_id, document_refs in _collect_refs(draft).items():
                if target_document_id and document_id != target_document_id:
                    continue
                group = groups.setdefault(
                    document_id, {"refs": {}, "drafts": set(), "file_name": ""}
                )
                group["drafts"].add(str(record.get("draft_id") or ""))
                for image_id, sources in document_refs.items():
                    target = group["refs"].setdefault(image_id, [])
                    target.extend(source for source in sources if source not in target)
                attachment = next(
                    (
                        item
                        for item in draft.get("attachments") or []
                        if isinstance(item, dict)
                        and str(item.get("document_id") or "") == document_id
                    ),
                    {},
                )
                group["file_name"] = group["file_name"] or str(attachment.get("file_name") or "")
        if page * result["page_size"] >= result["total"]:
            break
        page += 1

    page = 1
    while True:
        result = runtime.lifecycle.list_documents_page(
            knowledge_type="solution", page=page, page_size=100
        )
        _progress(f"扫描已发布方案文档，第 {page} 页，共 {result['total']} 份")
        for document in result["documents"]:
            version = document.get("active_version") or {}
            document_id = str(document.get("document_id") or "")
            if (
                not document_id
                or (target_document_id and document_id != target_document_id)
                or str(version.get("publication_status") or "").upper() != "PUBLISHED"
            ):
                continue
            parse_result = await runtime.storage.load_parse_result(document_id)
            if not parse_result:
                continue
            document_refs = _collect_parse_result_refs(document_id, parse_result)
            if not document_refs:
                continue
            group = groups.setdefault(
                document_id,
                {"refs": {}, "drafts": set(), "file_name": str(document.get("source_name") or "")},
            )
            for image_id, sources in document_refs.items():
                target = group["refs"].setdefault(image_id, [])
                target.extend(source for source in sources if source not in target)
            group["file_name"] = group["file_name"] or str(document.get("source_name") or "")
        if page * result["page_size"] >= result["total"]:
            break
        page += 1

    prepared = []
    for index, (document_id, group) in enumerate(sorted(groups.items()), start=1):
        _progress(
            f"预检 {index}/{len(groups)}：{group['file_name'] or document_id} [{document_id}]"
        )
        document = runtime.lifecycle.get_document(document_id)
        version = (document or {}).get("active_version") or {}
        if (
            not document
            or document.get("knowledge_type") != "solution"
            or str(version.get("publication_status") or "").upper() != "PUBLISHED"
        ):
            continue
        attachments = [
            item
            for record_id in sorted(group["drafts"])
            if (record := runtime.drafts.get_admin(record_id))
            for item in (record.get("draft") or {}).get("attachments") or []
            if isinstance(item, dict)
            and str(item.get("document_id") or "") == document_id
            and str(item.get("version_id") or "") == str(version.get("version_id") or "")
        ]
        if len(attachments) != len(group["drafts"]):
            _progress(f"跳过 {document_id}：已发布草稿附件版本与活动版本不一致")
            prepared.append(
                {
                    "document_id": document_id,
                    "status": "skipped",
                    "reason": "已发布草稿附件版本与活动版本不一致",
                }
            )
            continue
        manifest = await runtime.storage.load_image_manifest(document_id)
        valid_ids = set()
        for item in manifest:
            key = str(item.get("minio_object_key") or "")
            if item.get("image_id") and key and await runtime.storage.inspect_object(key):
                valid_ids.add(str(item["image_id"]))
        existing_hashes = {
            str(item.get("content_hash") or "")
            for item in manifest
            if item.get("image_id") in valid_ids and item.get("content_hash")
        }
        missing = {key: value for key, value in group["refs"].items() if key not in valid_ids}
        if not missing:
            _progress(f"图片引用完整：{document_id}")
            continue
        storage_key = str(version.get("storage_key") or "")
        source_info = await runtime.storage.inspect_object(storage_key) if storage_key else None
        if not source_info:
            _progress(f"跳过 {document_id}：活动原 PDF 对象不存在")
            prepared.append(
                {
                    "document_id": document_id,
                    "file_name": group["file_name"],
                    "status": "skipped",
                    "reason": "活动原 PDF 对象不存在",
                }
            )
            continue
        if int(source_info.get("size") or 0) > MAX_UPLOAD_BYTES:
            _progress(f"跳过 {document_id}：原 PDF 超过解析大小上限")
            prepared.append(
                {
                    "document_id": document_id,
                    "file_name": group["file_name"],
                    "status": "skipped",
                    "reason": f"原 PDF 超过解析上限 {MAX_UPLOAD_BYTES} bytes",
                    "draft_ids": sorted(group["drafts"]),
                }
            )
            continue
        pdf_directory = temporary_root / "pdfs"
        pdf_directory.mkdir(parents=True, exist_ok=True)
        local_pdf, file_name, _, _ = await runtime.storage.download_file_to_directory(
            storage_key, pdf_directory, MAX_UPLOAD_BYTES
        )
        _progress(f"开始重解析 {file_name} [{document_id}]，缺失图片引用 {len(missing)} 个")
        try:
            parsed = await parser.parse(local_pdf)
        except Exception as exc:  # noqa: BLE001 - parser errors vary by configured runner
            _progress(f"重解析失败 {document_id}：{type(exc).__name__}: {exc}")
            prepared.append(
                {
                    "document_id": document_id,
                    "file_name": file_name,
                    "status": "failed",
                    "reason": f"原 PDF 重新解析失败：{type(exc).__name__}: {exc}",
                    "draft_ids": sorted(group["drafts"]),
                }
            )
            continue
        all_images = [
            item for item in (parsed.metadata.get("images") or []) if isinstance(item, dict)
        ]
        candidate_images = _candidate_images(all_images)
        images = [
            item
            for item in candidate_images
            if not item.get("contentHash") or str(item["contentHash"]) not in existing_hashes
        ]
        mappings, unmapped, mapping_modes = plan_image_mappings(missing, images)
        _progress(
            f"重解析完成 {document_id}：发现图片 {len(all_images)} 张，"
            f"过滤装饰图 {len(all_images) - len(candidate_images)} 张，"
            f"可恢复 {len(mappings)} 个，未匹配 {len(unmapped)} 个"
        )
        output_root = (temporary_root / "mineru-output").resolve()
        safe_mappings = {}
        for image_id, asset in mappings.items():
            image_path = Path(str(asset.get("path") or "")).resolve()
            if output_root not in image_path.parents or not image_path.is_file():
                unmapped.append(image_id)
                continue
            safe_mappings[image_id] = asset
        prepared.append(
            {
                "document_id": document_id,
                "file_name": file_name,
                "version_id": version.get("version_id"),
                "storage_key": storage_key,
                "source_etag": source_info.get("etag"),
                "manifest": manifest,
                "mappings": safe_mappings,
                "mapping_modes": {key: mapping_modes[key] for key in safe_mappings},
                "unmapped": sorted(set(unmapped)),
                "image_count": len(all_images),
                "filtered_decorative_count": len(all_images) - len(candidate_images),
                "draft_ids": sorted(group["drafts"]),
            }
        )
    return prepared


async def _apply_group(runtime: Any, group: dict[str, Any], run_id: str) -> dict[str, Any]:
    current = runtime.lifecycle.get_document(group["document_id"])
    version = (current or {}).get("active_version") or {}
    source = await runtime.storage.inspect_object(group["storage_key"])
    if (
        not current
        or current.get("knowledge_type") != "solution"
        or str(version.get("publication_status") or "").upper() != "PUBLISHED"
        or version.get("version_id") != group["version_id"]
        or not source
        or source.get("etag") != group["source_etag"]
    ):
        raise RuntimeError("活动版本或原 PDF 在预检后发生变化")
    additions = []
    new_keys = []
    for image_id, asset in group["mappings"].items():
        image_path = Path(str(asset["path"]))
        relative = f"recovered-images/{image_id}{image_path.suffix.lower()}"
        key = await runtime.storage.upload_mineru_artifact(
            group["document_id"],
            relative,
            image_path,
            asset.get("contentType"),
            attempt_id=run_id,
        )
        new_keys.append(key)
        additions.append(_manifest_entry(group["document_id"], image_id, asset, key))
    original = list(group["manifest"])
    replacement_ids = {item["image_id"] for item in additions}
    updated_manifest = [
        item for item in original if str(item.get("image_id") or "") not in replacement_ids
    ] + additions
    manifest_write_started = False
    try:
        current_manifest = await runtime.storage.load_image_manifest(group["document_id"])
        if current_manifest != original:
            raise RuntimeError("图片清单在预检后发生变化，拒绝覆盖")
        manifest_write_started = True
        await runtime.storage.save_image_manifest(group["document_id"], updated_manifest)
        for item in additions:
            if not await runtime.storage.inspect_object(item["minio_object_key"]):
                raise RuntimeError(f"图片对象写入后核验失败：{item['image_id']}")
        return {
            "document_id": group["document_id"],
            "status": "SUCCESS",
            "restored": len(additions),
            "unmapped": group["unmapped"],
        }
    except Exception:
        if manifest_write_started:
            try:
                await runtime.storage.save_image_manifest(group["document_id"], original)
            except Exception as rollback_error:
                raise RuntimeError(
                    "图片清单写入失败且回滚清单也失败；新图片对象已保留，需人工核对"
                ) from rollback_error
        if new_keys:
            await runtime.storage.delete_derived_objects(group["document_id"], new_keys)
        raise


async def run(
    *, apply: bool, backup_file: Path | None, document_id: str | None = None
) -> dict[str, Any]:
    runtime = _build_runtime()
    with tempfile.TemporaryDirectory(prefix="solution-image-repair-") as temporary:
        try:
            prepared = await _prepare(runtime, Path(temporary), target_document_id=document_id)
            report = [
                {
                    key: value
                    for key, value in group.items()
                    if key not in {"manifest", "mappings", "mapping_modes"}
                }
                | {
                    "restorable": len(group.get("mappings", {})),
                    "mapping_modes": sorted(set(group.get("mapping_modes", {}).values())),
                    "mapping_plan": [
                        {
                            "image_id": image_id,
                            "relative_path": asset.get("relativePath")
                            or asset.get("relative_path"),
                            "page_number": asset.get("pageNumber"),
                            "mode": group["mapping_modes"][image_id],
                        }
                        for image_id, asset in group.get("mappings", {}).items()
                    ],
                }
                for group in prepared
            ]
            if not apply:
                return {"status": "dry_run", "groups": report}
            if backup_file is None:
                raise ValueError("--apply 必须同时提供 --backup-file")
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            before = {
                group["document_id"]: {
                    "version_id": group.get("version_id"),
                    "manifest": group.get("manifest", []),
                }
                for group in prepared
                if group.get("mappings")
            }
            fd = os.open(backup_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(before, stream, ensure_ascii=False, indent=2)
            results = []
            run_id = uuid.uuid4().hex
            for group in prepared:
                if not group.get("mappings"):
                    continue
                _progress(
                    f"写入 {len(results) + 1}/{sum(bool(item.get('mappings')) for item in prepared)}："
                    f"{group['file_name']} [{group['document_id']}]"
                )
                try:
                    result = await _apply_group(runtime, group, run_id)
                    results.append(result)
                    _progress(
                        f"写入完成 {group['document_id']}：恢复 {result['restored']} 张，"
                        f"未匹配 {len(result['unmapped'])} 个"
                    )
                except Exception as exc:  # noqa: BLE001 - report partial writes with backup path
                    _progress(f"写入失败 {group['document_id']}：{type(exc).__name__}: {exc}")
                    results.append(
                        {
                            "document_id": group["document_id"],
                            "status": "FAILED",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    break
            complete = (
                results
                and all(
                    item["status"] == "SUCCESS" and not item.get("unmapped") for item in results
                )
                and all(group.get("status") not in {"failed", "skipped"} for group in report)
            )
            return {
                "status": "applied" if complete else "partial",
                "backup_file": str(backup_file),
                "results": results,
                "preflight": report,
            }
        finally:
            runtime.lifecycle.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从已发布方案文档 PDF 恢复缺失图片对象；默认只预检"
    )
    parser.add_argument(
        "--apply", action="store_true", help="按预检映射写入 MinIO 图片对象和图片清单"
    )
    parser.add_argument("--document-id", help="仅处理指定的方案文档 ID")
    parser.add_argument("--confirm", help=f"执行确认词：{CONFIRMATION}")
    parser.add_argument("--backup-file", type=Path, help="执行前独占创建的 MinIO 图片清单备份 JSON")
    args = parser.parse_args(argv)
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
    if args.backup_file and not args.apply:
        parser.error("--backup-file 仅可与 --apply 一起使用")
    result = asyncio.run(
        run(apply=args.apply, backup_file=args.backup_file, document_id=args.document_id)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] in {"partial", "preflight_failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
