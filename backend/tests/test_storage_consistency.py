import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from agents.perception_agent import routes as perception_routes
from services.application import document_images
from admin.service import AdminService
from services.application.document_lifecycle import _recover_lifecycle
from services.storage.minio.minio_storage import MinioUploadService
from services.document_parsing.mineru_parser import (
    collect_mineru_images,
    normalize_markdown_image_references,
)
from runtime.workflows.file_workflow import FileWorkflowService


def test_minio_readiness_does_not_create_a_missing_bucket():
    calls = []

    class Client:
        def head_bucket(self, *, Bucket):
            calls.append(("head_bucket", Bucket))
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadBucket",
            )

        def create_bucket(self, *, Bucket):
            calls.append(("create_bucket", Bucket))

    service = object.__new__(MinioUploadService)
    service.bucket = "kechuang"
    service.client = Client()

    with pytest.raises(ClientError):
        asyncio.run(service.check_ready())

    assert calls == [("head_bucket", "kechuang")]


def test_mineru_artifact_attempt_uses_isolated_minio_prefix(tmp_path):
    artifact = tmp_path / "image.png"
    artifact.write_bytes(b"image")
    uploaded = []

    service = object.__new__(MinioUploadService)
    service.bucket = "kechuang"
    service.client = SimpleNamespace(put_object=lambda **kwargs: uploaded.append(kwargs))
    service._ensure_bucket = lambda: None

    key = service._upload_mineru_artifact_sync(
        "doc-1", "images/image.png", artifact, "image/png", "attempt-1"
    )

    assert key == "derived/doc-1/mineru/attempts/attempt-1/images/image.png"
    assert uploaded[0]["Key"] == key


def test_mineru_artifact_batch_failure_rolls_back_uploaded_attempt(monkeypatch, tmp_path):
    output_dir = tmp_path / "mineru-output"
    output_dir.mkdir()
    (output_dir / "01.txt").write_text("first", encoding="utf-8")
    (output_dir / "02.txt").write_text("second", encoding="utf-8")
    monkeypatch.setattr(perception_routes, "MINERU_OUTPUT_ROOT", tmp_path)

    deleted = []

    class Storage:
        async def upload_mineru_artifact(
            self, document_id, relative_path, artifact_path, content_type, *, attempt_id
        ):
            if relative_path == "02.txt":
                raise RuntimeError("MinIO upload interrupted")
            return f"derived/{document_id}/attempts/{attempt_id}/{relative_path}"

        async def save_mineru_artifact_manifest(self, *_args, **_kwargs):
            raise AssertionError("a failed batch must not commit its manifest")

        async def delete_derived_objects(self, document_id, storage_keys):
            deleted.append((document_id, list(storage_keys)))
            return len(storage_keys)

    result = SimpleNamespace(document_id="doc-1", metadata={"outputDir": str(output_dir)})

    with pytest.raises(RuntimeError, match="upload interrupted"):
        asyncio.run(perception_routes.persist_mineru_artifacts(result, Storage()))

    assert len(deleted) == 1
    assert deleted[0][0] == "doc-1"
    assert len(deleted[0][1]) == 1
    assert "/attempts/" in deleted[0][1][0]


def test_parse_stored_uses_requested_filename_for_parser_routing(monkeypatch):
    parsed_paths = []

    class Storage:
        async def download_file_to_directory(self, _key, directory, _limit):
            stored_path = directory / "opaque.bin"
            stored_path.write_bytes(b"pptx")
            return stored_path, "application/octet-stream", stored_path.name, 4

    async def run_agent(path, **kwargs):
        parsed_paths.append((path.name, kwargs["file_name"]))
        return perception_routes.PerceptionResult(
            status="SUCCESS",
            source_name=kwargs["file_name"],
        )

    async def identity_async(value):
        return value

    monkeypatch.setattr(perception_routes, "_storage", lambda: Storage())
    monkeypatch.setattr(perception_routes, "_create_task", lambda **_kwargs: "task-1")
    monkeypatch.setattr(perception_routes, "_run_agent_path", run_agent)
    monkeypatch.setattr(perception_routes, "validate_document_limits", lambda *_args: None)
    monkeypatch.setattr(perception_routes.metrics, "stage", lambda _name: nullcontext())
    monkeypatch.setattr(perception_routes, "persist_image_assets", identity_async)
    monkeypatch.setattr(perception_routes, "attach_image_urls", lambda value: value)
    monkeypatch.setattr(perception_routes, "persist_parse_result", identity_async)
    monkeypatch.setattr(perception_routes, "finish_parse_task", lambda _task_id, value: value)
    monkeypatch.setattr(
        perception_routes,
        "task_store",
        SimpleNamespace(update=lambda *_args, **_kwargs: None),
    )

    result = asyncio.run(
        perception_routes.parse_stored_document(
            document_id="doc-1",
            storage_key="uploads/file.bin",
            file_name="source.pptx",
        )
    )

    assert result.status == "SUCCESS"
    assert parsed_paths == [("source.pptx", "source.pptx")]


@pytest.mark.parametrize(
    "action, expected_call, expected_status",
    [
        ("restore", "republish_document", "PUBLISHED"),
        ("recycle", "unpublish_document", "RECYCLED"),
    ],
)
def test_lifecycle_recovery_handles_admin_restore_and_recycle(
    action, expected_call, expected_status
):
    calls = []

    class Lifecycle:
        def get_version(self, _version_id):
            return {"document_id": "doc-1", "scope_id": "public", "knowledge_type": "demand", "status": "RECYCLED"}

        def get_document(self, document_id, **kwargs):
            return {"document_id": document_id, "knowledge_type": "demand"}

        def update_version(self, version_id, **fields):
            calls.append(("update", version_id, fields))
            return {"version_id": version_id, **fields}

    class Retrieval:
        async def republish_document(self, *_args):
            calls.append(("republish",))
            return SimpleNamespace(status="SUCCESS", model_dump=lambda: {})

        async def unpublish_document(self, *_args):
            calls.append(("unpublish",))
            return SimpleNamespace(status="SUCCESS", model_dump=lambda: {})

    runtime = SimpleNamespace(
        lifecycle_service=Lifecycle(),
        require_lifecycle=lambda: Lifecycle(),
        task_store=None, outbox_repository=None,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        embedded_perception=SimpleNamespace(delete_stored_document_internal=None),
    )

    asyncio.run(
        _recover_lifecycle(
            runtime,
            {
                "payload": {"action": action, "version_id": "doc-1:v1", "user_id": "admin"},
            },
        )
    )

    assert (expected_call.split("_")[0],) in calls
    assert any(item[0] == "update" and item[2]["status"] == expected_status for item in calls)


def test_admin_recycle_repairs_legacy_empty_knowledge_type():
    captured = []

    class Lifecycle:
        def get_version(self, _version_id):
            return {"document_id": "doc-1", "scope_id": "public", "knowledge_type": ""}

        def get_document(self, _document_id, include_purged=True):
            return {"knowledge_type": ""}

        def update_version(self, version_id, **fields):
            return {"version_id": version_id, **fields}

    class Result:
        status = "SUCCESS"

        def model_dump(self):
            return {}

    class Retrieval:
        async def unpublish_document(self, _document_id, request):
            captured.append(request)
            return Result()

    runtime = SimpleNamespace(
        task_store=None,
        outbox_repository=None,
        require_lifecycle=lambda: Lifecycle(),
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
    )

    result, _ = asyncio.run(AdminService(runtime).lifecycle_action("recycle", "doc-1:v1", "admin"))

    assert result["status"] == "RECYCLED"
    assert captured[0].knowledge_type == "solution"
    assert captured[0].identity.user_role.value == "admin"


def test_admin_catalog_hides_user_draft_without_lifecycle_version():
    class Lifecycle:
        def list_documents_page(self, **_kwargs):
            return {"documents": [], "total": 0, "page": 1, "page_size": 20, "total_pages": 0}

    class DraftStore:
        def list_all(self, **_kwargs):
            return [
                {
                    "draft_id": "draft-1",
                    "draft_type": "solution",
                    "status": "PUBLISHED",
                    "user_id": "user-1",
                    "draft": {"attachments": [{"document_id": "doc-1", "file_name": "draft.pdf"}]},
                }
            ]

    runtime = SimpleNamespace(
        require_lifecycle=lambda: Lifecycle(),
        draft_store_service=DraftStore(),
        user_identity_reader=None,
        task_store=None,
    )

    result = AdminService(runtime).document_catalog(
        scope_id=None,
        user_id=None,
        knowledge_type=None,
        status=None,
        page=1,
        page_size=20,
    )

    assert result["documents"] == []


def test_lifecycle_recovery_purge_uses_internal_storage_delete():
    deleted = []

    class Lifecycle:
        def get_version(self, _version_id):
            return {
                "document_id": "doc-1",
                "storage_key": "uploads/doc.pdf",
                "scope_id": "public",
                "knowledge_type": "demand",
                "status": "RECYCLED",
            }

        def list_versions(self, _document_id):
            return [{"version_id": "doc-1:v1", **self.get_version("doc-1:v1")}]

        def get_document(self, document_id, **kwargs):
            return {"document_id": document_id, "knowledge_type": "demand"}

        def update_version(self, version_id, **fields):
            return {"version_id": version_id, **fields}

    class Retrieval:
        async def unpublish_document(self, *_args):
            return SimpleNamespace(status="SUCCESS", model_dump=lambda: {})

    class Perception:
        async def delete_stored_document_internal(self, **kwargs):
            deleted.append(kwargs)

    runtime = SimpleNamespace(
        lifecycle_service=Lifecycle(),
        require_lifecycle=lambda: Lifecycle(),
        require_draft_store=lambda: SimpleNamespace(list_for_document=lambda document_id: []),
        task_store=None, outbox_repository=None,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        embedded_perception=Perception(),
    )

    asyncio.run(
        _recover_lifecycle(
            runtime,
            {
                "payload": {"action": "purge", "version_id": "doc-1:v1", "user_id": "admin"},
            },
        )
    )

    assert deleted == [{"storage_key": "uploads/doc.pdf", "document_id": "doc-1"}]


def test_persisted_images_keep_duplicate_basenames_separate():
    images, manifest = document_images.merge_persisted_image_assets(
        "doc-1",
        [],
        [],
        [
            {"relative_path": "images/a/logo.png", "minio_object_key": "derived/a"},
            {"relative_path": "images/b/logo.png", "minio_object_key": "derived/b"},
        ],
    )

    assert [item["relativePath"] for item in images] == ["images/a/logo.png", "images/b/logo.png"]
    assert len({item["image_id"] for item in images}) == 2
    assert len(manifest) == 2


def test_persisted_images_reuse_existing_object_after_parse_save():
    images, manifest = document_images.merge_persisted_image_assets(
        "doc-1",
        [
            {
                "image_id": "image-1",
                "relativePath": "source/office/images/logo.png",
                "minio_object_key": "derived/doc-1/logo.png",
            },
            {
                "image_id": "image-duplicate",
                "relativePath": "other/office/images/logo.png",
                "minio_object_key": "derived/doc-1/logo.png",
            },
        ],
        [
            {
                "image_id": "image-1",
                "document_id": "doc-1",
                "minio_object_key": "derived/doc-1/logo.png",
            }
        ],
        [
            {
                "relative_path": "office/images/logo.png",
                "minio_object_key": "derived/doc-1/logo.png",
            }
        ],
    )

    assert len(images) == 1
    assert images[0]["image_id"] == "image-1"
    assert len(manifest) == 1


def test_mineru_image_references_keep_one_entry_per_source_path():
    text = (
        "![1_NFC防伪标签项目介绍[1]](/office/images/a.png)\n"
        "![1_NFC防伪标签项目介绍[1]](/office/images/a.png)\n"
        "![同名图](other/images/a.png)"
    )
    result = normalize_markdown_image_references(
        text,
        [
            {"image_id": "image-a", "relativePath": "office/images/a.png"},
            {"image_id": "image-b", "relativePath": "other/images/a.png"},
        ],
    )

    assert result.count("/office/images/a.png") == 1
    assert result.count("other/images/a.png") == 1
    assert "项目介绍[1]" in result


def test_mineru_image_references_support_parentheses_in_source_path():
    text = (
        "![封面](1_NFC防伪标签项目介绍(1)/office/images/a.png)\n"
        "![封面](1_NFC防伪标签项目介绍(1)/office/images/a.png)"
    )

    result = normalize_markdown_image_references(
        text,
        [
            {
                "image_id": "image-a",
                "relativePath": "1_NFC防伪标签项目介绍(1)/office/images/a.png",
            }
        ],
    )

    assert result.count("office/images/a.png") == 1


def test_mineru_image_references_support_spaces_in_source_path():
    path = "赛纳化学（大仓）有限公司 PPT/office/images/evidence.png"
    text = f"![{path}]({path})\n![{path}]({path})"

    result = normalize_markdown_image_references(
        text,
        [{"image_id": "image-a", "relativePath": path}],
    )

    assert result.count(f"]({path})") == 1


def test_mineru_image_references_use_canonical_image_url():
    path = "赛纳化学（大仓）有限公司 PPT/office/images/evidence.png"
    image_url = "/api/v1/perception/images/doc-1/image-a"

    result = normalize_markdown_image_references(
        f"![证据图片]({path})",
        [{"image_id": "image-a", "relativePath": path, "url": image_url}],
    )

    assert result == f"![证据图片]({image_url})"


def test_mineru_image_canonicalization_preserves_markdown_title():
    path = "PPT/office/images/evidence image.png"
    image_url = "/api/v1/perception/images/doc-1/image-a"

    result = normalize_markdown_image_references(
        f'![证据图片]({path} "说明")',
        [{"image_id": "image-a", "relativePath": path, "url": image_url}],
    )

    assert result == f'![证据图片]({image_url} "说明")'


def test_persisted_parse_result_canonicalizes_ppt_image_reference(monkeypatch):
    path = "赛纳化学（大仓）有限公司 PPT/office/images/evidence.png"
    result = SimpleNamespace(
        document_id="doc-1",
        raw_text=f"![证据图片]({path})",
        metadata={
            "images": [{"image_id": "image-a", "relativePath": path}],
            "artifact_refs": {},
        },
    )

    class Storage:
        async def save_image_manifest(self, *_args):
            return "manifest"

    async def persist_artifacts(_result, _storage):
        return {path: "derived/doc-1/image.png"}

    monkeypatch.setattr(perception_routes, "_storage", lambda: Storage())
    monkeypatch.setattr(perception_routes, "persist_mineru_artifacts", persist_artifacts)

    saved = asyncio.run(perception_routes.persist_image_assets(result))

    assert saved.raw_text == "![证据图片](/api/v1/perception/images/doc-1/image-a)"


def test_mineru_image_references_deduplicate_path_aliases_for_same_image():
    text = "![封面](1_NFC防伪标签项目介绍[1]/office/images/a.png)\n![封面](/office/images/a.png)"

    result = normalize_markdown_image_references(
        text,
        [
            {
                "image_id": "image-a",
                "contentHash": "same-image",
                "relativePath": "office/images/a.png",
            }
        ],
    )

    assert result.count("office/images/a.png") == 1


def test_parse_result_image_urls_are_rehydrated_for_admin_reads():
    class Storage:
        async def load_image_manifest(self, _document_id):
            return [
                {
                    "image_id": "image-1",
                    "minio_object_key": "derived/doc-1/image.png",
                }
            ]

        async def load_mineru_artifact_manifest(self, _document_id):
            return []

        async def inspect_object(self, _storage_key):
            return {"storage_key": _storage_key}

    result = asyncio.run(
        perception_routes.hydrate_parse_result_images(
            "doc-1",
            {
                "metadata": {
                    "images": [{"image_id": "image-1", "url": "/api/v1/perception/assets/old.png"}]
                }
            },
            Storage(),
        )
    )

    assert result["metadata"]["images"][0]["url"] == "/api/v1/perception/images/doc-1/image-1"


def test_reopened_parse_result_maps_every_ppt_image_to_persisted_url():
    images = [
        {"image_id": f"image-{index}", "relativePath": f"PPT/office/images/{index}.jpg"}
        for index in range(1, 4)
    ]

    class Storage:
        async def load_image_manifest(self, _document_id):
            return [
                {"image_id": image["image_id"], "minio_object_key": f"derived/doc-1/{image['image_id']}.jpg"}
                for image in images
            ]

        async def load_mineru_artifact_manifest(self, _document_id):
            return []

        async def inspect_object(self, _storage_key):
            return {"storage_key": _storage_key}

    raw_text = "\n".join(
        f"![slide {index}]({image['relativePath']})"
        for index, image in enumerate(images, start=1)
    )
    result = asyncio.run(
        document_images.hydrate_parse_result_images(
            "doc-1",
            {"raw_text": raw_text, "text": raw_text, "metadata": {"images": images}},
            Storage(),
        )
    )

    for image in images:
        expected = f"/api/v1/perception/images/doc-1/{image['image_id']}"
        assert f"]({expected})" in result["raw_text"]
        assert f"]({expected})" in result["text"]
    assert "PPT/office/images/" not in result["raw_text"]


def test_saving_parse_result_deduplicates_retained_images():
    current = {
        "metadata": {
            "images": [
                {
                    "image_id": "image-1",
                    "relativePath": "images/logo.png",
                    "minio_object_key": "derived/doc-1/logo.png",
                },
                {
                    "image_id": "image-duplicate",
                    "relativePath": "images/logo.png",
                    "minio_object_key": "derived/doc-1/logo.png",
                },
            ],
        },
    }
    manifest = [
        {"image_id": "image-1", "minio_object_key": "derived/doc-1/logo.png"},
        {"image_id": "image-duplicate", "minio_object_key": "derived/doc-1/logo.png"},
    ]

    updated, retained_manifest, _, removed = perception_routes._prune_removed_parse_images(
        "正文\n![logo](images/logo.png)",
        current,
        manifest,
        [],
    )

    assert len(updated["images"]) == 1
    assert len(retained_manifest) == 1
    assert removed == []


def test_saving_parse_result_keeps_images_referenced_by_api_url():
    image_id = "13b79ef02a14bc5228c9e8a758003ab0824ee817"
    object_key = "derived/doc-1/image.png"
    current = {
        "metadata": {
            "images": [
                {
                    "image_id": image_id,
                    "relativePath": "images/image.png",
                    "url": f"/api/v1/perception/images/doc-1/{image_id}",
                    "minio_object_key": object_key,
                }
            ],
        },
    }
    manifest = [{"image_id": image_id, "minio_object_key": object_key}]

    updated, retained_manifest, _, removed = perception_routes._prune_removed_parse_images(
        f"![evidence](/api/v1/perception/images/doc-1/{image_id})",
        current,
        manifest,
        [],
    )

    assert updated["images"] == current["metadata"]["images"]
    assert retained_manifest == manifest
    assert removed == []


def test_saving_parse_result_removes_images_without_api_url_reference():
    image_id = "13b79ef02a14bc5228c9e8a758003ab0824ee817"
    object_key = "derived/doc-1/image.png"
    current = {
        "metadata": {
            "images": [
                {
                    "image_id": image_id,
                    "url": f"/api/v1/perception/images/doc-1/{image_id}",
                    "minio_object_key": object_key,
                }
            ],
        },
    }
    manifest = [{"image_id": image_id, "minio_object_key": object_key}]

    updated, retained_manifest, _, removed = perception_routes._prune_removed_parse_images(
        "已移除图片",
        current,
        manifest,
        [],
    )

    assert updated["images"] == []
    assert retained_manifest == []
    assert removed == [object_key]


def test_missing_persisted_image_is_reported_as_not_found():
    service = object.__new__(MinioUploadService)
    service.bucket = "kechuang"
    service._load_image_manifest_sync = lambda _document_id: [
        {
            "image_id": "image-1",
            "minio_object_key": "derived/doc-1/image.png",
        }
    ]

    def get_object(**_kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    service.client = SimpleNamespace(get_object=get_object)

    with pytest.raises(FileNotFoundError):
        service._download_derived_image_sync("doc-1", "image-1")


def test_mineru_images_follow_page_order(tmp_path):
    output_dir = tmp_path / "output"
    (output_dir / "images").mkdir(parents=True)
    (output_dir / "images" / "first.png").write_bytes(b"first")
    (output_dir / "images" / "second.png").write_bytes(b"second")

    assets = collect_mineru_images(
        output_dir,
        [
            {"type": "image", "img_path": "images/second.png", "page_idx": 1},
            {"type": "image", "img_path": "images/first.png", "page_idx": 0},
        ],
        "",
    )

    assert [item["relativePath"] for item in assets] == ["images/first.png", "images/second.png"]


def test_replacing_same_lifecycle_version_only_removes_old_original():
    deleted = []

    class Storage:
        async def delete_original_file(self, storage_key):
            deleted.append(storage_key)

    class Adapter:
        async def delete_version(self, _arguments):
            raise AssertionError("same version replacement must not delete the new vectors")

    workflow = object.__new__(FileWorkflowService)
    workflow.adapter = Adapter()
    workflow.storage_service = Storage()
    workflow.outbox_repository = None
    workflow.lifecycle_store = None

    result = asyncio.run(
        workflow.cleanup_previous_version(
            {
                "document_id": "doc-1",
                "version_id": "doc-1:v1",
                "storage_key": "uploads/new.pdf",
                "previous_version_id": "doc-1:v1",
                "previous_storage_key": "uploads/old.pdf",
            }
        )
    )

    assert result == {"status": "DELETED", "storage_key": "uploads/old.pdf"}
    assert deleted == ["uploads/old.pdf"]


def test_parse_result_persists_saved_chunks(monkeypatch):
    class Storage:
        async def load_parse_result(self, _document_id):
            return {"document_id": "doc-1", "raw_text": "old", "metadata": {}}

        async def load_image_manifest(self, _document_id):
            return []

        async def load_mineru_artifact_manifest(self, _document_id):
            return []

        async def save_parse_result(self, _document_id, result):
            return result

        async def save_image_manifest(self, *_args):
            return "manifest"

    monkeypatch.setattr(perception_routes, "_storage", lambda: Storage())
    saved = asyncio.run(
        perception_routes.update_parse_result(
            "doc-1",
            {"raw_text": "edited", "saved_chunks": [{"text": "chunk-1"}]},
        )
    )

    assert saved["saved_chunks"] == [{"text": "chunk-1"}]


def test_parse_result_save_repairs_legacy_duplicate_image_references(monkeypatch):
    class Storage:
        async def load_parse_result(self, _document_id):
            return {
                "document_id": "doc-1",
                "raw_text": "old",
                "metadata": {
                    "images": [{"image_id": "image-1", "relativePath": "office/images/a.png"}]
                },
            }

        async def load_image_manifest(self, _document_id):
            return []

        async def load_mineru_artifact_manifest(self, _document_id):
            return []

        async def save_image_manifest(self, *_args):
            return "manifest"

        async def save_parse_result(self, _document_id, result):
            return result

    monkeypatch.setattr(perception_routes, "_storage", lambda: Storage())
    saved = asyncio.run(
        perception_routes.update_parse_result(
            "doc-1",
            {"raw_text": "![图](office/images/a.png)\n![图](office/images/a.png)"},
        )
    )

    assert saved["raw_text"].count("office/images/a.png") == 1


def test_normalize_parse_result_images_endpoint_repairs_saved_text(monkeypatch):
    class Storage:
        async def load_parse_result(self, _document_id):
            return {
                "document_id": "doc-1",
                "raw_text": "![封面](prefix/office/images/a.png)\n![封面](/office/images/a.png)",
                "metadata": {
                    "images": [
                        {
                            "image_id": "image-a",
                            "contentHash": "same-image",
                            "relativePath": "office/images/a.png",
                        }
                    ]
                },
            }

        async def save_parse_result(self, _document_id, result):
            return result

    monkeypatch.setattr(perception_routes, "_storage", lambda: Storage())
    saved = asyncio.run(perception_routes.normalize_parse_result_images("doc-1"))

    assert saved["changed"] is True
    assert saved["raw_text"].count("office/images/a.png") == 1
