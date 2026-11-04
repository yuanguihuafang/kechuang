from services.application.chat_response import build_recommendation_items
from services.storage.minio import image_service


def test_recommendation_card_uses_resolved_image_urls_not_markdown_paths():
    items = build_recommendation_items(
        [
            {
                "record_id": "solution-1",
                "knowledge_type": "solution",
                "document_id": "doc-1",
                "text": "方案说明 ![证据图片](images/chart.jpg)",
                "image_ids": ["image-1"],
                "image_urls": ["/images/chart.jpg"],
            }
        ]
    )

    assert items[0]["image_urls"] == [
        "/api/v1/perception/images/doc-1/image-1",
        "/images/chart.jpg",
    ]


def test_recommendation_card_preserves_legacy_image_reference_for_frontend_mapping():
    items = build_recommendation_items(
        [
            {
                "record_id": "solution-1",
                "knowledge_type": "solution",
                "text": "方案说明 ![证据图片](images/chart.jpg)",
                "image_urls": ["/images/chart.jpg"],
            }
        ]
    )

    assert items[0]["image_urls"] == ["/images/chart.jpg"]


def test_recommendation_card_returns_absolute_image_urls_from_public_api_base(monkeypatch):
    monkeypatch.setattr(
        image_service,
        "get_settings",
        lambda: type("Settings", (), {"public_api_base_url": "https://api.example.com/"})(),
    )

    items = build_recommendation_items(
        [{
            "record_id": "solution-1",
            "knowledge_type": "solution",
            "document_id": "doc-1",
            "image_ids": ["image-1"],
        }]
    )

    assert items[0]["image_urls"] == [
        "https://api.example.com/api/v1/perception/images/doc-1/image-1"
    ]
