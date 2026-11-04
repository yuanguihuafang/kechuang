from __future__ import annotations

from core.context_window import (
    ContextWindowPolicy,
    build_summary_input,
    limit_draft_context,
    user_history_text,
)


def test_summary_input_has_a_single_hard_character_budget():
    previous, messages = build_summary_input(
        "旧摘要" * 2000,
        [f"消息{i}" * 2000 for i in range(20)],
    )

    assert len(previous) + sum(len(message) for message in messages) <= 6000
    assert messages[-1].startswith("消息19")


def test_draft_context_limits_nested_source_blocks_without_losing_field_shape():
    context = limit_draft_context(
        {
            "draft_type": "demand",
            "fields": {"company": "青岛檬豆"},
            "text": "x" * 50000,
            "source_blocks": [{"id": "block-1", "text": "y" * 50000}],
        }
    )

    assert context["fields"]["company"] == "青岛檬豆"
    assert len(context["text"]) <= 8000
    assert len(context["source_blocks"][0]["text"]) <= 4000


def test_user_history_text_excludes_assistant_guidance():
    text = user_history_text(
        [
            "user: 企业名称是青岛檬豆",
            "assistant: 请继续补充预算和周期",
            "user: 提供智能化改造方案",
        ]
    )

    assert "企业名称是青岛檬豆" in text
    assert "提供智能化改造方案" in text
    assert "请继续补充预算和周期" not in text


def test_context_policy_caps_environment_values_at_production_limits(monkeypatch):
    monkeypatch.setenv("CONTEXT_SUMMARY_INPUT_CHARS", "20000")
    monkeypatch.setenv("CONTEXT_ROLLING_SUMMARY_CHARS", "20000")
    monkeypatch.setenv("CONTEXT_DRAFT_TEXT_CHARS", "20000")
    monkeypatch.setenv("CONTEXT_SOURCE_BLOCK_TEXT_CHARS", "20000")
    monkeypatch.setenv("REASONING_CONTEXT_MAX_TOKENS", "20000")

    policy = ContextWindowPolicy.from_env()

    assert policy.summary_input_chars == 6000
    assert policy.rolling_summary_chars == 1200
    assert policy.draft_text_chars == 8000
    assert policy.source_block_text_chars == 4000
    assert policy.max_tokens == 12000
