"""讯飞录音文件转写实现。"""

from __future__ import annotations

import asyncio
from base64 import b64encode
import hashlib
import hmac
import json
from pathlib import Path
import time

from runtime.config import Settings

_AUDIO_MAX_BYTES = 500 * 1024 * 1024


async def transcribe_audio(
    settings: Settings,
    content: bytes,
    *,
    filename: str,
    content_type: str | None = None,
) -> str:
    """调用讯飞录音文件转写标准版，返回纯文本结果。"""

    api_key = settings.effective_asr_api_key
    base_url = (settings.effective_asr_base_url or "").rstrip("/")
    app_id = str(getattr(settings, "asr_app_id", "") or "").strip()
    if not settings.asr_enabled:
        raise RuntimeError("语音识别服务未启用。")
    if not app_id or not api_key or not base_url:
        raise RuntimeError("语音识别服务未配置完整。")
    if not content:
        raise RuntimeError("音频内容为空。")
    if len(content) > _AUDIO_MAX_BYTES:
        raise RuntimeError("讯飞录音文件转写仅支持不超过 500MB 的单个音频文件。")

    order_id = await _upload_audio(settings, content, filename)
    return await _poll_result(settings, order_id)


async def _upload_audio(settings: Settings, content: bytes, filename: str) -> str:
    import httpx

    safe_name = Path(filename).name or "audio.m4a"
    params = _auth_params(settings)
    params.update({
        "fileName": safe_name,
        "fileSize": str(len(content)),
        "duration": "0",
        "language": settings.asr_language,
    })
    timeout = httpx.Timeout(
        min(60.0, float(settings.asr_timeout_seconds)),
        connect=float(settings.llm_connect_timeout_seconds),
    )
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            _endpoint(settings, "upload"),
            params=params,
            headers={"Content-Type": "application/octet-stream"},
            content=content,
        )
    _raise_http_error(response)
    payload = response.json()
    if payload.get("code") != "000000":
        raise RuntimeError(
            f"讯飞语音转写上传失败（{payload.get('code', 'unknown')}）："
            f"{payload.get('descInfo', '无错误详情')}"
        )
    order_id = payload.get("content", {}).get("orderId")
    if not isinstance(order_id, str) or not order_id:
        raise RuntimeError("讯飞语音转写未返回订单号。")
    return order_id


async def _poll_result(settings: Settings, order_id: str) -> str:
    import httpx

    timeout_seconds = float(settings.asr_timeout_seconds)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    request_timeout = httpx.Timeout(
        min(60.0, timeout_seconds),
        connect=float(settings.llm_connect_timeout_seconds),
    )
    interval = max(
        5.0,
        float(getattr(settings, "asr_poll_interval_seconds", 10)),
        timeout_seconds / 90,
    )
    async with httpx.AsyncClient(timeout=request_timeout, trust_env=False) as client:
        while asyncio.get_running_loop().time() < deadline:
            params = _auth_params(settings)
            params.update({"orderId": order_id, "resultType": "transfer"})
            response = await client.get(_endpoint(settings, "getResult"), params=params)
            _raise_http_error(response)
            payload = response.json()
            code = str(payload.get("code") or "")
            if code == "26605":
                await asyncio.sleep(interval)
                continue
            if code != "000000":
                raise RuntimeError(
                    f"讯飞语音转写查询失败（{code or 'unknown'}）："
                    f"{payload.get('descInfo', '无错误详情')}"
                )
            order_info = payload.get("content", {}).get("orderInfo", {})
            status = order_info.get("status")
            if status == 4:
                return _extract_text(payload.get("content", {}).get("orderResult"))
            if status == -1:
                raise RuntimeError(
                    f"讯飞语音转写失败（failType={order_info.get('failType', 'unknown')}）。"
                )
            await asyncio.sleep(interval)
    raise RuntimeError(f"讯飞语音转写在 {int(timeout_seconds)} 秒内未完成，请稍后重试。")


def _auth_params(settings: Settings) -> dict[str, str]:
    app_id = str(getattr(settings, "asr_app_id", "") or "").strip()
    secret_key = str(settings.effective_asr_api_key or "").strip()
    if not app_id or not secret_key:
        raise RuntimeError("讯飞语音转写未配置 ASR_APP_ID 或 ASR_API_KEY。")
    ts = str(int(time.time()))
    return {"appId": app_id, "ts": ts, "signa": _signa(app_id, ts, secret_key)}


def _signa(app_id: str, ts: str, secret_key: str) -> str:
    base = hashlib.md5(f"{app_id}{ts}".encode("utf-8")).hexdigest().encode("utf-8")
    return b64encode(hmac.new(secret_key.encode("utf-8"), base, hashlib.sha1).digest()).decode("utf-8")


def _endpoint(settings: Settings, path: str) -> str:
    return f"{str(settings.effective_asr_base_url or '').rstrip('/')}/{path}"


def _raise_http_error(response) -> None:
    import httpx

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text.strip()[:300]
        raise RuntimeError(f"讯飞语音转写请求失败（HTTP {response.status_code}）：{detail or '无错误详情'}") from exc


def _extract_text(order_result: object) -> str:
    try:
        result = json.loads(order_result) if isinstance(order_result, str) else order_result
        lattice = result.get("lattice", []) if isinstance(result, dict) else []
        parts: list[str] = []
        for item in lattice:
            best = json.loads(item.get("json_1best", "{}"))
            for segment in best.get("st", {}).get("rt", []):
                for word in segment.get("ws", []):
                    candidates = word.get("cw", [])
                    if candidates and isinstance(candidates[0].get("w"), str):
                        parts.append(candidates[0]["w"])
    except (AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("讯飞语音转写结果格式异常。") from exc
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("讯飞语音转写结果为空。")
    return text
