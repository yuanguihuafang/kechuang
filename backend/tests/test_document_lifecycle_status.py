from services.persistence.document_lifecycle import publication_status_label


def test_publication_status_label_comes_from_persisted_lifecycle_state():
    cases = [
        ({"status": "UPLOADED", "publication_status": "UNPUBLISHED"}, "未发布"),
        ({"status": "UNPUBLISHED", "publication_status": "UNPUBLISHED"}, "已下架"),
        ({"status": "RECYCLED", "publication_status": "UNPUBLISHED"}, "已回收"),
        ({"status": "PUBLISHED", "publication_status": "PUBLISHED"}, "可检索"),
    ]
    for version, expected in cases:
        assert publication_status_label(version) == expected
