"""记录系统开始时间、运行会话和结构化心跳。"""

from alembic import op


revision = "0014_runtime_evidence"
down_revision = "0013_milvus_count_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS runtime_evidence_config (
            config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
            system_start_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS runtime_sessions (
            session_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            status TEXT NOT NULL,
            end_reason TEXT,
            version TEXT,
            host TEXT,
            pid INTEGER
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS runtime_heartbeats (
            heartbeat_id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES runtime_sessions(session_id),
            sequence INTEGER NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,
            version TEXT
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_runtime_sessions_started_at
            ON runtime_sessions (started_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_runtime_heartbeats_session_observed
            ON runtime_heartbeats (session_id, observed_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS runtime_heartbeats")
    op.execute("DROP TABLE IF EXISTS runtime_sessions")
    op.execute("DROP TABLE IF EXISTS runtime_evidence_config")
