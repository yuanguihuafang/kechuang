import asyncio

import scripts.repair_solution_images as repair_script
from scripts.repair_solution_images import (
    _collect_parse_result_refs,
    _collect_refs,
    _candidate_images,
    match_reparsed_image,
    plan_image_mappings,
)


def test_run_passes_document_filter_to_preflight(monkeypatch):
    selected_id = "doc-selected"
    received = []

    class Lifecycle:
        class Engine:
            def dispose(self):
                pass

        engine = Engine()

    runtime = type("Runtime", (), {"lifecycle": Lifecycle()})()
    monkeypatch.setattr(repair_script, "_build_runtime", lambda: runtime)

    async def prepare(_runtime, _temporary_root, target_document_id=None):
        received.append(target_document_id)
        return []

    monkeypatch.setattr(repair_script, "_prepare", prepare)

    asyncio.run(
        repair_script.run(apply=False, backup_file=None, document_id=selected_id)
    )

    assert received == [selected_id]


def test_image_match_requires_unique_exact_basename():
    images = [
        {"relativePath": "auto/images/page-1.jpg", "path": "C:/tmp/page-1.jpg"},
        {"relativePath": "auto/images/page-2.jpg", "path": "C:/tmp/page-2.jpg"},
    ]

    assert match_reparsed_image(["mineru-output/old-run/images/page-2.jpg"], images) == images[1]


def test_repair_candidates_exclude_parser_marked_decorative_images():
    icon = {"relativePath": "images/icon.png", "imageRole": "decorative"}
    content = {"relativePath": "images/chart.png", "imageRole": "candidate"}
    legacy = {"relativePath": "images/photo.png"}

    assert _candidate_images([icon, content, legacy]) == [content, legacy]


def test_image_match_rejects_missing_or_ambiguous_basenames():
    images = [
        {"relativePath": "run-a/images/page-1.jpg"},
        {"relativePath": "run-b/images/page-1.jpg"},
    ]

    assert match_reparsed_image(["old/images/page-1.jpg"], images) is None
    assert match_reparsed_image(["old/images/missing.jpg"], images) is None
    assert match_reparsed_image(["old/images/page-1.jpg", "old/page-1.jpg"], images) is None


def test_positional_fallback_is_used_only_for_remaining_one_to_one_images():
    first = {"relativePath": "images/new-1.jpg", "pageNumber": 1}
    second = {"relativePath": "images/new-2.jpg", "pageNumber": 2}
    mappings, unresolved, modes = plan_image_mappings(
        {"old-a": ["old/images/unmatched-a.jpg"], "old-b": ["old/images/unmatched-b.jpg"]},
        [first, second],
    )

    assert unresolved == []
    assert mappings == {"old-a": first, "old-b": second}
    assert modes == {"old-a": "document_order", "old-b": "document_order"}


def test_positional_fallback_repairs_available_images_when_counts_differ():
    image = {"relativePath": "images/new.jpg", "pageNumber": 1}
    mappings, unresolved, modes = plan_image_mappings(
        {"old-a": ["old/images/a.jpg"], "old-b": ["old/images/b.jpg"]},
        [image],
    )

    assert mappings == {"old-a": image}
    assert unresolved == ["old-b"]
    assert modes == {"old-a": "document_order_partial"}


def test_exact_matches_are_preserved_before_positional_fallback():
    exact = {"relativePath": "images/known.jpg", "pageNumber": 1}
    positional = {"relativePath": "images/other.jpg", "pageNumber": 2}
    mappings, unresolved, modes = plan_image_mappings(
        {
            "old-known": ["old/images/known.jpg"],
            "old-unknown": ["old/images/missing-name.jpg"],
        },
        [exact, positional],
    )

    assert unresolved == []
    assert mappings == {"old-known": exact, "old-unknown": positional}
    assert modes == {"old-known": "exact_filename", "old-unknown": "document_order"}


def test_parse_result_image_references_are_collected_from_text_and_metadata():
    refs = _collect_parse_result_refs(
        "doc-1",
        {
            "raw_text": "![page](http://localhost/api/v1/perception/images/doc-1/old-a)",
            "text": "![other](/api/v1/perception/images/doc-1/old-b)",
            "metadata": {
                "images": [
                    {
                        "image_id": "old-c",
                        "relativePath": "images/page-3.jpg",
                        "url": "/api/v1/perception/images/doc-1/old-c",
                    }
                ]
            },
        },
    )

    assert refs == {
        "old-a": ["/api/v1/perception/images/doc-1/old-a"],
        "old-b": ["/api/v1/perception/images/doc-1/old-b"],
        "old-c": ["images/page-3.jpg", "/api/v1/perception/images/doc-1/old-c"],
    }


def test_draft_cover_image_reference_is_collected_by_its_document_owner():
    refs = _collect_refs(
        {
            "attachments": [
                {"document_id": "doc-1"},
                {"document_id": "doc-2"},
            ],
            "fields": {"cover_url": "/api/v1/perception/images/doc-2/cover-id"},
        }
    )

    assert refs == {"doc-2": {"cover-id": ["/api/v1/perception/images/doc-2/cover-id"]}}


def test_preflight_scans_published_solution_parse_results(monkeypatch, tmp_path, capsys):
    document_id = "doc-1"
    image_path = tmp_path / "mineru-output" / "images" / "page-1.jpg"

    class Parser:
        def __init__(self, output_path):
            self.output_path = output_path

        async def parse(self, _path):
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"image")
            icon_path = image_path.parent / "tiny-icon.png"
            icon_path.write_bytes(b"icon")
            return type(
                "Parsed",
                (),
                {
                    "metadata": {
                        "images": [
                            {
                                "path": str(image_path),
                                "relativePath": "images/page-1.jpg",
                                "pageNumber": 1,
                                "imageRole": "candidate",
                            },
                            {
                                "path": str(icon_path),
                                "relativePath": "images/tiny-icon.png",
                                "pageNumber": 1,
                                "imageRole": "decorative",
                            }
                        ]
                    }
                },
            )()

    monkeypatch.setattr(repair_script, "MinerUDocumentParser", Parser)

    class Lifecycle:
        def list_documents_page(self, **_kwargs):
            return {
                "documents": [
                    {
                        "document_id": document_id,
                        "source_name": "source.pdf",
                        "active_version": {
                            "publication_status": "PUBLISHED",
                            "version_id": "v1",
                            "storage_key": "uploads/source.pdf",
                        },
                    },
                    {
                        "document_id": "doc-other",
                        "source_name": "other.pdf",
                        "active_version": {
                            "publication_status": "PUBLISHED",
                            "version_id": "v1",
                            "storage_key": "uploads/other.pdf",
                        },
                    },
                ],
                "total": 2,
                "page_size": 100,
            }

        def get_document(self, _document_id):
            return {
                "knowledge_type": "solution",
                "active_version": {
                    "publication_status": "PUBLISHED",
                    "version_id": "v1",
                    "storage_key": "uploads/source.pdf",
                },
            }

    class Drafts:
        def list_page(self, **_kwargs):
            return {"items": [], "total": 0, "page_size": 100}

    class Storage:
        def __init__(self):
            self.loaded_document_ids = []

        async def load_parse_result(self, _document_id):
            self.loaded_document_ids.append(_document_id)
            return {"raw_text": f"![page](/api/v1/perception/images/{document_id}/old-id)"}

        async def load_image_manifest(self, _document_id):
            return []

        async def inspect_object(self, _key):
            return {"size": 1, "etag": "source-etag"}

        async def download_file_to_directory(self, _key, directory, _limit):
            pdf_path = directory / "source.pdf"
            pdf_path.write_bytes(b"pdf")
            return pdf_path, "source.pdf", "application/pdf", 3

    storage = Storage()
    runtime = type(
        "Runtime",
        (),
        {"lifecycle": Lifecycle(), "drafts": Drafts(), "storage": storage},
    )()

    prepared = asyncio.run(
        repair_script._prepare(runtime, tmp_path, target_document_id=document_id)
    )

    assert len(prepared) == 1
    assert prepared[0]["document_id"] == document_id
    assert set(prepared[0]["mappings"]) == {"old-id"}
    assert prepared[0]["mappings"]["old-id"]["relativePath"] == "images/page-1.jpg"
    assert prepared[0]["filtered_decorative_count"] == 1
    assert storage.loaded_document_ids == [document_id]
    progress = capsys.readouterr().err
    assert "预检 1/1" in progress
    assert "重解析完成" in progress
