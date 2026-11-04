"""科创后端不再携带第二套问答执行图。"""

from pathlib import Path
import ast


def test_retired_chat_agents_are_absent():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "agents/intent_agent",
        "agents/supervisor_agent",
        "agents/reasoning_agent",
        "runtime/agent_registry",
        "core/prompts/intent",
    ):
        assert not list((root / relative).rglob("*.py")), relative
    for relative in (
        "core/prompts/supervisor.py",
        "core/prompts/planning.py",
        "core/prompts/reasoning.py",
    ):
        assert not (root / relative).exists(), relative


def test_retired_model_roles_and_failover_are_absent():
    from runtime.config import Settings

    fields = Settings.model_fields
    assert not any("fallback" in name for name in fields)
    assert not any(
        name.startswith(("llm_intent_", "llm_planning_")) for name in fields
    )
    assert "llm_reasoning_model" in fields
    assert not (Path(__file__).resolve().parents[1] / "services/llm/fallback_client.py").exists()


def test_reasoning_role_uses_dedicated_model_and_falls_back_to_default():
    from runtime.config import Settings, settings_for_role

    settings = Settings(llm_model="default-model", llm_reasoning_model="reasoning-model")
    assert settings.model_for_role("reasoning") == "reasoning-model"
    assert settings_for_role(settings, "reasoning").llm_model == "reasoning-model"
    assert Settings(llm_model="default-model").model_for_role("reasoning") == "default-model"


def test_retired_memory_model_is_absent():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "services/memory/memory_ai.py").exists()
    assert not (root / "core/prompts/memory.py").exists()


def test_retired_supervisor_tool_contracts_are_absent():
    from core.contracts import tools
    from runtime.tool_adapters.adapters import InProcessToolAdapter

    assert not hasattr(tools, "ToolName")
    assert not hasattr(tools, "ConversationArguments")
    assert not hasattr(InProcessToolAdapter, "request_execution")
    root = Path(__file__).resolve().parents[1]
    assert not (root / "core/governance/output_validation.py").exists()


def test_admin_dtos_do_not_depend_on_assistant_http():
    root = Path(__file__).resolve().parents[1]
    forbidden = []
    for path in (root / "admin").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ImportFrom) and str(node.module).startswith(("api.v1", "assistant.api")):
                forbidden.append((path.name, node.module))
    assert forbidden == []


def test_admin_unpublish_registered_only_by_admin():
    from admin.api.v1.admin import build_admin_router
    from assistant.api.v1.drafts.routes import build_draft_router
    from runtime.container import GatewayRuntime
    runtime = GatewayRuntime(embedded_perception=None, embedded_retrieval=None)
    path = "/api/v1/admin/drafts/{draft_id}/unpublish"
    admin = build_admin_router(runtime)
    user = build_draft_router(runtime, lambda user_id: (user_id, "external", None))
    assert sum(route.path == path for route in admin.routes) == 1
    assert not any(route.path.startswith("/api/v1/admin/") for route in user.routes)


def test_module_import_direction():
    root = Path(__file__).resolve().parents[1]
    assert (root / "assistant/api/v1/router.py").exists()
    forbidden = []
    for directory in ("admin", "assistant", "services"):
        for path in (root / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.ImportFrom):
                    module = str(node.module or "")
                    if (directory == "admin" and module.startswith("assistant")) or (
                        directory == "assistant" and module.startswith("admin")) or (
                        directory == "services" and module.startswith(("admin", "assistant", "api"))):
                        forbidden.append((str(path.relative_to(root)), module))
    assert forbidden == []


def test_application_and_integration_do_not_depend_on_http_modules():
    root = Path(__file__).resolve().parents[1]
    imports = []
    for directory in ("assistant/application", "assistant/integrations"):
        for path in (root / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.ImportFrom) and str(node.module).startswith(("api.", "assistant.api.", "admin.api.")):
                    imports.append((path.name, node.module))
    assert imports == []


def test_missing_admin_is_explicitly_unavailable():
    import pytest
    from fastapi import HTTPException
    from runtime.container import GatewayRuntime
    runtime = GatewayRuntime(None, None)
    with pytest.raises(HTTPException) as error:
        runtime.require_admin()
    assert error.value.status_code == 503


def test_module_route_inventory_unchanged(monkeypatch):
    import json
    from fastapi import FastAPI
    import agents.perception_agent.routes as perception
    from assistant.api.v1.router import build_router
    from admin.api.v1.admin import build_admin_router
    from runtime.container import GatewayRuntime
    monkeypatch.setattr(perception, "configure_identity_resolver", lambda _: None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ENABLE_TEST_ENDPOINTS", raising=False)
    app = FastAPI()
    runtime = GatewayRuntime(None, None)
    app.include_router(build_router(runtime))
    app.include_router(build_admin_router(runtime))
    actual = sorted([method.upper(), path] for path, methods in app.openapi()["paths"].items() for method in methods)
    expected = json.loads((Path(__file__).parent / "http_routes_snapshot.json").read_text())
    assert actual == expected


def test_admin_uses_shared_dependencies_not_agent_routes():
    root = Path(__file__).resolve().parents[1]
    imports = []
    for path in (root / "admin").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ImportFrom) and str(node.module).endswith(".routes"):
                imports.append(node.module)
    assert imports == []
    from types import SimpleNamespace
    from admin.service import AdminService
    session_store = object()
    runtime = SimpleNamespace(task_store=None, session_store=session_store)
    assert AdminService(runtime, session_store=session_store)._session_store() is session_store
