"""Run backend tests without database/network access (MockTransport and SQLite work)."""

import socket
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from sqlalchemy.engine import Engine


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    local = threading.local()
    engine_connect = Engine.connect
    socket_connect = socket.socket.connect
    socketpair = socket.socketpair

    def denied(*_args, **_kwargs):
        raise RuntimeError("offline test: external I/O blocked")

    async def async_denied(*_args, **_kwargs):
        denied()

    def connect(engine):
        return engine_connect(engine) if engine.dialect.name == "sqlite" else denied()

    def pair(*args, **kwargs):
        # Windows 上的 asyncio 在内部使用 TCP socketpair；这不属于服务 I/O。
        local.socketpair = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            local.socketpair = False

    def socket_connect_guard(sock, *args, **kwargs):
        if getattr(local, "socketpair", False):
            return socket_connect(sock, *args, **kwargs)
        return denied()

    with (
        patch.object(Engine, "connect", connect),
        patch.object(socket, "socketpair", pair),
        patch.object(socket.socket, "connect", socket_connect_guard),
        patch.object(socket.socket, "connect_ex", denied),
        patch.object(psycopg.Connection, "connect", denied),
        patch.object(psycopg.AsyncConnection, "connect", async_denied),
    ):
        return pytest.main(
            sys.argv[1:]
            or ["tests", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "--tb=short"]
        )


if __name__ == "__main__":
    raise SystemExit(main())
