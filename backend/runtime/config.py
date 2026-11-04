"""应用配置。"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 应用
    app_name: str = "科创大模型"
    app_env: str = "local"
    app_host: str = "0.0.0.0"
    app_port: int = 8793
    # 不同环境用 PUBLIC_API_BASE_URL 配置返回给网页和小程序的公开图片接口地址。
    public_api_base_url: str = ""
    llm_role: str | None = None
    cors_origins: str = "http://localhost:8794,http://127.0.0.1:8794"
    cors_origin_regex: str | None = r"^http://(localhost|127\.0\.0\.1):\d+$"

    # 日志
    log_level: str = "INFO"
    log_file_path: str | None = "log/app.log"
    log_error_file_path: str | None = "log/error.log"
    log_retention_days: int = 7
    # 审计日志仅用于近期排障与操作回溯；由独立定时命令按此天数清理。
    audit_log_retention_days: int = 15

    # 智能体会话令牌；原系统 access_token 只用于登录阶段传递。
    auth_token_secret: str = "local-development-auth-secret-change-me"
    auth_token_ttl_seconds: int = 43200

    # DeerFlow 唯一聊天运行时；未启用时聊天明确返回服务不可用。
    deerflow_enabled: bool = False
    deerflow_base_url: str | None = None
    deerflow_api_key: str | None = None
    deerflow_internal_auth_token: str | None = Field(
        default=None,
        validation_alias="DEER_FLOW_INTERNAL_AUTH_TOKEN",
    )
    deerflow_agent_owner_user_id: str | None = None
    deerflow_internal_token_header: str | None = None
    deerflow_owner_user_id_header: str | None = None
    deerflow_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    kc_mcp_enabled: bool = False
    kc_mcp_path: str = "/mcp"

    # LLM
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    # 按职责选择模型；未单独指定时使用公共 LLM_MODEL。
    llm_draft_model: str | None = None
    llm_vision_model: str | None = None
    llm_reasoning_model: str | None = None
    # 各职责独立控制思考模式；None 表示不覆盖供应商默认值。
    llm_thinking_enabled: bool | None = None
    # 草稿字段是结构化抽取，不需要模型思考。
    llm_draft_thinking_enabled: bool = False
    llm_vision_thinking_enabled: bool = False
    llm_vision_base_url: str | None = None
    llm_vision_api_key: str | None = None

    # 外部联网搜索；未配置密钥时保持关闭，不影响知识库检索。
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"
    tavily_timeout_seconds: int = Field(default=30, ge=1, le=120)
    tavily_search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "basic"

    document_parser: str = "mineru"
    mineru_url: str | None = None
    mineru_command: str = "mineru"
    paddleocr_job_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    paddleocr_api_token: str | None = None
    paddleocr_model: str = "PP-OCRv6"
    paddleocr_timeout_seconds: int = Field(default=900, ge=1, le=1800)
    paddleocr_poll_interval_seconds: float = Field(default=5, gt=0, le=60)
    # 单进程同时执行的文件解析数；超过后排队等待。
    document_parse_concurrency: int = Field(default=10, ge=1, le=100)
    llm_timeout_seconds: int = 240
    # 建立 TCP/TLS 连接的独立超时；网络波动时不应受写死的 10 秒限制。
    llm_connect_timeout_seconds: int = 30
    llm_max_retries: int = 1
    # OpenAI 兼容接口的结构化输出能力。默认关闭，需经实际接口验证后再启用。
    llm_response_format: Literal["none", "json_object"] = "none"

    # 语音输入 / ASR：由适配器选择供应商；当前默认使用讯飞录音文件转写。
    asr_enabled: bool = True
    asr_provider: Literal["doubao", "xfyun"] = "xfyun"
    asr_base_url: str | None = "https://raasr.xfyun.cn/v2/api"
    asr_app_id: str | None = None
    asr_app_key: str | None = None
    asr_access_key: str | None = None
    asr_api_key: str | None = None
    asr_resource_id: str = "volc.seedasr.auc"
    asr_submit_url: str = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
    asr_query_url: str = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
    asr_audio_url_expire_seconds: int = Field(default=900, ge=60, le=3600)
    asr_timeout_seconds: int = 900
    asr_poll_interval_seconds: float = 10
    asr_language: str = "cn"

    # 数据库
    database_url: str | None = None

    # Milvus 向量数据库
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_database: str = "kechuang"
    milvus_username: str | None = None
    milvus_password: str | None = None

    # 仅读取 Perception 写入的图片关联清单；图片本体与向量库隔离。
    minio_endpoint: str = "127.0.0.1:9000"
    minio_public_endpoint: str = ""
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "kechuang"

    # Ollama（向量嵌入）
    ollama_url: str = "http://27.223.9.106:8999"
    embedding_model: str = "bge-m3"
    embedding_batch_size: int = 64
    embedding_batch_concurrency: int = 8

    # Reranker（重排模型）
    # 默认使用重排；请求重排但服务不可用时明确失败。
    rerank_enabled: bool = True
    reranker_url: str = "http://27.223.9.106:9998/v1/rerank"
    reranker_model: str = "bge-reranker-large"
    # 将不同 reranker 的原始相关度映射到统一置信度时的参考分数。
    rerank_confidence_reference_score: float = 0.15

    # RAG 配置
    # 默认关闭；仅显式开启后，外部用户才可查看公共企业库的完整字段。
    external_full_enterprise_access: bool = False
    # 当前工作台按原文展示；接入正式外部用户后再显式开启出站脱敏。
    output_redaction_enabled: bool = False
    rag_recall_k: int = 15  # 每个知识库初排最多多少条 (RAG_RECALL_K)
    rag_fusion_candidates: int = 60  # 合并后进入重排的最多条数 (RAG_FUSION_CANDIDATES)
    # full: 重排前补齐业务记录；step: 只重排命中的子块，详情时再补齐。
    rag_retrieval_mode: Literal["full", "step"] = "full"
    rag_max_evidence_per_document: int = 1
    rerank_top_k: int = 5  # 重排结果候选上限；最终返回数量由请求 top_k 决定 (RERANK_TOP_K)
    query_expansion_max_queries: int = 5
    query_expansion_max_length: int = 500
    rag_top_k: int = 5  # 传给 LLM 的证据数量 (RAG_TOP_K)

    model_config = SettingsConfigDict(
        # 本地用 .env.local 覆盖 .env；服务器没有该文件时继续使用 .env。
        env_file=("../../.env", "../../../.env", "../.env", ".env", ".env.local"),
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 域名配置解析成列表。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def model_for_role(self, role: str) -> str | None:
        """返回指定职责的模型；未单独指定时使用公共 LLM_MODEL。"""

        role_fields = {
            "draft": self.llm_draft_model,
            "vision": self.llm_vision_model,
            "reasoning": self.llm_reasoning_model,
        }
        return role_fields.get(role) or self.llm_model

    @property
    def effective_asr_api_key(self) -> str | None:
        """ASR 只使用其专属密钥，避免跨供应商发送 LLM 密钥。"""
        return self.asr_api_key

    @property
    def effective_asr_base_url(self) -> str | None:
        """ASR 不复用 LLM 地址，避免请求发送到错误供应商。"""
        return self.asr_base_url

    def allows_external_full_enterprise(
        self, user_role: str | None, knowledge_type: str | None
    ) -> bool:
        """判断是否允许外部身份查询企业库完整字段。"""
        return bool(
            self.external_full_enterprise_access
            and str(user_role or "").strip().lower() == "external"
            and str(knowledge_type or "").strip().lower() == "enterprise"
        )


def settings_for_role(settings: Settings, role: str) -> Settings:
    """复制配置并选择指定职责的模型。"""

    update = {
        "llm_model": settings.model_for_role(role),
        "llm_role": role,
    }
    thinking_fields = {
        "draft": settings.llm_draft_thinking_enabled,
        "vision": settings.llm_vision_thinking_enabled,
    }
    if role in thinking_fields:
        update["llm_thinking_enabled"] = thinking_fields[role]
    if role == "vision":
        update.update(
            {
                "llm_base_url": settings.llm_vision_base_url or settings.llm_base_url,
                "llm_api_key": settings.llm_vision_api_key or settings.llm_api_key,
            }
        )
    return settings.model_copy(update=update)


@lru_cache
def get_settings() -> Settings:
    return Settings()
