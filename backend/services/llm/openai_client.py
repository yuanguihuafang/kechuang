"""OpenAI 兼容 Chat Completions 客户端。"""

import asyncio
import json
from collections.abc import AsyncIterator
from time import perf_counter

from core.contracts.message import Message
from runtime.config import Settings
from services.llm.client import LLMClient
from services.llm.json_utils import prompt_version
from services.observability.logging import get_logger
from services.observability.trace_context import get_trace_context, record_llm_exchange
from services.persistence.repositories.observability_repository import PostgresMetricsStore

logger = get_logger("retrieval", "llm.openai_client")


def _record_llm_metric(
    *,
    status: str,
    duration_ms: float,
    error_code: str | None = None,
) -> None:
    try:
        PostgresMetricsStore().record(
            metric_name="llm",
            status=status,
            duration_ms=duration_ms,
            error_code=error_code,
            trace_id=get_trace_context().trace_id or None,
        )
    except Exception:  # noqa: BLE001,S110 - 指标失败不得影响 LLM 主流程
        # 观测失败不能影响 LLM 主流程。
        pass


class OpenAIClient(LLMClient):
    """最小 OpenAI 兼容客户端。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(self, prompt: str | list[Message | dict]) -> str:
        if not self.settings.llm_api_key:
            _record_llm_metric(status="FAILED", duration_ms=0, error_code="LLM_NOT_CONFIGURED")
            raise RuntimeError("LLM_API_KEY 未配置，无法调用 LLM。")

        import httpx

        model = self.settings.llm_model or ""
        base_url = (self.settings.llm_base_url or "").rstrip("/")

        # 构建 messages
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = [
                item.to_llm_dict() if isinstance(item, Message) else item for item in prompt
            ]

        is_responses_api = base_url.endswith("/responses")
        if is_responses_api:
            content = []
            for message in messages:
                value = message.get("content", "")
                if not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False)
                content.append(
                    {"type": "input_text", "text": f"[{message.get('role', 'user')}]\r\n{value}"}
                )
            payload: dict = {"model": model, "input": [{"role": "user", "content": content}]}
        else:
            payload = {"model": model, "messages": messages, "temperature": 0}
        if self.settings.llm_thinking_enabled is not None:
            if is_responses_api:
                payload["reasoning"] = {
                    "effort": "high" if self.settings.llm_thinking_enabled else "none"
                }
            else:
                payload["thinking"] = {
                    "type": "enabled" if self.settings.llm_thinking_enabled else "disabled"
                }
        response_format = _response_format_for(self.settings.llm_response_format)
        if response_format is not None:
            if is_responses_api:
                payload["text"] = {"format": response_format}
                payload["max_output_tokens"] = 16384
            else:
                payload["response_format"] = response_format
                payload["max_tokens"] = 16384
        format_key = "text" if is_responses_api else "response_format"

        started_at = perf_counter()
        llm_role = self.settings.llm_role or "default"
        logger.info(
            f"LLM 请求开始 [{llm_role}]",
            extra={
                "structured": {
                    "event": "llm_request_started",
                    "provider": self.settings.llm_provider,
                    "role": llm_role,
                    "model": model,
                    "baseUrl": base_url,
                    "messageCount": len(messages),
                    "promptVersion": prompt_version(messages),
                    "inputChars": sum(len(str(message.get("content", ""))) for message in messages),
                    "timeoutSeconds": self.settings.llm_timeout_seconds,
                    "connectTimeoutSeconds": self.settings.llm_connect_timeout_seconds,
                    "maxAttempts": max(1, self.settings.llm_max_retries + 1),
                    "responseFormatEnabled": format_key in payload,
                }
            },
        )
        timeout = httpx.Timeout(
            float(self.settings.llm_timeout_seconds),
            connect=float(self.settings.llm_connect_timeout_seconds),
        )
        max_attempts = max(1, self.settings.llm_max_retries + 1)
        # 当前服务地址应直连；避免系统 HTTPS_PROXY 将请求转发到不可用代理。
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            attempt = 1
            response_format_downgraded = False
            while attempt <= max_attempts:
                try:
                    response = await client.post(
                        base_url if is_responses_api else _chat_endpoint(base_url),
                        headers={
                            "Authorization": f"Bearer {self.settings.llm_api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    response.raise_for_status()
                    resp_data = _decode_response_json(response)
                    break
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as error:
                    error_response = getattr(error, "response", None)
                    retryable_status = getattr(error_response, "status_code", None)
                    retryable = (
                        retryable_status is None
                        or retryable_status == 429
                        or retryable_status >= 500
                    )
                    error_detail = ""
                    if retryable_status == 400 and error_response is not None:
                        try:
                            error_detail = error_response.text
                        except Exception:  # noqa: BLE001,S110 - 错误正文读取失败可忽略
                            pass
                    if (
                        format_key in payload
                        and not response_format_downgraded
                        and _response_format_is_unsupported(retryable_status, error_detail)
                    ):
                        # 供应商或模型不支持 JSON Mode 时，立刻用同一模型降级为
                        # 普通 JSON 提示调用；这不是网络重试，不消耗重试次数。
                        payload.pop(format_key)
                        response_format_downgraded = True
                        logger.warning(
                            "模型不支持 response_format，已降级为普通 JSON 调用",
                            extra={
                                "structured": {
                                    "event": "llm_response_format_downgraded",
                                    "provider": self.settings.llm_provider,
                                    "model": model,
                                    "promptVersion": prompt_version(messages),
                                    "statusCode": retryable_status,
                                }
                            },
                        )
                        continue
                    logger.warning(
                        "LLM 请求失败",
                        extra={
                            "structured": {
                                "event": "llm_request_failed",
                                "provider": self.settings.llm_provider,
                                "model": model,
                                "promptVersion": prompt_version(messages),
                                "attempt": attempt,
                                "maxAttempts": max_attempts,
                                "errorType": type(error).__name__,
                                "statusCode": retryable_status,
                                "willRetry": retryable and attempt < max_attempts,
                                "latencyMs": int((perf_counter() - started_at) * 1000),
                                "errorDetail": error_detail,
                            }
                        },
                    )
                    if not retryable or attempt == max_attempts:
                        _record_llm_metric(
                            status="FAILED",
                            duration_ms=(perf_counter() - started_at) * 1000,
                            error_code=type(error).__name__,
                        )
                        raise
                    await asyncio.sleep(attempt)
                    attempt += 1

        latency_ms = int((perf_counter() - started_at) * 1000)
        if is_responses_api:
            finish_reason = (resp_data.get("incomplete_details") or {}).get("reason") or resp_data.get("status")
            content = resp_data.get("output_text")
            if not isinstance(content, str):
                values = []
                for item in resp_data.get("output", []):
                    for part in item.get("content", []) if isinstance(item, dict) else []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            values.append(part["text"])
                content = "\r\n".join(values)
        else:
            choices = resp_data.get("choices")
            if not choices:
                raise RuntimeError("LLM 返回中缺少 choices。")
            finish_reason = choices[0].get("finish_reason")
            content = choices[0].get("message", {}).get("content")
        output_error = None
        if finish_reason in {"length", "max_output_tokens", "incomplete"} or (
            is_responses_api and resp_data.get("status") == "incomplete"
        ):
            output_error = "LLM_OUTPUT_TRUNCATED"
        elif not isinstance(content, str) or not content.strip():
            output_error = "LLM_OUTPUT_EMPTY"
        if response_format is not None and output_error:
            _record_llm_metric(status="FAILED", duration_ms=latency_ms, error_code=output_error)
            logger.warning(
                "LLM 输出不可用 [%s] code=%s model=%s finish_reason=%s response_chars=%s",
                llm_role, output_error, model, finish_reason,
                len(content) if isinstance(content, str) else 0,
            )
            raise ValueError(output_error)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM 返回内容为空。")
        _record_llm_metric(status="SUCCESS", duration_ms=latency_ms)
        logger.info(
            f"LLM 请求完成 [{llm_role}]",
            extra={"structured": {
                "event": "llm_request_finished", "provider": self.settings.llm_provider,
                "role": llm_role, "model": model, "promptVersion": prompt_version(messages),
                "latencyMs": latency_ms, "finishReason": finish_reason, "outputChars": len(content),
            }},
        )
        record_llm_exchange(
            provider=self.settings.llm_provider,
            role=llm_role,
            model=model,
            messages=messages,
            output=content,
            latency_ms=latency_ms,
        )
        return content

    async def stream(self, prompt: str | list[Message | dict]) -> AsyncIterator[str]:
        """调用 OpenAI 兼容接口并逐 token 返回内容。"""
        if not self.settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY 未配置，无法调用 LLM。")
        import httpx

        messages = (
            [{"role": "user", "content": prompt}]
            if isinstance(prompt, str)
            else [item.to_llm_dict() if isinstance(item, Message) else item for item in prompt]
        )
        model = self.settings.llm_model or ""
        llm_role = self.settings.llm_role or "default"
        payload = {"model": model, "messages": messages, "temperature": 0, "stream": True}
        timeout = httpx.Timeout(
            float(self.settings.llm_timeout_seconds),
            connect=float(self.settings.llm_connect_timeout_seconds),
        )
        started_at = perf_counter()
        output_parts: list[str] = []
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream(
                "POST",
                _chat_endpoint((self.settings.llm_base_url or "").rstrip("/")),
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        content = (
                            json.loads(data).get("choices", [{}])[0].get("delta", {}).get("content")
                        )
                    except (ValueError, IndexError, AttributeError):
                        continue
                    if isinstance(content, str) and content:
                        output_parts.append(content)
                        yield content
        record_llm_exchange(
            provider=self.settings.llm_provider,
            role=llm_role,
            model=model,
            messages=messages,
            output="".join(output_parts),
            latency_ms=int((perf_counter() - started_at) * 1000),
        )


def _response_format_for(mode: str) -> dict[str, str] | None:
    """根据显式配置生成 OpenAI 兼容接口的结构化输出参数。"""
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def _decode_response_json(response: object) -> dict:
    """把供应商的非 JSON 成功响应转换成可诊断的上游错误。"""

    try:
        payload = response.json()  # type: ignore[attr-defined]
    except (TypeError, ValueError) as error:
        status_code = getattr(response, "status_code", "unknown")
        headers = getattr(response, "headers", {})
        content_type = (
            headers.get("content-type", "unknown") if hasattr(headers, "get") else "unknown"
        )
        body = " ".join(str(getattr(response, "text", "") or "").split())[:300]
        detail = body or "<empty>"
        raise RuntimeError(
            f"LLM 上游返回非 JSON 响应（HTTP {status_code}, Content-Type {content_type}, body={detail}）"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"LLM 上游返回 JSON 类型错误（HTTP {getattr(response, 'status_code', 'unknown')}）"
        )
    return payload


def _chat_endpoint(base_url: str) -> str:
    """兼容 .env 中填写 API 根地址或完整 chat/completions 地址。"""
    return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"


def _response_format_is_unsupported(status_code: int | None, detail: str) -> bool:
    """识别 OpenAI 兼容供应商对 JSON Mode 的明确拒绝。"""

    normalized = detail.lower()
    return status_code == 400 and (
        "response_format" in normalized
        and "support" in normalized
        or "model_not_support" in normalized
    )
