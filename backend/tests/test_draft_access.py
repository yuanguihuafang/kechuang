import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from assistant.api.v1.drafts.routes import build_draft_router
from assistant.api.v1.schemas import (
    DraftCreateRequest,
    DraftUpdateRequest,
)
from core.contracts.workflows import (
    DraftPublishRequest,
)
from services.application.draft_publishing import DraftPublishingApplicationService
from services.persistence.postgres import InMemoryDraftStore


@pytest.mark.parametrize("action", ["create", "get", "update", "my_list", "list"])
def test_http_visit_drafts_recheck_current_role(action, monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *a, **kw: None)
    store = InMemoryDraftStore()
    record = store.create(
        user_id="1",
        user_role="internal",
        draft={
            "draft_type": "customer_visit",
            "fields": {"enterprise_name": "客户"},
        },
    )
    runtime = SimpleNamespace(require_draft_store=lambda: store)
    endpoints = {
        route.name: route.endpoint
        for route in build_draft_router(
            runtime,
            lambda user_id: (user_id, "external", None),
        ).routes
    }
    request = SimpleNamespace(state=SimpleNamespace(user_id="1"), headers={})
    if action in {"my_list", "list"}:
        endpoint = endpoints["my_drafts" if action == "my_list" else "list_drafts"]
        assert asyncio.run(endpoint(request, limit=50)) == []
        return
    if action == "create":
        coroutine = endpoints["create_draft"](DraftCreateRequest(draft=record["draft"]), request)
    elif action == "get":
        coroutine = endpoints["get_draft"](record["draft_id"], request)
    else:
        coroutine = endpoints["update_draft"](
            record["draft_id"], DraftUpdateRequest(draft={}), request
        )
    with pytest.raises(HTTPException) as error:
        asyncio.run(coroutine)
    assert error.value.status_code == 403


def test_edit_cannot_turn_public_draft_into_private_visit():
    store = InMemoryDraftStore()
    record = store.create(user_id="1", user_role="external", draft={"draft_type": "demand"})
    runtime = SimpleNamespace(require_draft_store=lambda: store)
    endpoint = next(
        route.endpoint
        for route in build_draft_router(
            runtime,
            lambda user_id: (user_id, "external", None),
        ).routes
        if route.name == "update_draft"
    )
    request = SimpleNamespace(state=SimpleNamespace(user_id="1"), headers={})
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            endpoint(
                record["draft_id"],
                DraftUpdateRequest(draft={"draft_type": "customer_visit"}),
                request,
            )
        )
    assert error.value.status_code == 409
    assert store.get(record["draft_id"], user_id="1")["draft_type"] == "demand"


def test_public_publisher_never_writes_visit_records():
    store = InMemoryDraftStore()
    record = store.create(user_id="1", user_role="internal", draft={"draft_type": "customer_visit"})
    runtime = SimpleNamespace(require_draft_store=lambda: store)
    service = DraftPublishingApplicationService(
        runtime, lambda user_id: (user_id, "internal", None)
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.publish(record["draft_id"], DraftPublishRequest(user_id="1")))
    assert error.value.status_code == 409
    assert "拜访" in str(error.value.detail)
