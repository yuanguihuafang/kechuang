"""删除未使用的兼容字段。"""

from alembic import op

revision = "0005_remove_orphan_columns"
down_revision = "0004_task_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform_sessions DROP COLUMN IF EXISTS tenant_id")
    op.execute("ALTER TABLE platform_tasks DROP COLUMN IF EXISTS last_error")


def downgrade() -> None:
    op.execute("ALTER TABLE platform_tasks ADD COLUMN IF NOT EXISTS last_error TEXT")
    op.execute("ALTER TABLE platform_sessions ADD COLUMN IF NOT EXISTS tenant_id TEXT")
