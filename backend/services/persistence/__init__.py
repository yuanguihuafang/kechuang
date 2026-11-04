"""平台状态持久化：任务、会话、审计和审批，不保存知识正文或向量。"""

from .factory import build_audit_sink, build_state_store
from .postgres import check_postgres

__all__ = ["build_audit_sink", "build_state_store", "check_postgres"]
