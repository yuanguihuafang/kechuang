from agents.retrieval_agent.application.retrieval_helpers import group_records
from core.contracts.retrieval import Evidence


def test_grouped_card_summary_prefers_best_source_chunk_over_draft():
    evidence = [
        Evidence(
            record_id="footer",
            text="谢谢",
            score=0.2,
            source="milvus",
            mysql_id="27",
            source_id="shared-source",
            chunk_role="agent_doc",
        ),
        Evidence(
            record_id="solution",
            text="青岛三态比特科技提供服装产业AI服务，拥有图像生成及虚拟试衣等技术。",
            score=0.9,
            source="milvus",
            matched_draft_mysql_id="27",
            source_id="shared-source",
            chunk_role="agent_doc",
        ),
        Evidence(
            record_id="draft", text="草稿内容", score=0.99, source="milvus",
            mysql_id="27", source_id="shared-source", chunk_role="admin_draft",
        ),
        Evidence(
            record_id="draft-28", text="同组另一条业务草稿", score=0.95, source="milvus",
            mysql_id="28", source_id="shared-source", chunk_role="admin_draft",
        ),
    ]

    records = group_records(evidence, [])

    assert len(records) == 2
    record_27 = next(record for record in records if record.mysql_id == "27")
    record_28 = next(record for record in records if record.mysql_id == "28")
    assert record_27.summary == evidence[1].text
    assert record_28.summary == "同组另一条业务草稿"
