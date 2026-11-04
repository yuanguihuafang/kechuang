"""Persist recovery anchors for the nine repaired solution source groups.

Run from ``backend``. Default mode is read-only; ``--apply`` requires an
exclusive backup path and the confirmation token printed by ``--help``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from agents.retrieval_agent.application.retrieval_service import index_record_id
from services.application.draft_publishing import draft_index_fingerprint
from services.application.index_recovery_sources import load_confirmed_version_request
from services.persistence.retention import serialize_storage_references

from scripts.repair_solution_source_groups import (
    TARGETS,
    _build_runtime,
    _collection_rows,
    _json_safe,
    _milvus_expr,
    _prepare_one,
    _target_expression,
)

CONFIRMATION = "BACKFILL-AUDITED-SOLUTION-SOURCES"


def build_anchor_payload(
    *, document_id: str, version_id: str, request: Any, task_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    storage_key = f"derived/{document_id}/tasks/{task_id}/index-request.json"
    identity = request.identity.model_dump(mode="json")
    reference = {
        "storage_key": storage_key,
        "task_id": task_id,
        "document_id": document_id,
        "version_id": version_id,
        "identity": identity,
    }
    payload = {
        "chunks": [chunk.model_dump(mode="json") for chunk in request.chunks],
        "identity": identity,
        "document_id": document_id,
        "version_id": version_id,
        "task_id": task_id,
        "scope_id": request.scope_id,
        "knowledge_type": request.knowledge_type,
    }
    return storage_key, reference, payload


def _assert_milvus_matches(group: dict[str, Any], target: Any) -> None:
    document_id = group["document_id"]
    mysql_id = group["mysql_id"]
    rows = _collection_rows(target, _target_expression(document_id, mysql_id))
    expected = {
        index_record_id(group["request"].document_id, chunk): re.sub(
            r"%5d", "]", chunk.chunk_text, flags=re.IGNORECASE
        )
        for chunk in group["request"].chunks
    }
    actual = {
        str(row.get("id") or ""): re.sub(
            r"%5d", "]", str(row.get("chunk_text") or ""), flags=re.IGNORECASE
        )
        for row in rows
        if row.get("id")
    }
    if (
        actual != expected
        or any(row.get("chunk_role") not in {"admin_doc", "admin_draft"} for row in rows)
        or any(str(row.get("source_id") or "") != group["source_id"] for row in rows)
    ):
        raise RuntimeError(f"{document_id} 当前 Milvus 分组与已修复内容不一致，拒绝回填")
    if _collection_rows(
        target,
        f'chunk_role == "mysql" && mysql_id == {_milvus_expr(mysql_id)}',
    ):
        raise RuntimeError(f"{document_id} 对应的旧 mysql 向量仍存在，拒绝回填")


async def _prepare(runtime) -> list[dict[str, Any]]:
    prepared = []
    target = runtime.agent.public_milvus["solution"]
    for item in TARGETS:
        group = await _prepare_one(runtime, item)
        _assert_milvus_matches(group, target)
        document = runtime.lifecycle.get_document(group["document_id"])
        if not document:
            raise ValueError(f"{group['document_id']} 文档目录不存在")
        version = document.get("active_version") or {}
        version_id = str(group.get("version_id") or "")
        snapshot = runtime.lifecycle.get_version(version_id)
        if (
            not snapshot
            or snapshot != version
            or snapshot.get("publication_status") != "PUBLISHED"
            or document.get("knowledge_type") != "solution"
        ):
            raise RuntimeError(f"{group['document_id']} 活动发布版本已变化，拒绝回填")

        existing = (snapshot.get("artifact_refs") or {}).get("confirmed_index_request")
        if existing:
            request = await load_confirmed_version_request(
                runtime.storage, document, snapshot
            )
            expected = {
                index_record_id(group["request"].document_id, chunk)
                for chunk in group["request"].chunks
            }
            if {
                index_record_id(request.document_id, chunk) for chunk in request.chunks
            } != expected:
                raise RuntimeError(f"{group['document_id']} 已有确认来源但内容不匹配")
            group["anchor_status"] = "already_linked"
            group["version_snapshot"] = snapshot
            prepared.append(group)
            continue

        task_id = f"task_repair_{uuid.uuid4().hex}"
        storage_key, reference, payload = build_anchor_payload(
            document_id=group["document_id"],
            version_id=version_id,
            request=group["request"],
            task_id=task_id,
        )
        group.update(
            anchor_status="pending",
            anchor_task_id=task_id,
            storage_key=storage_key,
            reference=reference,
            payload=payload,
            version_snapshot=snapshot,
            document=document,
        )
        prepared.append(group)
    return prepared


async def _save_anchor(runtime, group: dict[str, Any]) -> None:
    current_draft = runtime.drafts.get_admin(group["draft_id"])
    parse_result = await runtime.storage.load_parse_result(group["document_id"])
    current_version = runtime.lifecycle.get_version(group["version_id"])
    if (
        not current_draft
        or current_draft.get("status") != "PUBLISHED"
        or draft_index_fingerprint(current_draft.get("draft") or {})
        != group["draft_fingerprint"]
        or not parse_result
        or parse_result.get("edited") is not True
        or hashlib.sha256(
            str(parse_result.get("raw_text") or "").strip().encode("utf-8")
        ).hexdigest()
        != group["raw_fingerprint"]
        or current_version != group["version_snapshot"]
    ):
        raise RuntimeError(f"{group['document_id']} 草稿、原文或版本已变化，拒绝回填")

    stored_key = await runtime.storage.save_index_request(
        group["document_id"], group["anchor_task_id"], group["payload"]
    )
    if stored_key != group["storage_key"]:
        raise RuntimeError(f"{group['document_id']} MinIO 返回的对象键不符合预期")
    verified = await load_confirmed_version_request(
        runtime.storage,
        group["document"],
        {
            **group["version_snapshot"],
            "artifact_refs": {
                **(group["version_snapshot"].get("artifact_refs") or {}),
                "confirmed_index_request": group["reference"],
            },
        },
    )
    expected_ids = {
        index_record_id(group["request"].document_id, chunk)
        for chunk in group["request"].chunks
    }
    if {
        index_record_id(verified.document_id, chunk) for chunk in verified.chunks
    } != expected_ids:
        raise RuntimeError(f"{group['document_id']} MinIO 回读请求与 Milvus 分组不一致")
    updated = runtime.lifecycle.update_version(
        group["version_id"],
        expected_version=group["version_snapshot"],
        artifact_refs={"confirmed_index_request": group["reference"]},
    )
    if not updated:
        raise RuntimeError(f"{group['document_id']} PostgreSQL 版本引用写入失败")
    await load_confirmed_version_request(runtime.storage, group["document"], updated)


@serialize_storage_references
async def run(*, apply: bool, backup_file: Path | None) -> dict[str, Any]:
    runtime = _build_runtime()
    try:
        try:
            groups = await _prepare(runtime)
        except Exception as exc:  # noqa: BLE001 - report all writes as blocked on preflight failure
            return {"status": "preflight_failed", "error": f"{type(exc).__name__}: {exc}"}

        pending = [group for group in groups if group["anchor_status"] == "pending"]
        if not apply:
            return {
                "status": "dry_run",
                "pending": len(pending),
                "already_linked": len(groups) - len(pending),
                "groups": [
                    {
                        "document_id": group["document_id"],
                        "file_name": group["file_name"],
                        "version_id": group["version_id"],
                        "chunks": group["planned_count"],
                        "anchor_status": group["anchor_status"],
                    }
                    for group in groups
                ],
            }
        if backup_file is None:
            raise ValueError("--apply 必须同时提供 --backup-file")

        backup_file.parent.mkdir(parents=True, exist_ok=True)
        backup = {
            group["document_id"]: {
                "version_id": group["version_id"],
                "artifact_refs": _json_safe(
                    group["version_snapshot"].get("artifact_refs") or {}
                ),
                "anchor_task_id": group.get("anchor_task_id"),
                "storage_key": group.get("storage_key"),
            }
            for group in groups
        }
        descriptor = os.open(backup_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(backup, stream, ensure_ascii=False, separators=(",", ":"))

        results = []
        for group in pending:
            try:
                target = runtime.agent.public_milvus["solution"]
                _assert_milvus_matches(group, target)
                await _save_anchor(runtime, group)
                results.append(
                    {
                        "document_id": group["document_id"],
                        "status": "SUCCESS",
                        "storage_key": group["storage_key"],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - preserve partial results and stop on first failure
                results.append(
                    {
                        "document_id": group["document_id"],
                        "status": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                break
        complete = len(results) == len(pending) and all(
            result["status"] == "SUCCESS" for result in results
        )
        return {
            "status": "applied" if complete else "partial",
            "backup_file": str(backup_file),
            "already_linked": len(groups) - len(pending),
            "results": results,
        }
    finally:
        runtime.agent.close()
        runtime.lifecycle.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为已修复的 9 组方案库数据补回恢复锚点")
    parser.add_argument("--apply", action="store_true", help="预检通过后保存 MinIO 请求并回填 PostgreSQL 版本引用")
    parser.add_argument("--confirm", help=f"执行确认词：{CONFIRMATION}")
    parser.add_argument("--backup-file", type=Path, help="执行前独占创建的版本 artifact_refs 备份文件")
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
