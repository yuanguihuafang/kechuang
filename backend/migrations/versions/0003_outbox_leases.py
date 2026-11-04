"""为 Outbox 增加崩溃可恢复的处理租约。"""

from alembic import op

revision = "0003_outbox_leases"
down_revision = "0002_outbox_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_outbox_events "
        "ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform_outbox_events DROP COLUMN IF EXISTS lease_until"
    )
