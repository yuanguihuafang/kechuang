"""持久化 MySQL 同步断点，避免依赖任务历史。"""

from alembic import op


revision = "0011_mysql_sync_states"
down_revision = "0010_knowledge_type_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS mysql_sync_states (
            knowledge_type TEXT NOT NULL,
            source_table TEXT NOT NULL,
            last_mysql_id INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0,
            indexed_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'IDLE',
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (knowledge_type, source_table)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mysql_sync_states")
