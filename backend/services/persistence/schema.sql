CREATE TABLE IF NOT EXISTS platform_tasks (
    task_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    operation TEXT NOT NULL,
    document_id TEXT,
    status TEXT NOT NULL,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE platform_tasks ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE platform_tasks ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE platform_tasks ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_platform_tasks_service_status
    ON platform_tasks (service, status);

CREATE INDEX IF NOT EXISTS idx_platform_tasks_queue
    ON platform_tasks (service, status, next_run_at, lease_until);

CREATE TABLE IF NOT EXISTS platform_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT,
    user_role TEXT,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE platform_sessions ADD COLUMN IF NOT EXISTS user_role TEXT;

CREATE INDEX IF NOT EXISTS idx_platform_sessions_user
    ON platform_sessions (user_id, updated_at DESC);

-- 对话展示历史与 LangGraph Checkpoint 分开保存；不把完整消息塞入 platform_sessions.state。
CREATE TABLE IF NOT EXISTS platform_session_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL DEFAULT '',
    response JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_platform_session_messages_session
    ON platform_session_messages (session_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_platform_session_messages_user
    ON platform_session_messages (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS platform_match_results (
    result_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT,
    query TEXT NOT NULL,
    draft_id TEXT,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_platform_match_results_user
    ON platform_match_results (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_memories (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT,
    source TEXT NOT NULL DEFAULT 'user_confirmed',
    importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_memories_user_status
    ON user_memories (user_id, status, updated_at DESC);

ALTER TABLE user_memories ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_user_memories_user_hash
    ON user_memories (user_id, content_hash);

CREATE TABLE IF NOT EXISTS platform_audit_events (
    event_id BIGSERIAL PRIMARY KEY,
    event_name TEXT NOT NULL,
    request_id TEXT,
    session_id TEXT,
    task_id TEXT,
    trace_id TEXT,
    service TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_platform_audit_events_trace
    ON platform_audit_events (trace_id, created_at DESC);

-- 阶段指标明细：只保存耗时、状态、错误和可选成本估算，不保存业务正文。
CREATE TABLE IF NOT EXISTS platform_metric_events (
    metric_id BIGSERIAL PRIMARY KEY,
    metric_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SUCCESS',
    duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    error_code TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost DOUBLE PRECISION,
    trace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE platform_metric_events ADD COLUMN IF NOT EXISTS trace_id TEXT;

CREATE INDEX IF NOT EXISTS idx_platform_metric_events_name_time
    ON platform_metric_events (metric_name, created_at DESC);

-- 第一阶段资料生命周期元数据：不保存正文、解析块文本或向量。
CREATE TABLE IF NOT EXISTS platform_documents (
    document_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    storage_key TEXT NOT NULL DEFAULT '',
    scope_id TEXT NOT NULL,
    user_id TEXT,
    knowledge_type TEXT NOT NULL DEFAULT 'solution',
    status TEXT NOT NULL,
    active_version_id TEXT,
    version_number INTEGER NOT NULL DEFAULT 1,
    version_id TEXT,
    publication_status TEXT NOT NULL DEFAULT 'UNPUBLISHED',
    index_status TEXT NOT NULL DEFAULT 'NOT_INDEXED',
    artifact_refs JSONB NOT NULL DEFAULT '{}'::jsonb,
    parsed_block_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    index_time TIMESTAMPTZ,
    error TEXT,
    version_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    unpublish_reason_code TEXT,
    unpublish_reason_text TEXT,
    unpublished_by TEXT,
    unpublished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS storage_key TEXT NOT NULL DEFAULT '';
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS version_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS version_id TEXT;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS publication_status TEXT NOT NULL DEFAULT 'UNPUBLISHED';
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS index_status TEXT NOT NULL DEFAULT 'NOT_INDEXED';
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS artifact_refs JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS parsed_block_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS indexed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS index_time TIMESTAMPTZ;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS version_history JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS knowledge_type TEXT NOT NULL DEFAULT 'solution';
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS unpublish_reason_code TEXT;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS unpublish_reason_text TEXT;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS unpublished_by TEXT;
ALTER TABLE platform_documents ADD COLUMN IF NOT EXISTS unpublished_at TIMESTAMPTZ;

-- Milvus 数量日快照：仅用于后台趋势展示，不参与检索和同步。
CREATE TABLE IF NOT EXISTS milvus_count_snapshots (
    knowledge_type TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (knowledge_type, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_milvus_count_snapshots_date
    ON milvus_count_snapshots (snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_platform_documents_scope_status
    ON platform_documents (scope_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS platform_drafts (
    draft_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    draft_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CONFIRMED',
    thread_id TEXT,
    source_document_id TEXT,
    draft_title TEXT NOT NULL DEFAULT '',
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE platform_drafts ADD COLUMN IF NOT EXISTS draft_title TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_platform_drafts_title ON platform_drafts (draft_title);

CREATE INDEX IF NOT EXISTS idx_platform_drafts_user_status
    ON platform_drafts (user_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS platform_outbox_events (
    event_id BIGSERIAL PRIMARY KEY,
    operation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            processed_at TIMESTAMPTZ,
            lease_until TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_outbox_operation_type
    ON platform_outbox_events (operation_id, event_type);

CREATE INDEX IF NOT EXISTS idx_platform_outbox_status_created
    ON platform_outbox_events (status, created_at ASC);
