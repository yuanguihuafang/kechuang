"""管理员 API 专用请求模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from core.contracts.workflows import SupervisorTaskResponse


class AdminRetentionRunRequest(BaseModel):
    """管理员确认清理过期运行状态。"""

    operator_id: str = Field(min_length=1, max_length=64)
    confirm: bool = False


class AdminRuntimeStartRequest(BaseModel):
    """管理员确认开始累计正式运行时长。"""

    operator_id: str = Field(default="admin", min_length=1, max_length=64)
    confirm: bool = False


class AdminMysqlSyncRequest(BaseModel):
    """从现有 MySQL 业务表重建公开向量索引；不写回 MySQL。"""

    knowledge_type: Literal["enterprise", "demand", "achievement", "expert", "solution"]
    batch_size: int = Field(default=200, ge=1, le=200)


class AdminCollectionRestoreRequest(BaseModel):
    """管理员确认使用同一份预检缺失清单执行影子库恢复。"""

    confirm: bool = False
    preflight_id: str = Field(min_length=1, max_length=128)
    accepted_missing_fingerprint: str = Field(min_length=64, max_length=64)


class AdminCollectionActivateRequest(BaseModel):
    """管理员确认切换已校验的影子 Collection。"""

    confirm: bool = False
    task_id: str = Field(min_length=1, max_length=128)


class AdminKnowledgeUpdateConfirmRequest(BaseModel):
    """管理员确认已发布知识记录的修改。"""

    operator_id: str = Field(default="admin", min_length=1, max_length=64)
    drafts: list[dict[str, object]] = Field(default_factory=list, max_length=50)

class SupervisorTaskPageResponse(BaseModel):
    """管理员任务列表分页结果。"""

    items: list[SupervisorTaskResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

class SupervisorRecoveryRequest(BaseModel):
    """人工接管后恢复一次失败的 Supervisor 工具调用。"""

    operator_id: str = Field(min_length=1, max_length=64)
    decision_note: str | None = Field(default=None, max_length=2000)
