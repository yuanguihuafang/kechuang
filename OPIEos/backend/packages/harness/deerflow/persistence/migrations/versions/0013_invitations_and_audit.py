"""invitation-only registration and audit log.

Revision ID: 0013_invitations_and_audit
Revises: 0012_mcp_task_results
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0013_invitations_and_audit"
down_revision: str | Sequence[str] | None = "0012_mcp_task_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("used_by", sa.String(36)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_user_invitations_status_expires", "user_invitations", ["status", "expires_at"])
    op.create_index("ix_user_invitations_email", "user_invitations", ["email"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_user_id", sa.String(36)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])


def downgrade() -> None:
    from alembic import op

    op.drop_table("audit_logs")
    op.drop_table("user_invitations")
