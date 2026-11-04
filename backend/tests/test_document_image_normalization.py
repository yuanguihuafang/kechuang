import asyncio
from types import SimpleNamespace

import pytest

from services.application.document_images import (
    hydrate_parse_result_images,
    merge_persisted_image_assets,
)
from services.storage.minio import image_service


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(image_service, "get_settings", lambda: SimpleNamespace(
        public_api_base_url="", minio_endpoint="localhost:9000",
        minio_public_endpoint="https://objects.example.com", minio_bucket="images",
    ))


@pytest.mark.parametrize("value", [
    "images/chart.png", "derived/doc/chart.png",
    "https://objects.example.com/images/derived/doc/chart.png?signature=old",
    "http://localhost:9000/images/derived/doc/chart.png",
    "https://old-api.example.com/api/v1/perception/images/doc/image?old=true",
    "//api/v1/perception/images/doc/image",
])
def test_managed_images_and_covers_share_canonical_url(value):
    assert image_service.normalize_image_url(value, document_id="doc", images=[{
        "image_id": "image", "relativePath": "images/chart.png",
        "minio_object_key": "derived/doc/chart.png",
    }]) == "/api/v1/perception/images/doc/image"


def test_aliyun_absolute_url_is_preserved_exactly():
    value = "https://bucket.oss-cn-beijing.aliyuncs.com/images/chart.png?signature=a%2Fb"
    assert image_service.normalize_image_url(value, document_id="doc", images=[{
        "image_id": "image", "relativePath": "images/chart.png",
    }]) == value


def test_body_and_bare_cover_use_the_same_manifest_mapping():
    external = "https://bucket.oss-cn-beijing.aliyuncs.com/images/chart.png?signature=a%2Fb"
    text = f"封面：derived/doc/chart.png\n![图片](images/chart.png)\n![云图]({external})"
    normalized = image_service.normalize_image_references(text, document_id="doc", manifests={"doc": [{
        "image_id": "image", "relativePath": "images/chart.png", "minio_object_key": "derived/doc/chart.png",
    }]})
    assert normalized == f"封面：/api/v1/perception/images/doc/image\n![图片](/api/v1/perception/images/doc/image)\n![云图]({external})"


def test_url_with_adjacent_chinese_email_label_is_preserved_as_text():
    value = "http://www.xizihuanbao.com邮箱：xizihuanbao@163.com"
    assert image_service.normalize_image_references(
        value, document_id="doc", manifests={}
    ) == value


def test_unmapped_internal_key_fails_instead_of_becoming_public_url():
    with pytest.raises(ValueError, match="missing or ambiguous"):
        image_service.normalize_image_url("derived/doc/unknown.png", document_id="doc", images=[])


def test_manifest_object_key_is_authoritative_for_image_id():
    images, _ = merge_persisted_image_assets("doc", [{
        "image_id": "stale", "minio_object_key": "derived/doc/chart.png",
    }], [{"image_id": "image", "minio_object_key": "derived/doc/chart.png"}], [])
    assert images[0]["image_id"] == "image"
    assert images[0]["url"] == "/api/v1/perception/images/doc/image"


def test_parse_result_hydration_does_not_persist_recovered_image_manifest():
    saved_manifests = []

    class Storage:
        async def load_image_manifest(self, document_id):
            return []

        async def load_mineru_artifact_manifest(self, document_id):
            return [{
                "relative_path": "auto/images/chart.png",
                "minio_object_key": "derived/doc/auto/images/chart.png",
                "content_type": "image/png",
            }]

        async def save_image_manifest(self, document_id, images):
            saved_manifests.append((document_id, images))

    asyncio.run(hydrate_parse_result_images(
        "doc", {"raw_text": "![chart](auto/images/chart.png)", "metadata": {}}, Storage()
    ))

    assert saved_manifests == []


def test_parse_result_hydration_does_not_expose_artifact_only_images():
    class Storage:
        async def load_image_manifest(self, _document_id):
            return []

        async def load_mineru_artifact_manifest(self, _document_id):
            return [{
                "relative_path": "auto/images/chart.png",
                "minio_object_key": "derived/doc/auto/images/chart.png",
                "content_type": "image/png",
            }]

        async def inspect_object(self, storage_key):
            return {"storage_key": storage_key}

    result = asyncio.run(hydrate_parse_result_images("doc", {
        "raw_text": "![chart](auto/images/chart.png)",
        "metadata": {"images": []},
    }, Storage()))

    assert result["metadata"]["images"] == []
    assert result["raw_text"] == ""


def test_parse_result_hydration_drops_manifest_entries_whose_objects_are_missing():
    class Storage:
        async def load_image_manifest(self, _document_id):
            return [{
                "image_id": "gone",
                "relativePath": "images/gone.png",
                "minio_object_key": "derived/doc/images/gone.png",
            }]

        async def load_mineru_artifact_manifest(self, _document_id):
            return []

        async def inspect_object(self, _storage_key):
            return None

    result = asyncio.run(hydrate_parse_result_images("doc", {
        "first_img_url": "/api/v1/perception/images/doc/gone",
        "raw_text": "![gone](images/gone.png)\n![external](https://example.com/image.png)",
        "metadata": {"images": [{"image_id": "gone", "relativePath": "images/gone.png"}]},
    }, Storage()))

    assert result["metadata"]["images"] == []
    assert result["first_img_url"] in (None, "")
    assert "gone.png" not in result["raw_text"]
    assert "https://example.com/image.png" in result["raw_text"]


@pytest.mark.parametrize("query", ["", "?signature=a%2Fb"])
def test_hydration_normalizes_cover_and_preserves_external_markdown(query):
    external = "https://bucket.oss-cn-beijing.aliyuncs.com/images/chart.png" + query

    class Storage:
        async def load_image_manifest(self, document_id):
            return [{"image_id": "image", "minio_object_key": "derived/doc/chart.png"}]

        async def inspect_object(self, storage_key):
            return {"storage_key": storage_key}

    result = asyncio.run(hydrate_parse_result_images("doc", {
        "first_img_url": "derived/doc/chart.png",
        "raw_text": f"![external]({external})\n![managed](images/chart.png)",
        "metadata": {"first_img_url": external, "images": [{
            "relativePath": "images/chart.png", "minio_object_key": "derived/doc/chart.png",
        }]},
    }, Storage()))
    assert result["first_img_url"] == "/api/v1/perception/images/doc/image"
    assert result["metadata"]["first_img_url"] == external
    assert f"![external]({external})" in result["raw_text"]
    assert "![managed](/api/v1/perception/images/doc/image)" in result["raw_text"]
