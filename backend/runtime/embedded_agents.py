"""单进程模式加载两个 Agent，并把原有路由挂到统一端口。"""

from __future__ import annotations

from runtime.contracts import GatewayAgentEndpoint, RetrievalAgentEndpoint


def load_embedded_agents() -> tuple[GatewayAgentEndpoint, RetrievalAgentEndpoint]:
    """加载两个进程内 Agent 的网关端点。"""

    from agents.perception_agent import routes as perception
    from agents.retrieval_agent import routes as retrieval

    return perception, retrieval


def configure_task_store(
    agents: tuple[GatewayAgentEndpoint, GatewayAgentEndpoint], store: object
) -> None:
    """把网关唯一任务仓储注入两个进程内 Agent。"""
    for agent in agents:
        agent.configure_task_store(store)


def configure_task_runner(
    agents: tuple[GatewayAgentEndpoint, GatewayAgentEndpoint], runner: object
) -> None:
    """把统一任务 Runner 注入两个进程内 Agent。"""
    for agent in agents:
        agent.configure_task_runner(runner)
