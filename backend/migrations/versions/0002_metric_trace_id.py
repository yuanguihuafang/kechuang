"""为指标事件增加 TraceId 关联字段。"""

from alembic import op

revision = "0002_metric_trace_id"
down_revision = "0001_initial_platform_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_metric_events "
        "ADD COLUMN IF NOT EXISTS trace_id TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform_metric_events "
        "DROP COLUMN IF EXISTS trace_id"
    )
