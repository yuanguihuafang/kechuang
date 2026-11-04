import pytest

from scripts.repair_solution_reparsed_documents import (
    _blocks_match_ignoring_image_refs,
    _cover_field_patch,
    _filter_blocks,
    _include_image_references,
    _published_draft_chunks,
    _repair_target_expression,
    _prune_unavailable_block_images,
    _select_reviewed_images,
    _source_ids_match,
    _cover_image_url,
)
from agents.retrieval_agent.schemas import IndexChunk


def test_saved_parse_blocks_match_only_when_non_image_content_is_unchanged():
    saved = [{
        "text": "正文",
        "source_locator": "page:1",
        "metadata": {
            "image_ids": ["old-id"],
            "image_refs": [{"image_id": "old-id", "sources": ["old.jpg"]}],
            "heading": "标题",
        },
    }]
    parsed = [{
        "text": "正文",
        "source_locator": "page:1",
        "metadata": {
            "image_ids": ["new-id"],
            "image_refs": [{"image_id": "new-id", "sources": ["new.jpg"]}],
            "heading": "标题",
        },
    }]

    assert _blocks_match_ignoring_image_refs(saved, parsed)
    parsed[0]["text"] = "不同正文"
    assert not _blocks_match_ignoring_image_refs(saved, parsed)


def test_prune_unavailable_image_ids_keeps_current_images_and_text():
    blocks = [{
        "text": "正文\n![旧图](/api/v1/perception/images/doc-1/stale)",
        "metadata": {
            "image_ids": ["current", "stale"],
            "image_refs": [
                {"image_id": "current", "sources": ["images/current.jpg"]},
                {"image_id": "stale", "sources": ["images/stale.jpg"]},
            ],
        },
    }]

    cleaned, removed = _prune_unavailable_block_images(blocks, "doc-1", {"current"})

    assert removed == {"stale"}
    assert cleaned[0]["text"] == "正文"
    assert cleaned[0]["metadata"]["image_ids"] == ["current"]
    assert [item["image_id"] for item in cleaned[0]["metadata"]["image_refs"]] == ["current"]


def test_review_selection_uses_order_and_maps_reviewed_original_indices():
    review = [
        {"unique_index": 1, "original_index": 11},
        {"unique_index": 2, "original_index": 12},
        {"unique_index": 3, "original_index": 13},
    ]
    images = [{"image_id": f"new-{index}"} for index in range(1, 4)]

    kept, removed = _select_reviewed_images(
        images, review, {"expected_image_count": 3, "drops": {12}}
    )

    assert [image["image_id"] for image in kept] == ["new-1", "new-3"]
    assert [image["image_id"] for image in removed] == ["new-2"]


def test_replacement_mode_keeps_newly_parsed_images_and_ids():
    images = [{"image_id": "new-1"}, {"image_id": "new-2"}]

    kept, removed = _select_reviewed_images(
        images, [], {"replace_with_all_new_images": True}
    )

    assert kept == images
    assert removed == []


def test_source_id_guard_allows_missing_legacy_metadata_but_rejects_conflicts():
    assert _source_ids_match(set(), "source:expected")
    assert _source_ids_match({"source:expected"}, "source:expected")
    assert not _source_ids_match({"source:other"}, "source:expected")
    assert not _source_ids_match({"source:expected", "source:other"}, "source:expected")


def test_cover_image_is_empty_when_a_reparsed_document_has_no_images():
    assert _cover_image_url("doc-1", []) == ""
    assert _cover_image_url("doc-1", [{"image_id": "cover"}]).endswith("/doc-1/cover")


def test_empty_reparse_cover_preserves_existing_business_cover_fields():
    assert _cover_field_patch("") == {}
    assert _cover_field_patch("/images/new") == {
        "fields": {"first_img_url": "/images/new"},
        "mysql_fields": {"first_img_url": "/images/new"},
    }


@pytest.mark.parametrize(
    ("draft_role", "document_role"),
    [("admin_draft", "admin_doc"), ("agent_draft", "agent_doc")],
)
def test_published_draft_chunks_preserve_the_matching_document_role(draft_role, document_role):
    chunks = [
        IndexChunk(chunk_text="已发布字段", source_locator="draft", chunk_role=draft_role),
        IndexChunk(chunk_text="其他数据", source_locator="other", chunk_role="mysql"),
    ]

    selected, selected_document_role = _published_draft_chunks(chunks, "source:current")

    assert [chunk.chunk_text for chunk in selected] == ["已发布字段"]
    assert selected[0].source_id == "source:current"
    assert selected_document_role == document_role


def test_repair_target_expression_includes_both_published_draft_role_families():
    expression = _repair_target_expression("doc-1", "222")

    assert 'chunk_role == "admin_draft"' in expression
    assert 'chunk_role == "admin_doc"' in expression
    assert 'chunk_role == "agent_draft"' in expression
    assert 'chunk_role == "agent_doc"' in expression


def test_review_selection_rejects_count_mismatch_before_writes():
    with pytest.raises(RuntimeError, match="本次解析图片数量与复核预期不一致"):
        _select_reviewed_images(
            [{"image_id": "new-1"}],
            [{"unique_index": 1, "original_index": 1}, {"unique_index": 2, "original_index": 2}],
            {"expected_image_count": 2, "drop_positions": {1}},
        )


def test_image_references_are_added_when_mineru_only_attaches_them_to_blocks():
    result = _include_image_references(
        "解析原文",
        [{"relativePath": "images/one.jpg", "pageNumber": 3}],
    )

    assert result == "解析原文\n\n![第3页图片](images/one.jpg)"


def test_filtered_blocks_remove_image_reference_dicts_by_image_id():
    blocks = [{"text": "正文", "metadata": {
        "image_ids": ["keep", "drop"],
        "image_refs": [{"image_id": "keep", "sources": ["images/keep.jpg"]},
                       {"image_id": "drop", "sources": ["images/drop.jpg"]}],
    }}]

    result = _filter_blocks(
        blocks, "doc-1", [{"image_id": "drop", "relativePath": "images/drop.jpg"}]
    )

    assert result[0]["metadata"]["image_ids"] == ["keep"]
    assert result[0]["metadata"]["image_refs"] == [
        {"image_id": "keep", "sources": ["images/keep.jpg"]}
    ]
