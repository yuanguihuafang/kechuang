"""管理员应用层。管理员通过主进程内服务调用 Agent 和平台能力。"""

from typing import Any

__all__ = ["AdminService"]


def __getattr__(name: str) -> Any:
    if name == "AdminService":
        from .service import AdminService

        return AdminService
    raise AttributeError(name)
