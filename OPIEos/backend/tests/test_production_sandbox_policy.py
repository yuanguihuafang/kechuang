from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_production_config_uses_remote_sandbox_and_disables_host_bash() -> None:
    config = yaml.safe_load((ROOT / "config.production.example.yaml").read_text())
    sandbox = config["sandbox"]
    assert sandbox["use"] == "deerflow.community.aio_sandbox:AioSandboxProvider"
    assert sandbox["provisioner_url"] == "http://provisioner:8002"
    assert sandbox["provisioner_api_key"] == "$PROVISIONER_API_KEY"
    assert sandbox["thread_data_mounts"] is False
    assert sandbox["allow_host_bash"] is False


def test_base_compose_does_not_mount_docker_socket() -> None:
    compose = (ROOT / "docker/docker-compose.yaml").read_text()
    assert "/var/run/docker.sock:/var/run/docker.sock" not in compose
    assert "PROVISIONER_API_KEY=${PROVISIONER_API_KEY:?PROVISIONER_API_KEY is required}" in compose
    assert "SANDBOX_IMAGE=${SANDBOX_IMAGE:?SANDBOX_IMAGE is required}" in compose


def test_docker_socket_is_explicitly_opt_in_overlay_only() -> None:
    overlay = (ROOT / "docker/docker-compose.dood.yaml").read_text()
    assert "/var/run/docker.sock" in overlay
    assert "OPT-IN" in overlay


def test_provisioned_sandbox_has_no_privilege_escalation_or_host_namespaces() -> None:
    provisioner = (ROOT / "docker/provisioner/app.py").read_text()
    assert "allow_privilege_escalation=False" in provisioner
    assert "run_as_non_root=True" in provisioner
    assert "host_network=False" in provisioner
    assert "host_pid=False" in provisioner
    assert "host_ipc=False" in provisioner
    assert "automount_service_account_token=False" in provisioner
