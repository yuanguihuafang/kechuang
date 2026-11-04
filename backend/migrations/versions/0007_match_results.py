"""保存用户匹配结果。"""

from alembic import op

revision = "0007_match_results"
down_revision = "0006_observability_time_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
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
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_match_results")
