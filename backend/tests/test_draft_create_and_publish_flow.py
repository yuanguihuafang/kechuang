import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant.integrations.deerflow_adapter import build_deerflow_input
from services.persistence.repositories import workflow_repository


def test_postgres_create_makes_a_new_id_for_each_same_thread_draft(monkeypatch):
    inserted_ids = []

    class Connection:
        def exec_driver_sql(self, sql, parameters):
            if sql.lstrip().startswith("SELECT draft_id"):
                latest = inserted_ids[-1] if inserted_ids else None
                return SimpleNamespace(fetchone=lambda: (latest,) if latest else None)
            if sql.lstrip().startswith("INSERT INTO platform_drafts"):
                inserted_ids.append(parameters[0])
                return SimpleNamespace(rowcount=1)
            raise AssertionError(f"Unexpected SQL: {sql}")

    @contextmanager
    def connection(_dsn):
        yield Connection()

    monkeypatch.setattr(workflow_repository, "_pooled_connection", connection)
    store = workflow_repository.PostgresDraftStore("postgresql://test")
    store.update = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Creating a new draft must not update the prior draft")
    )
    store.get = lambda draft_id, *, user_id: {"draft_id": draft_id, "user_id": user_id}

    first = store.create(
        user_id="user-1",
        user_role="external",
        thread_id="thread-1",
        draft={"draft_type": "demand", "title": "第一项需求"},
    )
    second = store.create(
        user_id="user-1",
        user_role="external",
        thread_id="thread-1",
        draft={"draft_type": "demand", "title": "第二项需求"},
    )

    assert first["draft_id"] != second["draft_id"]
    assert inserted_ids == [first["draft_id"], second["draft_id"]]


def test_active_unpublished_draft_requires_confirmation_before_new_draft():
    result = build_deerflow_input(
        {
            "message": "我还有一个新需求，帮我建一份草稿",
            "draft_context": {
                "draft_id": "draft-current",
                "draft_type": "demand",
                "status": "CONFIRMED",
                "title": "当前需求",
            },
        },
        thread_id="thread-1",
        identity={"user_id": "user-1", "user_role": "external"},
    )

    assert "draft-current" in result["message"]
    assert "尚未发布" in result["message"]
    assert "其他用户暂时看不到" in result["message"]
    assert "确认新建" in result["message"]
    assert "确认前不得调用 kc_draft_create" in result["message"]


def test_short_confirmation_still_receives_active_draft_and_confirmation_rules():
    result = build_deerflow_input(
        {
            "message": "确认",
            "draft_context": {
                "draft_id": "draft-current",
                "draft_type": "demand",
                "status": "CONFIRMED",
                "title": "当前需求",
            },
        },
        thread_id="thread-1",
        identity={"user_id": "user-1", "user_role": "external"},
    )

    assert '"draft_id": "draft-current"' in result["message"]
    assert "确认新建时调用 kc_draft_create" in result["message"]


def test_explicit_publish_uses_current_draft_without_another_confirmation():
    result = build_deerflow_input(
        {
            "message": "发布当前草稿",
            "draft_context": {
                "draft_id": "draft-current",
                "draft_type": "demand",
                "status": "CONFIRMED",
                "version": 3,
                "title": "当前需求",
            },
        },
        thread_id="thread-1",
        identity={"user_id": "user-1", "user_role": "external"},
    )

    assert "kc_draft_publish" in result["message"]
    assert '"draft_id": "draft-current"' in result["message"]
    assert "不再追加发布确认" in result["message"]


def test_draft_skills_explain_natural_language_publish_and_unpublished_visibility():
    project_root = workflow_repository.__file__
    root = Path(project_root).resolve().parents[4]
    skills = ("kc-demand-draft", "kc-solution-draft", "kc-achievement-draft")
    for name in skills:
        source = root / "OPIEos" / "skills" / "custom" / name / "SKILL.md"
        packaged = root / "bakup" / "skills" / "custom" / name / "SKILL.md"
        source_text = source.read_text(encoding="utf-8")
        assert "kc_draft_publish" in source_text
        assert "其他用户暂时看不到" in source_text
        assert source_text == packaged.read_text(encoding="utf-8")


def test_document_extraction_creates_all_drafts_as_one_ordered_batch():
    from runtime.workflows.knowledge_document_draft import persist_drafts

    calls = []

    class Store:
        def create_many(self, **kwargs):
            calls.append(kwargs)
            return [
                {"draft_id": f"draft-{index + 1}", "draft": draft}
                for index, draft in enumerate(kwargs["drafts"])
            ]

    result = persist_drafts(
        Store(),
        {"user_id": "user-1", "user_role": "internal", "thread_id": "thread-1"},
        {"drafts": [
            {"draft_type": "demand", "title": "first"},
            {"draft_type": "demand", "title": "second"},
        ]},
        "demand",
    )

    assert len(calls) == 1
    assert [draft["title"] for draft in calls[0]["drafts"]] == ["first", "second"]
    assert result["draft_id"] == "draft-1"
    assert result["draft_ids"] == ["draft-1", "draft-2"]
    assert [draft["title"] for draft in result["draft_previews"]] == ["first", "second"]


def test_document_extraction_assigns_stable_shared_source_id_without_document_id(monkeypatch):
    from agents.perception_agent import PerceptionAgent
    from runtime.workflows.knowledge_document_draft import KnowledgeDocumentDraftService

    saved = []

    class Store:
        def create_many(self, **kwargs):
            saved.extend(kwargs["drafts"])
            return [
                {"draft_id": f"draft-{len(saved) - len(kwargs['drafts']) + index + 1}", "draft": draft}
                for index, draft in enumerate(kwargs["drafts"])
            ]

    async def extract(_draft, **_kwargs):
        return {
            "drafts": [
                {"draft_type": "demand", "title": "甲", "fields": {}},
                {"draft_type": "demand", "title": "乙", "fields": {}},
            ]
        }

    monkeypatch.setattr(PerceptionAgent, "extract_fields", extract)
    service = KnowledgeDocumentDraftService(Store())
    task = {
        "task_id": "task-source-123",
        "user_role": "internal",
        "knowledge_type": "demand",
        "document_id": "",
    }

    asyncio.run(service.build_task_fields(task, {"raw_text": "多项需求"}, "多项需求"))

    source_ids = [draft.get("source_id") for draft in saved]
    assert len(source_ids) == 2
    assert source_ids[0]
    assert source_ids[0] == source_ids[1]


def test_postgres_create_many_persists_batch_in_one_transaction_and_preserves_order(monkeypatch):
    transactions = []
    inserted = []

    class Connection:
        def exec_driver_sql(self, sql, parameters):
            assert sql.lstrip().startswith("INSERT INTO platform_drafts")
            inserted.append(parameters)
            return SimpleNamespace(rowcount=1)

    @contextmanager
    def connection(_dsn):
        transactions.append("begin")
        try:
            yield Connection()
        except Exception:
            inserted.clear()
            raise
        else:
            transactions.append("commit")

    monkeypatch.setattr(workflow_repository, "_pooled_connection", connection)
    store = workflow_repository.PostgresDraftStore("postgresql://test")

    created = store.create_many(
        user_id="user-1",
        user_role="internal",
        thread_id="thread-1",
        drafts=[
            {"draft_type": "demand", "title": "first", "source_id": "batch-1"},
            {"draft_type": "demand", "title": "second", "source_id": "batch-1"},
        ],
    )

    assert len(transactions) == 2
    assert transactions == ["begin", "commit"]
    assert len(inserted) == 2
    assert len({row[0] for row in inserted}) == 2
    assert [row[6] for row in inserted] == ["first", "second"]
    assert json.loads(inserted[0][7])["source_id"] == "batch-1"
    assert json.loads(inserted[1][7])["source_id"] == "batch-1"
    assert inserted[0][9] > inserted[1][9]
    assert [row["draft_title"] for row in created] == ["first", "second"]


def test_postgres_create_many_rolls_back_the_whole_batch_on_insert_failure(monkeypatch):
    inserted = []
    events = []

    class Connection:
        def exec_driver_sql(self, sql, parameters):
            assert sql.lstrip().startswith("INSERT INTO platform_drafts")
            if len(inserted) == 1:
                raise RuntimeError("injected second insert failure")
            inserted.append(parameters)
            return SimpleNamespace(rowcount=1)

    @contextmanager
    def connection(_dsn):
        events.append("begin")
        try:
            yield Connection()
        except Exception:
            inserted.clear()
            events.append("rollback")
            raise
        else:
            events.append("commit")

    monkeypatch.setattr(workflow_repository, "_pooled_connection", connection)
    store = workflow_repository.PostgresDraftStore("postgresql://test")

    with pytest.raises(RuntimeError, match="injected second insert failure"):
        store.create_many(
            user_id="user-1",
            user_role="internal",
            drafts=[
                {"draft_type": "demand", "title": "first"},
                {"draft_type": "demand", "title": "second"},
            ],
        )

    assert events == ["begin", "rollback"]
    assert inserted == []


def test_postgres_source_batch_lookup_uses_exact_persisted_source_id(monkeypatch):
    queries = []

    class Connection:
        def exec_driver_sql(self, sql, parameters):
            queries.append((sql, parameters))
            return SimpleNamespace(fetchall=list)

    @contextmanager
    def connection(_dsn):
        yield Connection()

    monkeypatch.setattr(workflow_repository, "_pooled_connection", connection)
    rows = workflow_repository.PostgresDraftStore("postgresql://test").list_for_source_id(
        'batch"-exact'
    )

    assert rows == []
    assert len(queries) == 1
    assert "content->>'source_id'=%s" in queries[0][0]
    assert queries[0][1] == ('batch"-exact',)
