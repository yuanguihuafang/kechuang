"""读取 Perception 写入 MinIO 的图片关联清单。"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import quote, unquote, urlsplit

from botocore.exceptions import ClientError

from runtime.config import get_settings
from services.storage.minio.client import get_minio_bucket, get_minio_client


def perception_image_url(document_id: str, image_id: str) -> str:
    """生成小程序和网页都可直接访问的受控图片接口地址。"""

    path = (
        f"/api/v1/perception/images/{quote(document_id, safe='')}/"
        f"{quote(image_id, safe='')}"
    )
    base_url = get_settings().public_api_base_url.strip().rstrip("/")
    return f"{base_url}{path}" if base_url else path


def normalize_image_url(
    value: str, *, document_id: str, images: list[dict[str, object]]
) -> str:
    """正文图片和封面共用清单映射；外部图片地址保持原样。"""
    try:
        parsed = urlsplit(value[1:] if value.startswith("//api/v1/perception/images/") else value)
    except ValueError:
        return value
    route = "/api/v1/perception/images/"
    if parsed.path.startswith(route):
        parts = parsed.path.removeprefix(route).split("/")
        if len(parts) == 2 and all(parts):
            return perception_image_url(unquote(parts[0]), unquote(parts[1]))
    settings = get_settings()
    if parsed.netloc:
        endpoints = (
            getattr(settings, "minio_endpoint", ""),
            getattr(settings, "minio_public_endpoint", ""),
        )
        hosts = {
            urlsplit(endpoint if "://" in endpoint else f"//{endpoint}").netloc
            for endpoint in endpoints if endpoint
        }
        if parsed.netloc not in hosts:
            return value
    reference = unquote(parsed.path).replace("\\", "/").lstrip("./")
    bucket = getattr(settings, "minio_bucket", "")
    if parsed.netloc and bucket and reference.startswith(f"{bucket}/"):
        reference = reference.removeprefix(f"{bucket}/")
    matches = {
        str(image["image_id"])
        for image in images if image.get("image_id")
        for field in ("minio_object_key", "relativePath", "relative_path", "path", "url", "image_url")
        if image.get(field)
        and reference == unquote(str(image[field])).replace("\\", "/").lstrip("./")
    }
    if len(matches) == 1:
        return perception_image_url(document_id, matches.pop())
    if reference.startswith("derived/") or parsed.netloc:
        raise ValueError("Managed image reference is missing or ambiguous in the image manifest")
    return value


def normalize_image_references(
    text: str, *, document_id: str, manifests: dict[str, list[dict[str, object]]],
) -> str:
    """正文与封面中的旧图片引用使用同一清单映射，外部 URL 不变。"""
    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        managed = re.search(r"(?:/+api/v1/perception/images/|/?derived/)([^/]+)/", value)
        owner = unquote(managed.group(1)) if managed else document_id
        return normalize_image_url(value, document_id=owner, images=manifests.get(owner, []))

    return re.sub(
        r"https?://[^\s<>\"'()]+|/+api/v1/perception/images/[^\s<>\"'()]+"
        r"|(?:/?derived/|/?images/)[^\s<>\"'()]+\.(?:png|jpe?g|gif|webp|svg)",
        replace, text, flags=re.IGNORECASE,
    )


class ImageManifestService:
    """图片不入 Milvus；仅按命中文本的文档与页码读取 MinIO 清单。"""

    def __init__(self) -> None:
        self.bucket = get_minio_bucket()
        self.client = get_minio_client()

    async def list_for_document(self, document_id: str) -> list[dict[str, object]]:
        """读取文档当前有效的图片清单，供发布索引校验图片引用。"""

        return await asyncio.to_thread(self._load_manifest, document_id)

    async def find_many(
        self,
        locations: list[tuple[str | None, str | None]],
        *,
        references: list[list[str]] | None = None,
    ) -> list[list[dict[str, object]]]:
        """按文档批量加载图片清单，并按每条证据的位置返回对齐结果。"""

        document_ids = list(dict.fromkeys(
            document_id for document_id, _ in locations if document_id
        ))
        manifests = await asyncio.gather(*(
            asyncio.to_thread(self._load_manifest, document_id)
            for document_id in document_ids
        ))
        manifest_by_document = dict(zip(document_ids, manifests, strict=True))
        groups: list[list[dict[str, object]]] = []
        for index, (document_id, source_locator) in enumerate(locations):
            if not document_id:
                groups.append([])
                continue
            page_number = _page_from_source_locator(source_locator)
            page_range = _page_range_from_source_locator(source_locator)
            slide_range = _slide_range_from_source_locator(source_locator)
            image_references = references[index] if references and index < len(references) else []
            groups.append([
                image for image in manifest_by_document.get(document_id, [])
                if _is_candidate_image(image)
                and (
                    _image_matches_location(
                        image,
                        page_number=page_number,
                        page_range=page_range,
                        slide_range=slide_range,
                    )
                    or any(_image_matches_reference(image, reference) for reference in image_references)
                )
            ])
        if hasattr(self, "client"):
            object_keys = {
                str(image.get("minio_object_key") or "")
                for group in groups for image in group
            }
            object_keys.discard("")
            existing = {
                key for key in object_keys
                if await asyncio.to_thread(self._object_exists, key)
            }
            groups = [[
                image for image in group
                if str(image.get("minio_object_key") or "") in existing
            ] for group in groups]
        return groups

    def _object_exists(self, object_key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=object_key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}:
                return False
            raise
        return True

    def _load_manifest(self, document_id: str) -> list[dict[str, object]]:
        if not _is_safe_identifier(document_id):
            return []
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=f"derived/{document_id}/image-manifest.json"
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}:
                return []
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        images = payload.get("images", []) if isinstance(payload, dict) else []
        return [image for image in images if isinstance(image, dict)]


def _is_presentation_locator(source_locator: str | None) -> bool:
    source_name = (source_locator or "").split("|", maxsplit=1)[0].lower()
    return source_name.endswith((".ppt", ".pptx"))


def _page_from_source_locator(source_locator: str | None) -> int | None:
    if not source_locator:
        return None
    for part in source_locator.split("|"):
        if part.startswith("page:"):
            try:
                return int(part.removeprefix("page:"))
            except ValueError:
                return None
    return None


def _page_range_from_source_locator(source_locator: str | None) -> tuple[int, int] | None:
    if not source_locator:
        return None
    for part in source_locator.split("|"):
        if not part.startswith("pages:"):
            continue
        try:
            start_text, end_text = part.removeprefix("pages:").split("-", maxsplit=1)
            start, end = int(start_text), int(end_text)
        except (ValueError, TypeError):
            return None
        return (start, end) if start > 0 and end >= start else None
    return None


def _slide_range_from_source_locator(source_locator: str | None) -> tuple[int, int] | None:
    if not source_locator:
        return None
    for part in source_locator.split("|"):
        if not part.startswith("slides:"):
            continue
        value = part.removeprefix("slides:")
        try:
            start_text, end_text = value.split("-", maxsplit=1)
            start, end = int(start_text), int(end_text)
        except (ValueError, TypeError):
            return None
        return (start, end) if start > 0 and end >= start else None
    return None


def _is_candidate_image(image: dict[str, object]) -> bool:
    role = str(image.get("image_role") or image.get("imageRole") or "candidate")
    return role != "decorative"


def _image_matches_reference(image: dict[str, object], reference: str) -> bool:
    normalized_reference = reference.replace("\\", "/").lstrip("/").casefold()
    if not normalized_reference:
        return False
    if normalized_reference == str(image.get("image_id") or "").casefold():
        return True
    return any(
        normalized_reference == candidate
        or candidate.endswith(f"/{normalized_reference}")
        for field in ("relativePath", "relative_path", "path")
        if image.get(field)
        for candidate in (str(image[field]).replace("\\", "/").lstrip("/").casefold(),)
    )


def _image_matches_location(
    image: dict[str, object],
    *,
    page_number: int | None,
    page_range: tuple[int, int] | None,
    slide_range: tuple[int, int] | None,
) -> bool:
    raw_page = image.get("page_number")
    if raw_page is None:
        raw_page = image.get("pageNumber")
    try:
        image_page = int(raw_page) if raw_page is not None else None
    except (TypeError, ValueError):
        image_page = None
    if slide_range is not None:
        return image_page is not None and slide_range[0] <= image_page <= slide_range[1]
    if page_range is not None:
        return image_page is not None and page_range[0] <= image_page <= page_range[1]
    if page_number is not None:
        return image_page == page_number
    # 证据没有可靠的页码/幻灯片范围时，不能退化为返回整份文档的图片。
    # 图片是证据增强项，宁可不返回，也不能把无关图片展示给用户。
    return False


def _is_safe_identifier(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and ".." not in value
