"""唯一网关中的业务 Agent HTTP 路由装配。"""

from fastapi import APIRouter

from runtime.container import GatewayRuntime


def build_agent_router(runtime: GatewayRuntime, resolve_effective_identity=None) -> APIRouter:
    """把进程内 Agent 的 HTTP 适配器集中挂载到网关。

    这个模块只负责路由装配；业务 Agent 不创建 FastAPI 应用，也不感知
    HTTP 请求对象。
    """

    # 路由适配器只负责 HTTP；Agent 实例仍由 runtime/bootstrap 统一装配。
    from agents.perception_agent.routes import app as perception_router
    from agents.perception_agent.routes import configure_identity_resolver
    from agents.retrieval_agent.routes import app as retrieval_router

    router = APIRouter()
    if resolve_effective_identity is not None:
        configure_identity_resolver(resolve_effective_identity)
    router.include_router(perception_router)
    router.include_router(retrieval_router)
    return router
