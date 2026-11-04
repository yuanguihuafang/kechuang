"""Docker-backed sandbox lifecycle for the Compose deployment.

The provisioner owns the Docker socket; the Gateway never receives it.  Sandbox
containers live on a dedicated network and are exposed to the Gateway through a
host-gateway port, keeping them off the application network.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path

import docker
from docker.errors import APIError, NotFound

logger = logging.getLogger(__name__)

SANDBOX_PORT = int(os.environ.get("SANDBOX_CONTAINER_PORT", "8080"))
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "")
SANDBOX_NETWORK = os.environ.get("SANDBOX_NETWORK", "deer-flow-sandbox")
SANDBOX_HOST = os.environ.get("SANDBOX_HOST", "host.docker.internal")
SANDBOX_BIND_HOST = os.environ.get("SANDBOX_BIND_HOST", "172.17.0.1")
CONTAINER_PREFIX = os.environ.get("SANDBOX_CONTAINER_PREFIX", "deer-flow-sandbox")
BASE_DIR = os.environ.get("DEER_FLOW_HOST_BASE_DIR", "/.deer-flow")
SANDBOX_CAPABILITIES = [
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "SETGID",
    "SETUID",
    "NET_BIND_SERVICE",
    "SYS_CHROOT",
    "MKNOD",
    "SETFCAP",
]


def _name(sandbox_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", sandbox_id).strip("-.")[:70]
    return f"{CONTAINER_PREFIX}-{safe or 'sandbox'}"


def _client():
    return docker.from_env(timeout=60)


def ensure_network() -> None:
    client = _client()
    try:
        client.networks.get(SANDBOX_NETWORK)
    except NotFound:
        client.networks.create(SANDBOX_NETWORK, driver="bridge")


def _common_volume_specs(user_id: str) -> dict[str, dict[str, str]]:
    specs: dict[str, dict[str, str]] = {}
    base = Path(BASE_DIR)
    for host, target in (
        (base / "skills_view" / "public", "/mnt/skills/public"),
        (base / "users" / user_id / "skills_view" / "custom", "/mnt/skills/custom"),
        (base / "users" / user_id / "skills_view" / "legacy", "/mnt/skills/legacy"),
    ):
        if host.exists():
            specs[str(host)] = {"bind": target, "mode": "ro"}
    return specs


def _inspect(name: str) -> dict | None:
    try:
        return _client().containers.get(name).attrs
    except NotFound:
        return None
    except APIError:
        return None


def _response(sandbox_id: str, data: dict) -> dict:
    port = ((data.get("NetworkSettings") or {}).get("Ports") or {}).get(f"{SANDBOX_PORT}/tcp") or []
    host_port = int(port[0]["HostPort"]) if port and port[0].get("HostPort") else None
    state = (data.get("State") or {}).get("Status", "unknown")
    if host_port is None:
        raise RuntimeError(f"sandbox {sandbox_id} has no published port")
    return {"sandbox_id": sandbox_id, "sandbox_url": f"http://{SANDBOX_HOST}:{host_port}", "status": state}


def _wait_for_published_port(name: str, sandbox_id: str, timeout: float = 15.0) -> dict:
    """Refresh Docker metadata until the daemon reports the allocated port."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = _inspect(name)
        if data:
            ports = ((data.get("NetworkSettings") or {}).get("Ports") or {}).get(f"{SANDBOX_PORT}/tcp") or []
            if ports and ports[0].get("HostPort"):
                return data
        time.sleep(0.25)
    raise RuntimeError(f"sandbox {sandbox_id} did not receive a published port in time")


def create(sandbox_id: str, thread_id: str, user_id: str, extra_mounts: list, **_: object) -> dict:
    ensure_network()
    name = _name(sandbox_id)
    existing = _inspect(name)
    if existing and (existing.get("State") or {}).get("Running"):
        return _response(sandbox_id, _wait_for_published_port(name, sandbox_id))
    if existing:
        try:
            _client().containers.get(name).remove(force=True)
        except (NotFound, APIError):
            pass

    try:
        client = _client()
        volumes = _common_volume_specs(user_id)
        volumes.update({m.host_path: {"bind": m.container_path, "mode": "ro" if m.read_only else "rw"} for m in extra_mounts or []})
        client.containers.run(
            SANDBOX_IMAGE,
            detach=True,
            name=name,
            network=SANDBOX_NETWORK,
            ports={f"{SANDBOX_PORT}/tcp": (SANDBOX_BIND_HOST, 0)},
            labels={
                "deer-flow.managed": "true",
                "deer-flow.sandbox-id": sandbox_id,
                "deer-flow.thread-id": thread_id,
                "deer-flow.user-id": user_id,
            },
            cap_drop=["ALL"],
            cap_add=SANDBOX_CAPABILITIES,
            security_opt=["no-new-privileges:true"],
            pids_limit=int(os.environ.get("SANDBOX_PIDS_LIMIT", "512")),
            mem_limit=os.environ.get("SANDBOX_MEMORY_LIMIT", "1g"),
            command=shlex.split(os.environ["SANDBOX_COMMAND"]) if os.environ.get("SANDBOX_COMMAND") else None,
            volumes=volumes,
        )
    except (APIError, docker.errors.DockerException) as exc:
        raise RuntimeError(f"sandbox container creation failed: {exc}") from exc
    data = _wait_for_published_port(name, sandbox_id)
    return _response(sandbox_id, data)


def destroy(sandbox_id: str) -> None:
    name = _name(sandbox_id)
    try:
        _client().containers.get(name).remove(force=True)
    except NotFound:
        pass


def get(sandbox_id: str) -> dict | None:
    data = _inspect(_name(sandbox_id))
    if not data or not (data.get("State") or {}).get("Running"):
        return None
    return _response(sandbox_id, data)


def list_all() -> list[dict]:
    containers = _client().containers.list(filters={"label": "deer-flow.managed=true"})
    items: list[dict] = []
    for container in containers:
        data = container.attrs
        labels = data.get("Config", {}).get("Labels", {})
        sid = labels.get("deer-flow.sandbox-id")
        if sid and (item := _response(sid, data)):
            items.append(item)
    return items
