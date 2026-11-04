from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "docker" / "provisioner" / "docker_backend.py"


def test_docker_backend_uses_dedicated_network_and_hardened_container_defaults():
    source = MODULE.read_text()
    assert "network=SANDBOX_NETWORK" in source
    assert 'cap_drop=["ALL"]' in source
    assert "cap_add=SANDBOX_CAPABILITIES" in source
    assert 'security_opt=["no-new-privileges:true"]' in source
    assert '"deer-flow.managed": "true"' in source
    assert 'ports={f"{SANDBOX_PORT}/tcp": (SANDBOX_BIND_HOST, 0)}' in source


def test_production_overlay_mounts_socket_only_on_provisioner():
    source = (ROOT / "docker" / "docker-compose.sandbox-docker.yaml").read_text()
    assert "/var/run/docker.sock:/var/run/docker.sock" in source
    assert "gateway:" not in source
    assert "PROVISIONER_BACKEND: docker" in source
