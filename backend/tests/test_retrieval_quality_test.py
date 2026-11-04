import csv

from openpyxl import load_workbook

from evaluation.rag_eval import EvalCase, EvalObservation
from evaluation.retrieval_quality_test import (
    ANNOTATION_REVIEW_FIELDS,
    _apply_review_annotations,
    _parse_args,
    _write_annotation_review,
)


def test_retrieval_quality_cli_defaults(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["retrieval_quality_test", "enterprise"],
    )

    args = _parse_args()

    assert args.base_url == "http://127.0.0.1:8793"
    assert args.top_k == 5
    assert args.concurrency == 2
    assert not hasattr(args, "use_rerank")
    assert not hasattr(args, "compare_rerank")


def test_annotation_cli_uses_top_20_and_review_dir_is_opt_in(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["retrieval_quality_test", "enterprise", "--annotate"],
    )

    args = _parse_args()

    assert args.annotate is True
    assert args.annotation_top_k == 20
    assert args.review_dir.name == "annotation"


def test_review_annotations_promote_selected_chunks_to_confirmed(tmp_path):
    case = EvalCase(
        case_id="enterprise-001",
        knowledge_type="enterprise",
        query="查询企业能力",
        expected_source_contains=["mysql:xp_enterprise_profile|id:1"],
        status="candidate",
    )
    review_path = tmp_path / "enterprise.csv"
    with review_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["case_id", "chunk_key", "review_relevant"],
        )
        writer.writeheader()
        writer.writerow({
            "case_id": case.case_id,
            "chunk_key": "mysql:xp_enterprise_profile|type:record|id:1|chunk:0",
            "review_relevant": "1",
        })

    reviewed = _apply_review_annotations([case], review_path)

    assert reviewed[0].status == "confirmed"
    assert reviewed[0].expected_chunk_source_contains == [
        "mysql:xp_enterprise_profile|type:record|id:1|chunk:0"
    ]


def test_annotation_review_contains_editable_chunk_key_and_llm_default(tmp_path):
    case = EvalCase(
        case_id="enterprise-001",
        knowledge_type="enterprise",
        query="查询企业能力",
        status="candidate",
    )
    observation = EvalObservation(
        case_id=case.case_id,
        evidences=[{
            "record_id": "chunk-id",
            "source_locator": "mysql:xp_enterprise_profile|type:record|id:1",
            "chunk_index": 0,
            "score": 0.9,
            "llm_relevant": True,
            "relevance_reason": "直接回答问题",
            "chunk_text": "企业能力说明",
        }],
        elapsed_ms=1,
    )
    review_path = tmp_path / "review.csv"

    _write_annotation_review(review_path, [case], [observation])

    with review_path.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["chunk_key"] == "mysql:xp_enterprise_profile|type:record|id:1|chunk:0"
    assert row["llm_relevant"] == "1"
    assert row["review_relevant"] == "1"


def test_annotation_review_puts_manual_review_fields_first():
    assert ANNOTATION_REVIEW_FIELDS[:7] == [
        "query",
        "score",
        "llm_relevant",
        "llm_reason",
        "review_relevant",
        "review_comment",
        "chunk_text",
    ]


def test_annotation_review_can_be_written_and_read_as_xlsx(tmp_path):
    case = EvalCase(
        case_id="enterprise-001",
        knowledge_type="enterprise",
        query="查询企业能力",
        status="candidate",
    )
    observation = EvalObservation(
        case_id=case.case_id,
        evidences=[{
            "record_id": "chunk-id",
            "source_locator": "mysql:xp_enterprise_profile|type:record|id:1",
            "chunk_index": 0,
            "score": 0.9,
            "llm_relevant": True,
            "relevance_reason": "直接回答问题",
            "chunk_text": "企业能力说明",
        }],
        elapsed_ms=1,
    )
    review_path = tmp_path / "enterprise.xlsx"

    _write_annotation_review(review_path, [case], [observation])

    workbook = load_workbook(review_path, read_only=True, data_only=True)
    row = next(workbook.active.iter_rows(min_row=2, values_only=True))
    assert row[10] == "mysql:xp_enterprise_profile|type:record|id:1|chunk:0"
    assert row[2] == 1
    assert row[4] == 1
    workbook.close()

    reviewed = _apply_review_annotations([case], review_path)
    assert reviewed[0].status == "confirmed"
