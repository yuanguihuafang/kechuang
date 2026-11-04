"""禁止文档知识类型静默回退到方案库。"""

from alembic import op


revision = "0010_knowledge_type_default"
down_revision = "0009_ensure_draft_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform_documents ALTER COLUMN knowledge_type DROP DEFAULT")


def downgrade() -> None:
    # 不恢复隐式类型，避免回滚后再次把未指定类型写成方案库。
    pass
