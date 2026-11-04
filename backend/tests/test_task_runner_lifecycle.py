import asyncio

from runtime.workflows.task_runner import SupervisorTaskRunner


class _TaskStore:
    def __init__(self, task_ids):
        self.items = {
            task_id: {"task_id": task_id, "operation": "demo", "status": "QUEUED"}
            for task_id in task_ids
        }
        self.claimed_leases = []

    def requeue_expired(self):
        return 0

    def claim_next(self, **_kwargs):
        item = next((item for item in self.items.values() if item["status"] == "QUEUED"), None)
        if item is None:
            return None
        self.claimed_leases.append(_kwargs["lease_seconds"])
        item.update(status="RUNNING", worker_id=_kwargs["worker_id"], lease_until=1)
        return dict(item)

    def update(self, task_id, **fields):
        self.items[task_id].update(fields)
        return dict(self.items[task_id])

    def interrupt_claim(self, task_id, *, worker_id):
        item = self.items[task_id]
        if item.get("worker_id") != worker_id or item.get("status") not in {
            "RUNNING", "PARSING", "INDEXING"
        }:
            return False
        item.update(status="INTERRUPTED", worker_id=None, lease_until=None)
        return True

    def renew_lease(self, **_kwargs):
        return True


def test_poll_removes_each_completed_task_from_running():
    async def scenario():
        runner = SupervisorTaskRunner(_TaskStore(["task-1", "task-2"]), max_concurrency=2)
        runner.register("demo", lambda _item: _completed())
        runner.start(poll_seconds=0.1)
        await asyncio.sleep(0.05)

        assert runner._running == {}
        await runner.stop()

    async def _completed():
        return {"status": "SUCCESS"}

    asyncio.run(scenario())


def test_async_parse_wrapper_returns_terminal_task_record(monkeypatch):
    from types import SimpleNamespace

    from agents.perception_agent import routes

    class TaskStore:
        def __init__(self):
            self.item = {"task_id": "task-parse"}

        def update(self, task_id, **fields):
            self.item.update(fields)
            return dict(self.item)

    store = TaskStore()
    monkeypatch.setattr(routes, "task_store", store)

    async def action():
        return SimpleNamespace(status="SUCCESS", source_name="file.pdf", error=None)

    result = asyncio.run(routes._run_async_parse("task-parse", "parse", action()))

    assert result["status"] == "SUCCEEDED"
    assert store.item["status"] == "SUCCEEDED"


def test_async_index_wrapper_returns_terminal_task_record(monkeypatch):
    from agents.retrieval_agent import routes
    from agents.retrieval_agent.schemas import IndexResult

    class TaskStore:
        def __init__(self):
            self.item = {"task_id": "task-index"}

        def update(self, task_id, **fields):
            self.item.update(fields)
            return dict(self.item)

        def complete(self, task_id, **fields):
            return self.update(task_id, **fields)

    store = TaskStore()
    monkeypatch.setattr(routes, "task_store", store)

    async def action():
        return IndexResult(status="SUCCESS", document_id="doc-1", indexed_count=1)

    result = asyncio.run(routes._run_async_index("task-index", "index", action()))

    assert result["status"] == "SUCCEEDED"
    assert store.item["status"] == "SUCCEEDED"


def test_active_workflow_status_keeps_claim_renewable_and_recoverable():
    from runtime.workflows.task_store import SupervisorTaskStore

    store = SupervisorTaskStore()
    task = store.create(operation="parse", status="QUEUED")
    claimed = store.claim(task["task_id"], worker_id="worker-1", lease_seconds=60)
    assert claimed is not None
    store.update(task["task_id"], status="PARSING")

    assert store.renew_lease(task["task_id"], worker_id="worker-1", lease_seconds=60)
    store.update(task["task_id"], lease_until=0)
    assert store.requeue_expired() == 1
    assert store.get(task["task_id"])["status"] == "QUEUED"


def test_postgres_task_status_column_is_authoritative_for_claim_lifecycle():
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine
    from services.persistence.repositories.runtime_repository import PostgresStateStore
    from services.persistence.sqlalchemy import metadata

    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    store = PostgresStateStore(service="supervisor", engine=engine)
    task_id = store.create(operation="parse", status="QUEUED", state={"status": "QUEUED"})
    assert store.claim(task_id, worker_id="worker-1", lease_seconds=60) is not None
    store.update(task_id, status="PARSING")

    assert store.get(task_id)["status"] == "PARSING"
    assert store.renew_lease(task_id, worker_id="worker-1", lease_seconds=60)
    store.update(task_id, lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert store.requeue_expired() == 1
    assert store.get(task_id)["status"] == "QUEUED"


def test_stop_cancels_and_waits_for_running_tasks():
    async def scenario():
        started = asyncio.Event()
        finished = asyncio.Event()
        never = asyncio.Event()

        async def work(_item):
            started.set()
            try:
                await never.wait()
            finally:
                finished.set()
            return {"status": "SUCCESS"}

        runner = SupervisorTaskRunner(_TaskStore(["task-1"]))
        runner.register("demo", work)
        runner.start(poll_seconds=0.1)
        await asyncio.wait_for(started.wait(), timeout=1)

        await runner.stop()

        assert finished.is_set()
        assert runner._running == {}
        assert runner._worker is None
        assert runner.task_store.items["task-1"]["status"] == "INTERRUPTED"
        assert runner.task_store.items["task-1"]["lease_until"] is None
        assert runner.task_store.items["task-1"]["worker_id"] is None
        assert runner.task_store.claimed_leases == [60]

    asyncio.run(scenario())


def test_partial_startup_closes_created_resources_once(monkeypatch):
    import pytest
    from types import SimpleNamespace
    import runtime.bootstrap as bootstrap
    from runtime.container import GatewayRuntime
    calls = []

    class Checkpointer:
        async def start(self):
            raise RuntimeError("startup interrupted")

        async def close(self):
            calls.append("checkpointer")

    monkeypatch.setattr(bootstrap, "check_schema_version", lambda: None)
    monkeypatch.setattr(bootstrap, "get_settings", lambda: SimpleNamespace(deerflow_enabled=False))
    monkeypatch.setattr(bootstrap, "PersistentCheckpointer", Checkpointer)
    monkeypatch.setattr(bootstrap, "close_postgres_pools", lambda: calls.append("pools"))
    runtime = GatewayRuntime(None, SimpleNamespace(close=lambda: calls.append("retrieval")))

    async def run():
        with pytest.raises(RuntimeError, match="startup interrupted"):
            async with bootstrap.lifespan(runtime):
                raise AssertionError("startup must not succeed")

    asyncio.run(run())
    assert sorted(calls) == ["checkpointer", "pools", "retrieval"]
    assert runtime.background_tasks == set()


def test_runner_stop_failure_still_waits_for_tasks_and_clears_runtime():
    import pytest
    from types import SimpleNamespace
    from runtime.bootstrap import _stop_runtime
    from runtime.container import GatewayRuntime

    async def run():
        started = asyncio.Event()
        finished = []

        async def background():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(True)

        async def fail():
            raise RuntimeError("runner shutdown failed")

        runtime = GatewayRuntime(None, None)
        runtime.task_runner = SimpleNamespace(stop=fail)
        runtime.admin_service = object()
        runtime.background_tasks.add(asyncio.create_task(background()))
        await started.wait()
        with pytest.raises(RuntimeError, match="runner shutdown failed"):
            await _stop_runtime(runtime)
        assert finished == [True]
        assert runtime.background_tasks == set()
        assert runtime.admin_service is None
        assert runtime.task_runner is None

    asyncio.run(run())


def test_runtime_shutdown_signals_and_waits_for_to_thread_worker():
    import threading
    from types import SimpleNamespace
    from runtime.bootstrap import _stop_runtime
    from runtime.container import GatewayRuntime

    async def run():
        stop_event = threading.Event()
        thread_finished = threading.Event()

        def worker():
            assert stop_event.wait(timeout=2)
            thread_finished.set()

        runtime = GatewayRuntime(None, None)
        task = asyncio.create_task(asyncio.to_thread(worker))
        runtime.background_tasks.add(task)
        runtime.background_stop_events[task] = stop_event
        await _stop_runtime(runtime)

        assert stop_event.is_set()
        assert thread_finished.is_set()
        assert task.done()
        assert runtime.background_stop_events == {}

    asyncio.run(run())


def test_runtime_shutdown_waits_for_thread_after_to_thread_wrapper_is_cancelled():
    import threading
    from runtime.bootstrap import _stop_runtime
    from runtime.container import GatewayRuntime

    async def run():
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def worker():
            started.set()
            release.wait()
            finished.set()

        runtime = GatewayRuntime(None, None)
        task = asyncio.create_task(asyncio.to_thread(worker))
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        threading.Timer(0.05, release.set).start()

        await _stop_runtime(runtime)

        assert finished.is_set()

    asyncio.run(run())


def test_schema_check_failure_closes_database_pools(monkeypatch):
    import pytest
    from types import SimpleNamespace
    import runtime.bootstrap as bootstrap
    from runtime.container import GatewayRuntime
    closed = []

    def check():
        raise RuntimeError("schema mismatch")

    monkeypatch.setattr(bootstrap, "check_schema_version", check)
    monkeypatch.setattr(bootstrap, "close_postgres_pools", lambda: closed.append("pools"))
    runtime = GatewayRuntime(None, SimpleNamespace(close=lambda: closed.append("retrieval")))

    async def run():
        with pytest.raises(RuntimeError, match="schema mismatch"):
            async with bootstrap.lifespan(runtime):
                pytest.fail("startup must not continue")

    asyncio.run(run())
    assert sorted(closed) == ["pools", "retrieval"]
