"""为持久化任务增加崩溃恢复所需的租约字段。"""

from alembic import op

revision = "0004_task_leases"
down_revision = "0003_outbox_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_tasks "
        "ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE platform_tasks "
        "ADD COLUMN IF NOT EXISTS worker_id TEXT"
    )
    op.execute(
        "ALTER TABLE platform_tasks "
        "ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE platform_tasks "
        "ADD COLUMN IF NOT EXISTS last_error TEXT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_tasks_queue "
        "ON platform_tasks (service, status, next_run_at, lease_until)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_platform_tasks_queue")
    op.execute("ALTER TABLE platform_tasks DROP COLUMN IF EXISTS last_error")
    op.execute("ALTER TABLE platform_tasks DROP COLUMN IF EXISTS next_run_at")
    op.execute("ALTER TABLE platform_tasks DROP COLUMN IF EXISTS worker_id")
    op.execute("ALTER TABLE platform_tasks DROP COLUMN IF EXISTS lease_until")
