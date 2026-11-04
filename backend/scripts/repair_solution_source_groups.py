"""Repair the nine audited solution source groups.

Run from ``backend``. Default mode is read-only; ``--apply`` requires an
exclusive backup path and the exact confirmation token printed by ``--help``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agents.retrieval_agent.application.retrieval_service import index_record_id
from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
from services.application.business_records import _source_id, canonical_source_id
from services.application.draft_publishing import (
    build_published_draft_index_request,
    draft_index_fingerprint,
)

CONFIRMATION = "REPAIR-AUDITED-SOLUTION-GROUPS"
TARGETS = (
    (
        "doc_01daa90c_94999da5f724c78f",
        "draft_bf0374c50ff54fb681f1ebb358d62bac",
        "106",
        "产业化成果目录.pdf",
    ),
    (
        "doc_0e35c0ab_56d2a9036d76df50",
        "draft_2c03e30257c045c2ba1e7b85040af48a",
        "42",
        "更新于2602-炽橙行业典型项目案例集（单页ppt自选）(1).pdf",
    ),
    (
        "doc_231d736f_6ed378d825e9135a",
        "draft_dbedb247c47c4b9293dc6f0255779b9e",
        "113",
        "国测海遥宣传册.pdf",
    ),
    (
        "doc_3d02c5cb_4ada8292b8b929a0",
        "draft_464c09569ffb44f0a9cf3d5f3d43a4f4",
        "142",
        "1.1 明思为智能运维案例集.pdf",
    ),
    (
        "doc_475af70c_cc56a471a966b93e",
        "draft_745be90db40b4112ba1cb798f0bacd80",
        "133",
        "公司宣传资料（简介）.pdf",
    ),
    (
        "doc_7961b4d8_e2dbdd602d3abf91",
        "draft_cd107ee50c8b4581bd129960df465105",
        "56",
        "兮易公司智能体合集介绍-数字人场景(20260613).pdf",
    ),
    (
        "doc_89b047c8_e2927ee571015065",
        "draft_8e5f0f81b238415d9946f0cbd6354e1f",
        "95",
        "AI企微机器人、AI外呼机器人.pdf",
    ),
    (
        "doc_b33f259b_7f6b43e743e028e0",
        "draft_7088fc60666848498ff86105e88ace15",
        "163",
        "尼欧迪克除尘设备产品手册.pdf",
    ),
    (
        "doc_e5d8d409_96da4efd067a612d",
        "draft_927f99cb4aaa4ee7927887e68ebe7e2d",
        "86",
        "青岛北创智能科技有限公司2025年新产品介绍.pdf",
    ),
)
IMAGE_MARKDOWN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
IMAGE_API_URL = re.compile(r"/api/v1/perception/images/([^/]+)/([^/?#)]+)")


def _load_project_environment() -> None:
    from dotenv import load_dotenv

    backend_root = Path(__file__).resolve().parents[1]
    load_dotenv(backend_root / ".env", override=False)
    load_dotenv(backend_root / ".env.local", override=True)


def resolve_source_id(
    draft_source_id: str | None,
    milvus_source_ids: list[str],
    document_ids: list[str] | None = None,
) -> str:
    """Use the single established group ID or the standard attachment fallback."""
    draft_id = canonical_source_id(draft_source_id)
    existing = {canonical_source_id(value) for value in milvus_source_ids if value}
    existing.discard("")
    if len(existing) > 1 or (draft_id and existing and existing != {draft_id}):
        raise ValueError(f"source_id 冲突：draft={draft_id or '—'}，Milvus={sorted(existing)}")
    resolved = draft_id or next(iter(existing), None)
    if not resolved:
        resolved = _source_id([{"document_id": value} for value in document_ids or []])
    if not resolved:
        raise ValueError("无法确定 source_id")
    return canonical_source_id(resolved)


def deduplicate_chunk_images(
    chunks: list[IndexChunk], raw_text: str
) -> tuple[list[IndexChunk], int]:
    """Keep the first occurrence of each image URL across overlapping chunks."""
    source_urls = list(
        dict.fromkeys(match.group(1).strip() for match in IMAGE_MARKDOWN.finditer(raw_text))
    )
    source_url_set = set(source_urls)
    seen: set[str] = set()
    removed = 0
    result: list[IndexChunk] = []
    for chunk in chunks:
        metadata = dict(chunk.metadata or {})
        text = chunk.chunk_text

        def keep(match: re.Match[str]) -> str:
            nonlocal removed
            url = match.group(1).strip()
            if url not in source_url_set or url not in seen:
                seen.add(url)
                return match.group(0)
            removed += 1
            return ""

        text = IMAGE_MARKDOWN.sub(keep, text)
        ids = []
        for url in source_urls:
            parsed = IMAGE_API_URL.search(url)
            if parsed and url in text and parsed.group(2) not in ids:
                ids.append(parsed.group(2))
        if ids:
            metadata["image_ids"] = ids
            metadata["image_urls"] = [
                match.group(1).strip()
                for match in IMAGE_MARKDOWN.finditer(text)
                if IMAGE_API_URL.search(match.group(1).strip())
            ]
        else:
            metadata.pop("image_ids", None)
            metadata.pop("image_urls", None)
        result.append(chunk.model_copy(update={"chunk_text": text.strip(), "metadata": metadata}))
    return [chunk for chunk in result if chunk.chunk_text], removed


def _build_runtime():
    _load_project_environment()

    from agents.retrieval_agent.application.retrieval_service import RetrievalAgent
    from services.mysql.business_search import MySQLBusinessSearchService
    from services.persistence.document_lifecycle import DocumentLifecycleStore
    from services.persistence.repositories.workflow_repository import PostgresDraftStore
    from services.storage.minio.minio_storage import MinioUploadService

    lifecycle = DocumentLifecycleStore()
    drafts = PostgresDraftStore()
    storage = MinioUploadService()
    agent = RetrievalAgent()
    return SimpleNamespace(
        lifecycle=lifecycle,
        drafts=drafts,
        storage=storage,
        agent=agent,
        mysql=MySQLBusinessSearchService(),
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _collection_rows(target, expression: str) -> list[dict[str, Any]]:
    collection = target._existing_collection()
    if collection is None:
        raise RuntimeError("Milvus solution collection 不存在")
    fields = [
        "id",
        "document_id",
        "mysql_id",
        "source_id",
        "chunk_role",
        "status",
        "update_time",
        "chunk_index",
        "chunk_text",
        "dense_vector",
        "source_locator",
        "field_names",
    ]
    return [row for batch in target._query_batches(collection, expression, fields) for row in batch]


def _milvus_expr(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _target_expression(document_id: str, mysql_id: str) -> str:
    mysql_literal = _milvus_expr(mysql_id)
    document_literal = _milvus_expr(document_id)
    return (
        f'(chunk_role == "admin_draft" && mysql_id == {mysql_literal}) || '
        f'(chunk_role == "admin_doc" && document_id == {document_literal}) || '
        f'(chunk_role == "mysql" && mysql_id == {mysql_literal})'
    )


async def _prepare_one(runtime, item: tuple[str, str, str, str]) -> dict[str, Any]:
    document_id, draft_id, mysql_id, file_name = item
    draft_record = runtime.drafts.get_admin(draft_id)
    if not draft_record or draft_record.get("status") != "PUBLISHED":
        raise ValueError(f"{draft_id} 不存在或不是 PUBLISHED 草稿")
    draft = draft_record.get("draft") or {}
    if str(draft.get("mysql_record_id") or "") != mysql_id:
        raise ValueError(f"{draft_id} 的 mysql_record_id 与审计清单不一致")
    attachments = [
        attachment
        for attachment in draft.get("attachments") or []
        if isinstance(attachment, dict) and str(attachment.get("document_id") or "") == document_id
    ]
    if len(attachments) != 1:
        raise ValueError(f"{draft_id} 未唯一关联审计附件 {document_id}")
    attachment = attachments[0]
    if str(attachment.get("file_name") or "") != file_name:
        raise ValueError(f"{draft_id} 附件文件名与审计清单不一致")
    document = runtime.lifecycle.get_document(document_id)
    if not document or document.get("knowledge_type") != "solution":
        raise ValueError(f"{document_id} 不是方案库文档")
    version = document.get("active_version") or {}
    if str(version.get("publication_status") or "").upper() != "PUBLISHED" or str(
        version.get("status") or ""
    ).upper() not in {"PUBLISHED", "INDEXED"}:
        raise ValueError(f"{document_id} 当前活动版本不是已发布来源")
    attached_version = str(attachment.get("version_id") or "").strip()
    if attached_version and attached_version != str(version.get("version_id") or ""):
        raise ValueError(f"{document_id} 草稿附件版本与活动版本不一致")
    storage_key = str(version.get("storage_key") or attachment.get("storage_key") or "").strip()
    if not storage_key or await runtime.storage.inspect_object(storage_key) is None:
        raise ValueError(f"{document_id} 原始附件对象缺失")
    parse_result = await runtime.storage.load_parse_result(document_id)
    if not parse_result or parse_result.get("edited") is not True:
        raise ValueError(f"{document_id} 没有已保存的管理员解析原文，拒绝用未确认内容覆盖")
    raw_text = str((parse_result or {}).get("raw_text") or "").strip()
    if not raw_text:
        raise ValueError(f"{document_id} MinIO 解析原文缺失")
    raw_fingerprint = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    parse_version = str(parse_result.get("version_id") or "").strip()
    if parse_version and parse_version != str(version.get("version_id") or ""):
        raise ValueError(f"{document_id} 解析原文属于其他版本")
    images = await runtime.storage.load_image_manifest(document_id)
    image_ids = {str(image.get("image_id") or "").strip() for image in images}
    for image in images:
        image_id = str(image.get("image_id") or "").strip()
        object_key = str(image.get("minio_object_key") or "").strip()
        if (
            not image_id
            or not object_key
            or await runtime.storage.inspect_object(object_key) is None
        ):
            raise ValueError(
                f"{document_id} 图片清单含缺失对象：{image_id or object_key or 'unknown'}"
            )
    mysql_rows = runtime.mysql.list_for_sync("solution", after_id=int(mysql_id) - 1, limit=1)
    if len(mysql_rows) != 1 or str(mysql_rows[0].get("id")) != mysql_id:
        raise ValueError(f"MySQL 方案记录 {mysql_id} 缺失或无法唯一读取")

    target = runtime.agent.public_milvus["solution"]
    existing = _collection_rows(target, _target_expression(document_id, mysql_id))
    unrelated_doc_rows = [
        row
        for row in existing
        if row.get("chunk_role") == "admin_doc"
        and str(row.get("mysql_id") or "") not in {"", mysql_id}
    ]
    if unrelated_doc_rows:
        raise ValueError(f"{document_id} 的 admin_doc 向量还关联其他 mysql_id，拒绝替换")
    source_rows = _collection_rows(
        target, f'chunk_role == "admin_draft" && mysql_id == {_milvus_expr(mysql_id)}'
    )
    if not source_rows:
        source_rows = _collection_rows(
            target, f'chunk_role == "mysql" && mysql_id == {_milvus_expr(mysql_id)}'
        )
    source_ids = [str(row.get("source_id") or "") for row in source_rows if row.get("source_id")]
    source_id = resolve_source_id(draft.get("source_id"), source_ids, [document_id])
    built = build_published_draft_index_request(draft_record)
    draft_chunks = [chunk for chunk in built.chunks if chunk.chunk_role == "admin_draft"]
    if not draft_chunks:
        raise ValueError(f"{draft_id} 未能从 PUBLISHED 草稿构建 admin_draft")
    chunks = [chunk.model_copy(update={"source_id": source_id}) for chunk in draft_chunks]
    from services.storage.minio.image_service import normalize_image_references

    raw_text = normalize_image_references(
        raw_text, document_id=document_id, manifests={document_id: images}
    )
    refs = {
        parsed.group(2)
        for match in IMAGE_MARKDOWN.finditer(raw_text)
        if (parsed := IMAGE_API_URL.search(match.group(1))) and parsed.group(1) == document_id
    }
    if refs - image_ids:
        raise ValueError(f"{document_id} 原文含未映射图片：{sorted(refs - image_ids)}")
    chunks.append(
        IndexChunk(
            chunk_text=raw_text,
            source_locator=f"{file_name}|type:text|page:unknown|ordinal:1",
            mysql_id=mysql_id,
            source_id=source_id,
            document_id=document_id,
            chunk_role="admin_doc",
        )
    )
    request = IndexRequest(
        document_id=document_id,
        version_id=str(version.get("version_id") or "") or None,
        scope_id="public",
        knowledge_type="solution",
        chunks=chunks,
        replace_existing=True,
        identity=built.identity,
    )
    preview = runtime.agent.preview_chunks(request)
    final_chunks, duplicate_images = deduplicate_chunk_images(preview, raw_text)
    if not final_chunks:
        raise ValueError(f"{document_id} 正式分块结果为空")
    final_request = request.model_copy(
        update={
            "chunks": final_chunks,
            "chunks_are_final": True,
            "replace_existing": True,
        }
    )
    return {
        "document_id": document_id,
        "draft_id": draft_id,
        "mysql_id": mysql_id,
        "file_name": file_name,
        "source_id": source_id,
        "version_id": version.get("version_id"),
        "draft_fingerprint": draft_index_fingerprint(draft),
        "raw_fingerprint": raw_fingerprint,
        "request": final_request,
        "before_rows": existing,
        "planned_count": len(final_chunks),
        "image_count": len(refs),
        "duplicate_images_removed": duplicate_images,
    }


async def run(*, apply: bool, backup_file: Path | None) -> dict[str, Any]:
    runtime = _build_runtime()
    try:
        prepared = []
        errors = []
        for item in TARGETS:
            try:
                prepared.append(await _prepare_one(runtime, item))
            except Exception as exc:  # noqa: BLE001 - collect all preflight failures before writing
                errors.append({"document_id": item[0], "error": f"{type(exc).__name__}: {exc}"})
        if errors:
            return {"status": "preflight_failed", "errors": errors}
        before = {
            group["document_id"]: {
                "draft_id": group["draft_id"],
                "mysql_id": group["mysql_id"],
                "rows": _json_safe(group["before_rows"]),
            }
            for group in prepared
        }
        if apply:
            if backup_file is None:
                raise ValueError("--apply 必须同时提供 --backup-file")
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(backup_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(before, stream, ensure_ascii=False, separators=(",", ":"))
            results = []
            for group in prepared:
                try:
                    current_draft = runtime.drafts.get_admin(group["draft_id"])
                    current_parse = await runtime.storage.load_parse_result(group["document_id"])
                    if (
                        not current_draft
                        or current_draft.get("status") != "PUBLISHED"
                        or draft_index_fingerprint(current_draft.get("draft") or {})
                        != group["draft_fingerprint"]
                        or not current_parse
                        or current_parse.get("edited") is not True
                        or hashlib.sha256(
                            str(current_parse.get("raw_text") or "").strip().encode("utf-8")
                        ).hexdigest()
                        != group["raw_fingerprint"]
                    ):
                        raise RuntimeError("草稿或已保存解析原文在预检后发生变化")
                    result = await runtime.agent.index(group["request"])
                    if result.status != "SUCCESS" or result.indexed_count != group["planned_count"]:
                        raise RuntimeError(
                            f"Milvus 索引失败：{result.error or result.status}; written={result.indexed_count}"
                        )
                    target = runtime.agent.public_milvus["solution"]
                    mysql_rows = _collection_rows(
                        target,
                        f'chunk_role == "mysql" && mysql_id == {_milvus_expr(group["mysql_id"])}',
                    )
                    target.delete_by_ids({str(row["id"]) for row in mysql_rows if row.get("id")})
                    actual = _collection_rows(
                        target, _target_expression(group["document_id"], group["mysql_id"])
                    )
                    actual_ids = {str(row.get("id") or "") for row in actual if row.get("id")}
                    expected_ids = {
                        index_record_id(group["request"].document_id, chunk)
                        for chunk in group["request"].chunks
                    }
                    if actual_ids != expected_ids or any(
                        str(row.get("source_id") or "") != group["source_id"] for row in actual
                    ):
                        raise RuntimeError("Milvus 写入后核验与预期不一致")
                    if _collection_rows(
                        target,
                        f'chunk_role == "mysql" && mysql_id == {_milvus_expr(group["mysql_id"])}',
                    ):
                        raise RuntimeError("旧 mysql 重复向量清理后仍有记录")
                    current_draft = runtime.drafts.get_admin(group["draft_id"])
                    if (
                        not current_draft
                        or current_draft.get("status") != "PUBLISHED"
                        or draft_index_fingerprint(current_draft.get("draft") or {})
                        != group["draft_fingerprint"]
                    ):
                        raise RuntimeError("PostgreSQL 已发布草稿写入后发生变化")
                    current_mysql = runtime.mysql.list_for_sync(
                        "solution", after_id=int(group["mysql_id"]) - 1, limit=1
                    )
                    if (
                        len(current_mysql) != 1
                        or str(current_mysql[0].get("id")) != group["mysql_id"]
                    ):
                        raise RuntimeError("MySQL 记录复核失败")
                    results.append(
                        {
                            "document_id": group["document_id"],
                            "status": "SUCCESS",
                            "written": result.indexed_count,
                            "removed_old_mysql_rows": len(mysql_rows),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - report partial writes with the backup location
                    results.append(
                        {
                            "document_id": group["document_id"],
                            "status": "FAILED",
                            "error": f"{type(exc).__name__}: {exc}",
                            "may_have_written": True,
                        }
                    )
                    break
            return {
                "status": "applied"
                if len(results) == len(prepared)
                and all(item["status"] == "SUCCESS" for item in results)
                else "partial",
                "backup_file": str(backup_file),
                "results": results,
            }
        return {
            "status": "dry_run",
            "groups": [
                {
                    key: group[key]
                    for key in (
                        "document_id",
                        "draft_id",
                        "mysql_id",
                        "file_name",
                        "source_id",
                        "version_id",
                        "planned_count",
                        "image_count",
                        "duplicate_images_removed",
                    )
                }
                for group in prepared
            ],
        }
    finally:
        runtime.agent.close()
        runtime.lifecycle.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定向重写已审计的 9 组方案库向量；默认只预检")
    parser.add_argument("--apply", action="store_true", help="预检全部通过后写入 Milvus")
    parser.add_argument("--confirm", help=f"执行确认词：{CONFIRMATION}")
    parser.add_argument(
        "--backup-file", type=Path, help="执行前独占创建的旧 Milvus 数据 JSON 备份路径"
    )
    args = parser.parse_args(argv)
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
    if args.backup_file and not args.apply:
        parser.error("--backup-file 仅可与 --apply 一起使用")
    report = asyncio.run(run(apply=args.apply, backup_file=args.backup_file))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] in {"preflight_failed", "partial"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
