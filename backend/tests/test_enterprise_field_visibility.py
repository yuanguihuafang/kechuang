from agents.retrieval_agent.retrieval.field_policy import split_business_fields
from agents.retrieval_agent.retrieval.search_service import (
    filter_external_demand_hits,
    filter_external_enterprise_hits,
)
from agents.retrieval_agent.schemas import RetrievalHit
from core.contracts.field_permissions import INTERNAL_FIELDS, PUBLIC_FIELDS, list_field_policies
from core.contracts.mysql_schema import business_fields
from core.governance.access_policy import ManualIdentity
from core.governance.redaction import TextRedactionService
from services.mysql.business_search import MySQLBusinessSearchService


def test_enterprise_creator_and_business_scope_are_internal_only():
    assert {"created_by", "business_scope"} <= INTERNAL_FIELDS["enterprise"]
    assert not {"created_by", "business_scope"} & PUBLIC_FIELDS["enterprise"]
    assert {"phone", "email", "address", "social_staff_num"} <= PUBLIC_FIELDS["enterprise"]
    assert not {"phone", "email", "address", "social_staff_num"} & INTERNAL_FIELDS["enterprise"]
    assert {"created_by", "business_scope"} <= set(business_fields("enterprise"))


def test_demand_cover_is_public_and_kept_in_mysql_sync():
    assert "first_img_url" in PUBLIC_FIELDS["demand"]
    assert "first_img_url" not in INTERNAL_FIELDS["demand"]

    public, internal, known = split_business_fields(
        "demand", {"first_img_url": "/uploads/demand-cover.png", "link_tel": "13812345678"}
    )

    assert known
    assert public == {"first_img_url": "/uploads/demand-cover.png"}
    assert internal == {"link_tel": "13812345678"}

    service = MySQLBusinessSearchService()
    service.list_for_sync = lambda *_args, **_kwargs: [
        {"id": 9, "first_img_url": "/uploads/demand-cover.png", "link_tel": "13812345678"}
    ]
    [record] = service.records_for_sync("demand")

    assert record.fields["first_img_url"] == "/uploads/demand-cover.png"
    assert "/uploads/demand-cover.png" in record.text


def test_demand_sensitive_fields_are_indexed_but_hidden_from_external_policy():
    hidden_fields = {"link_name", "link_tel"}
    public_fields = {"pub_dept", "company"}
    assert hidden_fields <= INTERNAL_FIELDS["demand"]
    assert not hidden_fields & PUBLIC_FIELDS["demand"]
    assert public_fields <= PUBLIC_FIELDS["demand"]
    assert not public_fields & INTERNAL_FIELDS["demand"]
    assert hidden_fields <= set(business_fields("demand"))
    assert public_fields <= set(business_fields("demand"))

    service = MySQLBusinessSearchService()
    service.list_for_sync = lambda *_args, **_kwargs: [{
        "id": 10,
        "title": "技术需求",
        "link_name": "联系人甲",
        "link_tel": "13812345678",
        "pub_dept": "发布部门甲",
        "company": "需求企业甲",
    }]
    [record] = service.records_for_sync("demand")

    assert hidden_fields | public_fields <= set(record.fields)
    assert all(value in record.text for value in ("联系人甲", "13812345678", "发布部门甲", "需求企业甲"))

    sanitized = TextRedactionService().sanitize_demand_payload({
        "knowledge_type": "demand",
        "link_name": "联系人甲",
        "link_tel": "13812345678",
        "pub_dept": "发布部门甲",
        "company": "需求企业甲",
    })
    assert sanitized == {
        "knowledge_type": "demand",
        "pub_dept": "发布部门甲",
        "company": "需求企业甲",
    }

    policies = {policy["type"]: policy for policy in list_field_policies()}
    assert set(policies["enterprise"]["internal"]) == {"created_by", "business_scope"}
    assert set(policies["demand"]["internal"]) == hidden_fields
    assert public_fields <= set(policies["demand"]["public"])


def test_enterprise_internal_only_fields_cannot_be_promoted_by_document_override():
    public, internal, known = split_business_fields(
        "enterprise",
        {"enterprise_name": "甲公司", "created_by": "内部员工", "business_scope": "仅内部"},
        {"created_by": "public", "business_scope": "public"},
    )

    assert known
    assert public == {"enterprise_name": "甲公司"}
    assert internal == {"created_by": "内部员工", "business_scope": "仅内部"}


def test_enterprise_mysql_sync_keeps_internal_fields_for_internal_retrieval():
    service = MySQLBusinessSearchService()
    service.list_for_sync = lambda *_args, **_kwargs: [
        {
            "id": 7,
            "enterprise_name": "甲公司",
            "created_by": "内部员工",
            "business_scope": "仅内部经营范围",
            "social_staff_num": "300",
            "phone": "13812345678",
            "email": "contact@example.com",
            "address": "内部地址",
        }
    ]

    [record] = service.records_for_sync("enterprise")

    assert record.fields == {
        "enterprise_name": "甲公司",
        "phone": "13812345678",
        "email": "contact@example.com",
        "address": "内部地址",
        "created_by": "内部员工",
        "business_scope": "仅内部经营范围",
        "social_staff_num": "300",
    }
    assert "内部员工" in record.text
    assert "仅内部经营范围" in record.text
    assert set(record.metadata["field_names"]) == set(record.fields)


def test_enterprise_single_record_sync_keeps_internal_fields():
    service = MySQLBusinessSearchService()
    service.list_for_sync = lambda *_args, **_kwargs: [
        {"id": 8, "enterprise_name": "乙公司", "created_by": "内部员工"}
    ]

    record, next_id = service.next_record_for_sync("enterprise")

    assert next_id == 8
    assert record.fields == {"enterprise_name": "乙公司", "created_by": "内部员工"}
    assert "内部员工" in record.text


def test_external_enterprise_search_hides_internal_fields_without_dropping_public_data():
    hit = RetrievalHit(
        record_id="enterprise-7",
        score=0.9,
        source="public",
        metadata={
            "source_knowledge_type": "enterprise",
            "chunk_role": "mysql",
            "mysql_id": "7",
            "field_names": [
                "enterprise_name", "created_by", "business_scope", "social_staff_num",
                "phone", "email", "address",
            ],
            "chunk_text": (
                "企业名称：甲公司\n创建人：内部员工\n"
                "经营范围：仅内部经营范围\n包含多行的内部经营补充\n"
                "参保人数：300\n联系电话：13812345678\n"
                "联系邮箱：contact@example.com\n通讯地址：内部地址"
            ),
        },
    )

    [external_hit] = filter_external_enterprise_hits(
        [hit], ManualIdentity(user_id="28", user_role="external")
    )

    assert external_hit.metadata["field_names"] == [
        "enterprise_name", "social_staff_num", "phone", "email", "address"
    ]
    assert "企业名称：甲公司" in external_hit.metadata["chunk_text"]
    assert "参保人数：300" in external_hit.metadata["chunk_text"]
    assert "联系电话：13812345678" in external_hit.metadata["chunk_text"]
    assert "联系邮箱：contact@example.com" in external_hit.metadata["chunk_text"]
    assert "通讯地址：内部地址" in external_hit.metadata["chunk_text"]
    assert external_hit is not hit
    assert "内部员工" not in external_hit.metadata["chunk_text"]
    assert "仅内部经营范围" not in external_hit.metadata["chunk_text"]


def test_external_enterprise_search_hides_indented_legacy_internal_field():
    hit = RetrievalHit(
        record_id="enterprise-9",
        score=0.9,
        source="public",
        metadata={
            "source_knowledge_type": "enterprise",
            "chunk_role": "mysql",
            "field_names": ["enterprise_name", "business_scope", "social_staff_num"],
            "chunk_text": (
                "企业名称：甲公司\n 经营范围 ：内部经营内容\n"
                " 仍属于经营范围的续行内容\n参保人数：300"
            ),
        },
    )

    [external_hit] = filter_external_enterprise_hits(
        [hit], ManualIdentity(user_id="28", user_role="external")
    )

    assert external_hit.metadata["chunk_text"] == "企业名称：甲公司\n参保人数：300"


def test_external_enterprise_search_fails_closed_on_unknown_legacy_field_label():
    hit = RetrievalHit(
        record_id="enterprise-10",
        score=0.9,
        source="public",
        metadata={
            "source_knowledge_type": "enterprise",
            "chunk_role": "mysql",
            "field_names": ["enterprise_name", "social_staff_num"],
            "chunk_text": "企业名称：甲公司\n内部扩展字段：不应对外显示\n后续私有内容\n参保人数：300",
        },
    )

    [external_hit] = filter_external_enterprise_hits(
        [hit], ManualIdentity(user_id="28", user_role="external")
    )

    assert external_hit.metadata["chunk_text"] == "企业名称：甲公司\n参保人数：300"


def test_external_enterprise_search_fails_closed_on_malformed_legacy_field_line():
    hit = RetrievalHit(
        record_id="enterprise-malformed",
        score=0.9,
        source="public",
        metadata={
            "source_knowledge_type": "enterprise",
            "chunk_role": "mysql",
            "field_names": ["enterprise_name", "social_staff_num"],
            "chunk_text": "企业名称：甲公司\n经营范围 内部经营内容\n内部续行内容\n参保人数：300",
        },
    )

    [external_hit] = filter_external_enterprise_hits(
        [hit], ManualIdentity(user_id="28", user_role="external")
    )

    assert external_hit.metadata["chunk_text"] == "企业名称：甲公司\n参保人数：300"


def test_external_enterprise_search_preserves_all_fields_for_internal_users():
    hit = RetrievalHit(
        record_id="enterprise-7",
        score=0.9,
        source="public",
        metadata={
            "source_knowledge_type": "enterprise",
            "chunk_role": "mysql",
            "mysql_id": "7",
            "field_names": ["enterprise_name", "created_by"],
            "chunk_text": "企业名称：甲公司\n创建人：内部员工",
        }
    )

    internal_hits = filter_external_enterprise_hits(
        [hit], ManualIdentity(user_id="17", user_role="internal")
    )

    assert internal_hits[0] is hit
    assert "创建人：内部员工" in internal_hits[0].metadata["chunk_text"]


def test_external_enterprise_mysql_hit_filters_structured_fields():
    hit = RetrievalHit(
        record_id="mysql:enterprise:7",
        score=0.8,
        source="mysql",
        metadata={
            "knowledge_type": "enterprise",
            "storage": "mysql",
            "fields": {
                "enterprise_name": "甲公司",
                "created_by": "内部员工",
                "phone": "13812345678",
            },
            "chunk_text": "enterprise_name：甲公司\ncreated_by：内部员工\nphone：13812345678",
        },
    )

    [external_hit] = filter_external_enterprise_hits(
        [hit], ManualIdentity(user_id="28", user_role="external")
    )

    assert external_hit.metadata["fields"] == {
        "enterprise_name": "甲公司", "phone": "13812345678"
    }
    assert external_hit.metadata["field_names"] == ["enterprise_name", "phone"]
    assert external_hit.metadata["chunk_text"] == (
        "enterprise_name：甲公司\nphone：13812345678"
    )


def test_external_demand_mysql_hit_removes_contacts_before_model_context():
    hit = RetrievalHit(
        record_id="mysql:demand:1680",
        score=0.9,
        source="mysql",
        metadata={
            "knowledge_type": "demand",
            "storage": "mysql",
            "fields": {
                "title": "车货匹配与线路优化",
                "link_name": "于静静",
                "link_tel": "0531-88257102",
                "pub_dept": "联盟秘书处",
            },
            "chunk_text": "标题：车货匹配与线路优化\n联系人：于静静\n联系电话：0531-88257102",
        },
    )

    [external_hit] = filter_external_demand_hits(
        [hit], ManualIdentity(user_id="28", user_role="external")
    )

    assert external_hit.metadata["fields"] == {
        "title": "车货匹配与线路优化", "pub_dept": "联盟秘书处"
    }
    assert "于静静" not in external_hit.metadata["chunk_text"]
    assert "0531-88257102" not in external_hit.metadata["chunk_text"]


def test_external_demand_legacy_hit_removes_colonless_contact_lines():
    hit = RetrievalHit(
        record_id="mysql:demand:1680",
        score=0.9,
        source="mysql",
        metadata={
            "knowledge_type": "demand",
            "chunk_role": "mysql",
            "field_names": ["title", "link_name", "link_tel", "pub_dept"],
            "chunk_text": (
                "需求名称：车货匹配与线路优化\n联系人于静静\n"
                "联系电话 0531-88257102\n发布部门：联盟秘书处"
            ),
        },
    )

    [external_hit] = filter_external_demand_hits(
        [hit], ManualIdentity(user_id="28", user_role="external")
    )

    assert external_hit.metadata["chunk_text"] == (
        "需求名称：车货匹配与线路优化\n发布部门：联盟秘书处"
    )
