"""持久化文档图片补全与稳定标识规则，共用于管理和解析入口。"""

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from services.document_parsing.mineru_parser import normalize_markdown_image_references
from services.storage.minio.image_service import normalize_image_url, perception_image_url


def merge_persisted_image_assets(
    document_id: str,
    images: list[dict[str, object]],
    manifest: list[dict[str, object]],
    artifacts: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """用完整 MinerU 产物清单补齐历史解析结果中缺失的图片。"""

    merged_images: list[dict[str, object]] = []
    seen_image_keys: set[str] = set()
    for item in images:
        if not isinstance(item, dict):
            continue
        identities = _image_identity_keys(item)
        if any(identity in seen_image_keys for identity in identities):
            continue
        seen_image_keys.update(identities)
        merged_images.append(dict(item))
    merged_manifest: list[dict[str, object]] = []
    seen_manifest_keys: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict):
            continue
        identities = _image_identity_keys(item)
        if any(identity in seen_manifest_keys for identity in identities):
            continue
        seen_manifest_keys.update(identities)
        merged_manifest.append(dict(item))
    manifest_by_key = {str(item.get("minio_object_key")): item for item in merged_manifest}
    image_by_path = {}
    image_by_name = {}
    image_by_object_key = {}
    for item in merged_images:
        object_key = str(item.get("minio_object_key") or "")
        entry = manifest_by_key.get(object_key)
        if entry and entry.get("image_id"):
            item["image_id"] = entry["image_id"]
            item["url"] = perception_image_url(document_id, str(entry["image_id"]))
        if object_key:
            image_by_object_key.setdefault(object_key, item)
        for key in (item.get("relativePath"), item.get("relative_path"), item.get("path")):
            if key:
                normalized = _normalize_image_reference(key)
                if normalized:
                    image_by_path.setdefault(normalized, item)
                    name = Path(normalized).name
                    # 只有文件名唯一时才允许旧产物按 basename 回退。
                    if name not in image_by_name:
                        image_by_name[name] = item
                    elif image_by_name[name] is not item:
                        image_by_name[name] = None
    for artifact in artifacts:
        relative_path = str(artifact.get("relative_path") or "").replace("\\", "/")
        object_key = str(artifact.get("minio_object_key") or "")
        if (
            "/images/" not in f"/{relative_path}"
            or not object_key
            or Path(relative_path).suffix.lower()
            not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
        ):
            continue
        image_id = hashlib.sha1(f"{document_id}:{relative_path}".encode()).hexdigest()
        entry = manifest_by_key.get(object_key)
        if entry is None:
            entry = {
                "image_id": image_id,
                "document_id": document_id,
                "minio_object_key": object_key,
                "content_type": artifact.get("content_type", "application/octet-stream"),
            }
            merged_manifest.append(entry)
            manifest_by_key[object_key] = entry
        normalized_path = _normalize_image_reference(relative_path)
        image = image_by_object_key.get(object_key) or image_by_path.get(normalized_path)
        if image is None and normalized_path == Path(normalized_path).name:
            image = image_by_name.get(normalized_path)
        if image is None:
            image = {"relativePath": relative_path, "path": relative_path}
            merged_images.append(image)
            image_by_path[normalized_path] = image
            name = Path(normalized_path).name
            if name not in image_by_name:
                image_by_name[name] = image
            elif image_by_name[name] is not image:
                image_by_name[name] = None
        image["image_id"] = str(entry.get("image_id") or image_id)
        image["minio_object_key"] = object_key
        image["url"] = perception_image_url(document_id, str(image["image_id"]))
    return merged_images, merged_manifest


def _has_verified_image_url(value: str, document_id: str, images: list[dict[str, object]]) -> bool:
    try:
        path = urlsplit(value).path
    except ValueError:
        return False
    prefix = "/api/v1/perception/images/"
    if not path.startswith(prefix):
        return True
    parts = path.removeprefix(prefix).split("/")
    if len(parts) != 2:
        return False
    image_document_id, image_id = map(unquote, parts)
    return any(
        str(image.get("image_id") or "") == image_id
        and str(image.get("document_id") or document_id) == image_document_id
        for image in images
    )


async def hydrate_parse_result_images(
    document_id: str,
    result: dict[str, object],
    storage: object,
) -> dict[str, object]:
    """恢复解析结果中的受控图片 URL，供所有解析结果入口复用。"""

    metadata = result.get("metadata")
    metadata_dict = dict(metadata) if isinstance(metadata, dict) else {}
    raw_images = metadata_dict.get("images")
    images = (
        [item for item in raw_images if isinstance(item, dict)]
        if isinstance(raw_images, list)
        else []
    )
    load_manifest = getattr(storage, "load_image_manifest", None)
    manifest = await load_manifest(document_id) if callable(load_manifest) else []
    load_artifacts = getattr(storage, "load_mineru_artifact_manifest", None)
    artifacts = await load_artifacts(document_id) if callable(load_artifacts) else []
    inspect_object = getattr(storage, "inspect_object", None)
    if manifest and not callable(inspect_object):
        raise RuntimeError("图片读取缺少 MinIO 对象预检能力")
    verified_manifest = []
    for item in manifest:
        if not isinstance(item, dict) or not item.get("image_id") or not item.get("minio_object_key"):
            continue
        if callable(inspect_object) and await inspect_object(str(item["minio_object_key"])) is not None:
            verified_manifest.append(item)
    verified_keys = {str(item["minio_object_key"]) for item in verified_manifest}
    verified_artifacts = [
        item for item in artifacts
        if isinstance(item, dict) and str(item.get("minio_object_key") or "") in verified_keys
    ]
    images, manifest = merge_persisted_image_assets(
        document_id, images, verified_manifest, verified_artifacts
    )
    valid_image_ids = {str(item.get("image_id") or "") for item in manifest}
    images = [
        item for item in images
        if str(item.get("image_id") or "") in valid_image_ids
    ]
    metadata_dict["images"] = images
    urls = {
        str(item.get("image_id")): perception_image_url(
            document_id, str(item.get("image_id"))
        )
        for item in manifest
        if isinstance(item, dict) and item.get("image_id")
    }
    for image in images:
        image_id = str(image.get("image_id") or "")
        if image_id in urls:
            image["url"] = urls[image_id]
    hydrated = {**result, "metadata": metadata_dict}
    for fields in (hydrated, metadata_dict):
        if isinstance(fields.get("first_img_url"), str):
            if not _has_verified_image_url(fields["first_img_url"], document_id, images + manifest):
                fields["first_img_url"] = ""
            else:
                try:
                    fields["first_img_url"] = normalize_image_url(
                        fields["first_img_url"], document_id=document_id, images=images + manifest
                    )
                except ValueError:
                    fields["first_img_url"] = ""
    external_images: dict[str, str] = {}
    for field in ("raw_text", "text"):
        text = hydrated.get(field)
        if isinstance(text, str):
            external_images.clear()

            def protect_external(match: re.Match[str]) -> str:
                source = match.group("source")
                try:
                    url = normalize_image_url(source, document_id=document_id, images=images)
                except ValueError:
                    return ""
                if url != source:
                    return match.group(0).replace(source, url, 1)
                if re.match(r"(?:/?(?:derived|images)/|/api/v1/perception/images/)", source):
                    return ""
                token = f"\x00external-image-{len(external_images)}\x00"
                external_images[token] = match.group(0)
                return token

            # External URLs must not match a managed image merely by basename.
            protected = re.sub(
                r"!\[[^\]]*\]\((?P<source>https?://(?:[^()\r\n]|\([^()\r\n]*\))+?)"
                r"(?P<title>\s+[\"'][^()\r\n]*[\"'])?\)",
                protect_external, text,
            )
            normalized = normalize_markdown_image_references(protected, images)
            for token, original in external_images.items():
                normalized = normalized.replace(token, original)
            def remove_unavailable_managed(match: re.Match[str]) -> str:
                source = match.group(1).strip().strip("<>")
                if source.startswith(("http://", "https://")):
                    return match.group(0)
                try:
                    canonical = normalize_image_url(
                        source, document_id=document_id, images=images
                    )
                except ValueError:
                    return ""
                if canonical != source:
                    return match.group(0).replace(source, canonical, 1)
                if source.startswith("/api/v1/perception/images/"):
                    return match.group(0) if _has_verified_image_url(source, document_id, images) else ""
                normalized_source = source.replace("\\", "/").lstrip("./")
                if "/images/" in f"/{normalized_source}" or normalized_source.startswith("images/"):
                    return ""
                return match.group(0)

            hydrated[field] = re.sub(
                r"!\[[^\]]*\]\(([^)]+)\)", remove_unavailable_managed, normalized
            )
    return hydrated


def _normalize_image_reference(value: object) -> str:
    """统一 Markdown 图片路径，兼容 URL 编码和 Windows 分隔符。"""

    try:
        normalized = unquote(str(value or ""))
    except Exception:  # noqa: BLE001 - 清单字段异常时保留原值判断
        normalized = str(value or "")
    normalized = normalized.split("?", 1)[0].split("#", 1)[0]
    return normalized.replace("\\", "/").lstrip("./").lstrip("/")


def _image_identity_keys(
    image: dict[str, object],
    manifest: dict[str, object] | None = None,
) -> tuple[str, ...]:
    """返回图片的稳定去重键；同一对象优先按 MinIO key，其次按 image_id/路径。"""

    manifest = manifest or {}
    keys: list[str] = []
    object_key = str(
        image.get("minio_object_key") or manifest.get("minio_object_key") or ""
    ).strip()
    image_id = str(image.get("image_id") or manifest.get("image_id") or "").strip()
    if object_key:
        keys.append(f"object:{object_key}")
    if image_id:
        keys.append(f"id:{image_id}")
    for field in ("relativePath", "relative_path", "path"):
        value = _normalize_image_reference(image.get(field))
        if value:
            keys.append(f"path:{value}")
    return tuple(dict.fromkeys(keys))
