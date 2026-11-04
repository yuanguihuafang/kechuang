"""Reparse audited solution PDFs while preserving published business fields."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from agents.retrieval_agent.application.retrieval_service import index_record_id
from scripts.repair_solution_source_groups import (
    _build_runtime,
    _collection_rows,
    _json_safe,
    _milvus_expr,
    _target_expression as _admin_target_expression,
    deduplicate_chunk_images,
    resolve_source_id,
)
from services.application.business_records import canonical_source_id
from services.application.draft_publishing import build_published_draft_index_request
from services.mysql.mysql_sink import MySQLBusinessRecordSink
from services.storage.minio.image_service import perception_image_url

CONFIRMATION = "REPARSE-AUDITED-SOLUTION-DOCUMENTS"
STAGING = Path("/home/zyx/kechuang-deerflow/BF/solution-reparse-staging-20261020")
UNIQUE_INDEX = STAGING / "review-unique" / "unique-index.json"

# original_index values manually rejected as irrelevant/decorative during visual review.
TARGETS = (
    {
        "document_id": "doc_429b5ab6_bb8221b9ae5cbc37",
        "draft_id": "draft_c36d6ca2b8c64387b687e44dfe6dfc7b",
        "mysql_id": "202",
        "source_id": "source:98a3633595c7881817beefd9",
        "file_name": "青岛元润沣 轨道装备有限公司AGV展示PPT.pdf",
        "expected_image_count": 26,
        "drop_positions": {3, 4, 6, 16, 20, 21, 24},
    },
    {
        "document_id": "doc_662b541f_70fe8201a229abd7",
        "draft_id": "draft_151ed6f71c1047d8a6d2b824a85a5012",
        "mysql_id": "200",
        "source_id": "source:7c70862a5d2af12af6d61f82",
        "file_name": "AGV多功能智能工具车.pdf",
        "parse_file_name": "AGV多功能智能工具车整体介绍.pdf",
        "expected_image_count": 24,
        "drop_positions": {1, 2, 3, 4, 5, 6, 20, 21, 22},
    },
    {
        "document_id": "doc_9e4369bf_ddabbdf049345639",
        "draft_id": "draft_74724906e3864e64a3155d2deb7118f7",
        "mysql_id": "189",
        "source_id": "source:c28719864da2276b25aa9fb0",
        "file_name": "中文版Protrace20231116印刷版.pdf",
        "expected_image_count": 141,
        "drop_positions": {139, 140, 141},
    },
    {
        "document_id": "doc_e40c66dd_a141ffc28d1571e1",
        "draft_id": "draft_0e9dff766b324872a47043ad9484ee0f",
        "mysql_id": "166",
        "source_id": "source:e6f0668385c3758765ea5ef0",
        "file_name": "青源峰达太赫兹企业介绍-2024.pdf",
        "expected_image_count": 94,
        "drops": {1, 5, 6, 7, 13, 14, 15, 17, 18, 19, 51, 60, 84, 85, 86, 87, 88, 89},
    },
)

IMAGE_MARKDOWN = re.compile(r"!\[(?P<alt>[\s\S]*?)\]\((?P<source>(?:[^()\r\n]|\([^()\r\n]*\))+?)\)")
IMAGE_API = re.compile(r"/api/v1/perception/images/([^/]+)/([^/\s)\]]+)")
MAX_SOURCE_BYTES = 100 * 1024 * 1024


def _repair_target_expression(document_id: str, mysql_id: str) -> str:
    return (
        f"({_admin_target_expression(document_id, mysql_id)}) || "
        f'(chunk_role == "agent_draft" && mysql_id == {_milvus_expr(mysql_id)}) || '
        f'(chunk_role == "agent_doc" && document_id == {_milvus_expr(document_id)})'
    )


ARTIFACT_REPAIR_DOCUMENT_IDS = (
    "doc_0c77a4d3_a4c7819ad7a55368",
    "doc_62df9955_eea8062cc17fa5b2",
    "doc_480411ba_1a8514a28e7e9b9c",
    "doc_451c3860_6ed8716e937f3ae6",
    "doc_ce86cbbf_d36e2ba9b0f03240",
    "doc_4ded48ff_f2bd8aeb82885158",
    "doc_67864c30_5b36791af2c27f9e",
    "doc_ea4e5916_2727278bc043c26e",
    "doc_be6b540b_f2fbe8d64a1681ee",
    "doc_e8afde8e_c7f91052de0bcd7b",
    "doc_8eaf887b_7a7c98fe25adf6dc",
    "doc_6fc8d49f_18b124410f3741d1",
    "doc_288d883a_3afd49f23b6379cc",
    "doc_be2984c2_4560892acd91b9a9",
    "doc_29046e8f_4288da2f070170a8",
    "doc_8afeedc9_f29fc89dedbc6433",
    "doc_3ebda724_f76bfb1ac7ad0616",
    "doc_56812951_ec25fc350fbd22e1",
    "doc_83b7c79f_aa7ad9f2c22984ea",
    "doc_c87e4892_f3a3af0afc3e0421",
    "doc_5491c545_ac11dbd972446dcb",
    "doc_a06e4007_3562c3463c721626",
    "doc_a9b46bc4_fc6d66b6da89ce7c",
)
PURGED_DOCUMENT_IDS = {
    "doc_24b4104a_3b7271489005d7f2",
    "doc_2e4327bf_1fac533e608ac5de",
    "doc_5a3e8744_736d6ebc3296e31f",
    "doc_6c7306ed_949b65b51ba10817",
    "doc_bf3e2039_bdeaf9bf66b717c2",
    "doc_e29255d6_523a06f6fb4c7214",
    "doc_fc4c813c_224521b30ef9742e",
}
SAVED_PARSE_TARGETS = (
    ("doc_be6b540b_f2fbe8d64a1681ee", "draft_a086d505eb7e434a91d6029d404cd3ac", "青岛讯和数聚2025.pdf"),
    ("doc_56812951_ec25fc350fbd22e1", "draft_69b15cece19340c1afd4789ba472dc77", "北京天倪简诚科技有限公司研发团队能力介绍（V202502）.pdf"),
    ("doc_29046e8f_4288da2f070170a8", "draft_b3118ed98e3c4618b02a403bd6d76920", "青岛图达互联信息科技有限公司简介2025.pdf"),
)


def _progress(message: str) -> None:
    print(f"[solution-reparse-repair] {message}", file=sys.stderr, flush=True)


def _source_ids_match(existing: set[str], expected: str) -> bool:
    return not existing or existing == {expected}


def _cover_image_url(document_id: str, images: list[dict[str, Any]]) -> str:
    if not images:
        return ""
    cover = min(images, key=lambda item: (int(item.get("page_number") or 10**9), str(item.get("image_id") or "")))
    return perception_image_url(document_id, str(cover["image_id"]))


def _cover_field_patch(cover_url: str) -> dict[str, dict[str, str]]:
    return ({"fields": {"first_img_url": cover_url},
             "mysql_fields": {"first_img_url": cover_url}} if cover_url else {})


def _blocks_match_ignoring_image_refs(left: list[Any], right: list[Any]) -> bool:
    if len(left) != len(right):
        return False

    def comparable(block: Any) -> dict[str, Any]:
        value = block.model_dump(mode="json") if hasattr(block, "model_dump") else dict(block)
        metadata = dict(value.get("metadata") or {})
        metadata.pop("image_ids", None)
        metadata.pop("image_refs", None)
        value["metadata"] = metadata
        return value

    return all(comparable(old) == comparable(new) for old, new in zip(left, right))


def _select_reviewed_images(
    images: list[dict[str, Any]], index: list[dict[str, Any]], target: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if target.get("replace_with_all_new_images"):
        return list(images), []
    expected = int(target["expected_image_count"])
    if len(images) != expected:
        raise RuntimeError(
            f"本次解析图片数量与复核预期不一致：当前 {len(images)} 张，预期 {expected} 张"
        )
    if "drop_positions" in target:
        drops = {int(value) for value in target["drop_positions"]}
    else:
        if len(index) != expected:
            raise RuntimeError(
                f"图片数量与人工审核版本不一致：当前 {len(images)} 张，审核 {len(index)} 张"
            )
        ordered = sorted(index, key=lambda item: int(item.get("unique_index") or 0))
        if [int(item.get("unique_index") or 0) for item in ordered] != list(range(1, expected + 1)):
            raise RuntimeError("人工审核图片索引不连续，拒绝按顺序套用筛选结果")
        original_drops = {int(value) for value in target.get("drops", set())}
        drops = {
            int(item["unique_index"])
            for item in ordered
            if int(item.get("original_index") or 0) in original_drops
        }
    if any(position < 1 or position > expected for position in drops):
        raise RuntimeError("人工筛选位置超出本次解析图片范围")
    kept = [image for position, image in enumerate(images, start=1) if position not in drops]
    removed = [image for position, image in enumerate(images, start=1) if position in drops]
    if not kept:
        raise RuntimeError("人工筛选后没有可保留图片")
    return kept, removed


def _remove_dropped_image_files(output_dir: Path, images: list[dict[str, Any]]) -> None:
    root = output_dir.resolve()
    if not root.is_dir():
        raise RuntimeError("MinerU 图片临时目录不存在，拒绝上传未筛选产物")
    for image in images:
        relative = str(image.get("relativePath") or image.get("relative_path") or "")
        if not relative:
            continue
        path = (root / relative).resolve()
        if root not in path.parents:
            raise RuntimeError("被筛除图片路径超出解析临时目录")
        if path.is_file():
            path.unlink()


def _write_backup(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(_json_safe(payload), stream, ensure_ascii=False, separators=(",", ":"))


def _strip_removed_markdown(text: str, document_id: str, removed: list[dict[str, Any]]) -> str:
    removed_ids = {str(item.get("image_id") or "") for item in removed}
    removed_paths = {
        str(item.get("relativePath") or item.get("relative_path") or "").replace("\\", "/").lstrip("./")
        for item in removed
    }
    removed_paths.discard("")

    def replace(match: re.Match[str]) -> str:
        source = match.group("source")
        api_match = IMAGE_API.search(source)
        if api_match and api_match.group(1) == document_id and api_match.group(2) in removed_ids:
            return ""
        normalized = source.replace("\\", "/").lstrip("./").lstrip("/")
        if normalized in removed_paths or any(normalized.endswith("/" + path) for path in removed_paths):
            return ""
        return match.group(0)

    return IMAGE_MARKDOWN.sub(replace, text)


def _include_image_references(text: str, images: list[dict[str, Any]]) -> str:
    """Some MinerU formats keep page-image links only in block metadata."""
    additions = []
    for image in images:
        path = str(image.get("relativePath") or image.get("relative_path") or "").replace("\\", "/")
        if path and path not in text:
            page = image.get("pageNumber") or image.get("page_number")
            alt = f"第{page}页图片" if page else "文档图片"
            additions.append(f"![{alt}]({path})")
    return f"{text.rstrip()}\n\n" + "\n\n".join(additions) if additions else text


def _filter_blocks(blocks: list[Any], document_id: str, removed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    removed_ids = {str(item.get("image_id") or "") for item in removed}
    removed_paths = {
        str(item.get("relativePath") or item.get("relative_path") or "").replace("\\", "/").lstrip("./")
        for item in removed
    }
    removed_paths.discard("")

    def is_removed(item: Any) -> bool:
        if isinstance(item, dict):
            image_id = str(item.get("image_id") or "")
            sources = [str(source).replace("\\", "/").lstrip("./") for source in item.get("sources", [])]
        else:
            image_id = str(item)
            sources = [image_id.replace("\\", "/").lstrip("./")]
        return image_id in removed_ids or any(
            source in removed_paths or any(source.endswith("/" + path) for path in removed_paths)
            for source in sources
        )

    result = []
    for block in blocks:
        value = block.model_dump(mode="json") if hasattr(block, "model_dump") else dict(block)
        metadata = dict(value.get("metadata") or {})
        for key in ("image_ids", "image_refs"):
            current = metadata.get(key)
            if isinstance(current, list):
                metadata[key] = [item for item in current if not is_removed(item)]
        value["metadata"] = metadata
        value["text"] = _strip_removed_markdown(str(value.get("text") or ""), document_id, removed)
        if value["text"].strip():
            result.append(value)
    return result


def _prune_unavailable_block_images(
    blocks: list[Any], document_id: str, valid_image_ids: set[str]
) -> tuple[list[dict[str, Any]], set[str]]:
    referenced_ids: set[str] = set()
    for block in blocks:
        value = block.model_dump(mode="json") if hasattr(block, "model_dump") else dict(block)
        metadata = value.get("metadata") or {}
        for item in metadata.get("image_ids") or []:
            image_id = str(item.get("image_id") or "") if isinstance(item, dict) else str(item)
            if image_id:
                referenced_ids.add(image_id)
        for item in metadata.get("image_refs") or []:
            if isinstance(item, dict) and item.get("image_id"):
                referenced_ids.add(str(item["image_id"]))
        for match in IMAGE_MARKDOWN.finditer(str(value.get("text") or "")):
            api_match = IMAGE_API.search(match.group("source"))
            if api_match and api_match.group(1) == document_id:
                referenced_ids.add(api_match.group(2))

    unavailable = referenced_ids - valid_image_ids
    if not unavailable:
        return _filter_blocks(blocks, document_id, []), set()
    removed = [{"image_id": image_id} for image_id in unavailable]
    cleaned = _filter_blocks(blocks, document_id, removed)
    for block in cleaned:
        block["text"] = str(block.get("text") or "").rstrip()
    return cleaned, unavailable


def _build_index_request(runtime: Any, target: dict[str, Any], draft_record: dict[str, Any],
                         draft: dict[str, Any], parse_result: dict[str, Any],
                         images: list[dict[str, Any]]) -> Any:
    built = build_published_draft_index_request({**draft_record, "draft": draft})
    source_id = canonical_source_id(draft.get("source_id"))
    draft_chunks, document_chunk_role = _published_draft_chunks(built.chunks, source_id)
    if not draft_chunks:
        raise RuntimeError("没有从已发布草稿生成草稿分块")
    raw_text = str(parse_result.get("raw_text") or "").strip()
    if not raw_text:
        raise RuntimeError("新解析原文为空")
    image_ids = {str(item.get("image_id") or "") for item in images}
    refs = {
        match.group(2)
        for item in IMAGE_MARKDOWN.finditer(raw_text)
        if (match := IMAGE_API.search(item.group("source"))) and match.group(1) == target["document_id"]
    }
    if refs - image_ids:
        raise RuntimeError(f"新解析原文仍引用未保留图片：{sorted(refs - image_ids)}")
    from agents.retrieval_agent.schemas import IndexChunk, IndexRequest

    chunks = draft_chunks + [
        IndexChunk(
            chunk_text=raw_text,
            source_locator=f"{target['file_name']}|type:text|page:unknown|ordinal:1",
            mysql_id=target["mysql_id"],
            source_id=source_id,
            document_id=target["document_id"],
            chunk_role=document_chunk_role,
        )
    ]
    request = IndexRequest(
        document_id=target["document_id"],
        version_id=str((runtime.lifecycle.get_document(target["document_id"]) or {}).get("active_version", {}).get("version_id") or "") or None,
        scope_id="public",
        knowledge_type="solution",
        chunks=chunks,
        replace_existing=True,
        identity=built.identity,
    )
    preview = runtime.agent.preview_chunks(request)
    final_chunks, duplicate_images = deduplicate_chunk_images(preview, raw_text)
    if not final_chunks:
        raise RuntimeError("分块预览结果为空")
    return request.model_copy(update={"chunks": final_chunks, "chunks_are_final": True}), duplicate_images


def _published_draft_chunks(chunks: list[Any], source_id: str) -> tuple[list[Any], str | None]:
    document_roles = {"admin_draft": "admin_doc", "agent_draft": "agent_doc"}
    selected = [
        chunk.model_copy(update={"source_id": source_id})
        for chunk in chunks
        if chunk.chunk_role in document_roles
    ]
    document_role = document_roles[selected[0].chunk_role] if selected else None
    return selected, document_role


async def _repair_one(runtime: Any, target: dict[str, Any], index: list[dict[str, Any]],
                      backup_file: Path) -> dict[str, Any]:
    document_id = target["document_id"]
    record = runtime.drafts.get_admin(target["draft_id"])
    document = runtime.lifecycle.get_document(document_id)
    if not record or record.get("status") != "PUBLISHED":
        raise RuntimeError(f"{target['draft_id']} 不再是已发布草稿")
    old_draft = record.get("draft") or {}
    if str(old_draft.get("mysql_record_id") or "") != target["mysql_id"]:
        raise RuntimeError("draft_id 与 mysql_id 不匹配")
    draft_source_id = canonical_source_id(old_draft.get("source_id"))
    if draft_source_id and draft_source_id != target["source_id"]:
        raise RuntimeError("source_id 与审计清单不匹配")
    attachment = next((item for item in old_draft.get("attachments", [])
                       if isinstance(item, dict) and item.get("document_id") == document_id), None)
    version = (document or {}).get("active_version") or {}
    if (not document or document.get("knowledge_type") != "solution"
            or str(version.get("publication_status") or "").upper() != "PUBLISHED"
            or not attachment or str(attachment.get("version_id") or "") != str(version.get("version_id") or "")):
        raise RuntimeError("文档、活动版本或已发布草稿附件状态不匹配")
    storage_key = str(version.get("storage_key") or "")
    if not storage_key or await runtime.storage.inspect_object(storage_key) is None:
        raise RuntimeError("活动原 PDF 不存在")
    mysql_rows = runtime.mysql.list_for_sync("solution", after_id=int(target["mysql_id"]) - 1, limit=1)
    if len(mysql_rows) != 1 or str(mysql_rows[0].get("id")) != target["mysql_id"]:
        raise RuntimeError("目标 MySQL 主键不存在或不唯一")

    target_collection = runtime.agent.public_milvus["solution"]
    before_rows = _collection_rows(target_collection, _repair_target_expression(document_id, target["mysql_id"]))
    source_ids = {canonical_source_id(row.get("source_id")) for row in before_rows if row.get("source_id")}
    if not _source_ids_match(source_ids, target["source_id"]):
        raise RuntimeError(f"Milvus source_id 不匹配：{sorted(source_ids)}")

    old_parse = await runtime.storage.load_parse_result(document_id)
    old_images = await runtime.storage.load_image_manifest(document_id)
    old_artifacts = await runtime.storage.load_mineru_artifact_manifest(document_id)
    backup_payload = {
        "document_id": document_id,
        "draft_record": record,
        "parse_result": old_parse,
        "image_manifest": old_images,
        "artifact_manifest": old_artifacts,
        "mysql_row": mysql_rows[0],
        "milvus_rows": before_rows,
    }
    _write_backup(backup_file, backup_payload)
    _progress(f"备份已写入：{backup_file}")

    from agents.perception_agent.routes import (
        _run_agent_path,
        attach_image_urls,
        configure_storage_service,
        persist_image_assets,
    )

    configure_storage_service(runtime.storage)
    _progress(f"开始解析 {target['file_name']}（不提取草稿字段）")
    with tempfile.TemporaryDirectory(prefix="solution-reparse-source-") as temporary:
        local_pdf, _, _, _ = await runtime.storage.download_file_to_directory(
            storage_key, Path(temporary), MAX_SOURCE_BYTES
        )
        parsed = await _run_agent_path(
            local_pdf,
            file_name=target.get("parse_file_name", target["file_name"]),
            document_id=document_id,
            storage_key=storage_key,
            knowledge_type="solution",
            extract_draft=False,
        )
    if parsed.status != "SUCCESS" or not parsed.raw_text.strip() or not parsed.blocks:
        raise RuntimeError(f"PDF 解析失败：{parsed.error or parsed.status}")
    all_images = [item for item in (parsed.metadata or {}).get("images", []) if isinstance(item, dict)]
    kept_images, removed = _select_reviewed_images(all_images, index, target)
    _progress(f"本次图片 {len(all_images)} 张，保留 {len(kept_images)} 张，过滤 {len(removed)} 张")
    kept_ids = {str(item.get("image_id") or "") for item in kept_images}
    if "" in kept_ids:
        raise RuntimeError("筛选后图片缺少 image_id")
    raw_text = _include_image_references(
        _strip_removed_markdown(parsed.raw_text, document_id, removed), kept_images
    )
    text = _strip_removed_markdown(parsed.text, document_id, removed)
    from agents.perception_agent.routes import _prune_removed_parse_images

    filtered_blocks = _filter_blocks(parsed.blocks, document_id, removed)
    parsed.raw_text = raw_text
    parsed.text = text
    parsed.metadata["images"] = kept_images
    _remove_dropped_image_files(Path(str((parsed.metadata or {}).get("outputDir") or "")), removed)
    parsed = attach_image_urls(await persist_image_assets(parsed))

    parsed_dict = parsed.model_dump(mode="json")
    # persist_image_assets replaces relative paths with canonical API URLs.
    parsed_dict["raw_text"] = parsed.raw_text
    parsed_dict["text"] = text
    parsed_dict["blocks"] = filtered_blocks
    parsed_dict["version_id"] = str(version.get("version_id") or "")
    parsed_dict["edited"] = True
    manifest = await runtime.storage.load_image_manifest(document_id)
    artifacts = await runtime.storage.load_mineru_artifact_manifest(document_id)
    metadata, manifest, artifacts, delete_keys = _prune_removed_parse_images(
        parsed.raw_text, parsed_dict, manifest, artifacts
    )
    metadata["images"] = [item for item in kept_images if str(item.get("image_id") or "") in {
        str(entry.get("image_id") or "") for entry in manifest
    }]
    parsed_dict["metadata"] = metadata
    if len(metadata["images"]) != len(kept_images):
        _progress(f"有 {len(kept_images) - len(metadata['images'])} 张图片未被解析原文引用，按无效附件清理")
    if kept_images and not metadata["images"]:
        raise RuntimeError("筛选后没有被解析原文引用的图片")
    image_manifest_key = await runtime.storage.save_image_manifest(document_id, manifest)
    artifact_manifest_key = await runtime.storage.save_mineru_artifact_manifest(document_id, artifacts)
    if delete_keys:
        await runtime.storage.delete_derived_objects(document_id, delete_keys)
    parse_result_key = await runtime.storage.save_parse_result(document_id, parsed_dict)
    _progress(f"解析完成：保留 {len(manifest)} 张审核图片，过滤 {len(removed)} 张；开始重建索引")

    cover_url = _cover_image_url(document_id, manifest)
    updated_draft = {
        **old_draft,
        "source_id": target["source_id"],
        "source_blocks": parsed_dict["blocks"],
        **_cover_field_patch(cover_url),
    }
    request, duplicate_images = _build_index_request(runtime, target, record, updated_draft, parsed_dict, manifest)
    expected_ids = {index_record_id(document_id, chunk) for chunk in request.chunks}
    result = await runtime.agent.index(request)
    if result.status != "SUCCESS" or result.indexed_count != len(expected_ids):
        raise RuntimeError(f"Milvus 重写未完整成功：{result.error or result.status}")
    old_mysql_vectors = _collection_rows(
        target_collection, f'chunk_role == "mysql" && mysql_id == {_milvus_expr(target["mysql_id"])}'
    )
    if old_mysql_vectors:
        target_collection.delete_by_ids({str(row["id"]) for row in old_mysql_vectors if row.get("id")})
    actual = _collection_rows(target_collection, _repair_target_expression(document_id, target["mysql_id"]))
    actual_ids = {str(row.get("id") or "") for row in actual if row.get("id")}
    if actual_ids != expected_ids or any(canonical_source_id(row.get("source_id")) != target["source_id"] for row in actual):
        raise RuntimeError("Milvus 重写后 ID/source_id 校验失败")
    if any(row.get("chunk_role") == "mysql" for row in actual):
        raise RuntimeError("Milvus 仍有旧 mysql 角色向量")

    draft_patch = {
        "source_id": target["source_id"],
        "source_blocks": updated_draft["source_blocks"],
        **_cover_field_patch(cover_url),
    }
    updated = runtime.drafts.update(
        target["draft_id"], user_id=str(record.get("user_id") or "admin"),
        draft=draft_patch, expected_version=record.get("version"), replace_source_blocks=True, replace_fields=False,
    )
    if not updated:
        raise RuntimeError("PostgreSQL 草稿保存失败；备份已保留")
    if cover_url:
        mysql_result = MySQLBusinessRecordSink().update(
            record_type="solution", record_id=target["mysql_id"],
            structured_data={"first_img_url": cover_url},
        )
        if mysql_result.get("status") != "UPDATED":
            raise RuntimeError("MySQL 原记录封面更新未确认；没有新建记录")
    runtime.lifecycle.update_version(
        str(version["version_id"]), status="PUBLISHED", publication_status="PUBLISHED",
        index_status="INDEXED", parsed_block_count=len(parsed_dict["blocks"]),
        chunk_count=len(request.chunks), indexed_count=result.indexed_count,
        artifact_refs={
            "manifest": artifact_manifest_key,
            "parse_result": parse_result_key,
            "images": image_manifest_key,
        },
    )
    final_record = runtime.drafts.get_admin(target["draft_id"])
    final_mysql = runtime.mysql.list_for_sync("solution", after_id=int(target["mysql_id"]) - 1, limit=1)
    if (not final_record or final_record.get("status") != "PUBLISHED"
            or str((final_record.get("draft") or {}).get("mysql_record_id")) != target["mysql_id"]
            or len(final_mysql) != 1 or str(final_mysql[0].get("id")) != target["mysql_id"]
            or (cover_url and final_mysql[0].get("first_img_url") != cover_url)):
        raise RuntimeError("PostgreSQL/MySQL 最终校验失败；备份已保留")
    return {
        "document_id": document_id, "draft_id": target["draft_id"], "mysql_id": target["mysql_id"],
        "source_id": target["source_id"], "retained_images": len(manifest),
        "filtered_images": len(removed), "indexed_chunks": result.indexed_count,
        "duplicate_chunk_images_removed": duplicate_images,
        "cover_image_id": next((item["image_id"] for item in manifest
                                 if perception_image_url(document_id, str(item["image_id"])) == cover_url), None),
        "business_fields_reextracted": False, "backup_file": str(backup_file),
    }


async def _discover_artifact_repair_targets(runtime: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    wanted = set(ARTIFACT_REPAIR_DOCUMENT_IDS) - PURGED_DOCUMENT_IDS
    matches: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {
        document_id: [] for document_id in wanted
    }
    page = 1
    while True:
        result = runtime.drafts.list_page(
            status="PUBLISHED", draft_types=["solution"], page=page, page_size=100
        )
        for record in result["items"]:
            draft = record.get("draft") or {}
            for attachment in draft.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                document_id = str(attachment.get("document_id") or "")
                if document_id in matches:
                    matches[document_id].append((record, attachment))
        if page * result["page_size"] >= result["total"]:
            break
        page += 1

    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for document_id in ARTIFACT_REPAIR_DOCUMENT_IDS:
        if document_id in PURGED_DOCUMENT_IDS:
            skipped.append({"document_id": document_id, "reason": "文档已 PURGED，跳过"})
            continue
        doc = runtime.lifecycle.get_document(document_id)
        version = (doc or {}).get("active_version") or {}
        if (
            not doc
            or doc.get("knowledge_type") != "solution"
            or str(version.get("publication_status") or "").upper() != "PUBLISHED"
        ):
            skipped.append({"document_id": document_id, "reason": "不是有效的已发布方案文档"})
            continue
        manifest_key = f"derived/{document_id}/mineru/artifact-manifest.json"
        parse_key = f"derived/{document_id}/mineru/parse-result.json"
        if await runtime.storage.inspect_object(manifest_key) and await runtime.storage.inspect_object(parse_key):
            skipped.append({"document_id": document_id, "reason": "解析产物已齐全"})
            continue
        attachments = [
            pair for pair in matches[document_id]
            if not pair[1].get("version_id") or str(pair[1].get("version_id")) == str(version.get("version_id"))
        ]
        if len(attachments) != 1:
            skipped.append({"document_id": document_id, "reason": f"已发布草稿附件匹配数为 {len(attachments)}，需人工确认"})
            continue
        record, attachment = attachments[0]
        draft = record.get("draft") or {}
        if len(draft.get("attachments") or []) != 1:
            skipped.append({"document_id": document_id, "reason": "草稿含多个附件，当前修复会替换全部解析块，跳过以保护其他附件"})
            continue
        mysql_id = str(draft.get("mysql_record_id") or "")
        if not mysql_id:
            skipped.append({"document_id": document_id, "reason": "草稿缺少 mysql_record_id"})
            continue
        collection = runtime.agent.public_milvus["solution"]
        rows = _collection_rows(collection, _repair_target_expression(document_id, mysql_id))
        source_ids = [str(row.get("source_id") or "") for row in rows if row.get("source_id")]
        try:
            source_id = resolve_source_id(draft.get("source_id"), source_ids, [document_id])
        except ValueError as exc:
            skipped.append({"document_id": document_id, "reason": str(exc)})
            continue
        file_name = str(attachment.get("file_name") or doc.get("source_name") or "")
        storage_key = str(version.get("storage_key") or attachment.get("storage_key") or "")
        if not storage_key or not await runtime.storage.inspect_object(storage_key):
            skipped.append({"document_id": document_id, "reason": "活动原附件对象缺失"})
            continue
        targets.append({
            "document_id": document_id,
            "draft_id": str(record.get("draft_id") or ""),
            "mysql_id": mysql_id,
            "source_id": source_id,
            "file_name": file_name,
            "replace_with_all_new_images": True,
        })
    return targets, skipped


async def run_artifact_repair(*, apply: bool, backup_dir: Path) -> dict[str, Any]:
    runtime = _build_runtime()
    try:
        targets, skipped = await _discover_artifact_repair_targets(runtime)
        if not apply:
            return {
                "status": "dry_run",
                "targets": targets,
                "skipped": skipped,
                "business_fields_reextracted": False,
            }
        backup_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:8]
        results = []
        for number, target in enumerate(targets, start=1):
            backup = backup_dir / f"{run_id}-{number:02d}-{target['document_id']}.json"
            _progress(f"[{number}/{len(targets)}] 整组替换开始：{target['file_name']} [{target['document_id']}]")
            try:
                repaired = await _repair_one(runtime, target, [], backup)
                results.append({**repaired, "status": "SUCCESS"})
                _progress(f"[{number}/{len(targets)}] 完成：新图 {repaired['retained_images']} 张，Milvus {repaired['indexed_chunks']} 条")
            except Exception as exc:
                results.append({
                    "document_id": target["document_id"],
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                    "backup_file": str(backup),
                })
                _progress(f"[{number}/{len(targets)}] 失败，停止后续写入：{type(exc).__name__}: {exc}")
                break
        complete = len(results) == len(targets) and all(item["status"] == "SUCCESS" for item in results)
        return {
            "status": "applied" if complete else "partial",
            "results": results,
            "skipped": skipped,
            "business_fields_reextracted": False,
        }
    finally:
        runtime.lifecycle.engine.dispose()


async def run_saved_parse_repair(*, apply: bool, backup_dir: Path) -> dict[str, Any]:
    runtime = _build_runtime()
    results = []
    try:
        for number, (document_id, draft_id, file_name) in enumerate(SAVED_PARSE_TARGETS, start=1):
            _progress(f"[{number}/{len(SAVED_PARSE_TARGETS)}] 核对已保存解析结果：{file_name} [{document_id}]")
            record = runtime.drafts.get_admin(draft_id)
            draft = (record or {}).get("draft") or {}
            document = runtime.lifecycle.get_document(document_id) or {}
            version = document.get("active_version") or {}
            attachment = next((item for item in draft.get("attachments") or []
                               if isinstance(item, dict) and item.get("document_id") == document_id), None)
            mysql_id = str(draft.get("mysql_record_id") or "")
            source_id = canonical_source_id(draft.get("source_id"))
            if (not record or record.get("status") != "PUBLISHED"
                    or document.get("knowledge_type") != "solution"
                    or str(version.get("publication_status") or "").upper() != "PUBLISHED"
                    or not attachment or str(attachment.get("version_id") or "") != str(version.get("version_id") or "")
                    or not mysql_id.isdigit() or not source_id):
                raise RuntimeError(f"{document_id} 的发布草稿、活动版本或来源标识不匹配")
            parse_result = await runtime.storage.load_parse_result(document_id)
            if (not parse_result or not parse_result.get("edited")
                    or str(parse_result.get("version_id") or "") != str(version.get("version_id") or "")
                    or not str(parse_result.get("raw_text") or "").strip()):
                raise RuntimeError(f"{document_id} 缺少当前活动版本的已保存解析原文")
            parsed_blocks = parse_result.get("blocks") or []
            old_blocks = draft.get("source_blocks") or []
            manifest = await runtime.storage.load_image_manifest(document_id)
            manifest_by_id = {str(item.get("image_id")): item for item in manifest if item.get("image_id")}
            parsed_images = ((parse_result.get("metadata") or {}).get("images") or [])
            parsed_image_ids = {str(item.get("image_id") or "") for item in parsed_images if isinstance(item, dict)}
            if not parsed_image_ids or "" in parsed_image_ids or parsed_image_ids - manifest_by_id.keys():
                raise RuntimeError(f"{document_id} 已保存解析图片与 MinIO 图片清单不一致")
            repaired_blocks, stale_image_ids = _prune_unavailable_block_images(
                parsed_blocks, document_id, parsed_image_ids
            )
            if not _blocks_match_ignoring_image_refs(old_blocks, repaired_blocks):
                raise RuntimeError(f"{document_id} 草稿正文与已保存解析结果存在非图片差异，拒绝覆盖")
            for image_id in parsed_image_ids:
                object_key = str(manifest_by_id[image_id].get("minio_object_key") or "")
                if not object_key or not await runtime.storage.inspect_object(object_key):
                    raise RuntimeError(f"{document_id} 新图片对象缺失：{image_id}")
            raw_text_refs = {
                match.group(2)
                for item in IMAGE_MARKDOWN.finditer(str(parse_result.get("raw_text") or ""))
                if (match := IMAGE_API.search(item.group("source"))) and match.group(1) == document_id
            }
            if raw_text_refs - parsed_image_ids:
                raise RuntimeError(f"{document_id} 已保存原文仍引用图片清单外 ID：{sorted(raw_text_refs - parsed_image_ids)}")

            mysql_rows = runtime.mysql.list_for_sync("solution", after_id=int(mysql_id) - 1, limit=1)
            if len(mysql_rows) != 1 or str(mysql_rows[0].get("id")) != mysql_id:
                raise RuntimeError(f"{document_id} 对应 MySQL 原记录不存在或不唯一")
            target = {"document_id": document_id, "mysql_id": mysql_id,
                      "source_id": source_id, "file_name": file_name}
            collection = runtime.agent.public_milvus["solution"]
            before_rows = _collection_rows(collection, _repair_target_expression(document_id, mysql_id))
            existing_sources = {canonical_source_id(row.get("source_id")) for row in before_rows if row.get("source_id")}
            if not before_rows or existing_sources != {source_id}:
                raise RuntimeError(f"{document_id} Milvus 来源组为空或 source_id 不匹配")

            existing_cover = str((draft.get("fields") or {}).get("first_img_url")
                                 or (draft.get("mysql_fields") or {}).get("first_img_url") or "")
            cover_match = IMAGE_API.search(existing_cover)
            if cover_match and cover_match.group(1) == document_id and cover_match.group(2) in parsed_image_ids:
                cover_url = existing_cover
            else:
                first_image = min(parsed_images, key=lambda item: (int(item.get("pageNumber") or 10**9), str(item.get("image_id") or "")))
                cover_url = perception_image_url(document_id, str(first_image["image_id"]))
            updated_draft = {
                **draft,
                "source_id": source_id,
                "source_blocks": repaired_blocks,
                "fields": {**(draft.get("fields") or {}), "first_img_url": cover_url},
                "mysql_fields": {**(draft.get("mysql_fields") or {}), "first_img_url": cover_url},
            }
            repaired_parse_result = {**parse_result, "blocks": repaired_blocks}
            request, duplicate_images = _build_index_request(
                runtime, target, record, updated_draft, repaired_parse_result, manifest
            )
            expected_ids = {index_record_id(document_id, chunk) for chunk in request.chunks}
            summary = {"document_id": document_id, "draft_id": draft_id, "mysql_id": mysql_id,
                       "stale_image_ids": len(stale_image_ids), "indexed_chunks": len(expected_ids),
                       "cover_changed": cover_url != existing_cover, "business_fields_reextracted": False}
            if not apply:
                results.append({**summary, "status": "READY"})
                continue

            backup = backup_dir / f"saved-parse-{number:02d}-{document_id}.json"
            _write_backup(backup, {"draft_record": record, "parse_result": parse_result,
                "image_manifest": manifest, "artifact_manifest": await runtime.storage.load_mineru_artifact_manifest(document_id),
                "mysql_row": mysql_rows[0], "milvus_rows": before_rows})
            _progress(f"[{number}/{len(SAVED_PARSE_TARGETS)}] 已备份，重写 Milvus {len(expected_ids)} 条：{document_id}")
            indexed = await runtime.agent.index(request)
            if indexed.status != "SUCCESS" or indexed.indexed_count != len(expected_ids):
                raise RuntimeError(f"{document_id} Milvus 重写未完整成功：{indexed.error or indexed.status}")
            stale_mysql_vectors = _collection_rows(collection, f'chunk_role == "mysql" && mysql_id == {_milvus_expr(mysql_id)}')
            if stale_mysql_vectors:
                collection.delete_by_ids({str(row["id"]) for row in stale_mysql_vectors if row.get("id")})
            actual = _collection_rows(collection, _repair_target_expression(document_id, mysql_id))
            actual_ids = {str(row.get("id") or "") for row in actual if row.get("id")}
            if actual_ids != expected_ids or any(canonical_source_id(row.get("source_id")) != source_id for row in actual):
                raise RuntimeError(f"{document_id} Milvus 重写后 ID/source_id 校验失败")
            if any(row.get("chunk_role") == "mysql" for row in actual):
                raise RuntimeError(f"{document_id} Milvus 仍有旧 mysql 角色向量")

            await runtime.storage.save_parse_result(document_id, repaired_parse_result)

            updated = runtime.drafts.update(draft_id, user_id=str(record.get("user_id") or "admin"),
                draft={"source_id": source_id, "source_blocks": repaired_blocks,
                       "fields": {"first_img_url": cover_url}, "mysql_fields": {"first_img_url": cover_url}},
                expected_version=record.get("version"), replace_source_blocks=True, replace_fields=False)
            if not updated:
                raise RuntimeError(f"{document_id} PostgreSQL 草稿保存失败；备份已保留")
            if cover_url != existing_cover:
                mysql_result = MySQLBusinessRecordSink().update(
                    record_type="solution", record_id=mysql_id,
                    structured_data={"first_img_url": cover_url})
                if mysql_result.get("status") != "UPDATED":
                    raise RuntimeError(f"{document_id} MySQL 原记录封面更新未确认")
            runtime.lifecycle.update_version(str(version["version_id"]), status="PUBLISHED",
                publication_status="PUBLISHED", index_status="INDEXED",
                parsed_block_count=len(parsed_blocks), chunk_count=len(request.chunks),
                indexed_count=indexed.indexed_count)
            final_record = runtime.drafts.get_admin(draft_id)
            final_mysql = runtime.mysql.list_for_sync("solution", after_id=int(mysql_id) - 1, limit=1)
            if (not final_record or final_record.get("status") != "PUBLISHED"
                    or not _blocks_match_ignoring_image_refs((final_record.get("draft") or {}).get("source_blocks") or [], repaired_blocks)
                    or len(final_mysql) != 1 or str(final_mysql[0].get("id")) != mysql_id
                    or (cover_url != existing_cover and final_mysql[0].get("first_img_url") != cover_url)):
                raise RuntimeError(f"{document_id} PostgreSQL/MySQL 最终核验失败；备份已保留")
            final_blocks = (final_record.get("draft") or {}).get("source_blocks") or []
            _, remaining_stale = _prune_unavailable_block_images(final_blocks, document_id, parsed_image_ids)
            if remaining_stale:
                raise RuntimeError(f"{document_id} 草稿仍引用无效图片 ID：{sorted(remaining_stale)}")
            results.append({**summary, "status": "SUCCESS", "duplicate_chunk_images_removed": duplicate_images,
                            "backup_file": str(backup)})
            _progress(f"[{number}/{len(SAVED_PARSE_TARGETS)}] 完成：{document_id}，索引 {indexed.indexed_count} 条")
    except Exception as exc:
        _progress(f"停止后续组：{type(exc).__name__}: {exc}")
        results.append({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        runtime.lifecycle.engine.dispose()
    complete = len(results) == len(SAVED_PARSE_TARGETS) and all(item["status"] in {"READY", "SUCCESS"} for item in results)
    return {"status": "dry_run" if not apply else "applied" if complete else "partial",
            "results": results, "business_fields_reextracted": False}


async def run(*, apply: bool, backup_dir: Path) -> dict[str, Any]:
    runtime = _build_runtime()
    try:
        review = json.loads(UNIQUE_INDEX.read_text(encoding="utf-8"))
        run_id = uuid.uuid4().hex[:8]
        results = []
        for number, target in enumerate(TARGETS, start=1):
            index = review.get(target.get("parse_file_name", target["file_name"]))
            if not isinstance(index, list):
                raise RuntimeError(f"缺少人工审核图片清单：{target['file_name']}")
            _progress(f"[{number}/{len(TARGETS)}] {target['file_name']}：人工审核清单 {len(index)} 张")
            if not apply:
                results.append({"document_id": target["document_id"], "status": "not_run", "approved_images": len(index)})
                continue
            backup = backup_dir / f"{run_id}-{number:02d}-{target['document_id']}.json"
            try:
                result = await _repair_one(runtime, target, index, backup)
                results.append({**result, "status": "SUCCESS"})
                _progress(f"[{number}/{len(TARGETS)}] 完成：{target['file_name']}，MySQL 原 ID {target['mysql_id']}，Milvus {result['indexed_chunks']} 条")
            except Exception as exc:
                results.append({"document_id": target["document_id"], "status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "backup_file": str(backup)})
                _progress(f"[{number}/{len(TARGETS)}] 失败并停止后续文档：{type(exc).__name__}: {exc}")
                break
        return {"status": "applied" if apply and len(results) == len(TARGETS) and all(r["status"] == "SUCCESS" for r in results) else "dry_run" if not apply else "partial", "results": results}
    finally:
        runtime.lifecycle.engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--artifact-groups", action="store_true", help="修复审计出的缺失解析产物组；保留新解析图片 ID 和现有草稿业务字段")
    parser.add_argument("--saved-parse-groups", action="store_true", help="使用已保存解析结果更新草稿图片引用和 Milvus，不重新调用 MinerU")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
    if args.artifact_groups and args.saved_parse_groups:
        parser.error("--artifact-groups 与 --saved-parse-groups 不能同时使用")
    if args.artifact_groups:
        result = asyncio.run(run_artifact_repair(apply=args.apply, backup_dir=args.backup_dir))
    elif args.saved_parse_groups:
        result = asyncio.run(run_saved_parse_repair(apply=args.apply, backup_dir=args.backup_dir))
    else:
        result = asyncio.run(run(apply=args.apply, backup_dir=args.backup_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
