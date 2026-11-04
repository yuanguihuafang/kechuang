"""Replace the audited robot solution attachment without re-extracting its draft."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from agents.retrieval_agent.application.retrieval_service import index_record_id
from scripts.repair_solution_source_groups import (
    _build_runtime,
    _collection_rows,
    _json_safe,
    _milvus_expr,
    _target_expression,
    deduplicate_chunk_images,
)
from services.application.business_records import canonical_source_id
from services.application.draft_publishing import build_published_draft_index_request
from services.mysql.mysql_sink import MySQLBusinessRecordSink
from services.storage.minio.image_service import perception_image_url
from services.storage.minio.minio_storage import MinioUploadService

CONFIRMATION = "REPLACE-AUDITED-ROBOT-SOLUTION-V2"
DOCUMENT_ID = "doc_bf3e2039_bdeaf9bf66b717c2"
DRAFT_ID = "draft_fccf4705e4a649f7ac9bd32d87802e73"
MYSQL_ID = "284"
EXPECTED_SOURCE_ID = "source:ca2535c9d35d99beb1ba148aa90b16c9"


def _progress(message: str) -> None:
    print(f"[robot-solution-repair] {message}", file=sys.stderr, flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_backup(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(_json_safe(payload), stream, ensure_ascii=False, separators=(",", ":"))


def _preflight(runtime: Any, source: Path) -> dict[str, Any]:
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("替换原件必须是存在的 PDF 文件")
    if source.stat().st_size <= 0 or source.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("PDF 文件大小不在允许范围内（1–50 MiB）")

    document = runtime.lifecycle.get_document(DOCUMENT_ID, include_purged=True)
    versions = runtime.lifecycle.list_versions(DOCUMENT_ID)
    if (
        not document
        or document.get("knowledge_type") != "solution"
        or document.get("status") != "PURGED"
        or len(versions) != 1
        or versions[0].get("version_id") != f"{DOCUMENT_ID}:v1"
        or versions[0].get("status") != "PURGED"
    ):
        raise ValueError("文档生命周期与审计快照不符，拒绝覆盖")

    draft_record = runtime.drafts.get_admin(DRAFT_ID)
    if not draft_record or draft_record.get("status") != "PUBLISHED":
        raise ValueError("目标草稿不存在或已不再是 PUBLISHED")
    draft = draft_record.get("draft") or {}
    if (
        str(draft.get("mysql_record_id") or "") != MYSQL_ID
        or canonical_source_id(draft.get("source_id")) != EXPECTED_SOURCE_ID
    ):
        raise ValueError("草稿 MySQL 主键或 source_id 与审计值不符")
    attachments = [
        item for item in draft.get("attachments") or []
        if isinstance(item, dict) and item.get("document_id") == DOCUMENT_ID
    ]
    if (
        len(attachments) != 1
        or attachments[0].get("version_id") != f"{DOCUMENT_ID}:v1"
        or not draft.get("source_blocks")
    ):
        raise ValueError("草稿附件/来源块不符合预期，拒绝重建")

    mysql_rows = runtime.mysql.list_for_sync("solution", after_id=int(MYSQL_ID) - 1, limit=1)
    if len(mysql_rows) != 1 or str(mysql_rows[0].get("id")) != MYSQL_ID:
        raise ValueError("MySQL 记录缺失或无法唯一读取")

    storage = MinioUploadService()
    old_key = str(attachments[0].get("storage_key") or "")
    if not old_key or asyncio.run(storage.inspect_object(old_key)) is not None:
        raise ValueError("旧附件对象状态与审计快照不符，拒绝覆盖")
    if (
        asyncio.run(storage.load_parse_result(DOCUMENT_ID)) is not None
        or asyncio.run(storage.load_image_manifest(DOCUMENT_ID))
        or asyncio.run(storage.load_mineru_artifact_manifest(DOCUMENT_ID))
    ):
        raise ValueError("文档派生产物已变化；请先重新审计，不覆盖现有产物")

    target = runtime.agent.public_milvus["solution"]
    rows = _collection_rows(target, _target_expression(DOCUMENT_ID, MYSQL_ID))
    source_ids = {canonical_source_id(row.get("source_id")) for row in rows if row.get("source_id")}
    if source_ids != {EXPECTED_SOURCE_ID}:
        raise ValueError(f"Milvus source_id 与审计值不符：{sorted(source_ids)}")
    if any(
        row.get("chunk_role") == "admin_doc"
        and str(row.get("mysql_id") or "") not in {"", MYSQL_ID}
        for row in rows
    ):
        raise ValueError("admin_doc 向量关联了其他 MySQL 记录，拒绝替换")

    return {
        "document": document,
        "draft_record": draft_record,
        "mysql_row": mysql_rows[0],
        "milvus_rows": rows,
        "old_storage_key": old_key,
        "source_sha256": _sha256(source),
        "source_size": source.stat().st_size,
        "new_file_name": source.name,
    }


async def _apply(runtime: Any, source: Path, backup_file: Path, state: dict[str, Any]) -> dict[str, Any]:
    storage = MinioUploadService()
    target = runtime.agent.public_milvus["solution"]
    draft_record = state["draft_record"]
    old_draft = draft_record["draft"]
    versions = runtime.lifecycle.list_versions(DOCUMENT_ID)
    version_number = max(int(item.get("version_number") or 0) for item in versions) + 1
    version_id = f"{DOCUMENT_ID}:v{version_number}"

    backup = {
        "document": state["document"],
        "draft_record": draft_record,
        "mysql_row": state["mysql_row"],
        "milvus_rows": _json_safe(state["milvus_rows"]),
        "source_sha256": state["source_sha256"],
        "old_storage_key": state["old_storage_key"],
        "new_version_id": version_id,
    }
    _write_backup(backup_file, backup)
    _progress(f"备份已写入 {backup_file}")

    storage_key, content_hash = await storage.upload_path(
        source,
        source.name,
        "application/pdf",
        content_hash=state["source_sha256"],
        document_id=DOCUMENT_ID,
        scope_id="public",
        user_id="admin",
        version=version_number,
    )
    info = await storage.inspect_object(storage_key)
    if not info or int(info.get("size") or 0) != state["source_size"]:
        raise RuntimeError("新 PDF 上传后大小核验失败；备份已保留")
    _progress(f"新附件上传完成：{storage_key} ({state['source_size']} bytes)")

    from agents.perception_agent.routes import configure_storage_service, parse_stored_document

    configure_storage_service(storage)
    _progress("开始解析 PDF（extract_draft=False，不触发字段提取）")
    parsed = await parse_stored_document(
        document_id=DOCUMENT_ID,
        storage_key=storage_key,
        file_name=source.name,
        knowledge_type="solution",
        extract_draft=False,
    )
    if parsed.status != "SUCCESS" or not parsed.raw_text.strip() or not parsed.blocks:
        raise RuntimeError(f"PDF 解析未成功：{parsed.error or parsed.status}；备份和新附件已保留")
    text = parsed.raw_text
    if sum(term in text for term in ("SLAM", "RK-682", "COMFOMASTER", "机器人")) < 2:
        raise RuntimeError("解析内容与已审阅的机器人方案不匹配；未改 PostgreSQL/MySQL/Milvus")

    images = [item for item in (parsed.metadata or {}).get("images", []) if isinstance(item, dict)]
    manifest = await storage.load_image_manifest(DOCUMENT_ID)
    image_ids = {str(item.get("image_id") or "") for item in manifest}
    if not images or not manifest or any(
        not str(image.get("image_id") or "")
        or str(image.get("image_id") or "") not in image_ids
        or not image.get("minio_object_key")
        for image in images
        if image.get("imageRole") != "decorative"
    ):
        raise RuntimeError("解析图片清单/对象不完整；未改 PostgreSQL/MySQL/Milvus")
    for image in manifest:
        key = str(image.get("minio_object_key") or "")
        if not key or await storage.inspect_object(key) is None:
            raise RuntimeError(f"解析图片对象回读失败：{image.get('image_id') or key}")
    saved_parse = await storage.load_parse_result(DOCUMENT_ID)
    if not saved_parse:
        raise RuntimeError("解析结果未能从 MinIO 回读；未改 PostgreSQL/MySQL/Milvus")
    saved_parse["version_id"] = version_id
    await storage.save_parse_result(DOCUMENT_ID, saved_parse)
    candidates = [item for item in manifest if item.get("image_role") != "decorative"]
    if not candidates:
        candidates = manifest
    candidates.sort(key=lambda item: (int(item.get("page_number") or 10**9), str(item.get("image_id") or "")))
    cover_id = str(candidates[0].get("image_id") or "")
    cover_url = perception_image_url(DOCUMENT_ID, cover_id)
    _progress(f"解析成功：{len(parsed.blocks)} 个来源块、{len(manifest)} 张图片；封面取第 {candidates[0].get('page_number') or '?'} 页候选图")

    artifact_refs = dict((parsed.metadata or {}).get("artifact_refs") or {})
    if not artifact_refs or not (parsed.metadata or {}).get("artifact_manifest_key"):
        raise RuntimeError("MinerU 解析产物清单缺失；未改 PostgreSQL/MySQL/Milvus")
    runtime.lifecycle.create_version(
        document_id=DOCUMENT_ID,
        version_id=version_id,
        source_name=source.name,
        storage_key=storage_key,
        scope_id="public",
        user_id="admin",
        version_number=version_number,
        knowledge_type="solution",
        status="UPLOADED",
        artifact_refs={
            "manifest": (parsed.metadata or {}).get("artifact_manifest_key"),
            "parse_result": (parsed.metadata or {}).get("parse_result_key"),
            "images": f"derived/{DOCUMENT_ID}/image-manifest.json",
        },
    )

    attachments = [
        {
            **item,
            "file_name": source.name,
            "version_id": version_id,
            "storage_key": storage_key,
        }
        if isinstance(item, dict) and item.get("document_id") == DOCUMENT_ID
        else item
        for item in old_draft.get("attachments") or []
    ]
    updated_draft = {
        **old_draft,
        "source_id": EXPECTED_SOURCE_ID,
        "attachments": attachments,
        "source_blocks": [block.model_dump(mode="json") for block in parsed.blocks],
        "fields": {**(old_draft.get("fields") or {}), "first_img_url": cover_url},
        "mysql_fields": {**(old_draft.get("mysql_fields") or {}), "first_img_url": cover_url},
    }
    request = build_published_draft_index_request({**draft_record, "draft": updated_draft})
    if request.version_id != version_id or request.document_id != DOCUMENT_ID:
        raise RuntimeError("重建索引请求没有绑定到新文档版本")
    if any(canonical_source_id(chunk.source_id) != EXPECTED_SOURCE_ID for chunk in request.chunks):
        raise RuntimeError("重建索引请求 source_id 不一致")
    preview = runtime.agent.preview_chunks(request)
    final_chunks, removed_images = deduplicate_chunk_images(preview, parsed.raw_text)
    if not final_chunks:
        raise RuntimeError("正式分块结果为空")
    request = request.model_copy(update={
        "chunks": final_chunks,
        "chunks_are_final": True,
        "replace_existing": True,
    })
    expected_ids = {index_record_id(request.document_id, chunk) for chunk in request.chunks}
    _progress(f"开始替换 Milvus 来源组：{len(request.chunks)} 个分块；跨块去重图片 {removed_images} 张")
    result = await runtime.agent.index(request)
    if result.status != "SUCCESS" or result.indexed_count != len(expected_ids):
        raise RuntimeError(f"Milvus 替换未完整成功：{result.error or result.status}; indexed={result.indexed_count}")

    mysql_vector_rows = _collection_rows(
        target, f'chunk_role == "mysql" && mysql_id == {_milvus_expr(MYSQL_ID)}'
    )
    target.delete_by_ids({str(row["id"]) for row in mysql_vector_rows if row.get("id")})
    actual = _collection_rows(target, _target_expression(DOCUMENT_ID, MYSQL_ID))
    actual_ids = {str(row.get("id") or "") for row in actual if row.get("id")}
    if actual_ids != expected_ids or any(
        canonical_source_id(row.get("source_id")) != EXPECTED_SOURCE_ID
        for row in actual
    ):
        raise RuntimeError("Milvus 替换后主键或 source_id 核验失败；备份已保留")
    if any(row.get("chunk_role") == "mysql" for row in actual):
        raise RuntimeError("旧 mysql 重复向量清理后仍有残留")
    _progress(f"Milvus 核验通过：{len(actual)} 条，无旧 mysql 角色向量")

    updated = runtime.drafts.update(
        DRAFT_ID,
        user_id=str(draft_record.get("user_id") or "admin"),
        draft={
            "source_id": EXPECTED_SOURCE_ID,
            "attachments": attachments,
            "source_blocks": updated_draft["source_blocks"],
            "fields": {"first_img_url": cover_url},
            "mysql_fields": {"first_img_url": cover_url},
        },
        expected_version=draft_record.get("version"),
        replace_source_blocks=True,
        replace_fields=False,
    )
    if not updated:
        raise RuntimeError("PostgreSQL 草稿更新失败；索引和备份已保留")
    _progress("PostgreSQL 草稿附件版本、解析来源块和封面已更新；其余业务字段保持原值")

    mysql_result = MySQLBusinessRecordSink().update(
        record_type="solution",
        record_id=MYSQL_ID,
        structured_data={"first_img_url": cover_url},
    )
    if mysql_result.get("status") != "UPDATED":
        raise RuntimeError("MySQL 原记录封面更新未确认；没有创建新记录")
    runtime.lifecycle.update_version(
        version_id,
        status="PUBLISHED",
        publication_status="PUBLISHED",
        index_status="INDEXED",
        parsed_block_count=len(parsed.blocks),
        chunk_count=len(request.chunks),
        indexed_count=result.indexed_count,
    )
    _progress("MySQL 原主键 284 已更新，生命周期版本已标记 PUBLISHED/INDEXED")

    final_draft = runtime.drafts.get_admin(DRAFT_ID)
    final_document = runtime.lifecycle.get_document(DOCUMENT_ID)
    final_mysql = runtime.mysql.list_for_sync("solution", after_id=int(MYSQL_ID) - 1, limit=1)
    if (
        not final_draft
        or final_draft.get("status") != "PUBLISHED"
        or str((final_draft.get("draft") or {}).get("mysql_record_id")) != MYSQL_ID
        or (final_draft.get("draft") or {}).get("attachments", [{}])[0].get("version_id") != version_id
        or not final_document
        or (final_document.get("active_version") or {}).get("publication_status") != "PUBLISHED"
        or len(final_mysql) != 1
        or str(final_mysql[0].get("id")) != MYSQL_ID
        or final_mysql[0].get("first_img_url") != cover_url
    ):
        raise RuntimeError("最终 PostgreSQL/MySQL 核验失败；备份已保留")
    return {
        "status": "applied",
        "document_id": DOCUMENT_ID,
        "version_id": version_id,
        "draft_id": DRAFT_ID,
        "mysql_id": MYSQL_ID,
        "source_id": EXPECTED_SOURCE_ID,
        "storage_key": storage_key,
        "sha256": content_hash,
        "images": len(manifest),
        "indexed": result.indexed_count,
        "cover_image_id": cover_id,
        "backup_file": str(backup_file),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", required=True, type=Path)
    parser.add_argument("--backup-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    runtime = _build_runtime()
    try:
        state = _preflight(runtime, args.source_file)
        print(json.dumps({
            "status": "ready_to_apply" if not args.apply else "preflight_passed",
            "document_id": DOCUMENT_ID,
            "draft_id": DRAFT_ID,
            "mysql_id": MYSQL_ID,
            "source_id": EXPECTED_SOURCE_ID,
            "source_file": str(args.source_file),
            "source_size": state["source_size"],
            "source_sha256": state["source_sha256"],
            "existing_milvus_rows": len(state["milvus_rows"]),
            "planned_version": 2,
            "will_extract_draft": False,
        }, ensure_ascii=False, indent=2))
        if not args.apply:
            return 0
        if args.confirm != CONFIRMATION or args.backup_file is None:
            raise ValueError(f"--apply 必须提供 --backup-file 和 --confirm {CONFIRMATION}")
        report = asyncio.run(_apply(runtime, args.source_file, args.backup_file, state))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 必须保留部分写入上下文
        _progress(f"失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
