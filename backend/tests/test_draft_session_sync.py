from types import SimpleNamespace

from services.application.draft_session import sync_session_draft
from services.persistence.postgres import InMemoryDraftStore


class SessionStore:
    def __init__(self):
        self.session = {
            "user_id": "user-1",
            "user_role": "external",
            "state": {
                "chat_flow": {
                    "active": {"business_intent": "customer_visit"},
                    "request_context": {"draft_context": {"match_direction": "demand_to_supply"}},
                },
            },
        }
        self.updates = []

    def get(self, thread_id):
        return self.session if thread_id == "session-1" else None

    def upsert(self, thread_id, **fields):
        self.updates.append((thread_id, fields))
        self.session["state"].update(fields.get("state") or {})


def test_sync_session_draft_replaces_active_draft_and_keeps_flow_context():
    store = SessionStore()
    runtime = SimpleNamespace(require_conversation=lambda: SimpleNamespace(session_store=store))
    draft = {"draft_id": "draft-1", "draft_type": "customer_visit", "fields": {"company": "新企业"}}

    sync_session_draft(runtime, "session-1", draft, user_id="user-1", user_role="external")

    flow = store.updates[-1][1]["state"]["chat_flow"]
    assert flow["active_draft"] == draft
    assert flow["active_draft_id"] == "draft-1"
    assert flow["request_context"]["draft_context"]["match_direction"] == "demand_to_supply"


def test_sync_session_draft_survives_draft_index_lookup_failure():
    store = SessionStore()

    def unavailable_draft_store():
        raise RuntimeError("draft store unavailable")

    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=store),
        require_draft_store=unavailable_draft_store,
    )
    draft = {"draft_id": "draft-1", "draft_type": "solution", "title": "当前方案"}

    sync_session_draft(runtime, "session-1", draft, user_id="user-1", user_role="external")

    flow = store.updates[-1][1]["state"]["chat_flow"]
    assert flow["active_draft_id"] == "draft-1"
    assert flow["draft_index"] == [
        {
            "draft_id": "draft-1",
            "ordinal": 1,
            "draft_type": "solution",
            "status": "DRAFT",
            "title": "当前方案",
        }
    ]


def test_next_turn_uses_latest_unpublished_fields():
    from assistant.integrations.deerflow_adapter import build_deerflow_input
    from services.application.draft_session import load_session_draft_context
    store = SessionStore()
    drafts = InMemoryDraftStore()
    row = drafts.create(user_id="user-1", user_role="external", thread_id="session-1",
                        draft={"draft_type": "demand", "fields": {"title": "旧内容"}})
    store.session["state"]["chat_flow"]["active_draft_id"] = row["draft_id"]
    drafts.update(row["draft_id"], user_id="user-1", draft={"fields": {"title": "最新机器人需求"}})
    runtime = SimpleNamespace(require_conversation=lambda: SimpleNamespace(session_store=store),
                              require_draft_store=lambda: drafts)
    context = load_session_draft_context(runtime, thread_id="session-1", user_id="user-1")
    result = build_deerflow_input({"message": "继续完善", "draft_context": context},
                                 thread_id="session-1", identity={"user_id": "user-1"})
    assert "最新机器人需求" in result["message"]
    assert "旧内容" not in result["message"]


def test_session_draft_context_indexes_each_draft_in_stable_creation_order():
    from services.application.draft_session import load_session_draft_context

    session_store = SessionStore()
    drafts = InMemoryDraftStore()
    first = drafts.create(
        user_id="user-1", user_role="external", thread_id="session-1",
        draft={"draft_type": "solution", "title": "第一份方案"},
    )
    second = drafts.create(
        user_id="user-1", user_role="external", thread_id="session-1",
        draft={"draft_type": "demand", "title": "第二份需求"},
    )
    drafts._items[first["draft_id"]]["created_at"] = "2026-10-10T10:00:00+08:00"
    drafts._items[second["draft_id"]]["created_at"] = "2026-10-10T10:00:01+08:00"
    drafts._items[first["draft_id"]]["updated_at"] = "2026-10-12T10:00:00+08:00"
    session_store.session["state"]["chat_flow"]["active_draft_id"] = first["draft_id"]
    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=session_store),
        require_draft_store=lambda: drafts,
    )

    context = load_session_draft_context(runtime, thread_id="session-1", user_id="user-1")

    assert [item["title"] for item in context["session_drafts"]] == ["第一份方案", "第二份需求"]
    assert [item["ordinal"] for item in context["session_drafts"]] == [1, 2]
    assert context["session_drafts"][0]["draft_id"] == first["draft_id"]
    assert context["session_drafts"][1]["draft_id"] == second["draft_id"]


def test_sync_session_draft_keeps_ordinals_and_appends_new_drafts():
    store = SessionStore()
    drafts = InMemoryDraftStore()
    first = drafts.create(
        user_id="user-1", user_role="external", thread_id="session-1",
        draft={"draft_type": "solution", "title": "第一份方案"},
    )
    second = drafts.create(
        user_id="user-1", user_role="external", thread_id="session-1",
        draft={"draft_type": "demand", "title": "第二份需求"},
    )
    drafts._items[first["draft_id"]]["created_at"] = "2026-10-10T10:00:00+08:00"
    drafts._items[second["draft_id"]]["created_at"] = "2026-10-10T10:00:01+08:00"
    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=store),
        require_draft_store=lambda: drafts,
    )

    sync_session_draft(
        runtime, "session-1", {**first["draft"], "draft_id": first["draft_id"]},
        user_id="user-1", user_role="external",
    )
    flow = store.updates[-1][1]["state"]["chat_flow"]
    assert [(item["draft_id"], item["ordinal"]) for item in flow["draft_index"]] == [
        (first["draft_id"], 1), (second["draft_id"], 2),
    ]

    updated = drafts.update(
        first["draft_id"], user_id="user-1", draft={"title": "更新后的第一份方案"}
    )
    sync_session_draft(
        runtime, "session-1", {**updated["draft"], "draft_id": first["draft_id"]},
        user_id="user-1", user_role="external",
    )
    flow = store.updates[-1][1]["state"]["chat_flow"]
    assert [(item["draft_id"], item["ordinal"]) for item in flow["draft_index"]] == [
        (first["draft_id"], 1), (second["draft_id"], 2),
    ]

    third = drafts.create(
        user_id="user-1", user_role="external", thread_id="session-1",
        draft={"draft_type": "achievement", "title": "第三份成果"},
    )
    sync_session_draft(
        runtime, "session-1", {**third["draft"], "draft_id": third["draft_id"]},
        user_id="user-1", user_role="external",
    )
    flow = store.updates[-1][1]["state"]["chat_flow"]
    assert [(item["draft_id"], item["ordinal"]) for item in flow["draft_index"]] == [
        (first["draft_id"], 1), (second["draft_id"], 2), (third["draft_id"], 3),
    ]


def test_other_user_active_draft_is_not_injected():
    from services.application.draft_session import load_session_draft_context
    store = SessionStore()
    drafts = InMemoryDraftStore()
    row = drafts.create(user_id="other", user_role="external", thread_id="other-session",
                        draft={"draft_type": "demand", "fields": {"title": "他人草稿"}})
    store.session["state"]["chat_flow"]["active_draft_id"] = row["draft_id"]
    runtime = SimpleNamespace(require_conversation=lambda: SimpleNamespace(session_store=store),
                              require_draft_store=lambda: drafts)
    assert load_session_draft_context(runtime, thread_id="session-1", user_id="user-1") is None


def test_session_draft_cards_are_owner_scoped_and_newest_first():
    from services.application.draft_session import load_session_draft_cards

    session_store = SessionStore()
    drafts = InMemoryDraftStore()
    older = drafts.create(
        user_id="user-1", user_role="external", thread_id="session-1",
        draft={"draft_type": "solution", "title": "旧草稿"},
    )
    newer = drafts.create(
        user_id="user-1", user_role="external", thread_id="session-1",
        draft={"draft_type": "demand", "title": "新草稿"},
    )
    other_user = drafts.create(
        user_id="other", user_role="external", thread_id="session-1",
        draft={"draft_type": "solution", "title": "他人草稿"},
    )
    drafts._items[older["draft_id"]]["created_at"] = "2026-10-08T10:00:00+08:00"
    drafts._items[newer["draft_id"]]["created_at"] = "2026-10-09T10:00:00+08:00"
    drafts._items[older["draft_id"]]["updated_at"] = "2026-10-09T10:00:00+08:00"
    drafts._items[newer["draft_id"]]["updated_at"] = "2026-10-10T10:00:00+08:00"
    drafts._items[other_user["draft_id"]]["updated_at"] = "2026-10-11T10:00:00+08:00"
    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=session_store),
        require_draft_store=lambda: drafts,
    )

    cards = load_session_draft_cards(
        runtime, thread_id="session-1", user_id="user-1", user_role="external"
    )

    assert [item["title"] for item in cards] == ["新草稿", "旧草稿"]
    assert [item["draft_id"] for item in cards] == [newer["draft_id"], older["draft_id"]]
    assert [item["ordinal"] for item in cards] == [2, 1]


def test_session_cards_include_all_same_source_batch_drafts_newest_first():
    from services.application.draft_session import load_session_draft_cards

    session_store = SessionStore()
    drafts = InMemoryDraftStore()
    first = drafts.create(
        user_id="user-1",
        user_role="internal",
        thread_id="session-1",
        draft={"draft_type": "demand", "title": "source-first", "source_id": "batch-1"},
    )
    second = drafts.create(
        user_id="user-1",
        user_role="internal",
        thread_id="session-1",
        draft={"draft_type": "demand", "title": "source-second", "source_id": "batch-1"},
    )
    drafts._items[first["draft_id"]]["updated_at"] = "2026-10-10T10:00:00+08:00"
    drafts._items[second["draft_id"]]["updated_at"] = "2026-10-10T10:00:01+08:00"
    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=session_store),
        require_draft_store=lambda: drafts,
    )

    cards = load_session_draft_cards(
        runtime, thread_id="session-1", user_id="user-1", user_role="internal"
    )

    assert [card["title"] for card in cards] == ["source-second", "source-first"]


def test_session_draft_cards_hide_visit_drafts_from_external_users():
    from services.application.draft_session import load_session_draft_cards

    session_store = SessionStore()
    drafts = InMemoryDraftStore()
    drafts.create(
        user_id="user-1", user_role="internal", thread_id="session-1",
        draft={"draft_type": "customer_visit", "title": "拜访记录"},
    )
    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=session_store),
        require_draft_store=lambda: drafts,
    )

    cards = load_session_draft_cards(
        runtime, thread_id="session-1", user_id="user-1", user_role="external"
    )

    assert cards == []


def test_external_draft_context_hides_customer_visit_records():
    from services.application.draft_session import load_session_draft_context

    session_store = SessionStore()
    drafts = InMemoryDraftStore()
    visit = drafts.create(
        user_id="user-1",
        user_role="internal",
        thread_id="session-1",
        draft={"draft_type": "customer_visit", "title": "内部拜访记录"},
    )
    demand = drafts.create(
        user_id="user-1",
        user_role="external",
        thread_id="session-1",
        draft={"draft_type": "demand", "title": "公开需求"},
    )
    session_store.session["state"]["chat_flow"]["active_draft_id"] = visit["draft_id"]
    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=session_store),
        require_draft_store=lambda: drafts,
    )

    context = load_session_draft_context(
        runtime, thread_id="session-1", user_id="user-1", user_role="external"
    )

    assert "draft_id" not in context
    assert [item["title"] for item in context["session_drafts"]] == ["公开需求"]
    assert context["session_drafts"][0]["draft_id"] == demand["draft_id"]
