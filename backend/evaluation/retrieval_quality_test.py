"""Run retrieval quality tests for the four public knowledge libraries.

This test only calls ``/api/v1/knowledge/retrieve``. It never uploads files,
parses documents, extracts fields, or creates drafts.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from evaluation.rag_eval import (
    EvalCase,
    EvalObservation,
    _auc_roc,
    _case_counts,
    _gold_relevant,
    _hit_chunk_key,
    _hit_score,
    _is_relevant,
    calculate_metrics,
    judge_retrieval_relevance,
    load_cases,
    run_cases,
)

CASES_PER_LIBRARY = 100
KNOWLEDGE_TYPES = ("enterprise", "demand", "achievement", "expert")
DEFAULT_CASES_DIR = Path(__file__).parent / "retrieval_questions"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "output"
ANNOTATION_REVIEW_FIELDS = [
    "query",
    "score",
    "llm_relevant",
    "llm_reason",
    "review_relevant",
    "review_comment",
    "chunk_text",
    "case_id",
    "knowledge_type",
    "rank",
    "chunk_key",
    "record_id",
    "document_id",
    "chunk_index",
    "source_locator",
    "error",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="四类知识库检索质量评测")
    parser.add_argument(
        "knowledge_type",
        nargs="?",
        choices=[*KNOWLEDGE_TYPES, "all"],
        default="all",
        help="单独评测一个知识库，默认依次评测四个知识库",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8793")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="允许 candidate 题集进入 LLM chunk 标注流程，不计算正式指标",
    )
    parser.add_argument(
        "--annotation-top-k",
        type=int,
        default=20,
        help="标注候选池大小；默认 step + rerank Top-20",
    )
    parser.add_argument(
        "--review-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "annotation",
        help="人工复核目录，默认 evaluation/output/annotation（兼容 CSV）",
    )
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--llm-judge",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="使用 LLM 逐条判断 chunk 相关性；默认关闭，使用固定 chunk 标注",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _parse_review_relevant(value: Any, *, row_number: int) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "是"}:
        return True
    if normalized in {"0", "false", "no", "n", "否"}:
        return False
    raise ValueError(f"人工复核文件第 {row_number} 行 review_relevant 必须是 0/1 或 true/false")


def _apply_review_annotations(
    cases: list[EvalCase],
    review_path: Path,
) -> list[EvalCase]:
    """把人工复核表转换为正式 chunk 级 gold，不改写原始题集。"""

    if not review_path.exists():
        raise ValueError(f"找不到人工复核文件：{review_path}")
    by_case = {case.case_id: case for case in cases}
    selected: dict[str, list[str]] = {case.case_id: [] for case in cases}
    rows = _read_review_rows(review_path)
    fieldnames = set(rows[0]) if rows else set()
    required = {"case_id", "chunk_key", "review_relevant"}
    missing = required - fieldnames
    if missing:
        raise ValueError(f"人工复核文件缺少字段：{', '.join(sorted(missing))}")
    for row_number, row in enumerate(rows, 2):
        case_id = str(row.get("case_id") or "").strip()
        chunk_key = str(row.get("chunk_key") or "").strip()
        if case_id not in by_case:
            raise ValueError(f"人工复核文件第 {row_number} 行包含未知 case_id：{case_id}")
        if _parse_review_relevant(row.get("review_relevant"), row_number=row_number):
            if not chunk_key:
                raise ValueError(f"人工复核文件第 {row_number} 行缺少 chunk_key")
            if chunk_key not in selected[case_id]:
                selected[case_id].append(chunk_key)

    reviewed: list[EvalCase] = []
    for case in cases:
        chunk_keys = selected[case.case_id]
        if case.answerable and not chunk_keys:
            raise ValueError(f"可回答案例没有人工确认的相关 chunk：{case.case_id}")
        reviewed.append(
            replace(
                case,
                relevant_chunk_ids=[],
                expected_chunk_source_contains=chunk_keys,
                status="confirmed",
            )
        )
    return reviewed


def _read_review_rows(review_path: Path) -> list[dict[str, Any]]:
    if review_path.suffix.lower() == ".xlsx":
        workbook = load_workbook(review_path, read_only=True, data_only=True)
        try:
            rows = workbook.active.iter_rows(values_only=True)
            fieldnames = [str(value or "") for value in next(rows, ())]
            return [
                {
                    fieldname: value
                    for fieldname, value in zip(fieldnames, row, strict=False)
                    if fieldname
                }
                for row in rows
            ]
        finally:
            workbook.close()

    with review_path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _resolve_review_path(review_dir: Path, knowledge_type: str) -> Path:
    candidates = (
        review_dir / f"{knowledge_type}.xlsx",
        review_dir / knowledge_type / "review.xlsx",
        review_dir / f"{knowledge_type}.csv",
        review_dir / knowledge_type / "review.csv",
    )
    return next((path for path in candidates if path.exists()), candidates[0])


def _write_annotation_review(
    review_path: Path,
    cases: list[EvalCase],
    observations: list[EvalObservation],
) -> None:
    """输出可编辑的 LLM chunk 判断表，review_relevant 默认沿用 LLM 判断。"""

    review_path.parent.mkdir(parents=True, exist_ok=True)
    by_case = {case.case_id: case for case in cases}
    rows: list[dict[str, Any]] = []
    for observation in observations:
        case = by_case[observation.case_id]
        hits = observation.evidences
        if not hits:
            rows.append(
                {
                    "case_id": case.case_id,
                    "knowledge_type": case.knowledge_type,
                    "query": case.query,
                    "error": observation.error or "无检索结果",
                }
            )
            continue
        for rank, hit in enumerate(hits, 1):
            llm_relevant = hit.get("llm_relevant")
            rows.append(
                {
                    "case_id": case.case_id,
                    "knowledge_type": case.knowledge_type,
                    "query": case.query,
                    "rank": rank,
                    "chunk_key": _hit_chunk_key(hit, rank - 1),
                    "record_id": hit.get("record_id", ""),
                    "document_id": hit.get("document_id", ""),
                    "chunk_index": hit.get("chunk_index", ""),
                    "source_locator": hit.get("source_locator", ""),
                    "score": _hit_score(hit),
                    "llm_relevant": "" if llm_relevant is None else int(bool(llm_relevant)),
                    "llm_reason": hit.get("relevance_reason", ""),
                    "review_relevant": "" if llm_relevant is None else int(bool(llm_relevant)),
                    "review_comment": "",
                    "chunk_text": hit.get("chunk_text") or hit.get("text") or "",
                    "error": observation.error or "",
                }
            )

    if review_path.suffix.lower() == ".xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "chunk标注"
        sheet.append(ANNOTATION_REVIEW_FIELDS)
        for row in rows:
            sheet.append([row.get(field, "") for field in ANNOTATION_REVIEW_FIELDS])
        header_fill = PatternFill("solid", fgColor="D9EAF7")
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=cell.column in {1, 4, 6, 7, 15, 16},
                )
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        widths = {
            1: 36,
            2: 12,
            3: 14,
            4: 28,
            5: 16,
            6: 28,
            7: 80,
            8: 18,
            9: 14,
            10: 8,
            11: 70,
            12: 42,
            13: 24,
            14: 12,
            15: 70,
            16: 24,
        }
        for column_index, width in widths.items():
            sheet.column_dimensions[chr(64 + column_index)].width = width
        workbook.save(review_path)
        return

    with review_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ANNOTATION_REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _write_results(
    output_dir: Path,
    cases: list[EvalCase],
    observations: list[EvalObservation],
    metrics: dict[str, float | None],
    *,
    top_k: int,
    use_llm_judgement: bool,
) -> None:
    """按 tech_eval 的方式落盘检索结果和逐问题指标。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    by_case = {case.case_id: case for case in cases}
    with (output_dir / "retrieval_results.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "case_id",
                "knowledge_type",
                "query",
                "rank",
                "record_id",
                "document_id",
                "chunk_index",
                "source_locator",
                "score",
                "relevant",
                "gold_relevant",
                "chunk_relevant",
                "relevance_method",
                "relevance_reason",
                "chunk_text",
                "rerank_applied",
                "retrieval_mode",
                "error",
            ],
        )
        writer.writeheader()
        for observation in observations:
            case = by_case[observation.case_id]
            hits = observation.evidences[:top_k]
            if not hits:
                writer.writerow(
                    {
                        "case_id": case.case_id,
                        "knowledge_type": case.knowledge_type,
                        "query": case.query,
                        "error": observation.error or "",
                    }
                )
                continue
            for rank, hit in enumerate(hits, start=1):
                writer.writerow(
                    {
                        "case_id": case.case_id,
                        "knowledge_type": case.knowledge_type,
                        "query": case.query,
                        "rank": rank,
                        "record_id": hit.get("record_id", ""),
                        "document_id": hit.get("document_id", ""),
                        "chunk_index": hit.get("chunk_index", ""),
                        "source_locator": hit.get("source_locator", ""),
                        "score": _hit_score(hit),
                        "relevant": int(_gold_relevant(case, hit)),
                        "gold_relevant": int(_gold_relevant(case, hit)),
                        "chunk_relevant": int(_is_relevant(case, hit)),
                        "relevance_method": hit.get("relevance_method", "source_fallback"),
                        "relevance_reason": hit.get("relevance_reason", ""),
                        "chunk_text": hit.get("chunk_text") or hit.get("text") or "",
                        "rerank_applied": observation.rerank_applied,
                        "retrieval_mode": observation.retrieval_mode or "",
                        "error": observation.error or "",
                    }
                )

    with (output_dir / "retrieval_results.jsonl").open("w", encoding="utf-8") as stream:
        for observation in observations:
            case = by_case[observation.case_id]
            stream.write(
                json.dumps(
                    {
                        "case_id": case.case_id,
                        "knowledge_type": case.knowledge_type,
                        "query": case.query,
                        "relevant_chunk_ids": case.relevant_chunk_ids,
                        "relevant_record_ids": case.relevant_record_ids,
                        "expected_chunk_source_contains": case.expected_chunk_source_contains,
                        "expected_document_ids": case.expected_document_ids,
                        "expected_source_contains": case.expected_source_contains,
                        "answerable": case.answerable,
                        "status": case.status,
                        "results": observation.evidences,
                        "error": observation.error,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    with (output_dir / "retrieval_per_query_metrics.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "case_id",
                "knowledge_type",
                "tp",
                "fp",
                "fn",
                "rerank_applied",
                "retrieval_mode",
                "precision",
                "recall",
                "f1",
                "auc_roc",
            ],
        )
        writer.writeheader()
        for case in cases:
            observation = next(item for item in observations if item.case_id == case.case_id)
            counts = _case_counts(
                case,
                observation,
                k=top_k,
                use_llm_judgement=use_llm_judgement,
            )
            tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
            writer.writerow(
                {
                    "case_id": case.case_id,
                    "knowledge_type": case.knowledge_type,
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "rerank_applied": observation.rerank_applied,
                    "retrieval_mode": observation.retrieval_mode or "",
                    "precision": precision,
                    "recall": recall,
                    "f1": f1,
                    "auc_roc": _auc_roc(counts["auc_labels"], counts["auc_scores"]),
                }
            )

    (output_dir / "retrieval_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def run_test(
    args: argparse.Namespace,
    knowledge_type: str,
) -> dict[str, Any]:
    case_path = DEFAULT_CASES_DIR / f"{knowledge_type}.jsonl"
    if not case_path.exists():
        raise ValueError(f"找不到测试集：{case_path}")
    all_cases = load_cases([case_path])
    if args.annotate:
        cases = all_cases
        if args.annotation_top_k <= 0:
            raise ValueError("--annotation-top-k 必须大于 0")
        if len(cases) != CASES_PER_LIBRARY:
            raise ValueError(
                f"{knowledge_type} 标注题集必须是 {CASES_PER_LIBRARY} 条，当前为 {len(cases)} 条"
            )
        if any(case.knowledge_type == "solution" for case in cases):
            raise ValueError("当前评测暂不包含方案库")

        def show_progress(completed: int, total: int) -> None:
            print(f"\r[{knowledge_type}] 标注检索进度：{completed}/{total}", end="", flush=True)

        mode_name = f"step_rerank_on_top{args.annotation_top_k}_annotation"
        print(f"[{knowledge_type}] 开始 LLM 标注：{mode_name}")
        observations = await run_cases(
            cases,
            base_url=args.base_url,
            top_k=args.annotation_top_k,
            concurrency=args.concurrency,
            progress_callback=show_progress,
        )
        print()

        def show_judge_progress(completed: int, total: int) -> None:
            print(
                f"\r[{knowledge_type}/{mode_name}] LLM 相关性判断：{completed}/{total}",
                end="",
                flush=True,
            )

        observations = await judge_retrieval_relevance(
            cases,
            observations,
            concurrency=args.concurrency,
            progress_callback=show_judge_progress,
            checkpoint_path=(
                args.output_dir / "annotation" / knowledge_type / "llm_judgements.jsonl"
            ),
        )
        print()
        fallback_hits = [
            (observation.case_id, index)
            for observation in observations
            for index, hit in enumerate(observation.evidences, 1)
            if hit.get("relevance_method") != "llm"
        ]
        if fallback_hits:
            raise RuntimeError(
                f"LLM 标注未完成，不能用规则兜底生成标注；失败结果数：{len(fallback_hits)}"
            )
        review_path = args.output_dir / "annotation" / knowledge_type / "review.xlsx"
        _write_annotation_review(review_path, cases, observations)
        report = {
            "knowledge_type": knowledge_type,
            "mode": mode_name,
            "top_k": args.annotation_top_k,
            "review_file": str(review_path),
            "message": "请人工复核 review.xlsx 的 review_relevant 列，再用 --review-dir 做正式评测",
        }
        report_path = review_path.with_name("annotation_report.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    if args.review_dir:
        cases = _apply_review_annotations(
            all_cases,
            _resolve_review_path(args.review_dir, knowledge_type),
        )
    else:
        cases = [case for case in all_cases if case.status == "confirmed"]
    if len(cases) != CASES_PER_LIBRARY:
        raise ValueError(
            f"{knowledge_type} 正式评测必须是 {CASES_PER_LIBRARY} 条 confirmed chunk 标注，"
            f"当前 confirmed 为 {len(cases)} 条；candidate 不进入正式指标"
        )
    if any(case.knowledge_type == "solution" for case in cases):
        raise ValueError("当前评测暂不包含方案库")

    def show_progress(completed: int, total: int) -> None:
        print(f"\r[{knowledge_type}] 测试进度：{completed}/{total}", end="", flush=True)

    mode_name = "step_rerank_on"
    print(f"[{knowledge_type}] 开始测试：{mode_name}")
    observations = await run_cases(
        cases,
        base_url=args.base_url,
        top_k=args.top_k,
        concurrency=args.concurrency,
        progress_callback=show_progress,
    )
    print()
    if args.llm_judge:

        def show_judge_progress(completed: int, total: int) -> None:
            print(
                f"\r[{knowledge_type}/{mode_name}] LLM相关性判断：{completed}/{total}",
                end="",
                flush=True,
            )

        observations = await judge_retrieval_relevance(
            cases,
            observations,
            concurrency=args.concurrency,
            progress_callback=show_judge_progress,
            checkpoint_path=(args.output_dir / knowledge_type / mode_name / "llm_judgements.jsonl"),
        )
        print()
    metrics = calculate_metrics(
        cases,
        observations,
        k=args.top_k,
        use_llm_judgement=args.llm_judge,
    )
    _write_results(
        args.output_dir / knowledge_type / mode_name,
        cases,
        observations,
        metrics,
        top_k=args.top_k,
        use_llm_judgement=args.llm_judge,
    )
    report = {"knowledge_type": knowledge_type, mode_name: metrics}
    (args.output_dir / knowledge_type / "report.json").parent.mkdir(parents=True, exist_ok=True)
    (args.output_dir / knowledge_type / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.output:
        output_path = (
            args.output / knowledge_type / "report.json"
            if not args.output.suffix
            else args.output.with_name(f"{args.output.stem}_{knowledge_type}{args.output.suffix}")
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    args = _parse_args()
    knowledge_types = KNOWLEDGE_TYPES if args.knowledge_type == "all" else (args.knowledge_type,)
    summaries = {
        knowledge_type: asyncio.run(run_test(args, knowledge_type))
        for knowledge_type in knowledge_types
    }
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
