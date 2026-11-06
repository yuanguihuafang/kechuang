import asyncio
from types import SimpleNamespace

import pytest

from admin.service import AdminService
from services.application.document_lifecycle import _recover_lifecycle


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


def runtime_for_purge(*, linked_status="PUBLISHED", fail_once=False):
    effects = []
    linked = {"draft_id": "d", "user_id": "u", "status": linked_status,
              "draft_type": "solution", "draft": {"mysql_record_id": "7"}}
    version = {"document_id": "doc", "version_id": "doc:v1", "status": "RECYCLED",
               "storage_key": "uploads/doc.pdf", "knowledge_type": "solution", "scope_id": "public"}
    failures = [fail_once]

    class Lifecycle:
        def get_version(self, version_id):
            return version

        def list_versions(self, document_id):
            return [version]

        def is_storage_key_referenced(self, storage_key, *, exclude_version_ids=()):
            return False

        def get_document(self, document_id, **kwargs):
            return {"knowledge_type": "solution", "active_version_id": "doc:v1"}

        def update_version(self, version_id, **fields):
            if failures[0]:
                failures[0] = False
                raise RuntimeError("postgres interrupted")
            version.update(fields)
            return dict(version)

    class Drafts:
        def list_for_document(self, document_id):
            return [linked]

        def list_business_references(self, *args, **kwargs):
            return [linked]

        def list_storage_key_references(self, *args):
            return []

        def logical_delete(self, draft_id, **kwargs):
            effects.append("draft")
            linked["status"] = "DELETED"
            return linked

    class Result:
        status = "SUCCESS"

        def model_dump(self):
            return {}

    class Retrieval:
        public_milvus = {"solution": SimpleNamespace(delete_by_mysql_id=lambda value: effects.append("mysql-vector"))}

        async def unpublish_document(self, *args):
            effects.append("vector")
            return Result()

    class Perception:
        async def delete_stored_document_internal(self, **kwargs):
            effects.append("minio")

    lifecycle = Lifecycle()
    drafts = Drafts()
    runtime = SimpleNamespace(
        task_store=None, outbox_repository=None,
        require_lifecycle=lambda: lifecycle, lifecycle_service=lifecycle,
        require_draft_store=lambda: drafts, draft_store_service=drafts,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        embedded_perception=Perception(),
        business_record_sink=SimpleNamespace(delete=lambda **kwargs: effects.append("mysql")),
    )
    return runtime, effects, linked, version


def test_admin_purge_removes_published_link_and_all_related_data():
    runtime, effects, linked, version = runtime_for_purge()
    result, _ = asyncio.run(AdminService(runtime).lifecycle_action("purge", "doc:v1", "admin"))
    assert result["status"] == "PURGED"
    assert result["deleted_draft_count"] == 1
    assert linked["status"] == "DELETED"
    assert version["status"] == "PURGED"
    assert effects == ["vector", "mysql", "mysql-vector", "draft", "minio"]


def test_non_admin_still_cannot_purge_published_link():
    from services.application.document_lifecycle import DocumentLifecycleApplicationService
    runtime, effects, _, _ = runtime_for_purge()
    with pytest.raises(ValueError, match="发布"):
        asyncio.run(DocumentLifecycleApplicationService(runtime).run_action("purge", "doc:v1", "u"))
    assert effects == []


def test_admin_purge_does_not_report_success_when_linked_draft_update_fails():
    runtime, effects, linked, version = runtime_for_purge()
    runtime.require_draft_store().logical_delete = lambda *a, **k: None
    result, _ = asyncio.run(AdminService(runtime).lifecycle_action("purge", "doc:v1", "admin"))
    assert result is None
    assert linked["status"] == "PUBLISHED"
    assert version["status"] == "RECYCLED"
    assert "minio" not in effects


def test_replay_completes_same_linked_draft_cleanup():
    runtime, effects, linked, version = runtime_for_purge(linked_status="WITHDRAWN")
    asyncio.run(_recover_lifecycle(runtime, {"payload": {
        "action": "purge", "version_id": "doc:v1", "user_id": "admin",
    }}))
    assert linked["status"] == "DELETED"
    assert version["status"] == "PURGED"
    assert "mysql" in effects


def test_partial_purge_replay_preserves_shared_final_state():
    runtime, effects, linked, version = runtime_for_purge(linked_status="WITHDRAWN", fail_once=True)
    result, _ = asyncio.run(AdminService(runtime).lifecycle_action("purge", "doc:v1", "admin"))
    assert result is None
    asyncio.run(_recover_lifecycle(runtime, {"payload": {
        "action": "purge", "version_id": "doc:v1", "user_id": "admin",
    }}))
    assert linked["status"] == "DELETED"
    assert version["status"] == "PURGED"
    assert effects.count("mysql") == 1


def test_recovery_reuses_original_task_without_new_outbox(monkeypatch):
    from services.application.document_lifecycle import DocumentLifecycleApplicationService
    runtime, _, _, _ = runtime_for_purge(linked_status="WITHDRAWN")
    task = {"task_id": "task_original"}
    runtime.task_store = SimpleNamespace(get=lambda task_id: task)
    submitted = []

    async def run(self, action, version_id, user_id, task=None, **kwargs):
        submitted.append((task, kwargs.get("recovering")))
        return {}, task

    monkeypatch.setattr(DocumentLifecycleApplicationService, "run_action", run)
    asyncio.run(_recover_lifecycle(runtime, {"operation_id": "task_original", "payload": {
        "action": "purge", "version_id": "doc:v1", "user_id": "admin",
    }}))
    assert submitted == [(task, True)]


def test_version_cleanup_rejects_missing_postgres_update():
    from services.application.document_lifecycle import cleanup_document_version

    async def deleted(_payload):
        return {"status": "DELETED"}

    with pytest.raises(RuntimeError, match="版本更新失败"):
        asyncio.run(cleanup_document_version(SimpleNamespace(delete_version=deleted),
            SimpleNamespace(update_version=lambda *_a, **_kw: None), {"version_id": "doc:v1"}))


def test_version_purge_keeps_other_live_version_artifacts_and_drafts():
    runtime, effects, linked, version = runtime_for_purge(linked_status="WITHDRAWN")
    older = {"version_id": "doc:v0", "status": "PUBLISHED", "storage_key": "uploads/older.pdf"}
    runtime.lifecycle_service.list_versions = lambda _doc: [older, version]

    async def original(**kwargs):
        effects.append("original-only")

    runtime.embedded_perception.delete_original_file = original
    result, _ = asyncio.run(AdminService(runtime).lifecycle_action("purge", "doc:v1", "admin"))
    assert result["status"] == "PURGED"
    assert effects == ["original-only"]
    assert linked["status"] == "WITHDRAWN"


def test_purge_replay_finishes_task_if_version_was_already_committed():
    runtime, effects, _, version = runtime_for_purge(linked_status="WITHDRAWN")
    version["status"] = "PURGED"
    updates = []
    runtime.task_store = SimpleNamespace(
        get=lambda _id: {"task_id": "task_original", "status": "FAILED"},
        complete=lambda task_id, **kwargs: updates.append((task_id, kwargs)),
    )
    asyncio.run(_recover_lifecycle(runtime, {"operation_id": "task_original", "payload": {
        "action": "purge", "version_id": "doc:v1", "user_id": "admin"}}))
    assert effects == []
    assert len(updates) == 1
    assert updates[0][1]["status"] == "SUCCEEDED"
