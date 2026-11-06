import asyncio
import json

import httpx
import pytest

from runtime.config import Settings
from services.llm.openai_client import OpenAIClient


@pytest.mark.parametrize("responses_api", [False, True])
@pytest.mark.parametrize("kind", ["valid", "empty", "truncated"])
def test_json_requests_and_unusable_output_are_explicit(monkeypatch, responses_api, kind):
    import services.llm.openai_client as module

    def handle(request):
        payload = json.loads(request.content)
        if responses_api:
            assert payload["text"]["format"] == {"type": "json_object"}
            assert "response_format" not in payload
            assert payload["max_output_tokens"] > 0
            return httpx.Response(200, json={
                "status": "incomplete" if kind == "truncated" else "completed",
                "incomplete_details": {"reason": "max_output_tokens"} if kind == "truncated" else None,
                "output_text": "   " if kind == "empty" else '{"fields":{"title":"成果"}}',
            })
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] > 0
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "length" if kind == "truncated" else "stop",
            "message": {"content": "   " if kind == "empty" else '{"fields":{"title":"成果"}}'},
        }]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    metrics = []
    monkeypatch.setattr(module, "_record_llm_metric", lambda **kwargs: metrics.append(kwargs))
    monkeypatch.setattr(module, "record_llm_exchange", lambda **kwargs: None)
    client = OpenAIClient(Settings(
        _env_file=None, llm_provider="SidusAI", llm_model="DeepSeek-V4.1-Flash-1017",
        llm_base_url="https://example.test/v1/responses" if responses_api else "https://example.test/v1",
        llm_api_key="test", llm_response_format="json_object", llm_max_retries=0,
    ))
    if kind == "valid":
        assert json.loads(asyncio.run(client.complete("Return JSON")))["fields"]["title"] == "成果"
        assert metrics[-1]["status"] == "SUCCESS"
    else:
        code = "LLM_OUTPUT_EMPTY" if kind == "empty" else "LLM_OUTPUT_TRUNCATED"
        with pytest.raises(ValueError, match=code):
            asyncio.run(client.complete("Return JSON"))
        assert metrics[-1]["status"] == "FAILED"
        assert metrics[-1]["error_code"] == code


def test_explicit_provider_rejection_retries_without_json_mode(monkeypatch):
    import services.llm.openai_client as module

    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(400, json={"error": "response_format is not supported"})
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": '{"title":"成果"}'},
        }]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    monkeypatch.setattr(module, "_record_llm_metric", lambda **kwargs: None)
    monkeypatch.setattr(module, "record_llm_exchange", lambda **kwargs: None)
    client = OpenAIClient(Settings(
        _env_file=None, llm_provider="SidusAI", llm_model="test", llm_api_key="test",
        llm_base_url="https://example.test/v1", llm_response_format="json_object", llm_max_retries=0,
    ))
    assert json.loads(asyncio.run(client.complete("Return JSON")))["title"] == "成果"
    assert len(calls) == 2
    assert "response_format" not in calls[1]
