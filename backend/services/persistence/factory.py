"""平台状态组件工厂。"""

from __future__ import annotations

import os
from typing import Any

from .repositories.observability_repository import PostgresAuditSink
from .repositories.runtime_repository import PostgresStateStore


def use_postgres() -> bool:
    """平台状态统一使用 PostgreSQL；缺少配置时尽早失败。"""
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL 未配置，平台状态不再使用 JSON 回退")
    return True


def build_state_store(*, service: str, json_store_factory: Any | None = None, json_path: Any | None = None) -> Any:
    use_postgres()
    return PostgresStateStore(service=service)


def build_audit_sink() -> Any:
    use_postgres()
    return PostgresAuditSink()
