import asyncio
from types import SimpleNamespace

import pytest

from admin.service import AdminService


def _service(*, count, checkpoint=None):
    created = []
    launched = []
    tasks = SimpleNamespace(
        list=lambda **_: [],
        list_page=lambda **_: {"items": [], "total_pages": 0},
        create=lambda **fields: created.append(fields) or {"task_id": "new"},
    )

    async def ready():
        return None

    runtime = SimpleNamespace(
        task_store=tasks,
        sync_state_store=SimpleNamespace(get=lambda *_: checkpoint),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={
                    "demand": SimpleNamespace(count_by_mysql_records=count, check_ready=ready),
                }
            ),
        ),
    )
    service = AdminService(runtime)
    service._launch_mysql_sync = lambda *args: launched.append(args)
    return service, created, launched


def test_count_failure_rejects_sync_without_creating_task():
    def fail(_):
        raise RuntimeError("querynode unavailable")

    service, created, launched = _service(count=fail)
    with pytest.raises(RuntimeError, match="querynode unavailable"):
        asyncio.run(
            service.start_mysql_sync(SimpleNamespace(knowledge_type="demand", batch_size=64))
        )
    assert created == launched == []


def test_missing_or_unloaded_collection_rejects_sync_before_counting():
    counts = []
    service, created, launched = _service(count=lambda _: counts.append("count") or 0)

    async def not_ready():
        raise RuntimeError("Collection Loading")

    service.runtime.embedded_retrieval.agent.public_milvus["demand"].check_ready = not_ready
    with pytest.raises(RuntimeError, match="Collection Loading"):
        asyncio.run(
            service.start_mysql_sync(SimpleNamespace(knowledge_type="demand", batch_size=64))
        )
    assert created == launched == counts == []


@pytest.mark.parametrize("actual, recorded", [(0, 0), (2, 10)])
def test_older_vector_data_rejects_newer_checkpoint(actual, recorded):
    service, created, launched = _service(
        count=lambda _: actual,
        checkpoint={
            "status": "SUCCESS",
            "last_mysql_id": 99,
            "processed_count": 10,
            "indexed_count": recorded,
        },
    )
    with pytest.raises(RuntimeError, match="断点"):
        asyncio.run(
            service.start_mysql_sync(SimpleNamespace(knowledge_type="demand", batch_size=64))
        )
    assert created == launched == []
