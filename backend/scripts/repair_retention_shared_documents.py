"""恢复已审计的临时任务误清理文档；默认只读，--apply 才写入。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import tempfile
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from scripts.repair_solution_source_groups import _build_runtime, _collection_rows, _json_safe
from services.persistence.retention import storage_reference_guard

DOCUMENT_IDS = (
    "doc_24b4104a_3b7271489005d7f2", "doc_2e4327bf_1fac533e608ac5de",
    "doc_5a3e8744_736d6ebc3296e31f", "doc_6c7306ed_949b65b51ba10817",
    "doc_bf3e2039_bdeaf9bf66b717c2", "doc_e29255d6_523a06f6fb4c7214",
    "doc_fc4c813c_224521b30ef9742e",
)
IMAGE_REF = re.compile(r"/api/v1/perception/images/([^/\s]+)/([a-f0-9]{40})")


def _artifact_refs(doc, parse_key, metadata):
    refs = {"parse_result": parse_key}
    if isinstance(metadata.get("images"), list):
        refs["images"] = f"derived/{doc}/image-manifest.json"
    # DOCX 等原生解析器没有 MinerU 产物，不能保留历史错误引用。
    if metadata.get("artifact_manifest_key"):
        refs["manifest"] = metadata["artifact_manifest_key"]
    return refs


def _snapshot(dsn, doc):
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute(
            "SELECT to_jsonb(d) FROM platform_documents d WHERE document_id=%s", (doc,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"文档目录不存在：{doc}")
        drafts = [r[0] for r in conn.execute(
            "SELECT to_jsonb(p) FROM platform_drafts p WHERE source_document_id=%s "
            "OR content::text LIKE %s ORDER BY draft_id", (doc, f"%{doc}%")
        ).fetchall()]
    return row[0], drafts


def _published_version(document, drafts):
    attachments = [a for d in drafts if d["status"] == "PUBLISHED"
                   for a in d["content"].get("attachments", [])
                   if a.get("document_id") == document["document_id"]]
    ids = {a["version_id"] for a in attachments}
    if len(ids) != 1:
        raise RuntimeError("已发布草稿未唯一绑定同一版本，拒绝自动恢复")
    versions = {v["version_id"]: v for v in [document] + document.get("version_history", [])}
    version = dict(versions[ids.pop()])
    if any(a.get("storage_key") != version.get("storage_key") for a in attachments):
        raise RuntimeError("已发布附件与文档版本对象键不一致")
    return version, [v for k, v in versions.items() if k != version["version_id"]]


async def run(args):
    runtime = _build_runtime()
    from agents.perception_agent.routes import (
        _run_agent_path,
        attach_image_urls,
        configure_storage_service,
        persist_image_assets,
    )
    from runtime.config import get_settings

    dsn = get_settings().database_url
    storage = runtime.storage
    configure_storage_service(storage)
    collection = runtime.agent.public_milvus["solution"]
    plans = []
    targets = args.document_id or DOCUMENT_IDS
    total = len(targets)
    for number, doc in enumerate(targets, 1):
        document, drafts = _snapshot(dsn, doc)
        version, history = _published_version(document, drafts)
        key = version["storage_key"]
        original = await storage.inspect_object(key)
        local = args.source_dir / document["source_name"]
        if original is None:
            if not local.is_file():
                raise RuntimeError(f"找不到可信原件：{local}")
            if not hashlib.sha256(local.read_bytes()).hexdigest().startswith(doc.rsplit("_", 1)[1]):
                raise RuntimeError(f"原件指纹不匹配：{local}")
        rows = _collection_rows(collection, f'document_id == "{doc}"')
        if not rows:
            raise RuntimeError(f"既有索引为空，不能仅恢复状态：{doc}")
        required = {image for owner, image in IMAGE_REF.findall(
            json.dumps([[d for d in drafts if d['status'] == 'PUBLISHED'], rows],
                       ensure_ascii=False, default=str)) if owner == doc}
        print(f"[{number}/{total}] {document['source_name']}：发布版本={version['version_id']}，"
              f"原件={'存在' if original else '待补回'}，索引={len(rows)}，引用图片={len(required)}", flush=True)
        plans.append((document, drafts, version, history, rows, required, local, original))
    if not args.apply:
        print("只读检查通过；未修改任何数据", flush=True)
        return
    args.backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    async with storage_reference_guard():
        for number, (document, drafts, version, history, rows, required, local, original) in enumerate(plans, 1):
            doc = document["document_id"]
            backup = args.backup_dir / f"{doc}.json"
            backup.write_text(json.dumps(_json_safe({"document": document, "drafts": drafts,
                                                    "milvus_rows": rows}), ensure_ascii=False), encoding="utf-8")
            print(f"[{number}/{total}] 已备份 {backup}；恢复原件和解析图片，不提取草稿字段", flush=True)
            if original is None:
                await asyncio.to_thread(storage.client.put_object, Bucket=storage.bucket,
                                        Key=version["storage_key"], Body=local.read_bytes(),
                                        IfNoneMatch="*")
            with tempfile.TemporaryDirectory(prefix="retention-repair-") as temp:
                source, _, _, _ = await storage.download_file_to_directory(
                    version["storage_key"], Path(temp), 100 * 1024 * 1024)
                parsed = await _run_agent_path(
                    source, file_name=document["source_name"], document_id=doc,
                    storage_key=version["storage_key"], knowledge_type="solution", extract_draft=False)
            if parsed.status != "SUCCESS" or not parsed.raw_text.strip():
                raise RuntimeError(f"重解析失败：{doc}：{parsed.error}")
            images = {str(i.get("image_id")) for i in (parsed.metadata or {}).get("images", [])}
            if required - images:
                raise RuntimeError(f"新解析无法原样恢复图片 ID，停止恢复状态：{doc}：{sorted(required-images)}")
            parsed = attach_image_urls(await persist_image_assets(parsed))
            payload = parsed.model_dump(mode="json")
            payload["version_id"] = version["version_id"]
            parse_key = await storage.save_parse_result(doc, payload)
            artifacts = _artifact_refs(doc, parse_key, parsed.metadata or {})
            manifest = await storage.load_image_manifest(doc)
            for image in manifest:
                if await storage.inspect_object(str(image["minio_object_key"])) is None:
                    raise RuntimeError(f"图片回读失败：{doc}：{image['image_id']}")
            for artifact in artifacts.values():
                if await storage.inspect_object(artifact) is None:
                    raise RuntimeError(f"解析产物回读失败：{artifact}")
            with psycopg.connect(dsn) as conn:
                current, current_drafts = _snapshot(dsn, doc)
                if current != document or current_drafts != drafts:
                    raise RuntimeError(f"恢复期间文档或草稿发生变化，停止：{doc}")
                result = conn.execute(
                    "UPDATE platform_documents SET version_id=%s,active_version_id=%s,version_number=%s,"
                    "storage_key=%s,status='PUBLISHED',publication_status='PUBLISHED',index_status='INDEXED',"
                    "artifact_refs=%s,parsed_block_count=%s,chunk_count=%s,indexed_count=%s,"
                    "version_history=%s,error=NULL,updated_at=now() WHERE document_id=%s AND updated_at=%s",
                    (version["version_id"], version["version_id"], version["version_number"],
                     version["storage_key"], Jsonb(artifacts), len(parsed.blocks),
                     len(rows), len(rows), Jsonb(history),
                     doc, document["updated_at"]),
                )
                if result.rowcount != 1:
                    raise RuntimeError("文档更新未唯一命中")
            after, after_drafts = _snapshot(dsn, doc)
            if after["publication_status"] != "PUBLISHED" or after["active_version_id"] != version["version_id"]:
                raise RuntimeError("发布版本回读不一致")
            if after_drafts != drafts:
                raise RuntimeError("草稿字段发生变化")
            after_rows = _collection_rows(collection, f'document_id == "{doc}"')
            if sorted(_json_safe(rows), key=lambda r:r["id"]) != sorted(_json_safe(after_rows), key=lambda r:r["id"]):
                raise RuntimeError("既有 Milvus 索引发生变化")
            print(f"[{number}/{total}] 完成：{doc}，原件/产物/图片可读，草稿和向量内容保持不变", flush=True)
    print(f"{total}份发布文档恢复完成", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--document-id", action="append", choices=DOCUMENT_IDS)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
