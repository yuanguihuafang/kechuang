"""增加跨存储失败补偿的持久化 Outbox。"""

from alembic import op

revision = "0002_outbox_events"
down_revision = "0002_metric_trace_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS platform_outbox_events (
            event_id BIGSERIAL PRIMARY KEY,
            operation_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            processed_at TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_outbox_operation_type
        ON platform_outbox_events (operation_id, event_type)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_platform_outbox_status_created
        ON platform_outbox_events (status, created_at ASC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_outbox_events CASCADE")
