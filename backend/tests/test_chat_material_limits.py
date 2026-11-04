import asyncio
from io import BytesIO

from fastapi import Request
from starlette.datastructures import UploadFile

from agents.perception_agent.schemas import UploadResponse
from assistant.application.materials import _material_limit_answer
from assistant.api.v1.chat.routes import build_chat_router


class _Supervisor:
    session_store = None


class _Perception:
    async def upload_document(self, **_kwargs):
        return UploadResponse(
            status="FAILED",
            document_id="",
            storage_key="",
            file_name="large.pdf",
            file_size=50 * 1024 * 1024 + 1,
            error="文件超过限制：单个文件不能超过50MB",
        )


class _Runtime:
    embedded_perception = _Perception()
    deerflow_runner = object()  # 文件限制触发后不应调用问答服务。

    def require_conversation(self):
        return _Supervisor()


def test_chat_material_limit_is_returned_as_assistant_answer():
    router = build_chat_router(
        _Runtime(),
        lambda user_id: (user_id or "user-1", "external", None),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/v1/chat/materials/parse"
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/materials/parse",
            "state": {"user_id": "user-1"},
            "headers": [],
        }
    )

    result = asyncio.run(
        endpoint(
            request=request,
            file=UploadFile(filename="large.pdf", file=BytesIO(b"x")),
            thread_id="thread-1",
            message="",
            intent_hint=None,
            knowledge_type="",
            attachment_mode="new",
            debug=False,
        )
    )

    assert result["status"] == "FAILED"
    assert "50MB" in result["answer"]


def test_chat_material_size_and_page_limits_use_the_same_assistant_answer():
    size_answer = _material_limit_answer("文件超过限制：单个文件不能超过50MB")
    page_answer = _material_limit_answer("文件超过限制：PDF/PPT最多支持50页，当前文件有51页")

    assert size_answer == page_answer
    assert (
        size_answer
        == "本次请求未完成：文件超出处理限制：单个文件不能超过50MB或50页，请压缩或拆分后重新上传。"
    )
