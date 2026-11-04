"""LangGraph PostgreSQL 持久化 Checkpointer。"""

from __future__ import annotations

import math
import os

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class PersistentCheckpointer:
    """管理一个网关进程共享的 PostgreSQL LangGraph Checkpointer。"""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL", "")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL 未配置，无法启用持久化 LangGraph Checkpointer")
        self._pool: AsyncConnectionPool | None = None
        self.checkpointer: AsyncPostgresSaver | None = None
        self.serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("core.contracts.agent", "AgentContext"),
            ]
        )

    async def start(self) -> AsyncPostgresSaver:
        min_size = int(os.getenv("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE", "1"))
        max_size = int(os.getenv("LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE", "4"))
        timeout = float(os.getenv("LANGGRAPH_CHECKPOINT_POOL_TIMEOUT_SECONDS", "10"))
        if min_size < 1 or max_size < min_size or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(
                "LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE must be >= 1, "
                "LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE must be >= min_size, "
                "and LANGGRAPH_CHECKPOINT_POOL_TIMEOUT_SECONDS must be > 0"
            )
        pool = AsyncConnectionPool(
            conninfo=self.database_url,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
        )
        self._pool = pool
        try:
            await pool.open(wait=True)
            self.checkpointer = AsyncPostgresSaver(pool, serde=self.serde)
            await self.checkpointer.setup()
            return self.checkpointer
        except Exception:
            await pool.close()
            self._pool = None
            self.checkpointer = None
            raise

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self.checkpointer = None
