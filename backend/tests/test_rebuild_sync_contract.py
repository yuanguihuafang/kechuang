import asyncio
import threading
from types import SimpleNamespace

from admin.service import AdminService
from agents.retrieval_agent.schemas import IndexResult
from core.contracts.knowledge import CleanedKnowledgeRecord
from services.vector.milvus.milvus_service import MilvusService


class _Tasks:
    def list(self, *, operation, limit):
        assert operation == "mysql_sync"
        assert limit == 500
        return []

    def list_page(self, **_):
        return {"items": [], "total_pages": 0}


def test_rebuild_runs_read_only_recovery_preflight(monkeypatch):
    calls = []

    target = SimpleNamespace(rebuild_collection=lambda: calls.append(("rebuild",)))
    runtime = SimpleNamespace(
        task_store=_Tasks(),
        sync_state_store=SimpleNamespace(reset=lambda *args: calls.append(("reset", *args))),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={"demand": target})),
    )

    from runtime.workflows.task_store import _TestTaskStore

    runtime.task_store = _TestTaskStore()

    async def prepare(*_):
        calls.append(("sources",))
        return [], [], []

    def count_mysql(knowledge_type):
        calls.append(("mysql", knowledge_type))
        return 0

    monkeypatch.setattr("admin.mysql_sync.prepare_collection_recovery", prepare)
    monkeypatch.setattr(
        "admin.mysql_sync.MySQLBusinessSearchService",
        lambda: SimpleNamespace(count_for_sync=count_mysql),
    )
    service = AdminService(runtime)
    runtime.background_tasks = set()
    runtime.track_background_thread = lambda task, _stop: runtime.background_tasks.add(task)

    async def scenario():
        result = await service.rebuild_collection("demand", confirm=True)
        await asyncio.gather(*runtime.background_tasks)
        return result

    result = asyncio.run(scenario())

    assert calls == [("sources",), ("mysql", "demand")]
    assert result["status"] == "PREFLIGHT_COMPLETE"
    assert not runtime.background_tasks
    assert result["mysql_record_count"] == 0
    assert result["recoverable_count"] == 0


def test_rebuild_mysql_data_keeps_non_mysql_vectors():
    calls = []

    target = SimpleNamespace(
        delete_by_chunk_role=lambda role: calls.append(("delete_role", role)),
        rebuild_collection=lambda: calls.append(("rebuild",)),
    )
    runtime = SimpleNamespace(
        task_store=_Tasks(),
        sync_state_store=SimpleNamespace(reset=lambda *args: calls.append(("reset", *args))),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={"demand": target})),
    )

    result = AdminService(runtime).rebuild_mysql_data("demand", confirm=True)

    assert calls[0][0] == "reset"
    assert calls[1] == ("delete_role", "mysql")
    assert ("rebuild",) not in calls
    assert result["status"] == "MYSQL_DATA_REBUILT"
    assert result["collection"] == "demand"


def test_rebuild_mysql_data_requires_confirmation():
    runtime = SimpleNamespace(
        task_store=_Tasks(),
        sync_state_store=None,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={"demand": object()})
        ),
    )

    try:
        AdminService(runtime).rebuild_mysql_data("demand", confirm=False)
    except ValueError as exc:
        assert "confirm=true" in str(exc)
    else:
        raise AssertionError("expected explicit confirmation")


def test_milvus_mysql_cleanup_filters_by_chunk_role():
    calls = []

    class Collection:
        def delete(self, expression):
            calls.append(expression)

        def flush(self):
            calls.append("flush")

    service = object.__new__(MilvusService)
    service._existing_collection = lambda: Collection()
    service.requires_user_id = False

    service.delete_by_chunk_role("mysql")

    assert calls == ['chunk_role == "mysql"', "flush"]


def test_delete_conversation_removes_all_business_draft_types_including_published():
    deleted = []
    deleted_session_ids = []
    draft_types = ("demand", "solution", "achievement", "customer_visit")
    drafts = [
        {
            "draft_id": f"{draft_type}-draft",
            "thread_id": "session-1",
            "draft_type": draft_type,
            "user_id": "user-1",
            "draft": {
                "draft_type": draft_type,
                "mysql_record_id": f"{draft_type}-1",
                "attachments": [],
            },
            "status": "PUBLISHED",
        }
        for draft_type in draft_types
    ]

    class SessionStore:
        def get(self, session_id):
            return {"session_id": session_id}

        def delete_session_data(self, session_id):
            deleted_session_ids.append(session_id)

    class DraftStore:
        def list_for_thread(self, thread_id):
            assert thread_id == "session-1"
            return drafts

        def list_business_references(self, *_args, **_kwargs):
            return []

    class Sink:
        def delete(self, **kwargs):
            deleted.append(kwargs)

    class VectorTarget:
        def delete_by_mysql_id(self, _value, **_kwargs):
            return None

        def delete_by_source_id(self, _value, **_kwargs):
            return None

    class Runtime:
        task_store = SimpleNamespace()
        sync_state_store = None
        conversation_service = SimpleNamespace(session_store=SessionStore())
        embedded_retrieval = SimpleNamespace(
            agent=SimpleNamespace(
                private_milvus=VectorTarget(),
                public_milvus={
                    draft_type: VectorTarget()
                    for draft_type in draft_types
                    if draft_type != "customer_visit"
                },
            )
        )
        business_record_sink = Sink()
        storage_service = None

        def require_draft_store(self):
            return DraftStore()

    runtime = Runtime()
    asyncio.run(AdminService(runtime, session_store=runtime.conversation_service.session_store).delete_conversation("session-1"))

    assert {(item["record_type"], item["record_id"]) for item in deleted} == {
        (draft_type, f"{draft_type}-1") for draft_type in draft_types
    }
    assert deleted_session_ids == ["session-1"]


def test_delete_conversation_continues_when_mysql_record_is_missing():
    deleted_vectors = []
    deleted_session_ids = []

    class SessionStore:
        def get(self, session_id):
            return {"session_id": session_id}

        def delete_session_data(self, session_id):
            deleted_session_ids.append(session_id)

    class DraftStore:
        def list_for_thread(self, thread_id):
            assert thread_id == "session-1"
            return [
                {
                    "draft_id": "demand-draft",
                    "thread_id": "session-1",
                    "draft_type": "demand",
                    "user_id": "user-1",
                    "draft": {
                        "draft_type": "demand",
                        "mysql_record_id": "missing-1",
                        "attachments": [],
                    },
                    "status": "DRAFT",
                }
            ]

        def list_business_references(self, *_args, **_kwargs):
            return []

    class Sink:
        def delete(self, **_kwargs):
            raise ValueError("MySQL 业务记录不存在或无权删除")

    class VectorTarget:
        def delete_by_mysql_id(self, value, **_kwargs):
            deleted_vectors.append(value)

    class Runtime:
        task_store = SimpleNamespace()
        sync_state_store = None
        conversation_service = SimpleNamespace(session_store=SessionStore())
        embedded_retrieval = SimpleNamespace(
            agent=SimpleNamespace(
                private_milvus=VectorTarget(),
                public_milvus={"demand": VectorTarget()},
            )
        )
        business_record_sink = Sink()
        storage_service = None

        def require_draft_store(self):
            return DraftStore()

    runtime = Runtime()
    result = asyncio.run(AdminService(runtime, session_store=runtime.conversation_service.session_store).delete_conversation("session-1"))

    assert result["status"] == "DELETED"
    assert result["mysql_count"] == 0
    assert result["milvus_count"] == 1
    assert deleted_vectors == ["missing-1"]
    assert deleted_session_ids == ["session-1"]


def test_reset_checkpoint_starts_a_full_sync_from_zero():
    created = {}

    class Tasks:
        def list(self, *, operation, limit):
            return [
                {"task_id": "old", "knowledge_type": "demand", "last_id": 99, "status": "SUCCESS"}
            ]

        def list_page(self, **_):
            return {"items": [], "total_pages": 0}

        def create(self, **fields):
            created.update(fields)
            return {"task_id": "new"}

    class Checkpoints:
        def get(self, _knowledge_type, _source_table):
            return {"status": "RESET", "last_mysql_id": 0, "processed_count": 0}

    async def ready():
        return None
    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=Checkpoints(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"demand": SimpleNamespace(count_by_mysql_records=lambda _prefix: 0, check_ready=ready)},
            )
        ),
    )
    service = AdminService(runtime)
    service._launch_mysql_sync = lambda *_args: None

    result = asyncio.run(
        service.start_mysql_sync(SimpleNamespace(knowledge_type="demand", batch_size=64))
    )

    assert result["task_id"] == "new"
    assert created["last_id"] == 0
    assert created["processed"] == 0
    assert created["replace_existing"] is True


def test_resumed_sync_replaces_only_its_current_batch_after_checkpoint(monkeypatch):
    import admin.mysql_sync as service_module

    class Source:
        def count_for_sync(self, _knowledge_type):
            return 2

        def records_for_sync(self, _knowledge_type, *, after_id, limit):
            if after_id != 1:
                return []
            return [CleanedKnowledgeRecord(mysql_id="2", record_id="2", text="第二条")]

        def list_for_sync(self, _knowledge_type, *, after_id, limit):
            return []

    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)
    state = {
        "task_id": "sync-2",
        "status": "RUNNING",
        "knowledge_type": "demand",
        "last_id": 1,
        "processed": 1,
        "indexed_count": 1,
        "replace_existing": True,
        "stop_requested": False,
    }
    seen = []

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    async def import_business_data(request):
        seen.append(request.replace_existing)
        return IndexResult(status="SUCCESS", document_id="mysql:demand", indexed_count=1)

    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=None,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"demand": SimpleNamespace(
                    published_draft_mysql_ids=lambda _ids: set(),
                    source_ids_by_mysql_ids=lambda _ids: {},
                    count_by_mysql_records=lambda _prefix: 2,
                )},
                import_business_data=import_business_data,
            )
        ),
    )

    asyncio.run(
        AdminService(runtime)._run_mysql_sync(
            "sync-2",
            SimpleNamespace(knowledge_type="demand", batch_size=64),
            stop_event=threading.Event(),
        )
    )

    assert seen == [True]
    assert state["status"] == "SUCCESS"


def test_failed_batch_persists_previous_successful_checkpoint(monkeypatch):
    import admin.mysql_sync as service_module

    class Source:
        def count_for_sync(self, _knowledge_type):
            return 2

        def records_for_sync(self, _knowledge_type, *, after_id, limit):
            if after_id != 1:
                return []
            return [CleanedKnowledgeRecord(mysql_id="2", record_id="2", text="失败批次")]

        def list_for_sync(self, _knowledge_type, *, after_id, limit):
            return []

    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)
    state = {
        "task_id": "sync-3",
        "status": "RUNNING",
        "knowledge_type": "demand",
        "last_id": 1,
        "processed": 1,
        "indexed_count": 1,
        "replace_existing": False,
        "stop_requested": False,
    }
    checkpoints = []

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    class Checkpoints:
        def save(self, *args, **kwargs):
            checkpoints.append((args, kwargs))

    async def import_business_data(_request):
        return IndexResult(status="FAILED", document_id="mysql:demand", error="Milvus unavailable")

    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=Checkpoints(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"demand": SimpleNamespace(
                    published_draft_mysql_ids=lambda _ids: set(),
                    source_ids_by_mysql_ids=lambda _ids: {},
                    count_by_mysql_records=lambda _prefix: 1,
                )},
                import_business_data=import_business_data,
            )
        ),
    )

    asyncio.run(
        AdminService(runtime)._run_mysql_sync(
            "sync-3",
            SimpleNamespace(knowledge_type="demand", batch_size=64),
            stop_event=threading.Event(),
        )
    )

    assert state["status"] == "FAILED"
    assert state["error"] == "Milvus unavailable"
    assert state["last_id"] == 1
    assert state["processed"] == 1
    assert checkpoints[-1][1]["last_mysql_id"] == 1
    assert checkpoints[-1][1]["status"] == "FAILED"


def test_collection_recovery_skips_mysql_rows_with_restored_published_drafts(monkeypatch):
    import admin.mysql_sync as service_module

    state = {
        "status": "QUEUED",
        "stop_requested": False,
        "processed": 0,
        "indexed_count": 0,
        "skipped_count": 0,
        "last_id": 0,
    }

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    class Source:
        def count_for_sync(self, _knowledge_type):
            return 2

        def records_for_sync(self, _knowledge_type, *, after_id, limit):
            if after_id:
                return []
            return [
                CleanedKnowledgeRecord(record_id="1", mysql_id="1", text="已恢复草稿"),
                CleanedKnowledgeRecord(record_id="2", mysql_id="2", text="仅 MySQL 记录"),
            ]

        def list_for_sync(self, _knowledge_type, *, after_id, limit):
            return []

    imported = []
    target = SimpleNamespace(
        published_draft_mysql_ids=lambda _ids: set(),
        source_ids_by_mysql_ids=lambda ids: {},
        count_by_mysql_records=lambda _prefix: 1,
        flush_for_mysql_sync=lambda: None,
    )

    async def import_business_data(request):
        imported.extend(request.records)
        return IndexResult(status="SUCCESS", document_id="mysql:solution", indexed_count=1)

    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)
    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=None,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"solution": target},
                import_business_data=import_business_data,
            )
        ),
    )
    service = AdminService(runtime)
    service._refresh_mysql_counts_after_sync = lambda _knowledge_type: None
    service._maybe_refresh_mysql_counts = lambda **_kwargs: None

    asyncio.run(
        service._run_mysql_sync(
            "recovery-sync",
            SimpleNamespace(knowledge_type="solution", batch_size=64),
            stop_event=threading.Event(),
            excluded_mysql_ids={"1"},
        )
    )

    assert [record.mysql_id for record in imported] == ["2"]
    assert state["processed"] == 2
    assert state["skipped_count"] == 1
    assert state["last_id"] == 2
    assert state["status"] == "SUCCESS"


def test_regular_mysql_sync_skips_records_covered_by_active_drafts(monkeypatch):
    import admin.mysql_sync as service_module

    state = {"status": "RUNNING", "stop_requested": False, "last_id": 0}

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    class Source:
        def count_for_sync(self, _knowledge_type):
            return 2

        def records_for_sync(self, _knowledge_type, *, after_id, limit):
            if after_id:
                return []
            return [
                CleanedKnowledgeRecord(record_id="1", mysql_id="1", text="草稿已覆盖"),
                CleanedKnowledgeRecord(record_id="2", mysql_id="2", text="仅 MySQL"),
            ]

        def list_for_sync(self, _knowledge_type, *, after_id, limit):
            return []

    class Target:
        def published_draft_mysql_ids(self, mysql_ids):
            return {"1"} & set(mysql_ids)

        def source_ids_by_mysql_ids(self, mysql_ids):
            return {}

        def count_by_mysql_records(self, _prefix):
            return 1

        def flush_for_mysql_sync(self):
            return None

    imported = []

    async def import_business_data(request):
        imported.extend(request.records)
        return IndexResult(status="SUCCESS", document_id="mysql:solution", indexed_count=1)

    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)
    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=None,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"solution": Target()},
                import_business_data=import_business_data,
            )
        ),
    )
    service = AdminService(runtime)
    service._refresh_mysql_counts_after_sync = lambda _knowledge_type: None
    service._maybe_refresh_mysql_counts = lambda **_kwargs: None

    asyncio.run(
        service._run_mysql_sync(
            "regular-sync",
            SimpleNamespace(knowledge_type="solution", batch_size=64),
            stop_event=threading.Event(),
        )
    )

    assert [record.mysql_id for record in imported] == ["2"]
    assert state["skipped_count"] == 1
