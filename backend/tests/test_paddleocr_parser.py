import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from runtime.config import Settings
from services.document_parsing.file_ingestion import ParsedDocument
from services.document_parsing.paddleocr_parser import (
    PaddleOCRDocumentParser,
    parse_paddleocr_jsonl,
)
from services.document_parsing.text_parser import HybridDocumentParser


def _mock_paddle_http(monkeypatch, handler):
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "services.document_parsing.paddleocr_parser.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )


def test_parse_paddleocr_jsonl_keeps_page_order_and_text():
    content = "\n".join(
        [
            json.dumps(
                {
                    "result": {
                        "ocrResults": [
                            {"prunedResult": {"rec_texts": ["第一页标题", "第一页正文"]}},
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "result": {
                        "ocrResults": [
                            {"prunedResult": {"rec_texts": ["第二页内容"]}},
                        ]
                    }
                },
                ensure_ascii=False,
            ),
        ]
    )

    assert parse_paddleocr_jsonl(content) == {
        1: "第一页标题\n第一页正文",
        2: "第二页内容",
    }


def test_parse_paddleocr_jsonl_splits_multiple_ocr_pages_in_one_record():
    content = json.dumps(
        {
            "result": {
                "ocrResults": [
                    {"prunedResult": {"rec_texts": ["第一页"]}},
                    {"prunedResult": {"rec_texts": ["第二页"]}},
                ]
            }
        },
        ensure_ascii=False,
    )

    assert parse_paddleocr_jsonl(content) == {1: "第一页", 2: "第二页"}


def test_parse_paddleocr_document_layout_results_as_slide_pages():
    content = json.dumps(
        {
            "result": {
                "layoutParsingResults": [
                    {"markdown": {"text": "第一页标题\n第一页内容"}},
                    {"markdown": {"text": "第二页内容"}},
                ]
            }
        },
        ensure_ascii=False,
    )

    assert parse_paddleocr_jsonl(content) == {
        1: "第一页标题\n第一页内容",
        2: "第二页内容",
    }


class _Parser:
    supported_extensions = {"ppt", "pptx", "png"}

    def __init__(self):
        self.calls = []

    async def parse(self, path: Path, **_kwargs):
        self.calls.append(path.suffix.lower())
        return path.suffix.lower()


def test_hybrid_parser_routes_presentations_to_local_mineru_and_image_to_paddle():
    paddle = _Parser()
    local_mineru = _Parser()
    parser = HybridDocumentParser(paddle_parser=paddle, local_mineru_parser=local_mineru)

    assert asyncio.run(parser.parse(Path("sample.ppt"))) == ".ppt"
    assert asyncio.run(parser.parse(Path("sample.pptx"))) == ".pptx"
    assert asyncio.run(parser.parse(Path("sample.png"))) == ".png"
    assert local_mineru.calls == [".ppt", ".pptx"]
    assert paddle.calls == [".png"]


def test_paddleocr_settings_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("PADDLEOCR_API_TOKEN", "test-token")
    monkeypatch.setenv("PADDLEOCR_MODEL", "PP-OCRv6")

    settings = Settings(_env_file=None)

    assert settings.paddleocr_api_token == "test-token"
    assert settings.paddleocr_model == "PP-OCRv6"
    assert settings.paddleocr_job_url == "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"


def test_paddleocr_parser_submits_polls_and_parses_api_result(monkeypatch, tmp_path):
    jsonl = json.dumps(
        {
            "result": {
                "ocrResults": [
                    {"prunedResult": {"rec_texts": ["图片识别文本"]}},
                ]
            }
        },
        ensure_ascii=False,
    )
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        if request.method == "POST":
            assert request.headers["Authorization"] == "Bearer test-token"
            body = request.read()
            assert b'filename="sample.png"' in body
            assert b"PP-OCRv6" in body and b"fake-image" in body
            assert b'"useTextlineOrientation": false' in body
            return httpx.Response(200, json={"code": 0, "data": {"jobId": "job-1"}})
        if str(request.url).endswith("/job-1"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "state": "done",
                        "extractProgress": {"totalPages": 1},
                        "resultUrl": {"jsonUrl": "https://result.example/result.jsonl"},
                    },
                },
            )
        return httpx.Response(200, text=jsonl)

    _mock_paddle_http(monkeypatch, handler)
    path = tmp_path / "sample.png"
    path.write_bytes(b"fake-image")
    parser = PaddleOCRDocumentParser(
        job_url="https://paddleocr.example/api/v2/ocr/jobs",
        token="test-token",
        model="PP-OCRv6",
        timeout_seconds=60,
        poll_interval_seconds=0.01,
    )

    result = asyncio.run(parser.parse(path))

    assert result.text == "图片识别文本"
    assert result.page_texts == {1: "图片识别文本"}
    assert result.metadata["parser"] == "paddleocr"
    assert calls == [
        ("POST", "https://paddleocr.example/api/v2/ocr/jobs"),
        ("GET", "https://paddleocr.example/api/v2/ocr/jobs/job-1"),
        ("GET", "https://result.example/result.jsonl"),
    ]


def test_paddleocr_parser_reports_api_error_code_and_message(monkeypatch, tmp_path):
    _mock_paddle_http(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"code": 10007, "msg": "模型名称无效"}),
    )
    path = tmp_path / "sample.png"
    path.write_bytes(b"fake-image")
    parser = PaddleOCRDocumentParser(
        job_url="https://paddleocr.example/api/v2/ocr/jobs",
        token="test-token",
        model="PP-OCRv6",
        timeout_seconds=60,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="10007.*模型名称无效"):
        asyncio.run(parser.parse(path))


def test_perception_default_parser_uses_local_mineru_for_presentations(monkeypatch):
    from agents.perception_agent.application.perception_service import PerceptionAgent
    from services.document_parsing.ark_visual_parser import ArkVisualDocumentParser
    from services.document_parsing.mineru_parser import MinerUDocumentParser
    from services.document_parsing.mineru_runner import LocalMinerURunner

    settings = SimpleNamespace(
        mineru_url=None,
        mineru_command="mineru",
        document_parser="mineru",
        paddleocr_job_url="https://paddleocr.example/api/v2/ocr/jobs",
        paddleocr_api_token="test-token",
        paddleocr_model="PP-OCRv6",
        paddleocr_timeout_seconds=60,
        paddleocr_poll_interval_seconds=0.01,
    )
    calls = []

    async def fake_parser_parse(self, path, **_kwargs):
        calls.append((type(self).__name__, Path(path).suffix.lower()))
        return ParsedDocument(
            source_name=Path(path).name,
            text="parsed",
            raw_text="parsed",
            metadata={
                "parser": "paddleocr" if isinstance(self, PaddleOCRDocumentParser) else "fallback"
            },
        )

    monkeypatch.setattr("runtime.config.get_settings", lambda: settings)
    monkeypatch.setattr(PaddleOCRDocumentParser, "parse", fake_parser_parse)
    monkeypatch.setattr(ArkVisualDocumentParser, "parse", fake_parser_parse)
    monkeypatch.setattr(MinerUDocumentParser, "parse", fake_parser_parse)
    agent = PerceptionAgent()
    assert isinstance(agent.parser._local_mineru_parser.runner, LocalMinerURunner)

    results = [
        asyncio.run(agent.parser.parse(Path("sample.ppt"))),
        asyncio.run(agent.parser.parse(Path("sample.pptx"))),
        asyncio.run(agent.parser.parse(Path("sample.png"))),
    ]

    assert [result.metadata["parser"] for result in results] == ["fallback", "fallback", "paddleocr"]
    assert calls == [
        ("MinerUDocumentParser", ".ppt"),
        ("MinerUDocumentParser", ".pptx"),
        ("PaddleOCRDocumentParser", ".png"),
    ]
