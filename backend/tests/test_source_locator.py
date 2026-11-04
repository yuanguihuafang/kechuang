from core.contracts.mysql_schema import append_attachment_names, mysql_record_locator_with_attachments
from core.contracts.retrieval import Evidence
from agents.retrieval_agent.application.retrieval_helpers import related_documents
from urllib.parse import unquote


def test_mysql_record_locator_keeps_attachment_names():
    locator = mysql_record_locator_with_attachments(
        "solution",
        22,
        [{"document_id": "doc-1", "file_name": "技术方案.pdf"}],
    )

    assert locator.startswith("mysql:xp_pub_tech_solution|type:record|id:22|attachments:")
    assert "技术方案.pdf" in unquote(locator)


def test_mysql_record_locator_stays_within_milvus_limit():
    locator = mysql_record_locator_with_attachments(
        "solution",
        22,
        [{"document_id": "doc-1", "file_name": "很长的附件名称" * 100}],
    )

    assert len(locator.encode("utf-8")) <= 512


def test_related_documents_reads_attachment_name_from_locator():
    locator = mysql_record_locator_with_attachments(
        "solution", 22, [{"document_id": "doc-1", "file_name": "技术方案.pdf"}]
    )

    documents = related_documents([
        Evidence(
            record_id="mysql-22",
            text="方案内容",
            score=1,
            source="mysql",
            source_locator=locator,
        )
    ])

    assert [(item.document_id, item.source_name) for item in documents] == [
        ("doc-1", "技术方案.pdf")
    ]


def test_related_documents_hides_mysql_record_and_keeps_real_attachment():
    locator = mysql_record_locator_with_attachments(
        "demand", 22, [{"document_id": "doc-1", "file_name": "尽调材料汇总.docx"}]
    )

    documents = related_documents([
        Evidence(
            record_id="mysql-22",
            text="企业内容",
            score=1,
            source="mysql",
            document_id="mysql:xp_pub_content_simple",
            source_locator=locator,
        )
    ])

    assert [(item.document_id, item.source_name) for item in documents] == [
        ("doc-1", "尽调材料汇总.docx")
    ]


def test_private_visit_locator_keeps_attachment_names():
    locator = append_attachment_names(
        "ai_inner_inquiry|record_type:customer_visit|type:record|id:22",
        [{"document_id": "doc-1", "file_name": "拜访记录.docx"}],
    )

    assert "拜访记录.docx" in unquote(locator)
