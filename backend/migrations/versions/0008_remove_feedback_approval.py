"""Remove retired feedback and PostgreSQL approval persistence."""

from alembic import op

revision = "0008_remove_feedback_approval"
down_revision = "0007_match_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_feedback_events")
    op.execute("DROP TABLE IF EXISTS platform_approval_states")


def downgrade() -> None:
    pass
