"""五类知识检索评测 CLI 与纯指标计算。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Literal

import httpx

KnowledgeType = Literal["enterprise", "demand", "achievement", "expert", "solution"]
CaseStatus = Literal["candidate", "confirmed"]


@dataclass(frozen=True)
class EvalCase:
    """一条可审计检索问题；candidate 不进入正式指标。"""

    case_id: str
    knowledge_type: KnowledgeType
    query: str
    relevant_chunk_ids: list[str] = field(default_factory=list)
    expected_chunk_source_contains: list[str] = field(default_factory=list)
    relevant_record_ids: list[str] = field(default_factory=list)
    expected_document_ids: list[str] = field(default_factory=list)
    expected_source_contains: list[str] = field(default_factory=list)
    answerable: bool = True
    scope_id: str = "public"
    identity: dict[str, str] = field(
        default_factory=lambda: {
            "user_role": "internal",
            "user_id": "eval-user",
        }
    )
    forbidden_document_ids: list[str] = field(default_factory=list)
    status: CaseStatus = "candidate"

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.query.strip():
            raise ValueError("case_id 和 query 不能为空")
        if self.status == "confirmed" and self.answerable and not _expected_chunk_sources(self):
            raise ValueError(f"正式可回答案例必须标注相关 chunk：{self.case_id}")


@dataclass(frozen=True)
class EvalObservation:
    """一次实际检索调用的原始观测。"""

    case_id: str
    evidences: list[dict[str, Any]]
    elapsed_ms: float
    rerank_applied: bool = False
    retrieval_mode: str | None = None
    response_status: str | None = None
    error: str | None = None


def _parse_relevance_judgement(raw: str, expected_count: int) -> list[dict[str, Any]]:
    """解析 LLM 的逐结果相关性判断。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I)
    payload = json.loads(cleaned)
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("judgements") or []
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise ValueError("LLM 相关性判断数量不匹配")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict) or not isinstance(item.get("relevant"), bool):
            raise ValueError(f"LLM 相关性判断格式错误：第 {index} 条")
        result.append(
            {
                "relevant": item["relevant"],
                "reason": str(item.get("reason") or "").strip()[:500],
            }
        )
    return result


def _relevance_prompt(case: EvalCase, hits: list[dict[str, Any]]) -> str:
    candidates = []
    for index, hit in enumerate(hits, 1):
        text = str(hit.get("chunk_text") or hit.get("text") or hit.get("summary") or "").strip()[
            :4000
        ]
        candidates.append(f"候选{index}:\n{text}")
    return (
        "你是 RAG 检索评测标注员。请判断每个候选文本是否与用户问题相关。\n"
        "相关的标准：候选文本包含能直接帮助回答用户问题的事实、技术方向、业务能力、应用场景或解决方案；"
        "仅有泛化词、无关信息或无法支持回答的问题判为不相关。多个候选文本可以同时相关。\n"
        "只返回严格 JSON 数组，不要 Markdown，不要额外文字。数组长度必须与候选数量一致，格式："
        '[{"relevant":true,"reason":"一句话说明"}]。\n\n'
        f"用户问题：{case.query}\n\n" + "\n\n".join(candidates)
    )


def _load_judgement_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    cached: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("case_id"):
            cached[str(item["case_id"])] = item
    return cached


def _same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(
        right, ensure_ascii=False, sort_keys=True
    )


def _judgement_input_signature(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("record_id", "mysql_id", "document_id", "source_locator", "chunk_text", "text")
    return [{field: hit.get(field) for field in fields} for hit in hits]


async def judge_retrieval_relevance(
    cases: list[EvalCase],
    observations: list[EvalObservation],
    *,
    concurrency: int = 1,
    progress_callback: Callable[[int, int], None] | None = None,
    checkpoint_path: Path | None = None,
) -> list[EvalObservation]:
    """用 LLM 判断每个检索结果 chunk 是否相关；失败时保留可审计的规则兜底。"""
    from runtime.config import get_settings
    from services.llm.router import get_llm_client

    by_case = {case.case_id: case for case in cases}
    original_by_case = {observation.case_id: observation for observation in observations}
    cached = _load_judgement_checkpoint(checkpoint_path) if checkpoint_path else {}
    reusable = {
        case_id
        for case_id, item in cached.items()
        if item.get("completed") is True
        and case_id in original_by_case
        and _same_json(
            item.get("input_signature"),
            _judgement_input_signature(original_by_case[case_id].evidences),
        )
    }
    if checkpoint_path:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    client = None
    client_error: str | None = None
    try:
        if not all(
            (settings.llm_provider, settings.llm_model, settings.llm_base_url, settings.llm_api_key)
        ):
            raise RuntimeError("LLM 配置不完整")
        client = get_llm_client(settings)
    except Exception as exc:
        client_error = f"{type(exc).__name__}: {exc}"

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def judge_one(observation: EvalObservation) -> EvalObservation:
        case = by_case[observation.case_id]
        hits = [dict(hit) for hit in observation.evidences]
        if not hits:
            return observation
        if observation.case_id in reusable:
            item = cached[observation.case_id]
            return EvalObservation(
                case_id=observation.case_id,
                evidences=item["evidences"],
                elapsed_ms=observation.elapsed_ms,
                rerank_applied=observation.rerank_applied,
                retrieval_mode=observation.retrieval_mode,
                response_status=observation.response_status,
                error=observation.error,
            )
        async with semaphore:
            judgements: list[dict[str, Any]] | None = None
            judge_error = client_error
            if client is not None:
                try:
                    judgements = _parse_relevance_judgement(
                        await client.complete(_relevance_prompt(case, hits)), len(hits)
                    )
                except Exception as exc:
                    judge_error = f"{type(exc).__name__}: {exc}"
            for index, hit in enumerate(hits):
                if judgements is None:
                    hit["llm_relevant"] = _is_relevant(case, hit)
                    hit["relevance_method"] = "source_fallback"
                    hit["relevance_reason"] = judge_error or "LLM 未返回判断"
                else:
                    hit["llm_relevant"] = judgements[index]["relevant"]
                    hit["relevance_method"] = "llm"
                    hit["relevance_reason"] = judgements[index]["reason"]
            return EvalObservation(
                case_id=observation.case_id,
                evidences=hits,
                elapsed_ms=observation.elapsed_ms,
                rerank_applied=observation.rerank_applied,
                retrieval_mode=observation.retrieval_mode,
                response_status=observation.response_status,
                error=observation.error,
            )

    tasks = [asyncio.create_task(judge_one(observation)) for observation in observations]
    judged: list[EvalObservation] = []
    for completed, task in enumerate(asyncio.as_completed(tasks), 1):
        result = await task
        judged.append(result)
        if checkpoint_path and result.case_id not in reusable:
            original = original_by_case[result.case_id]
            with checkpoint_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "case_id": result.case_id,
                            "input_signature": _judgement_input_signature(original.evidences),
                            "evidences": result.evidences,
                            "completed": bool(result.evidences)
                            and all(
                                hit.get("relevance_method") == "llm" for hit in result.evidences
                            ),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        if progress_callback is not None:
            progress_callback(completed, len(tasks))
    order = {case.case_id: index for index, case in enumerate(cases)}
    return sorted(judged, key=lambda item: order[item.case_id])


def load_cases(
    paths: list[Path],
) -> list[EvalCase]:
    """读取并校验 JSONL 测试案例。"""

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for path in paths:
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
                case = EvalCase(**payload)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{line_number} 案例格式错误：{exc}") from exc
            if case.case_id in seen_ids:
                raise ValueError(f"重复 case_id：{case.case_id}")
            seen_ids.add(case.case_id)
            cases.append(case)
    return cases


def calculate_metrics(
    cases: list[EvalCase],
    observations: list[EvalObservation],
    *,
    k: int,
    use_llm_judgement: bool = False,
) -> dict[str, float | None]:
    """计算 chunk 级 Precision、Recall、F1 和 AUC-ROC。"""

    if k <= 0:
        raise ValueError("k 必须大于 0")
    by_case = _observations_by_case(cases, observations)
    counts = [
        _case_counts(case, by_case[case.case_id], k=k, use_llm_judgement=use_llm_judgement)
        for case in cases
    ]

    true_positive = sum(item["tp"] for item in counts)
    false_positive = sum(item["fp"] for item in counts)
    false_negative = sum(item["fn"] for item in counts)
    auc_labels = [label for item in counts for label in item["auc_labels"]]
    auc_scores = [score for item in counts for score in item["auc_scores"]]

    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "auc_roc": _auc_roc(auc_labels, auc_scores),
    }


def _observations_by_case(
    cases: list[EvalCase], observations: list[EvalObservation]
) -> dict[str, EvalObservation]:
    by_case = {observation.case_id: observation for observation in observations}
    missing = [case.case_id for case in cases if case.case_id not in by_case]
    if missing:
        raise ValueError(f"缺少评测观测：{', '.join(missing)}")
    return by_case


def _expected_chunk_sources(case: EvalCase) -> list[str]:
    for values in (case.relevant_chunk_ids, case.expected_chunk_source_contains):
        sources = list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
        if sources:
            return sources
    return []


def _hit_chunk_key(hit: dict[str, Any], index: int) -> str:
    chunk_id = str(hit.get("chunk_id") or "").strip()
    if chunk_id:
        return chunk_id
    document_id = str(hit.get("document_id") or "").strip()
    chunk_index = hit.get("chunk_index")
    if document_id and isinstance(chunk_index, int) and not isinstance(chunk_index, bool):
        return f"{document_id}|chunk:{chunk_index}"
    source_locator = str(hit.get("source_locator") or "").strip()
    if source_locator:
        if isinstance(chunk_index, int) and not isinstance(chunk_index, bool):
            return f"{source_locator}|chunk:{chunk_index}"
        return source_locator
    return f"__chunk_{index}"


def _gold_relevant(case: EvalCase, hit: dict[str, Any]) -> bool:
    expected_chunk_sources = _expected_chunk_sources(case)
    if expected_chunk_sources:
        chunk_key = _hit_chunk_key(hit, 0)
        source_locator = str(hit.get("source_locator") or "")
        return any(
            expected == chunk_key or expected in chunk_key or expected in source_locator
            for expected in expected_chunk_sources
        )
    record_id = str(hit.get("record_id") or hit.get("mysql_id") or "").strip()
    document_id = str(hit.get("document_id") or "").strip()
    expected_record_ids = set(str(item).strip() for item in case.relevant_record_ids)
    expected_document_ids = set(str(item).strip() for item in case.expected_document_ids)
    if record_id in expected_record_ids or document_id in expected_document_ids:
        return True
    source_locator = str(hit.get("source_locator") or "")
    normalized_locator = source_locator.replace("|type:record|", "|")
    return any(
        expected.replace("|type:record|", "|") in normalized_locator
        for expected in case.expected_source_contains
    )


def _judged_relevant(case: EvalCase, hit: dict[str, Any]) -> bool:
    if "llm_relevant" in hit:
        return bool(hit["llm_relevant"])
    return _gold_relevant(case, hit)


def _case_counts(
    case: EvalCase,
    observation: EvalObservation,
    *,
    k: int,
    use_llm_judgement: bool,
) -> dict[str, Any]:
    relevance_fn = _judged_relevant if use_llm_judgement else _gold_relevant
    hits = observation.evidences[:k]
    if not case.answerable:
        return {
            "tp": 0,
            "fp": len(hits),
            "fn": 0,
            "auc_labels": [0] * len(hits),
            "auc_scores": [_hit_score(item) for item in hits],
        }
    expected_count = max(1, len(_expected_chunk_sources(case)))
    relevant = [relevance_fn(case, hit) for hit in hits]
    tp = sum(relevant)
    fn = max(0, expected_count - tp)
    return {
        "tp": tp,
        "fp": len(hits) - tp,
        "fn": fn,
        # 未召回的 chunk 没有实际排序分数，不伪造 0 分参与 AUC；漏召回由 Recall/FN 体现。
        "auc_labels": [int(value) for value in relevant],
        "auc_scores": [_hit_score(item) for item in hits],
    }


async def run_cases(
    cases: list[EvalCase],
    *,
    base_url: str,
    top_k: int,
    concurrency: int = 4,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[EvalObservation]:
    """通过统一网关执行案例，保留原始观测供审计。"""

    semaphore = asyncio.Semaphore(max(1, concurrency))
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:

        async def execute(case: EvalCase) -> EvalObservation:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = None
                    for attempt in range(3):
                        response = await client.post(
                            f"{base_url.rstrip('/')}/api/v1/knowledge/retrieve",
                            json={
                                "query": case.query,
                                "scope_id": case.scope_id,
                                "knowledge_type": case.knowledge_type,
                                "top_k": top_k,
                                "use_rerank": True,
                                "expand_query": False,
                                "identity": case.identity,
                            },
                        )
                        if response.status_code != 429 or attempt == 2:
                            break
                        await asyncio.sleep(_retry_delay_seconds(response, attempt=attempt))
                    assert response is not None
                    if response.is_error:
                        detail = response.text[:500].replace("\n", " ")
                        raise httpx.HTTPStatusError(
                            f"{response.status_code}: {detail}",
                            request=response.request,
                            response=response,
                        )
                    body = response.json()
                    trace = body.get("trace") or {}
                    return EvalObservation(
                        case_id=case.case_id,
                        evidences=list(body.get("evidences") or []),
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                        rerank_applied=bool(trace.get("rerank_applied")),
                        retrieval_mode=str(trace.get("retrieval_mode") or "") or None,
                        response_status=body.get("status"),
                    )
                except Exception as exc:
                    return EvalObservation(
                        case_id=case.case_id,
                        evidences=[],
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                        error=f"{type(exc).__name__}: {exc}",
                    )

        tasks = [asyncio.create_task(execute(case)) for case in cases]
        completed = 0
        observations: list[EvalObservation] = []
        for task in asyncio.as_completed(tasks):
            observations.append(await task)
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, len(cases))
        order = {case.case_id: index for index, case in enumerate(cases)}
        ordered = sorted(observations, key=lambda item: order[item.case_id])
        mode_mismatches = [
            item.case_id for item in ordered if not item.error and item.retrieval_mode != "step"
        ]
        if mode_mismatches:
            raise RuntimeError(
                "评测要求 step 检索模式，但服务返回了其他模式或未返回模式："
                + ", ".join(mode_mismatches)
            )
        return ordered


def _is_relevant(case: EvalCase, hit: dict[str, Any]) -> bool:
    return _judged_relevant(case, hit)


def _retry_delay_seconds(response: httpx.Response, *, attempt: int) -> float:
    retry_after = response.headers.get("retry-after", "")
    try:
        delay = float(retry_after)
    except ValueError:
        try:
            delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            try:
                details = (response.json().get("error") or {}).get("details") or {}
                delay = float(details.get("retry_after_seconds"))
            except (TypeError, ValueError, AttributeError, KeyError):
                delay = float(attempt + 1)
    return min(60.0, max(0.5, delay))


def _hit_score(hit: dict[str, Any]) -> float:
    try:
        score = float(hit.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) else 0.0


def _auc_roc(labels: list[int], scores: list[float]) -> float | None:
    positives = [score for label, score in zip(labels, scores, strict=True) if label]
    negatives = [score for label, score in zip(labels, scores, strict=True) if not label]
    if not positives or not negatives:
        return None
    # ponytail: 两两比较计算 AUC 的复杂度为 O(n²)；当前 top-k 评估集规模较小。
    wins = sum(
        1.0 if positive > negative else 0.5 if positive == negative else 0.0
        for positive in positives
        for negative in negatives
    )
    return wins / (len(positives) * len(negatives))
