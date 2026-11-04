from types import SimpleNamespace

from admin.service import AdminService


def test_conversation_detail_includes_session_match_results():
    class SessionStore:
        def get(self, session_id):
            return {
                "session_id": session_id,
                "user_id": "user-1",
                "user_role": "external",
                "state": {"title": "无人机巡检", "stage": "SUPPLY_DEMAND_MATCH"},
            }

        def list_session_messages_admin(self, session_id):
            return []

    class DraftStore:
        def list_for_thread(self, thread_id):
            return []

    class MatchStore:
        def list_by_session(self, session_id, limit=100):
            return [
                {
                    "result_id": "match-1",
                    "session_id": session_id,
                    "query": "无人机巡检方案",
                    "draft_id": None,
                    "created_at": "2026-09-16T10:00:00+00:00",
                    "result": {"match_results": [{"title": "无人机巡检方案"}]},
                }
            ]

    runtime = SimpleNamespace(
        task_store=None,
        sync_state_store=None,
        user_identity_reader=None,
        conversation_service=SimpleNamespace(session_store=SessionStore()),
        match_result_store=MatchStore(),
    )
    runtime.require_draft_store = lambda: DraftStore()

    detail = AdminService(runtime, session_store=runtime.conversation_service.session_store).conversation_detail("session-1")

    assert detail["session"]["state"]["stage"] == "SUPPLY_DEMAND_MATCH"
    assert detail["match_results"][0]["query"] == "无人机巡检方案"
    assert detail["match_results"][0]["items"][0]["title"] == "无人机巡检方案"


def test_conversation_detail_keeps_attachments_on_their_draft():
    attachment = {"document_id": "doc-1", "file_name": "方案资料.pdf"}

    class SessionStore:
        def get(self, session_id):
            return {
                "session_id": session_id,
                "user_id": "user-1",
                "user_role": "external",
                "state": {},
            }

        def list_session_messages_admin(self, session_id):
            return []

    class DraftStore:
        def list_for_thread(self, thread_id):
            return [
                {
                    "draft_id": "draft-1",
                    "thread_id": thread_id,
                    "user_id": "user-1",
                    "draft_type": "solution",
                    "draft_title": "技术方案",
                    "status": "CONFIRMED",
                    "draft": {
                        "draft_type": "solution",
                        "title": "技术方案",
                        "attachments": [attachment],
                    },
                }
            ]

    runtime = SimpleNamespace(
        task_store=None,
        sync_state_store=None,
        user_identity_reader=None,
        conversation_service=SimpleNamespace(session_store=SessionStore()),
        match_result_store=None,
    )
    runtime.require_draft_store = lambda: DraftStore()

    detail = AdminService(runtime, session_store=runtime.conversation_service.session_store).conversation_detail("session-1")

    assert detail["drafts"][0]["draft"]["attachments"] == [attachment]
    assert detail["attachments"][0]["document_id"] == "doc-1"
    assert detail["attachments"][0]["file_name"] == "方案资料.pdf"
    assert detail["attachments"][0]["file_url"].endswith("doc-1/file?user_role=admin&user_id=admin")


def test_admin_conversation_owner_is_resolved_from_linked_admin_draft():
    class SessionStore:
        def list_all_sessions_page(self, *, page, page_size):
            assert (page, page_size) == (1, 20)
            return {
                "items": [{"session_id": "file-1", "user_id": None, "user_role": None}],
                "total": 1,
                "page": page,
                "page_size": page_size,
                "total_pages": 1,
            }

    class DraftStore:
        def owner_hints_for_threads(self, thread_ids):
            assert thread_ids == ["file-1"]
            return {"file-1": {"user_id": "admin3", "user_role": "admin"}}

        def list_all(self, **_kwargs):
            raise AssertionError("session catalog must not scan all drafts")

    runtime = SimpleNamespace(
        task_store=None,
        sync_state_store=None,
        user_identity_reader=None,
        conversation_service=SimpleNamespace(session_store=SessionStore()),
    )
    runtime.require_draft_store = lambda: DraftStore()

    item = AdminService(runtime, session_store=runtime.conversation_service.session_store).conversations()["items"][0]

    assert item["user_id"] == "admin3"
    assert item["user_role"] == "admin"
    assert item["user_name"] == "管理员"
