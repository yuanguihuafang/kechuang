"""补齐草稿列表依赖的标题字段。"""

from alembic import op


revision = "0009_ensure_draft_title"
down_revision = "0008_remove_feedback_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_drafts "
        "ADD COLUMN IF NOT EXISTS draft_title TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_drafts_title "
        "ON platform_drafts (draft_title)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_platform_drafts_title")
    op.execute("ALTER TABLE platform_drafts DROP COLUMN IF EXISTS draft_title")
