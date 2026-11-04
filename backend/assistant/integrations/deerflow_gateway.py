"""Optional HTTP runner for the OPIEos/DeerFlow gateway."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from secrets import token_urlsafe
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

_ASSISTANT_ID = "kechuang"
logger = logging.getLogger(__name__)


class DeerFlowGatewayRunner:
    """Call DeerFlow's native LangGraph-compatible streaming endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        internal_auth_token: str | None = None,
        agent_owner_user_id: str | None = None,
        internal_token_header: str | None = None,
        owner_user_id_header: str | None = None,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.internal_auth_token = internal_auth_token
        self.agent_owner_user_id = (agent_owner_user_id or "").strip()
        self.internal_token_header = (internal_token_header or "").strip()
        self.owner_user_id_header = (owner_user_id_header or "").strip()
        if self.internal_auth_token and not all(
            (self.agent_owner_user_id, self.internal_token_header, self.owner_user_id_header)
        ):
            raise ValueError(
                "DEERFLOW_AGENT_OWNER_USER_ID, DEERFLOW_INTERNAL_TOKEN_HEADER, and "
                "DEERFLOW_OWNER_USER_ID_HEADER must be configured for internal DeerFlow calls"
            )
        self.timeout = timeout
        self.transport = transport

    async def stream(self, request: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
        thread_id = str(request.get("thread_id") or "").strip()
        if not thread_id:
            raise ValueError("thread_id 不能为空")
        message = str(request.get("message") or "").strip()
        if not message:
            raise ValueError("message 不能为空")

        prior_messages = request.get("prior_messages", [])
        if not isinstance(prior_messages, list):
            raise ValueError("prior_messages 必须是消息列表")
        normalized_prior_messages = []
        for prior_message in prior_messages:
            if (
                not isinstance(prior_message, Mapping)
                or prior_message.get("role") not in {"user", "assistant"}
                or not isinstance(prior_message.get("content"), str)
                or not prior_message["content"].strip()
            ):
                raise ValueError("prior_messages 包含无效消息")
            normalized_prior_messages.append(
                {"role": prior_message["role"], "content": prior_message["content"]}
            )

        body = {
            "input": {
                "messages": [
                    *normalized_prior_messages,
                    {"role": "user", "content": message},
                ]
            },
            "assistant_id": _ASSISTANT_ID,
            "metadata": dict(request.get("metadata") or {}),
            "context": dict(request.get("context") or {}),
            "stream_mode": ["messages-tuple", "values", "custom"],
            "if_not_exists": "create",
            "on_disconnect": "cancel",
        }
        if model_name := request.get("model_name"):
            body["context"]["model_name"] = model_name
        secrets = request.get("secrets")
        if isinstance(secrets, Mapping) and secrets:
            body["config"] = {"context": {"secrets": dict(secrets)}}
        run_secrets = body.get("config", {}).get("context", {}).get("secrets", {})
        logger.info(
            "deerflow_run_context_payload trace_id=%s kc_access_token_present=%s kc_thread_id_present=%s",
            body["metadata"].get("kc_trace_id"),
            bool(run_secrets.get("kc_access_token")),
            bool(run_secrets.get("kc_thread_id")),
        )
        headers = {"Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.internal_auth_token:
            headers[self.internal_token_header] = self.internal_auth_token
            csrf_token = token_urlsafe(64)
            headers["Cookie"] = f"csrf_token={csrf_token}"
            headers["X-CSRF-Token"] = csrf_token
            headers[self.owner_user_id_header] = self.agent_owner_user_id
        thread_url = f"{self.base_url}/api/threads"
        run_url = f"{thread_url}/{quote(thread_id, safe='')}/runs/stream"

        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
            trust_env=False,
        ) as client:
            create_response = await client.post(
                thread_url,
                json={
                    "thread_id": thread_id,
                    "assistant_id": _ASSISTANT_ID,
                    "metadata": body["metadata"],
                },
                headers=headers,
            )
            create_response.raise_for_status()
            stream_method = "POST"
            stream_url = run_url
            stream_body: dict[str, Any] | None = body
            stream_headers = headers
            run_id = ""
            while True:
                async with client.stream(
                    stream_method,
                    stream_url,
                    json=stream_body,
                    headers=stream_headers,
                ) as response:
                    response.raise_for_status()
                    if not run_id:
                        content_location = urlsplit(
                            response.headers.get("Content-Location", "")
                        ).path
                        run_prefix = f"/api/threads/{quote(thread_id, safe='')}/runs/"
                        run_id = (
                            content_location.removeprefix(run_prefix)
                            if content_location.startswith(run_prefix)
                            else ""
                        )
                        on_run_id = request.get("on_run_id")
                        if run_id and "/" not in run_id and callable(on_run_id):
                            on_run_id(run_id)

                    resume_event_id: str | None = None
                    async for event in _iter_sse_events(response.aiter_lines()):
                        if event.get("type") == "gap":
                            data = event.get("data")
                            cursor = (
                                data.get("latest_available_event_id")
                                if isinstance(data, Mapping)
                                else None
                            )
                            if not run_id or not isinstance(cursor, str) or not cursor:
                                raise RuntimeError(
                                    "DeerFlow SSE gap cannot be resumed without a run ID and event cursor"
                                )
                            resume_event_id = cursor
                            break
                        yield event

                if resume_event_id is None:
                    return
                stream_method = "GET"
                stream_url = (
                    f"{thread_url}/{quote(thread_id, safe='')}/runs/{quote(run_id, safe='')}/join"
                )
                stream_body = None
                stream_headers = {**headers, "Last-Event-ID": resume_event_id}


async def _iter_sse_events(lines: Any) -> AsyncIterator[dict[str, Any]]:
    event_name: str | None = None
    data_lines: list[str] = []

    async for line in lines:
        if not line:
            if event_name is not None:
                yield _decode_sse_event(event_name, data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if event_name is not None:
        yield _decode_sse_event(event_name, data_lines)


def _decode_sse_event(event_name: str, data_lines: list[str]) -> dict[str, Any]:
    raw = "\n".join(data_lines).strip()
    if not raw or raw == "null":
        data: Any = {}
    else:
        data = json.loads(raw)
    return {"type": event_name, "data": data if isinstance(data, (Mapping, list)) else {}}
