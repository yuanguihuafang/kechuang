"""为指标和审计时间窗口查询补充索引。"""

from alembic import op

revision = "0006_observability_time_indexes"
down_revision = "0005_remove_orphan_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_metric_events_created_at "
        "ON platform_metric_events (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_audit_events_created_at "
        "ON platform_audit_events (created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_platform_metric_events_created_at")
    op.execute("DROP INDEX IF EXISTS idx_platform_audit_events_created_at")
