import asyncio

import scripts.repair_solution_artifacts as repair
from agents.perception_agent.schemas import PerceptionResult
from scripts.repair_solution_artifacts import (
    _apply_group,
    _manifest_entries_from_parse_result,
    map_reparsed_images,
    rebind_result_images,
)


def test_maps_reparsed_assets_to_existing_image_ids_by_content_hash():
    images = [
        {
            "image_id": "new-id",
            "relativePath": "images/page-1.jpg",
            "contentHash": "sha1-a",
            "source_locator": "source.pdf|type:image|page:1|ordinal:1",
        }
    ]
    manifest = [
        {
            "image_id": "published-id",
            "minio_object_key": "derived/doc-1/mineru/recovered-images/published-id.jpg",
            "content_hash": "sha1-a",
            "source_locator": "source.pdf|type:image|page:1|ordinal:1",
        }
    ]

    mapped, unresolved, mapping_modes = map_reparsed_images(images, manifest)

    assert unresolved == []
    assert mapped == {"images/page-1.jpg": manifest[0]}
    assert mapping_modes == {"images/page-1.jpg": "source_locator"}
    assert images[0]["image_id"] == "new-id"


def test_rejects_ambiguous_content_hash_in_existing_manifest():
    images = [{"image_id": "new-id", "relativePath": "images/page-1.jpg", "contentHash": "same"}]
    manifest = [
        {"image_id": "old-a", "minio_object_key": "a.jpg", "content_hash": "same"},
        {"image_id": "old-b", "minio_object_key": "b.jpg", "content_hash": "same"},
    ]

    mapped, unresolved, mapping_modes = map_reparsed_images(images, manifest)

    assert mapped == {}
    assert unresolved == ["images/page-1.jpg"]
    assert mapping_modes == {}


def test_maps_unmatched_images_by_position_only_when_counts_match():
    images = [
        {"image_id": "new-a", "relativePath": "images/page-1.jpg", "contentHash": "new-a"},
        {"image_id": "new-b", "relativePath": "images/page-2.jpg", "contentHash": "new-b"},
    ]
    manifest = [
        {"image_id": "old-a", "minio_object_key": "a.jpg", "content_hash": "old-a"},
        {"image_id": "old-b", "minio_object_key": "b.jpg", "content_hash": "old-b"},
    ]

    mapped, unresolved, mapping_modes = map_reparsed_images(images, manifest)

    assert unresolved == []
    assert [mapped[f"images/page-{page}.jpg"]["image_id"] for page in (1, 2)] == [
        "old-a",
        "old-b",
    ]
    assert mapping_modes == {
        "images/page-1.jpg": "position",
        "images/page-2.jpg": "position",
    }


def test_rebinds_parse_result_to_existing_image_id():
    result = PerceptionResult(
        status="SUCCESS",
        source_name="source.pdf",
        document_id="doc-1",
        raw_text="![page](images/page-1.jpg)",
        text="![page](images/page-1.jpg)",
        metadata={
            "images": [
                {
                    "image_id": "new-id",
                    "relativePath": "images/page-1.jpg",
                    "contentHash": "sha1-a",
                    "url": "/api/v1/perception/images/doc-1/new-id",
                }
            ]
        },
    )
    mapped = {
        "images/page-1.jpg": {
            "image_id": "published-id",
            "minio_object_key": "derived/doc-1/recovered.jpg",
            "content_hash": "sha1-a",
        }
    }

    rebind_result_images(result, "doc-1", mapped)

    image = result.metadata["images"][0]
    assert image["image_id"] == "published-id"
    assert image["minio_object_key"] == "derived/doc-1/recovered.jpg"
    assert "doc-1/published-id" in result.raw_text


def test_recovers_manifest_entries_from_matching_parse_result_without_reparsing():
    payload = {
        "document_id": "doc-1",
        "storage_key": "uploads/source.pdf",
        "metadata": {
            "parser": "mineru",
            "artifact_refs": {
                "layout.json": "derived/doc-1/mineru/attempts/old/layout.json"
            },
        },
    }

    assert _manifest_entries_from_parse_result(
        "doc-1", "uploads/source.pdf", payload
    ) == [
        {
            "relative_path": "layout.json",
            "minio_object_key": "derived/doc-1/mineru/attempts/old/layout.json",
        }
    ]
    assert _manifest_entries_from_parse_result("doc-other", "uploads/source.pdf", payload) is None
    assert _manifest_entries_from_parse_result("doc-1", "uploads/other.pdf", payload) is None


def test_non_mineru_parse_result_can_restore_an_empty_manifest():
    assert _manifest_entries_from_parse_result(
        "doc-1",
        "uploads/source.docx",
        {
            "document_id": "doc-1",
            "storage_key": "uploads/source.docx",
            "metadata": {"parser": "docx"},
        },
    ) == []


def test_apply_restores_only_parse_artifacts_without_writing_image_manifest(tmp_path):
    artifact = tmp_path / "content.md"
    artifact.write_text("parsed", encoding="utf-8")

    class Lifecycle:
        def get_document(self, document_id):
            return {
                "knowledge_type": "solution",
                "active_version": {
                    "publication_status": "PUBLISHED",
                    "version_id": "doc-1:v1",
                },
            }

    class Storage:
        def __init__(self):
            self.objects = {"uploads/source.pdf": {"etag": '"source"', "size": 10}}
            self.manifest = []
            self.parse_result = None
            self.image_manifest_writes = 0

        async def inspect_object(self, key):
            return self.objects.get(key)

        async def load_image_manifest(self, document_id):
            return []

        async def save_image_manifest(self, document_id, images):
            self.image_manifest_writes += 1
            raise AssertionError("repair must not replace the image manifest")

        async def upload_mineru_artifact(
            self, document_id, relative_path, path, content_type, *, attempt_id
        ):
            key = f"derived/{document_id}/mineru/attempts/{attempt_id}/{relative_path}"
            self.objects[key] = {"etag": '"artifact"', "size": path.stat().st_size}
            return key

        async def save_mineru_artifact_manifest(self, document_id, artifacts):
            self.manifest = artifacts
            self.objects[f"derived/{document_id}/mineru/artifact-manifest.json"] = {
                "etag": '"manifest"',
                "size": 1,
            }

        async def load_mineru_artifact_manifest(self, document_id):
            return self.manifest

        async def save_parse_result(self, document_id, result):
            self.parse_result = result
            self.objects[f"derived/{document_id}/mineru/parse-result.json"] = {
                "etag": '"parse"',
                "size": 1,
            }

        async def load_parse_result(self, document_id):
            return self.parse_result

        async def delete_derived_objects(self, document_id, keys):
            for key in keys:
                self.objects.pop(key, None)

    storage = Storage()
    runtime = type("Runtime", (), {"lifecycle": Lifecycle(), "storage": storage})()
    group = {
        "document_id": "doc-1",
        "storage_key": "uploads/source.pdf",
        "version_id": "doc-1:v1",
        "source_info": {"etag": '"source"'},
        "manifest_missing": True,
        "parse_result_missing": True,
        "image_manifest": [],
        "existing_artifacts": [],
        "artifact_files": [artifact],
        "output_dir": tmp_path,
        "image_mapping": {},
        "image_count": 0,
        "result": PerceptionResult(
            status="SUCCESS",
            source_name="source.pdf",
            document_id="doc-1",
            raw_text="source text",
            text="source text",
        ),
    }

    result = asyncio.run(_apply_group(runtime, group, "repair-run"))

    assert result["status"] == "SUCCESS"
    assert storage.image_manifest_writes == 0
    assert storage.parse_result["raw_text"] == "source text"


def test_prepare_passes_original_filename_not_content_type_to_parser(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    seen = {}

    class Parser:
        def __init__(self, output_path):
            self.output_path = output_path

    class Agent:
        def __init__(self):
            self.parser = Parser(tmp_path / "mineru-output")

        async def run_path(self, **kwargs):
            seen.update(kwargs)
            output_dir = self.parser.output_path / "parsed"
            output_dir.mkdir(parents=True)
            (output_dir / "content.md").write_text("parsed", encoding="utf-8")
            return PerceptionResult(
                status="SUCCESS",
                source_name=kwargs["file_name"],
                metadata={"outputDir": str(output_dir), "images": []},
            )

    class Lifecycle:
        def list_documents_page(self, **kwargs):
            return {
                "documents": [
                    {
                        "document_id": "doc-1",
                        "source_name": "source.pdf",
                        "active_version": {
                            "publication_status": "PUBLISHED",
                            "version_id": "doc-1:v1",
                            "storage_key": "uploads/source.pdf",
                        },
                    }
                ],
                "total": 1,
                "page_size": 100,
            }

    class Storage:
        async def inspect_object(self, key):
            if key == "uploads/source.pdf":
                return {"etag": '"source"', "size": 3}
            return None

        async def load_mineru_artifact_manifest(self, document_id):
            return []

        async def load_image_manifest(self, document_id):
            return []

        async def download_file_to_directory(self, storage_key, directory, max_bytes):
            return source, "application/pdf", "source.pdf", 3

    monkeypatch.setattr(repair, "PerceptionAgent", Agent)
    runtime = type("Runtime", (), {"lifecycle": Lifecycle(), "storage": Storage()})()

    groups = asyncio.run(repair._prepare(runtime, tmp_path))

    assert seen["file_name"] == "source.pdf"
    assert groups[0]["status"] == "ready"


def test_prepare_restores_missing_manifest_from_existing_parse_result(tmp_path, monkeypatch):
    class Parser:
        def __init__(self, output_path):
            raise AssertionError("existing parse result should avoid reparsing")

    class Lifecycle:
        def list_documents_page(self, **kwargs):
            return {
                "documents": [
                    {
                        "document_id": "doc-1",
                        "source_name": "source.docx",
                        "active_version": {
                            "publication_status": "PUBLISHED",
                            "version_id": "doc-1:v1",
                            "storage_key": "uploads/source.docx",
                        },
                    }
                ],
                "total": 1,
                "page_size": 100,
            }

    class Storage:
        async def inspect_object(self, key):
            if key == "uploads/source.docx":
                return {"etag": '"source"', "size": 3}
            if key == "derived/doc-1/mineru/layout.json":
                return {"size": 8, "content_type": "application/json"}
            if key == "derived/doc-1/mineru/parse-result.json":
                return {"size": 128}
            return None

        async def load_mineru_artifact_manifest(self, document_id):
            return []

        async def load_image_manifest(self, document_id):
            return []

        async def load_parse_result(self, document_id):
            return {
                "document_id": "doc-1",
                "storage_key": "uploads/source.docx",
                "metadata": {
                    "parser": "mineru",
                    "artifact_refs": {
                        "layout.json": "derived/doc-1/mineru/layout.json"
                    },
                },
            }

    monkeypatch.setattr(repair, "MinerUDocumentParser", Parser)
    runtime = type("Runtime", (), {"lifecycle": Lifecycle(), "storage": Storage()})()

    groups = asyncio.run(repair._prepare(runtime, tmp_path))

    assert groups[0]["status"] == "ready"
    assert groups[0]["manifest_only_repair"] is True
    assert groups[0]["existing_artifacts"][0]["size_bytes"] == 8


def test_prepare_accepts_non_mineru_parser_without_artifact_directory(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"

    class Agent:
        async def run_path(self, **kwargs):
            assert kwargs["file_name"] == "source.docx"
            return PerceptionResult(
                status="SUCCESS",
                source_name="source.docx",
                metadata={"parser": "python_docx", "images": []},
            )

    class Lifecycle:
        def list_documents_page(self, **kwargs):
            return {
                "documents": [
                    {
                        "document_id": "doc-1",
                        "source_name": "source.docx",
                        "active_version": {
                            "publication_status": "PUBLISHED",
                            "version_id": "doc-1:v1",
                            "storage_key": "uploads/source.docx",
                        },
                    }
                ],
                "total": 1,
                "page_size": 100,
            }

    class Storage:
        async def inspect_object(self, key):
            if key == "uploads/source.docx":
                return {"etag": '"source"', "size": 3}
            return None

        async def load_mineru_artifact_manifest(self, document_id):
            return []

        async def load_image_manifest(self, document_id):
            return []

        async def download_file_to_directory(self, storage_key, directory, max_bytes):
            source.write_bytes(b"docx")
            return source, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "source.docx", 4

    monkeypatch.setattr(repair, "PerceptionAgent", Agent)
    runtime = type("Runtime", (), {"lifecycle": Lifecycle(), "storage": Storage()})()

    groups = asyncio.run(repair._prepare(runtime, tmp_path))

    assert groups[0]["status"] == "ready"
    assert groups[0]["artifact_files"] == []
    assert groups[0]["result"].metadata["parser"] == "python_docx"


def test_prepare_allows_hash_mismatch_for_count_matched_positional_images(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"

    class Parser:
        def __init__(self, output_path):
            self.output_path = output_path

    class Agent:
        def __init__(self):
            self.parser = Parser(tmp_path / "mineru-output")

        async def run_path(self, **kwargs):
            output_dir = self.parser.output_path / "parsed"
            image_dir = output_dir / "images"
            image_dir.mkdir(parents=True)
            images = []
            for index in (1, 2):
                path = image_dir / f"page-{index}.jpg"
                path.write_bytes(b"reparsed-image")
                images.append(
                    {
                        "image_id": f"new-{index}",
                        "relativePath": f"images/page-{index}.jpg",
                        "contentHash": f"new-hash-{index}",
                        "path": str(path),
                    }
                )
            (output_dir / "content.md").write_text("parsed", encoding="utf-8")
            return PerceptionResult(
                status="SUCCESS",
                source_name="source.pdf",
                metadata={"parser": "mineru", "outputDir": str(output_dir), "images": images},
            )

    class Lifecycle:
        def list_documents_page(self, **kwargs):
            return {
                "documents": [
                    {
                        "document_id": "doc-1",
                        "source_name": "source.pdf",
                        "active_version": {
                            "publication_status": "PUBLISHED",
                            "version_id": "doc-1:v1",
                            "storage_key": "uploads/source.pdf",
                        },
                    }
                ],
                "total": 1,
                "page_size": 100,
            }

    class Storage:
        async def inspect_object(self, key):
            if key == "uploads/source.pdf":
                return {"etag": '"source"', "size": 3}
            return None

        async def load_mineru_artifact_manifest(self, document_id):
            return []

        async def load_image_manifest(self, document_id):
            return [
                {"image_id": f"old-{index}", "minio_object_key": f"old-{index}.jpg"}
                for index in (1, 2)
            ]

        async def download_file_to_directory(self, storage_key, directory, max_bytes):
            source.write_bytes(b"pdf")
            return source, "application/pdf", "source.pdf", 3

        async def download_derived_image(self, document_id, image_id):
            return b"different-existing-image", "image/jpeg", f"{image_id}.jpg"

    monkeypatch.setattr(repair, "PerceptionAgent", Agent)
    runtime = type("Runtime", (), {"lifecycle": Lifecycle(), "storage": Storage()})()

    groups = asyncio.run(repair._prepare(runtime, tmp_path))

    assert groups[0]["status"] == "ready"
    assert groups[0]["mapping_modes"] == {
        "images/page-1.jpg": "position",
        "images/page-2.jpg": "position",
    }


def test_prepare_can_limit_repair_to_one_document_id(tmp_path):
    class Lifecycle:
        def list_documents_page(self, **kwargs):
            return {
                "documents": [
                    {
                        "document_id": "doc-1",
                        "active_version": {"publication_status": "PUBLISHED"},
                    }
                ],
                "total": 1,
                "page_size": 100,
            }

    runtime = type("Runtime", (), {"lifecycle": Lifecycle(), "storage": object()})()

    groups = asyncio.run(repair._prepare(runtime, tmp_path, "doc-other"))

    assert groups == []
