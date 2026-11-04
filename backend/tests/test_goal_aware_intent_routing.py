from pathlib import Path


def test_agent_rules_prioritize_goal_and_quick_entry_alignment():
    soul_path = (
        Path(__file__).resolve().parents[2]
        / "OPIEos"
        / "agents"
        / "kechuang"
        / "SOUL.md"
    )
    rules = soul_path.read_text(encoding="utf-8")

    assert "目标优先于局部关键词" in rules
    assert "快速入口与本轮目标明显不一致" in rules
    assert "拜访前准备" in rules
    assert "资料来源" in rules
    assert "联网公开资料、内部知识库还是两者结合" in rules
    assert "外部用户不调用仅限内部的拜访能力" in rules
    assert "有名称或技术主题即检索" not in rules
    assert "只能依据系统注入并经后端校验的角色" in rules


def test_customer_visit_skill_supports_previsit_research_without_saving_a_record():
    skill_path = (
        Path(__file__).resolve().parents[2]
        / "OPIEos"
        / "skills"
        / "custom"
        / "kc-customer-visit"
        / "SKILL.md"
    )
    skill = skill_path.read_text(encoding="utf-8")

    assert "kc_retrieval" in skill
    assert "kc_web_search" in skill
    assert "kc_web_fetch" in skill
    assert "拜访前准备" in skill
    assert "不创建、保存或发布拜访记录" in skill
