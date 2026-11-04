import pytest
import httpx

from evaluation.rag_eval import (
    EvalCase,
    EvalObservation,
    _is_relevant,
    _parse_relevance_judgement,
    _retry_delay_seconds,
    calculate_metrics,
)


def _case(case_id: str = "case-1") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        knowledge_type="solution",
        query="查找方案",
        relevant_chunk_ids=["doc-a|chunk:0"],
        status="confirmed",
    )


def test_confirmed_answerable_case_requires_chunk_annotation():
    with pytest.raises(ValueError, match="必须标注相关 chunk"):
        EvalCase(
            case_id="missing-chunk-label",
            knowledge_type="enterprise",
            query="查询企业能力",
            expected_document_ids=["doc-a"],
            status="confirmed",
        )


def test_calculate_metrics_returns_only_precision_recall_f1_and_auc_roc():
    metrics = calculate_metrics(
        [_case()],
        [
            EvalObservation(
                case_id="case-1",
                evidences=[
                    {"document_id": "doc-a", "chunk_index": 0, "score": 0.9},
                    {"document_id": "doc-b", "score": 0.8},
                ],
                elapsed_ms=1,
            )
        ],
        k=2,
    )

    assert set(metrics) == {"precision", "recall", "f1", "auc_roc"}
    assert metrics == {
        "precision": 0.5,
        "recall": 1.0,
        "f1": 2 / 3,
        "auc_roc": 1.0,
    }


def test_calculate_metrics_counts_unreturned_expected_document_as_false_negative():
    metrics = calculate_metrics(
        [_case()],
        [
            EvalObservation(
                case_id="case-1",
                evidences=[{"document_id": "doc-b", "score": 0.9}],
                elapsed_ms=1,
            )
        ],
        k=1,
    )

    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0
    assert metrics["f1"] == 0.0
    assert metrics["auc_roc"] is None


def test_calculate_metrics_reports_undefined_auc_without_both_classes():
    case = _case()
    metrics = calculate_metrics(
        [case],
        [
            EvalObservation(
                case_id=case.case_id,
                evidences=[{"document_id": "doc-a", "chunk_index": 0, "score": 0.9}],
                elapsed_ms=1,
            )
        ],
        k=1,
    )

    assert metrics["auc_roc"] is None


def test_source_locator_matching_accepts_record_type_segment():
    case = EvalCase(
        case_id="case-source",
        knowledge_type="enterprise",
        query="查询企业",
        expected_source_contains=["mysql:xp_enterprise_profile|id:1"],
    )

    assert _is_relevant(
        case,
        {"source_locator": "mysql:xp_enterprise_profile|type:record|id:1"},
    )


def test_retry_delay_reads_retry_after_seconds_from_error_body():
    response = httpx.Response(
        429,
        json={"error": {"details": {"retry_after_seconds": 26}}},
        request=httpx.Request("POST", "http://test/api/v1/knowledge/retrieve"),
    )

    assert _retry_delay_seconds(response, attempt=0) == 26.0


def test_llm_relevance_judgement_can_mark_multiple_chunks_relevant():
    judgements = _parse_relevance_judgement(
        '[{"relevant": true, "reason": "技术方向匹配"}, '
        '{"relevant": true, "reason": "应用场景匹配"}, '
        '{"relevant": false, "reason": "无关"}]',
        3,
    )

    assert [item["relevant"] for item in judgements] == [True, True, False]
    assert _is_relevant(
        _case(),
        {"document_id": "doc-b", "llm_relevant": True},
    ) is True


def test_calculate_metrics_counts_each_chunk_independently():
    case = EvalCase(
        case_id="case-record",
        knowledge_type="enterprise",
        query="查询企业能力",
        relevant_chunk_ids=["doc-a|chunk:0"],
        status="confirmed",
    )

    metrics = calculate_metrics(
        [case],
        [
            EvalObservation(
                case_id=case.case_id,
                evidences=[
                    {"document_id": "doc-a", "chunk_index": 0, "score": 0.9},
                    {"document_id": "doc-a", "chunk_index": 1, "score": 0.8},
                ],
                elapsed_ms=1,
            )
        ],
        k=2,
    )

    assert metrics["precision"] == 0.5
    assert metrics["recall"] == 1.0
    assert metrics["f1"] == 2 / 3


def test_expected_chunk_source_locator_matches_chunk():
    case = EvalCase(
        case_id="case-source-alias",
        knowledge_type="enterprise",
        query="查询企业能力",
        expected_chunk_source_contains=["mysql:xp_enterprise_profile|type:record|id:1|chunk:0"],
        status="confirmed",
    )

    metrics = calculate_metrics(
        [case],
        [
            EvalObservation(
                case_id=case.case_id,
                evidences=[
                    {
                        "mysql_id": "1",
                        "source_locator": "mysql:xp_enterprise_profile|type:record|id:1",
                        "chunk_index": 0,
                        "score": 0.9,
                    }
                ],
                elapsed_ms=1,
            )
        ],
        k=1,
    )

    assert metrics["recall"] == 1.0
