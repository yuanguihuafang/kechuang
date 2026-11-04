import asyncio
import json

import httpx
import pytest

from core.contracts.chat import ChatRequest
from runtime.config import Settings
from services.application.chat_response import compose_business_response
from services.web_search.tavily import TavilySearchClient


def test_tavily_search_normalizes_and_deduplicates_results():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "query": "量子电池 最新进展",
                "results": [
                    {
                        "title": "A",
                        "url": "https://example.com/a",
                        "content": " first ",
                        "score": 0.9,
                    },
                    {
                        "title": "A duplicate",
                        "url": "https://example.com/a",
                        "content": "duplicate",
                        "score": 0.8,
                    },
                    {
                        "title": "B",
                        "url": "https://example.com/b",
                        "content": "second",
                        "score": 0.7,
                    },
                ],
            },
        )

    client = TavilySearchClient(
        Settings(tavily_api_key="tvly-test"),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.search("量子电池 最新进展", max_results=5))

    assert result["status"] == "SUCCESS"
    assert [item["url"] for item in result["results"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert seen["authorization"] == "Bearer tvly-test"
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["payload"] == {
        "query": "量子电池 最新进展",
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }


def test_tavily_search_without_key_is_explicitly_unavailable():
    client = TavilySearchClient(Settings(tavily_api_key=None))
    result = asyncio.run(client.search("测试"))

    assert result == {
        "status": "FAILED",
        "error_code": "WEB_SEARCH_NOT_CONFIGURED",
        "error": "联网搜索服务未配置。",
        "query": "测试",
        "results": [],
    }


def test_tavily_extract_posts_one_url_and_bounds_content():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://example.com/article", "raw_content": "x" * 12000}],
                "failed_results": [],
            },
        )

    client = TavilySearchClient(
        Settings(tavily_api_key="tvly-test"), transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(client.extract("https://example.com/article"))

    assert seen == {
        "url": "https://api.tavily.com/extract",
        "authorization": "Bearer tvly-test",
        "payload": {"urls": "https://example.com/article", "extract_depth": "basic"},
    }
    assert result["status"] == "SUCCESS"
    assert result["url"] == "https://example.com/article"
    assert len(result["content"]) == 8000


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/x",
        "https://sub.localhost/x",
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/x",
        "https://user:pass@example.com/x",
        "http://127.0.0.1./x",
        "https://localhost./x",
        "https://example.com/" + "x" * 2048,
    ],
)
def test_tavily_extract_rejects_unsafe_url_without_request(url):
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid URL reached Tavily")

    client = TavilySearchClient(
        Settings(tavily_api_key="tvly-test"), transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(client.extract(url))
    assert result["status"] == "FAILED"
    assert result["error_code"] == "WEB_FETCH_INVALID_URL"


def test_tavily_extract_reports_unconfigured_and_upstream_failures():
    client = TavilySearchClient(Settings(tavily_api_key=None))
    assert (
        asyncio.run(client.extract("https://example.com"))["error_code"]
        == "WEB_FETCH_NOT_CONFIGURED"
    )

    def failed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
                "failed_results": [
                    {"url": "https://example.com", "error": "private upstream reason"}
                ],
            },
        )

    client = TavilySearchClient(
        Settings(tavily_api_key="tvly-test"), transport=httpx.MockTransport(failed)
    )
    result = asyncio.run(client.extract("https://example.com"))
    assert result["status"] == "FAILED"
    assert result["error_code"] == "WEB_FETCH_UPSTREAM_ERROR"
    assert "private upstream reason" not in str(result)

    client = TavilySearchClient(
        Settings(tavily_api_key="tvly-test"),
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, text="upstream secret")),
    )
    result = asyncio.run(client.extract("https://example.com"))
    assert result["error_code"] == "WEB_FETCH_UPSTREAM_ERROR"
    assert "upstream secret" not in str(result)


def test_web_results_become_grounded_evidence_and_a_dedicated_ui_block():
    web_results = [{"title": "网页", "url": "https://example.com", "content": "内容", "score": 0.8}]
    request = ChatRequest(message="测试", user_id="u", user_role="internal")
    blocks, _ = compose_business_response(
        request,
        answer="根据网页内容回答。",
        status="SUCCESS",
        evidences=[],
        images=[],
        documents=[],
        web_results=web_results,
    )

    assert any(block["type"] == "web_search_results" for block in blocks)
    assert not any(block["type"] == "evidence_list" for block in blocks)


def test_plain_knowledge_results_do_not_expose_match_batch_action():
    request = ChatRequest(message="查找工业机器人", user_id="u", user_role="internal")
    _, actions = compose_business_response(
        request,
        answer="根据知识库回答。",
        status="SUCCESS",
        evidences=[{"record_id": "e1", "text": "内容", "score": 0.9}],
        images=[],
        documents=[],
        match_results=[{"record_id": "e1", "title": "结果", "score": 0.9}],
    )

    assert not any(action["id"] == "next_match_batch" for action in actions)


def test_enterprise_profile_results_are_recommendation_cards_without_match_actions():
    request = ChatRequest(
        message="查找青岛地铁相关企业",
        user_id="u",
        user_role="internal",
        intent_hint="enterprise_profile",
    )
    blocks, actions = compose_business_response(
        request,
        answer="找到相关企业。",
        status="SUCCESS",
        evidences=[
            {
                "record_id": "e1",
                "title": "青岛地铁相关企业",
                "text": "提供地铁工程服务。",
                "score": 0.9,
                "knowledge_type": "enterprise",
            }
        ],
        images=[],
        documents=[],
    )

    assert any(block["type"] == "recommendation_list" for block in blocks)
    assert not any(action["id"] == "next_match_batch" for action in actions)
