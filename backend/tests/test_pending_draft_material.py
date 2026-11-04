from assistant.application.pending_draft_material import (
    attach_pending_draft_material,
    pending_draft_material_for_response,
    valid_pending_draft_material,
)


def test_multi_item_confirmation_saves_full_material_not_summary():
    full_text = "完整解析正文：" + "多个独立事项。" * 1000

    pending = pending_draft_material_for_response(
        "DRAFT_MULTI_CONFIRM_REQUIRED",
        user_message="整理草稿",
        full_material=full_text,
        file_name="资料.docx",
        now=100,
    )

    assert pending["text"] == full_text
    assert pending["file_name"] == "资料.docx"
    assert pending["expires_at"] == 1900


def test_only_multi_item_confirmation_keeps_material_pending():
    assert pending_draft_material_for_response(
        "DRAFT_GENERATED", user_message="整理草稿", full_material="资料", now=100
    ) is None


def test_intent_confirmation_keeps_full_upload_for_the_drafting_choice():
    source = "完整原文\n" + "不截断。" * 100
    pending = pending_draft_material_for_response(
        "INTENT_CONFIRM_REQUIRED", user_message="上传资料", full_material=source, now=100
    )

    assert pending["text"] == source


def test_summary_only_upload_keeps_full_source_for_a_later_draft_request():
    source = "第八项独立方案不能丢失。"
    pending = pending_draft_material_for_response(
        "MATERIAL_CONTEXT_AVAILABLE",
        user_message="",
        full_material=source,
        file_name="资料.docx",
        now=100,
    )

    assert pending["text"] == source
    assert pending["file_name"] == "资料.docx"


def test_pending_material_is_expired_and_attached_as_untrusted_context():
    pending = {"text": "完整原文", "file_name": "资料.docx", "expires_at": 100}

    assert valid_pending_draft_material(pending, now=101) is None
    attached = attach_pending_draft_material("拆分生成多个", pending)
    assert "<untrusted_material>" in attached
    assert "完整原文" in attached
    assert "</untrusted_material>" in attached
