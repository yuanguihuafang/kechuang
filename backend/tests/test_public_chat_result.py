from types import SimpleNamespace

import pytest

from core.contracts.chat import ChatRequest, ChatResponse
from core.governance.redaction import TextRedactionService
from services.application.chat_response import sanitize_document_references
from services.application.public_chat_result import attach_debug_result, public_chat_result


def test_public_chat_result_keeps_llm_answer_with_match_results():
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": "根据您的需求，优先推荐第一项方案，原因是技术路线匹配度更高。",
            "match_results": [{"id": "match-1", "title": "方案一", "score": 0.91}],
            "evidences": [],
            "actions": [],
            "ui_blocks": [],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result)

    assert public["match_results"]
    assert public["answer"] == "根据您的需求，优先推荐第一项方案，原因是技术路线匹配度更高。"
    assert "evidences" not in public
    assert "evidences" not in public["match_results"][0]


def test_public_match_card_keeps_full_details_for_the_details_dialog():
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": "已找到企业画像。",
            "match_results": [
                {
                    "id": "mysql:enterprise:1",
                    "title": "青岛檬豆网络科技有限公司",
                    "summary": "企业简介：科技服务",
                    "details": "企业名称：青岛檬豆网络科技有限公司\n统一社会信用代码：9137...",
                    "chunk_role": "mysql",
                }
            ],
            "evidences": [],
            "actions": [],
            "ui_blocks": [],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result)

    assert "统一社会信用代码" in public["match_results"][0]["details"]


def test_external_demand_match_cards_are_sanitized_without_changing_supply_cards():
    demand = {
        "id": "demand-1",
        "title": "技术需求",
        "summary": "联系人：张三；联系电话：13812345678；发布部门：研发部",
        "details": "技术类别：设计；联系人：张三；联系电话：13812345678；发布部门：研发部",
        "knowledge_type": "demand",
        "score": 0.9,
    }
    supply = {
        "id": "solution-1",
        "title": "技术方案",
        "summary": "联系人：李四；联系电话：13912345678",
        "details": "联系人：李四；联系电话：13912345678",
        "knowledge_type": "solution",
        "score": 0.8,
    }
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": "已找到匹配结果。",
            "match_results": [demand, supply],
            "evidences": [],
            "actions": [],
            "ui_blocks": [
                {
                    "type": "recommendation_list",
                    "data": {"items": [demand, supply]},
                }
            ],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result, user_role="external")
    demand_card = next(
        item for item in public["match_results"] if item["knowledge_type"] == "demand"
    )
    supply_card = next(
        item for item in public["match_results"] if item["knowledge_type"] == "solution"
    )
    block_items = public["ui_blocks"][0]["data"]["items"]
    demand_block = next(item for item in block_items if item["knowledge_type"] == "demand")
    supply_block = next(item for item in block_items if item["knowledge_type"] == "solution")

    assert demand_card["details"] == "技术类别：设计；发布部门：研发部"
    assert demand_card["summary"] == "发布部门：研发部"
    assert demand_block["details"] == "技术类别：设计；发布部门：研发部"
    assert demand_block["summary"] == "发布部门：研发部"
    assert "李四" in supply_card["details"]
    assert "13912345678" in supply_card["details"]
    assert "李四" in supply_block["details"]
    assert "13912345678" in supply_block["details"]


def test_external_demand_answer_and_answer_block_are_sanitized():
    sensitive_answer = "已找到需求；联系人：张三；联系电话：13812345678"
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": sensitive_answer,
            "match_results": [{"id": "demand-1", "knowledge_type": "demand"}],
            "actions": [],
            "ui_blocks": [{"type": "answer", "data": {"text": sensitive_answer}}],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result, user_role="external")

    assert public["answer"] == "已找到需求"
    assert public["ui_blocks"][0]["data"]["text"] == "已找到需求"


@pytest.mark.parametrize("intent", [{"name": "demand_discovery"}, {"business_intent": "demand_discovery"}])
def test_external_demand_discovery_answer_is_sanitized_without_demand_cards(intent):
    sensitive_answer = (
        "联系人：张三；联系电话：13812345678；地址：青岛市某路1号；"
        "邮箱：contact@example.com；统一社会信用代码：91370212ABCDE12345；发布部门：研发部"
    )
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": sensitive_answer,
            "intent": intent,
            "match_results": [],
            "actions": [],
            "ui_blocks": [{"type": "answer", "data": {"text": sensitive_answer}}],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result, user_role="external")

    assert "张三" not in public["answer"]
    assert "13812345678" not in public["answer"]
    assert "青岛市某路1号" not in public["answer"]
    assert "contact@example.com" not in public["answer"]
    assert "91370212ABCDE12345" not in public["answer"]
    assert "发布部门：研发部" in public["answer"]
    assert public["ui_blocks"][0]["data"]["text"] == public["answer"]


def test_external_non_demand_answer_is_unchanged():
    answer = "技术方案联系人：李四；联系电话：13912345678"
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": answer,
            "match_results": [{"id": "solution-1", "knowledge_type": "solution"}],
            "actions": [],
            "ui_blocks": [],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result, user_role="external")

    assert public["answer"] == answer


def test_external_response_does_not_expose_customer_visit_draft_or_discuss_visit_records():
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": "客户拜访记录已整理，请查看草稿。",
            "intent": {"name": "customer_visit"},
            "draft": {
                "draft_id": "visit-1",
                "draft_type": "customer_visit",
                "title": "客户拜访记录",
                "fields": {"company": "内部客户"},
            },
            "drafts": [],
            "match_results": [],
            "actions": [{"id": "save_draft", "type": "SAVE_DRAFT"}],
            "ui_blocks": [
                {
                    "type": "draft_card",
                    "data": {"draft": {"draft_type": "customer_visit", "title": "客户拜访记录"}},
                }
            ],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result, user_role="external")

    assert public["answer"] == "当前暂不支持处理客户拜访记录，您可以继续整理需求、方案或成果草稿。"
    assert "draft" not in public
    assert public["drafts"] == []
    assert public["match_results"] == []
    assert public["actions"] == []
    assert public["intent"] is None
    assert public["ui_blocks"] == [
        {
            "type": "answer",
            "data": {
                "text": "当前暂不支持处理客户拜访记录，您可以继续整理需求、方案或成果草稿。",
                "status": "SUCCESS",
            },
        }
    ]


@pytest.mark.parametrize(
    ("answer", "documents"),
    [
        ("客户拜访管理系统支持企业安排外勤计划。", []),
        ("已找到相关资料。", [{"title": "拜访记录管理规范", "content": "客户拜访流程介绍"}]),
    ],
)
def test_external_response_can_mention_customer_visit_normally(answer, documents):
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": answer,
            "documents": documents,
            "match_results": [],
            "actions": [],
            "ui_blocks": [],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result, user_role="external")

    assert public["answer"] == answer
    assert public["documents"] == documents


def test_external_visit_intent_without_draft_is_declined_without_permission_language():
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": "已为您整理客户拜访记录。",
            "intent": {"business_intent": "customer_visit"},
            "match_results": [],
            "actions": [],
            "ui_blocks": [],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result, user_role="external")

    assert public["answer"] == "当前暂不支持处理客户拜访记录，您可以继续整理需求、方案或成果草稿。"
    assert "权限" not in public["answer"]


def test_public_chat_result_exposes_only_safe_live_intent_fields():
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "MATCH_CONFIRM_REQUIRED",
            "answer": "请确认是否开始匹配。",
            "intent": {
                "name": "supply_demand_match",
                "business_intent": "supply_demand_match",
                "business_knowledge_types": ["solution"],
                "confirmation_required": True,
                "entities": {
                    "next_match_batch": True,
                    "match_direction": "demand_to_supply",
                    "internal_prompt": "不要返回",
                },
                "internal_prompt": "不要返回",
            },
            "internal_flow": {"secret": "不要返回"},
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result)

    assert public["intent"] == {
        "confirmation_required": True,
        "business_intent": "supply_demand_match",
        "business_knowledge_types": ["solution"],
        "name": "supply_demand_match",
        "entities": {
            "next_match_batch": True,
            "match_direction": "demand_to_supply",
        },
    }


def test_public_chat_result_keeps_test_debug_llm_exchange():
    result = SimpleNamespace(
        model_dump=lambda mode=None: {
            "status": "SUCCESS",
            "answer": "已完成。",
            "intent": {"name": "supply_demand_match", "query": "工业机器人"},
            "debug": {
                "context": {
                    "draft": {"title": "机器人需求"},
                    "query": "工业机器人",
                    "match_results": [{"id": "match-1"}],
                },
                "llm_exchanges": [
                    {
                        "messages": [{"role": "user", "content": "实际提示词"}],
                        "output": "实际模型输出",
                    }
                ],
            },
            "match_results": [],
            "evidences": [],
            "actions": [],
            "ui_blocks": [],
        },
        thread_id="thread-1",
    )

    public = public_chat_result(result)

    assert public["debug"]["context"]["query"] == "工业机器人"
    assert public["debug"]["context"]["draft"]["title"] == "机器人需求"
    assert public["debug"]["llm_exchanges"][0]["output"] == "实际模型输出"
    assert "output" not in public["debug"]
    assert "match_results" not in public["debug"]


def test_debug_context_includes_persisted_active_draft_and_selected_type():
    draft = {
        "draft_id": "draft-1",
        "draft_type": "demand",
        "title": "扫地机器人采购需求",
        "fields": {"category": "物料采购", "analyse_contect": "采购用于办公场所的扫地机器人。"},
    }

    class SessionStore:
        def get(self, thread_id):
            assert thread_id == "thread-1"
            return {
                "state": {
                    "chat_flow": {
                        "active_draft": draft,
                        "request_context": {
                            "draft_context": {
                                "draft_id": "draft-1",
                                "draft_type": "demand",
                                "demand_type": "物料采购",
                            }
                        },
                    }
                }
            }

    payload = SimpleNamespace(
        debug=True,
        thread_id="thread-1",
        model_dump=lambda mode=None: {"draft_context": {"demand_type": "物料采购"}},
    )
    result = ChatResponse(status="DRAFT_GENERATED", answer="草稿已生成。", thread_id="thread-1")

    debug = attach_debug_result(result, payload, SessionStore()).debug["context"]

    assert debug["active_draft"] == draft
    assert debug["draft_context"]["demand_type"] == "物料采购"


def test_external_documents_hide_demand_and_allow_published_supply_files():
    documents = sanitize_document_references(
        ChatRequest(message="匹配方案", user_id="external-1", user_role="external"),
        [
            {
                "document_id": "demand-1",
                "knowledge_type": "demand",
                "source_name": "需求.pdf",
                "file_url": "/demand",
            },
            {
                "document_id": "solution-1",
                "knowledge_type": "solution",
                "source_name": "方案.pdf",
                "file_url": "/solution",
            },
            {
                "document_id": "achievement-1",
                "knowledge_type": "achievement",
                "source_name": "成果.pdf",
                "file_url": "/achievement",
            },
        ],
    )

    assert [item["document_id"] for item in documents] == ["solution-1", "achievement-1"]
    assert all(item["can_download"] and item["file_url"] for item in documents)


def test_demand_redaction_recurses_through_record_wrappers():
    payload = [
        {
            "record_id": "demand-1",
            "evidences": [
                {
                    "knowledge_type": "demand",
                    "text": "技术类别：设计；联系人：张三；联系电话：13812345678；发布部门：研发部",
                }
            ],
        }
    ]

    sanitized = TextRedactionService().sanitize_demand_payload(payload)

    assert sanitized[0]["evidences"][0]["text"] == "技术类别：设计；发布部门：研发部"


def test_nested_demand_contact_fields_are_omitted_but_public_fields_remain():
    card = {"knowledge_type": "demand", "fields": {
                "title": "技术需求", "link_name": "需求张三", "link_tel": "13812345678",
                "pub_dept": "发布部门", "company": "需求企业"
            },
            "related": [{"knowledge_type": "solution", "fields": {"link_name": "供给李四", "link_tel": "13912345678"}}]}
    clean = TextRedactionService().sanitize_demand_payload(card)
    assert clean["fields"] == {"title": "技术需求", "pub_dept": "发布部门", "company": "需求企业"}
    assert clean["related"][0] == card["related"][0]
    assert card["fields"]["link_name"] == "需求张三"
