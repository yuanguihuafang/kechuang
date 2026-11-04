"""火山引擎 Doubao-录音文件识别 2.0 适配器。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import uuid

import httpx

from runtime.config import Settings
from services.storage.minio.client import get_minio_bucket, get_minio_client, get_minio_public_client

_AUDIO_MAX_BYTES = 500 * 1024 * 1024
_SUCCESS = "20000000"
_PROCESSING = {"20000001", "20000002"}
logger = logging.getLogger(__name__)


async def transcribe_audio(settings: Settings, content: bytes, *, filename: str, content_type: str | None = None) -> str:
    if not settings.asr_enabled:
        raise RuntimeError("语音识别服务未启用。")
    has_legacy_auth = bool(
        str(getattr(settings, "asr_app_key", "") or "").strip()
        and str(getattr(settings, "asr_access_key", "") or "").strip()
    )
    if not settings.effective_asr_api_key and not has_legacy_auth:
        raise RuntimeError("Doubao 语音识别未配置 ASR_API_KEY，或 ASR_APP_KEY 与 ASR_ACCESS_KEY。")
    if not content:
        raise RuntimeError("音频内容为空。")
    if len(content) > _AUDIO_MAX_BYTES:
        raise RuntimeError("Doubao 录音文件识别仅支持不超过 500MB 的单个音频文件。")

    audio_url, object_key = await _upload_audio(content, filename, content_type, settings)
    task_id = str(uuid.uuid4())
    headers = _headers(settings, task_id)
    timeout = httpx.Timeout(min(60.0, float(settings.asr_timeout_seconds)), connect=float(settings.llm_connect_timeout_seconds))
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(settings.asr_submit_url, headers=headers, json={
                "user": {"uid": "kc-rag-agent"},
                "audio": {"format": _audio_format(filename, content_type), "url": audio_url},
                "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
            })
            _check_response(response, "提交")
            status = response.headers.get("X-Api-Status-Code", "")
            if status != _SUCCESS:
                raise RuntimeError(f"Doubao 语音识别提交失败（{status or 'unknown'}）：{response.headers.get('X-Api-Message', '')}")
            return await _poll_result(client, settings, headers)
    finally:
        await _delete_audio(object_key)


async def _poll_result(client: httpx.AsyncClient, settings: Settings, headers: dict[str, str]) -> str:
    deadline = asyncio.get_running_loop().time() + float(settings.asr_timeout_seconds)
    interval = max(1.0, float(settings.asr_poll_interval_seconds))
    while asyncio.get_running_loop().time() < deadline:
        response = await client.post(settings.asr_query_url, headers=headers, json={})
        _check_response(response, "查询")
        status = response.headers.get("X-Api-Status-Code", "")
        if status in _PROCESSING:
            await asyncio.sleep(interval)
            continue
        if status == _SUCCESS:
            text = str((response.json().get("result") or {}).get("text") or "").strip()
            if not text:
                raise RuntimeError("Doubao 语音识别结果为空。")
            return text
        if status == "20000003":
            raise RuntimeError("音频中未检测到人声。")
        raise RuntimeError(f"Doubao 语音识别查询失败（{status or 'unknown'}）：{response.headers.get('X-Api-Message', '')}")
    raise RuntimeError(f"Doubao 语音识别在 {int(settings.asr_timeout_seconds)} 秒内未完成，请稍后重试。")


async def _upload_audio(content: bytes, filename: str, content_type: str | None, settings: Settings) -> tuple[str, str]:
    key = f"asr/{uuid.uuid4().hex}/{Path(filename).name or 'voice.webm'}"
    await asyncio.to_thread(
        get_minio_client().put_object,
        Bucket=get_minio_bucket(),
        Key=key,
        Body=content,
        ContentType=content_type or "application/octet-stream",
    )
    try:
        url = await asyncio.to_thread(
            get_minio_public_client().generate_presigned_url,
            "get_object",
            Params={"Bucket": get_minio_bucket(), "Key": key},
            ExpiresIn=int(getattr(settings, "asr_audio_url_expire_seconds", 900)),
        )
    except Exception:
        await _delete_audio(key)
        raise
    return url, key


async def _delete_audio(key: str) -> None:
    try:
        await asyncio.to_thread(get_minio_client().delete_object, Bucket=get_minio_bucket(), Key=key)
    except Exception:
        logger.warning("Doubao ASR 临时音频清理失败: %s", key, exc_info=True)


def _headers(settings: Settings, task_id: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": str(settings.asr_resource_id),
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
    }
    app_key = str(getattr(settings, "asr_app_key", "") or "").strip()
    access_key = str(getattr(settings, "asr_access_key", "") or "").strip()
    if app_key and access_key:
        headers.update({"X-Api-App-Key": app_key, "X-Api-Access-Key": access_key})
    else:
        headers["X-Api-Key"] = str(settings.effective_asr_api_key)
    return headers


def _audio_format(filename: str, content_type: str | None) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {"m4a", "mp4"} or content_type == "audio/mp4":
        return "mp4"
    if suffix in {"wav", "mp3", "ogg", "raw"}:
        return suffix
    return {"audio/mpeg": "mp3", "audio/ogg": "ogg"}.get(content_type, "raw")


def _check_response(response: httpx.Response, action: str) -> None:
    if response.status_code >= 400:
        detail = response.headers.get("X-Api-Message") or response.text[:300]
        logger.error(
            "Doubao 语音识别%s失败 status=%s message=%s logid=%s",
            action, response.status_code, detail, response.headers.get("X-Tt-Logid", ""),
        )
        raise RuntimeError(f"Doubao 语音识别{action}请求失败（HTTP {response.status_code}）。")
