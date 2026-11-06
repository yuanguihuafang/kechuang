from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "OPIEos"
DRAFT_SKILLS = ("kc-demand-draft", "kc-solution-draft", "kc-achievement-draft")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_agent_published_draft_rule_requires_choice_before_mutation():
    soul = _read(SOURCE_ROOT / "agents/kechuang/SOUL.md")

    assert "已发布草稿后续处理" in soul
    assert "更新该草稿" in soul
    assert "创建新草稿" in soul
    assert "确认前不得调用" in soul


def test_external_customer_visit_request_uses_neutral_capability_message():
    soul = _read(SOURCE_ROOT / "agents/kechuang/SOUL.md")

    assert "不调用拜访工具" in soul
    assert "不要说用户“没有权限”或“无权访问”" in soul
    assert "正常提到拜访不构成拜访功能请求" in soul


def test_draft_skills_define_published_draft_update_or_create_flow():
    for skill_name in DRAFT_SKILLS:
        skill = _read(
            SOURCE_ROOT / f"skills/custom/{skill_name}/SKILL.md"
        )

        assert "已发布草稿后续处理" in skill, skill_name
        assert "expected_version" in skill, skill_name
        assert "确认前不得调用" in skill, skill_name
        assert "kc_draft_create" in skill, skill_name
