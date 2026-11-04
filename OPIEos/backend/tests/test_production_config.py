from pathlib import Path

import yaml

from deerflow.config.app_config import AppConfig

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker" / "docker-compose.yaml"
PRODUCTION_CONFIG_PATH = ROOT / "config.production.example.yaml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_production_compose_contains_private_postgres_and_redis_services() -> None:
    compose = _compose()
    services = compose["services"]

    assert services["postgres"]["image"].startswith("postgres:")
    assert services["redis"]["image"].startswith("redis:")
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
    assert "postgres-data" in compose["volumes"]
    assert "redis-data" in compose["volumes"]
    assert "REDIS_PASSWORD" in services["redis"]["environment"]
    assert "requirepass" in str(services["redis"]["command"])


def test_gateway_uses_internal_postgres_and_redis_dependencies() -> None:
    compose = _compose()
    gateway = compose["services"]["gateway"]
    environment = gateway["environment"]
    depends_on = gateway["depends_on"]

    assert any(item.startswith("DATABASE_URL=") for item in environment)
    assert any(item.startswith("DEER_FLOW_STREAM_BRIDGE_REDIS_URL=") for item in environment)
    assert any(item.startswith("GATEWAY_WORKERS=") for item in environment)
    assert depends_on["postgres"]["condition"] == "service_healthy"
    assert depends_on["redis"]["condition"] == "service_healthy"
    assert depends_on["crawl4ai"]["condition"] == "service_healthy"


def test_production_compose_runs_private_crawl4ai_service() -> None:
    compose = _compose()
    crawl4ai = compose["services"]["crawl4ai"]

    assert crawl4ai["image"] == "unclecode/crawl4ai:0.9.2"
    assert "ports" not in crawl4ai
    assert crawl4ai["shm_size"] == "1gb"
    assert "CRAWL4AI_API_TOKEN" in crawl4ai["environment"]


def test_gateway_extensions_config_mount_is_writable_for_runtime_updates() -> None:
    compose = _compose()
    gateway = compose["services"]["gateway"]
    mounts = gateway["volumes"]

    extension_mount = next(mount for mount in mounts if "extensions-config" in mount)
    assert not extension_mount.endswith(":ro")


def test_production_config_selects_postgres_db_persistent_events_and_redis_bridge() -> None:
    config = yaml.safe_load(PRODUCTION_CONFIG_PATH.read_text())

    assert config["database"]["backend"] == "postgres"
    assert config["database"]["postgres_url"] == "$DATABASE_URL"
    assert config["run_events"]["backend"] == "db"
    assert config["stream_bridge"]["type"] == "redis"
    assert config["stream_bridge"]["redis_url"] == "$DEER_FLOW_STREAM_BRIDGE_REDIS_URL"


def test_production_config_validates_against_app_config_schema() -> None:
    config = yaml.safe_load(PRODUCTION_CONFIG_PATH.read_text())

    parsed = AppConfig.model_validate(config)

    assert parsed.database.backend == "postgres"
    assert parsed.run_events.backend == "db"
    assert parsed.stream_bridge is not None
    assert parsed.stream_bridge.type == "redis"


def test_production_config_enables_keyless_web_tools() -> None:
    config = yaml.safe_load(PRODUCTION_CONFIG_PATH.read_text())
    tools = {entry["name"]: entry for entry in config["tools"]}

    assert tools["web_search"]["use"] == "deerflow.community.ddg_search.tools:web_search_tool"
    assert tools["image_search"]["use"] == "deerflow.community.image_search.tools:image_search_tool"


def test_production_config_enables_sandbox_file_tools_and_run_guards() -> None:
    config = yaml.safe_load(PRODUCTION_CONFIG_PATH.read_text())
    tools = {entry["name"] for entry in config["tools"]}

    assert {"ls", "read_file", "glob", "grep", "write_file", "str_replace", "bash"} <= tools
    assert config["token_budget"]["enabled"] is True
    assert config["summarization"]["enabled"] is True
