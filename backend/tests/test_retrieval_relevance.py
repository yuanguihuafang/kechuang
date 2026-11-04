import asyncio

from assistant.application import retrieval_relevance


class _LLM:
    def __init__(self, response):
        self.response = response
        self.prompt = None

    async def complete(self, prompt):
        self.prompt = prompt
        return self.response


class _Adapter:
    def __init__(self, response=None):
        self.response = response or {"status": "SUCCESS", "data": {"results": []}}
        self.calls = []

    async def search_tavily(self, arguments, *, context):
        self.calls.append((arguments, context))
        return self.response


def _mock_judge(monkeypatch, llm):
    monkeypatch.setattr(retrieval_relevance, "get_settings", lambda: object())
    monkeypatch.setattr(retrieval_relevance, "settings_for_role", lambda settings, _role: settings)
    monkeypatch.setattr(retrieval_relevance, "get_llm_client", lambda _settings: llm)


def test_keeps_only_llm_judged_relevant_evidence(monkeypatch):
    llm = _LLM('{"relevant_indexes":[1],"web_query":""}')
    _mock_judge(monkeypatch, llm)
    context = object()
    adapter = _Adapter()
    result = {
        "status": "SUCCESS",
        "data": {
            "evidences": [
                {"title": "名字相似但无关企业", "text": "普通制造", "score": 0.9},
                {"title": "目标企业", "fields": {"company_name": "目标企业", "industry": "精细化工"}, "score": 0.4},
            ]
        },
    }

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            result,
            user_goal="帮我了解目标企业，下午要拜访并挖掘应用需求",
            retrieval_query="目标企业",
            adapter=adapter,
            context=context,
        )
    )

    assert filtered["data"]["evidences"] == [result["data"]["evidences"][1]]
    assert filtered["evidences"] == [result["data"]["evidences"][1]]
    assert adapter.calls == []
    assert "下午要拜访并挖掘应用需求" in llm.prompt
    assert "精细化工" in llm.prompt


def test_relevance_filter_keeps_records_and_nested_evidence_in_sync(monkeypatch):
    llm = _LLM('{"relevant_indexes":[1]}')
    _mock_judge(monkeypatch, llm)
    selected_evidence = {
        "record_id": "record-2",
        "mysql_id": "2",
        "source_id": "source-2",
        "document_id": "doc-2",
        "text": "目标企业资料",
    }
    result = {
        "status": "SUCCESS",
        "data": {
            "evidences": [
                {"record_id": "record-1", "mysql_id": "1", "source_id": "source-1", "document_id": "doc-1", "text": "无关资料"},
                selected_evidence,
            ],
            "records": [
                {"record_id": "record-1", "mysql_id": "1", "source_id": "source-1", "document_id": "doc-1", "evidences": [{"record_id": "record-1", "text": "无关资料"}]},
                {"record_id": "record-2", "mysql_id": "2", "source_id": "source-2", "document_id": "doc-2", "evidences": [selected_evidence]},
            ],
        },
    }

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            result,
            user_goal="查目标企业",
            retrieval_query="目标企业",
            adapter=_Adapter(),
            context=object(),
            allow_web_fallback=False,
        )
    )

    assert filtered["data"]["evidences"] == [selected_evidence]
    assert [record["record_id"] for record in filtered["data"]["records"]] == ["record-2"]
    assert filtered["data"]["records"][0]["evidences"] == [selected_evidence]


def test_no_relevant_evidence_uses_safe_web_query_without_returning_kb_cards(monkeypatch):
    llm = _LLM('{"relevant_indexes":[]}')
    _mock_judge(monkeypatch, llm)
    context = object()
    adapter = _Adapter({"status": "SUCCESS", "data": {"results": [{"title": "公开资料"}]}})
    result = {
        "status": "SUCCESS",
        "evidences": [{"title": "无关记录", "text": "候选正文不得进入联网查询"}],
        "data": {"evidences": [{"title": "无关记录", "text": "候选正文不得进入联网查询"}]},
    }

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            result,
            user_goal="仅查河北利仕化学的公开信息，准备客户拜访",
            retrieval_query="河北利仕化学",
            adapter=adapter,
            context=context,
        )
    )

    assert filtered["evidences"] == filtered["data"]["evidences"] == []
    assert filtered["web_results"] == [{"title": "公开资料"}]
    assert adapter.calls == [
        ({"query": "河北利仕化学", "max_results": 5}, context)
    ]
    assert "候选正文不得进入联网查询" not in adapter.calls[0][0]["query"]


def test_judge_failure_fails_closed_without_web_search_or_unfiltered_cards(monkeypatch):
    llm = _LLM("not-json")
    _mock_judge(monkeypatch, llm)
    adapter = _Adapter()
    result = {"status": "SUCCESS", "data": {"evidences": [{"text": "未验证候选"}]}}

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            result,
            user_goal="查某企业公开信息",
            retrieval_query="某企业",
            adapter=adapter,
            context=object(),
        )
    )

    assert filtered["status"] == "RELEVANCE_CHECK_FAILED"
    assert filtered["data"]["evidences"] == []
    assert adapter.calls == []


def test_failed_retrieval_is_not_converted_to_web_fallback(monkeypatch):
    llm = _LLM('{"relevant_indexes":[]}')
    _mock_judge(monkeypatch, llm)
    adapter = _Adapter()
    result = {"status": "FAILED", "error": "Milvus unavailable", "data": {}}

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            result,
            user_goal="查 topic",
            retrieval_query="topic",
            adapter=adapter,
            context=object(),
        )
    )

    assert filtered["status"] == "FAILED"
    assert filtered["error"] == "Milvus unavailable"
    assert adapter.calls == []


def test_empty_retrieval_skips_judge_and_falls_back_to_web(monkeypatch):
    class _UnexpectedLLM:
        async def complete(self, _prompt):
            raise AssertionError("empty candidate sets should not call the judge model")

    _mock_judge(monkeypatch, _UnexpectedLLM())
    adapter = _Adapter({"status": "SUCCESS", "data": {"results": [{"title": "public"}]}})
    context = object()

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            {"status": "SUCCESS", "data": {"evidences": []}},
            user_goal="查公开企业信息",
            retrieval_query="目标公司公开资料",
            adapter=adapter,
            context=context,
            allow_web_fallback=True,
        )
    )

    assert filtered["status"] == "WEB_FALLBACK"
    assert adapter.calls[0][0]["query"] == "目标公司公开资料"


def test_explicit_internal_only_skips_web_fallback(monkeypatch):
    class _UnexpectedLLM:
        async def complete(self, _prompt):
            raise AssertionError("empty candidate sets should not call the judge model")

    _mock_judge(monkeypatch, _UnexpectedLLM())
    adapter = _Adapter()

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            {"status": "SUCCESS", "data": {"evidences": []}},
            user_goal="只查内部知识库",
            retrieval_query="内部主题",
            adapter=adapter,
            context=object(),
            allow_web_fallback=False,
        )
    )

    assert filtered["status"] == "NO_EVIDENCE"
    assert adapter.calls == []


def test_partial_retrieval_without_candidates_is_not_treated_as_zero_hits(monkeypatch):
    class _UnexpectedLLM:
        async def complete(self, _prompt):
            raise AssertionError("partial empty retrieval should not be judged as a complete miss")

    _mock_judge(monkeypatch, _UnexpectedLLM())
    adapter = _Adapter()
    result = {"status": "PARTIAL_SUCCESS", "data": {"evidences": []}}

    filtered = asyncio.run(
        retrieval_relevance.judge_and_fallback(
            result,
            user_goal="查目标公司的公开资料",
            retrieval_query="目标公司",
            adapter=adapter,
            context=object(),
        )
    )

    assert filtered["status"] == "PARTIAL_SUCCESS"
    assert filtered["answer"] == "知识库检索未完整，暂时无法确认是否存在相关资料。"
    assert adapter.calls == []
