"""补充审计筛选和单实例任务查询索引。"""

from alembic import op


revision = "0012_schema_indexes"
down_revision = "0011_mysql_sync_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 审计列表支持服务、事件、任务和 JSONB 业务标识筛选。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_audit_service_time "
        "ON platform_audit_events (service, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_audit_event_name_time "
        "ON platform_audit_events (event_name, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_audit_task_time "
        "ON platform_audit_events (task_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_audit_user_time "
        "ON platform_audit_events ((payload->>'user_id'), created_at DESC) "
        "WHERE payload ? 'user_id'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_audit_document_time "
        "ON platform_audit_events ((payload->>'document_id'), created_at DESC) "
        "WHERE payload ? 'document_id'"
    )

    # Worker 领取队列时按服务、状态、重试时间和创建时间过滤/排序。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_tasks_claim "
        "ON platform_tasks (service, status, next_run_at, created_at, task_id)"
    )


def downgrade() -> None:
    for index_name in (
        "idx_platform_tasks_claim",
        "idx_platform_audit_document_time",
        "idx_platform_audit_user_time",
        "idx_platform_audit_task_time",
        "idx_platform_audit_event_name_time",
        "idx_platform_audit_service_time",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
