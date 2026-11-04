import asyncio

from agents.perception_agent.application.draft_extractor import (
    compact_document_summary,
    extract_material_facts,
    material_segments,
    split_material,
)


def test_document_summary_keeps_paragraphs_without_blank_lines_and_is_limited_to_500_chars():
    paragraph = "这是资料中的重点内容。" * 20

    result = compact_document_summary("\n\n".join([
        paragraph + "第一部分。",
        paragraph + "第二部分。",
        paragraph + "第三部分。",
    ]))

    assert len(result) <= 500
    assert "\n" in result
    assert "\n\n" not in result


def test_document_summary_streams_only_the_final_limited_text():
    streamed: list[str] = []

    class Client:
        async def complete(self, prompt: str) -> str:
            assert "分段总结" in prompt
            assert "不要编号" in prompt
            assert "不要输出“名称：”或“描述：”字段标签" in prompt
            return "第一段总结。\n\n第二段总结。"

    async def on_token(value: str) -> None:
        streamed.append(value)

    result, segment_count = asyncio.run(
        extract_material_facts(
            "资料原文",
            "document",
            client=Client(),
            on_token=on_token,
        )
    )

    assert segment_count == 1
    assert result == "第一段总结。\n第二段总结。"
    assert "".join(streamed) == result


def test_document_summary_drops_incomplete_trailing_paragraph():
    result = compact_document_summary(
        "第一项完整内容。\n第二项是一个很长的内容但没有句末标点",
        max_chars=16,
    )

    assert result == "第一项完整内容。"


def test_document_summary_merges_numbered_name_and_description_lines():
    result = compact_document_summary(
        "1. 名称：义齿工厂业务接单智能体（面向义齿厂商务/客服）\n"
        "描述：内置全品类义齿产品库，支持门诊咨询应答和订单进度查询。\n"
        "2. 名称：义齿工艺知识库智能体\n"
        "描述：导入私有工艺知识库，支持技工问答和返工案例复盘。"
    )

    assert result == (
        "义齿工厂业务接单智能体（面向义齿厂商务/客服）："
        "内置全品类义齿产品库，支持门诊咨询应答和订单进度查询。\n"
        "义齿工艺知识库智能体：导入私有工艺知识库，支持技工问答和返工案例复盘。"
    )


def test_material_segments_splits_oversized_page_blocks_without_dropping_text():
    source = "甲" * 24000

    segments = material_segments(
        source,
        [{"page_number": 1, "text": source}],
    )

    assert [len(segment) for segment in segments] == [12000, 12000]
    assert "".join(segments) == source


def test_material_split_rejects_content_beyond_extraction_call_limit():
    import pytest

    with pytest.raises(ValueError, match="超过单次解析上限"):
        split_material("甲" * 60001)
