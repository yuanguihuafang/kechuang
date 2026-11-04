"""保存 Milvus 各知识库的每日数量快照。"""

from alembic import op


revision = "0013_milvus_count_snapshots"
down_revision = "0012_schema_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS milvus_count_snapshots (
            knowledge_type TEXT NOT NULL,
            snapshot_date DATE NOT NULL,
            count INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (knowledge_type, snapshot_date)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_milvus_count_snapshots_date
            ON milvus_count_snapshots (snapshot_date DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS milvus_count_snapshots")
