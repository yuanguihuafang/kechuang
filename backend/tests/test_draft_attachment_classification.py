import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from core.contracts.tools import ToolContext
from services.application.drafts import DraftApplicationService
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.persistence.postgres import InMemoryDraftStore
from services.persistence.sqlalchemy import metadata


ATTACHMENT = {"document_id": "doc-1", "version_id": "doc-1:v1", "storage_key": "uploads/file.pptx"}


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *a, **kw: None)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    store = DocumentLifecycleStore(engine=engine)
    store.create_version(
        document_id="doc-1",
        source_name="file.pptx",
        storage_key=ATTACHMENT["storage_key"],
        scope_id="public",
        user_id="owner",
        version_number=1,
        knowledge_type="",
    )
    yield store
    engine.dispose()


def test_bind_type_preserves_all_attachment_data(lifecycle):
    from services.persistence.sqlalchemy import platform_documents

    with lifecycle.engine.connect() as conn:
        before = dict(conn.execute(platform_documents.select()).mappings().one())
    assert lifecycle.bind_draft_attachments(
        [ATTACHMENT], user_id="owner", knowledge_type="solution"
    ) == ["doc-1"]
    with lifecycle.engine.connect() as conn:
        after = dict(conn.execute(platform_documents.select()).mappings().one())
    assert after["knowledge_type"] == "solution"
    assert {k: v for k, v in before.items() if k not in {"knowledge_type", "updated_at"}} == {
        k: v for k, v in after.items() if k not in {"knowledge_type", "updated_at"}
    }
    assert (
        lifecycle.bind_draft_attachments([ATTACHMENT], user_id="owner", knowledge_type="solution")
        == []
    )


@pytest.mark.parametrize(
    "conflict", ["owner", "version", "path", "scope", "type", "purged", "published"]
)
def test_binding_rejects_conflicting_or_published_sources(lifecycle, conflict):
    attachment = dict(ATTACHMENT)
    owner = "other" if conflict == "owner" else "owner"
    if conflict == "version":
        attachment["version_id"] = "doc-1:v0"
    if conflict == "path":
        attachment["storage_key"] = "uploads/another.pptx"
    if conflict == "scope":
        from services.persistence.sqlalchemy import platform_documents

        with lifecycle.engine.begin() as conn:
            conn.execute(platform_documents.update().values(scope_id="private"))
    if conflict == "type":
        lifecycle.bind_draft_attachments([ATTACHMENT], user_id="owner", knowledge_type="demand")
    if conflict == "purged":
        lifecycle.update_version("doc-1:v1", status="PURGED")
    if conflict == "published":
        lifecycle.update_version("doc-1:v1", publication_status="PUBLISHED", index_status="INDEXED")
    before = deepcopy(lifecycle.get_document("doc-1", include_purged=True))
    with pytest.raises(ValueError):
        lifecycle.bind_draft_attachments([attachment], user_id=owner, knowledge_type="solution")
    assert lifecycle.get_document("doc-1", include_purged=True) == before


def test_batch_binding_is_atomic(lifecycle):
    before = deepcopy(lifecycle.get_document("doc-1"))
    with pytest.raises(ValueError):
        lifecycle.bind_draft_attachments(
            [ATTACHMENT, {**ATTACHMENT, "document_id": "missing"}],
            user_id="owner",
            knowledge_type="solution",
        )
    assert lifecycle.get_document("doc-1") == before


@pytest.mark.parametrize("entry", ["create", "save"])
def test_draft_confirmation_binds_late_selected_type(lifecycle, entry):
    store = InMemoryDraftStore()
    service = DraftApplicationService(
        SimpleNamespace(
            require_draft_store=lambda: store,
            require_lifecycle=lambda: lifecycle,
            require_conversation=lambda: SimpleNamespace(session_store=None),
        )
    )
    context = ToolContext(user_id="owner", user_role="internal", trace_id="test")
    draft = {"draft_type": "solution", "title": "方案", "attachments": [ATTACHMENT]}
    if entry == "create":
        asyncio.run(service.create(draft, context=context))
    else:
        row = store.create(user_id="owner", user_role="internal", thread_id=None, draft=draft)
        asyncio.run(service.save(row["draft_id"], {"title": "更新方案"}, context=context))
    assert lifecycle.get_document("doc-1")["knowledge_type"] == "solution"
