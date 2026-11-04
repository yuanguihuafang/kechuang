import re

from runtime.thread_identity import resolve_deerflow_thread_id


def test_valid_legacy_thread_id_is_preserved():
    assert resolve_deerflow_thread_id("session-abc_123", "user-1") == "session-abc_123"


def test_invalid_thread_id_maps_to_a_stable_deerflow_safe_id():
    mapped = resolve_deerflow_thread_id("历史会话/含有超长内容" * 10, "user-1")

    assert mapped == resolve_deerflow_thread_id("历史会话/含有超长内容" * 10, "user-1")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mapped)
    assert len(mapped) <= 64


def test_invalid_thread_id_mapping_is_namespaced_by_user():
    external_id = "历史会话/1"

    assert resolve_deerflow_thread_id(external_id, "user-1") != resolve_deerflow_thread_id(
        external_id, "user-2"
    )
