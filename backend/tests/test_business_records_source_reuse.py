from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.contracts.business_records import ConfirmedBusinessRecordRequest
from services.application.business_records import (
    BusinessRecordApplicationService,
    source_group_mysql_record_id,
)


class DraftStore:
    current_mysql_id = None

    def get(self, draft_id: str, *, user_id: str):
        assert draft_id == "current-draft"
        assert user_id == "user-1"
        return {
            "draft_id": draft_id,
            "user_id": user_id,
            "draft_type": "solution",
            "status": "CONFIRMED",
            "draft": {
                "source_id": "source:shared",
                "mysql_record_id": self.current_mysql_id,
                "fields": {"title": "updated"},
            },
        }

    def list_for_source_id(self, source_id: str):
        assert source_id in {"source:shared", "source:batch:shared"}
        return [
            {
                "draft_id": "previous-draft",
                "user_id": "user-1",
                "draft_type": "solution",
                "status": "PUBLISHED",
                "draft": {
                    "source_id": source_id,
                    "mysql_record_id": "existing-mysql-id",
                },
            }
        ]

    def update(self, draft_id: str, *, user_id: str, draft: dict):
        assert draft_id == "current-draft"
        return {"draft_id": draft_id, "user_id": user_id, "draft": draft}


class BusinessRecordSink:
    def __init__(self):
        self.updated = []
        self.saved = []

    def update(self, **kwargs):
        self.updated.append(kwargs)
        return {"business_record_id": kwargs["record_id"], "status": "UPDATED"}

    def save(self, payload):
        self.saved.append(payload)
        return {"business_record_id": "new-mysql-id", "status": "SAVED"}


class RetrievalAgent:
    async def publish_document(self, _document_id, _request):
        return SimpleNamespace(status="SUCCESS", model_dump=lambda: {"status": "SUCCESS"})


def test_solution_confirmation_reuses_mysql_record_from_source_group():
    sink = BusinessRecordSink()
    runtime = SimpleNamespace(
        require_business_record_sink=lambda: sink,
        require_draft_store=lambda: DraftStore(),
        embedded_retrieval=SimpleNamespace(agent=RetrievalAgent()),
    )
    payload = ConfirmedBusinessRecordRequest(
        user_id="user-1",
        record_type="solution",
        confirmed=True,
        confirmed_by="user-1",
        draft_id="current-draft",
        structured_data={
            "title": "updated",
            "source_id": "source:shared",
            "fields": {"title": "updated", "technical_summary": "updated content"},
        },
    )

    result = asyncio.run(BusinessRecordApplicationService(runtime).confirm(payload))

    assert sink.saved == []
    assert len(sink.updated) == 1
    assert sink.updated[0]["record_id"] == "existing-mysql-id"
    assert result["business_record"]["business_record_id"] == "existing-mysql-id"


def test_solution_confirmation_updates_current_draft_when_source_group_has_other_drafts():
    sink = BusinessRecordSink()
    store = DraftStore()
    store.current_mysql_id = "current-mysql-id"
    runtime = SimpleNamespace(
        require_business_record_sink=lambda: sink,
        require_draft_store=lambda: store,
        embedded_retrieval=SimpleNamespace(agent=RetrievalAgent()),
    )
    payload = ConfirmedBusinessRecordRequest(
        user_id="user-1",
        record_type="solution",
        confirmed=True,
        confirmed_by="user-1",
        draft_id="current-draft",
        structured_data={"title": "updated", "source_id": "source:shared"},
    )

    result = asyncio.run(BusinessRecordApplicationService(runtime).confirm(payload))

    assert sink.saved == []
    assert len(sink.updated) == 1
    assert sink.updated[0]["record_id"] == "current-mysql-id"
    assert result["business_record"]["business_record_id"] == "current-mysql-id"


def test_source_group_reuses_deterministic_id_for_identical_duplicate_drafts():
    class Store:
        def list_for_source_id(self, _source_id):
            return [
                {
                    "draft_id": f"duplicate-{mysql_id}",
                    "user_id": "user-1",
                    "draft_type": "solution",
                    "status": "PUBLISHED",
                    "draft": {
                        "source_id": "source:shared",
                        "mysql_record_id": mysql_id,
                        "title": "same",
                        "fields": {"title": "same", "description": "identical"},
                    },
                }
                for mysql_id in ("mysql-b", "mysql-a")
            ]

    assert source_group_mysql_record_id(
        Store(),
        source_id="source:shared",
        source_id_values=set(),
        draft_id="new-draft",
        user_id="user-1",
        draft_type="solution",
        current_draft={"title": "same", "fields": {"title": "same", "description": "identical"}},
    ) == "mysql-a"


def test_reparse_reuses_matching_mysql_record_across_task_source_ids():
    class Store:
        def list_for_source_id(self, _source_id):
            return []

        def list_for_document(self, document_id):
            assert document_id == "doc-1"
            return [{
                "draft_id": "previous-draft",
                "user_id": "admin",
                "draft_type": "solution",
                "status": "PUBLISHED",
                "draft": {
                    "source_id": "source:old-task",
                    "mysql_record_id": "mysql-existing",
                    "title": "同一方案",
                    "attachments": [{"document_id": "doc-1"}],
                },
            }]

    assert source_group_mysql_record_id(
        Store(),
        source_id="source:new-task",
        source_id_values=set(),
        draft_id="new-draft",
        user_id="admin",
        draft_type="solution",
        current_draft={
            "title": "同一方案",
            "attachments": [{"document_id": "doc-1"}],
        },
        allow_source_group_rewrite=True,
    ) == "mysql-existing"


def test_source_group_keeps_conflict_for_same_title_with_different_content():
    class Store:
        def list_for_source_id(self, _source_id):
            return [
                {
                    "draft_id": f"draft-{mysql_id}",
                    "user_id": "user-1",
                    "draft_type": "solution",
                    "status": "PUBLISHED",
                    "draft": {
                        "source_id": "source:shared",
                        "mysql_record_id": mysql_id,
                        "title": "same",
                        "fields": {"title": "same", "description": description},
                    },
                }
                for mysql_id, description in (("mysql-a", "first"), ("mysql-b", "second"))
            ]

    with pytest.raises(ValueError, match="同名 MySQL 草稿"):
        source_group_mysql_record_id(
            Store(),
            source_id="source:shared",
            source_id_values=set(),
            draft_id="new-draft",
            user_id="user-1",
            draft_type="solution",
            current_draft={"title": "same"},
        )


def test_admin_source_group_rewrite_returns_no_old_id_for_ambiguous_unmatched_draft():
    class Store:
        def list_for_source_id(self, _source_id):
            return [
                {
                    "draft_id": f"draft-{mysql_id}",
                    "user_id": "admin",
                    "draft_type": "solution",
                    "status": "PUBLISHED",
                    "draft": {
                        "source_id": "source:shared",
                        "mysql_record_id": mysql_id,
                        "title": title,
                    },
                }
                for mysql_id, title in (("mysql-a", "old-a"), ("mysql-b", "old-b"))
            ]

    assert source_group_mysql_record_id(
        Store(),
        source_id="source:shared",
        source_id_values=set(),
        draft_id="new-draft",
        user_id="admin",
        draft_type="solution",
        current_draft={"title": "edited title"},
        allow_source_group_rewrite=True,
    ) is None
