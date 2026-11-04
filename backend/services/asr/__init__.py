"""语音识别能力入口，与具体 ASR 厂商实现解耦。"""

from runtime.config import Settings


async def transcribe_audio(settings: Settings, content: bytes, *, filename: str, content_type: str | None = None) -> str:
    """按配置选择 ASR 适配器，保持上层调用契约稳定。"""
    if getattr(settings, "asr_provider", "xfyun") == "xfyun":
        from .xfyun import transcribe_audio as provider
    else:
        from .doubao import transcribe_audio as provider
    return await provider(settings, content, filename=filename, content_type=content_type)

__all__ = ["transcribe_audio"]
