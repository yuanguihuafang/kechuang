"""平台公共 LLM 能力入口。"""

from services.asr import transcribe_audio

from .client import LLMClient
from .json_utils import parse_json_object, prompt_version
from .openai_client import OpenAIClient
from .router import get_llm_client

__all__ = [
    "LLMClient",
    "OpenAIClient",
    "get_llm_client",
    "parse_json_object",
    "prompt_version",
    "transcribe_audio",
]
