"""MySQL 业务库检索适配器。

内部业务字段以关系库为准，不复制到内部 Milvus Collection。该适配器只读，
使用共享 MySQL 表单契约生成安全的表名和字段白名单。
"""

from __future__ import annotations

import os
import html
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - 运行环境依赖负责提供
    pymysql = None
    DictCursor = Any

from core.contracts.mysql_schema import (
    MYSQL_FIELD_COMMENTS,
    MYSQL_FORM_CONTRACTS,
    all_mysql_fields,
    business_fields,
    mysql_fields_to_text,
    ordered_fields,
)

if TYPE_CHECKING:
    from agents.retrieval_agent.schemas import RetrievalHit


class MySQLBusinessSearchService:
    """只读查询 MySQL 业务表，返回与 Milvus 兼容的检索证据。"""

    def __init__(self, settings: Any | None = None, connect_factory: Callable[..., Any] | None = None) -> None:
        self.settings = settings
        self._connect_factory = connect_factory
        self._column_comments: dict[str, dict[str, str]] = {}

    def _comments_for_sync(self, knowledge_type: str) -> dict[str, str]:
        """同步固定使用手工维护的中文字段说明，不被数据库注释覆盖。"""
        return dict(MYSQL_FIELD_COMMENTS.get(knowledge_type, {}))

    def _connect(self) -> Any:
        if self._connect_factory is not None:
            return self._connect_factory()
        if pymysql is None:
            raise RuntimeError("未安装 PyMySQL，无法查询内部业务库")
        settings = self.settings
        return pymysql.connect(
            host=getattr(settings, "host", os.getenv("MYSQL_HOST")),
            port=int(getattr(settings, "port", os.getenv("MYSQL_PORT"))),
            user=getattr(settings, "user", os.getenv("MYSQL_USER")),
            password=getattr(settings, "password", os.getenv("MYSQL_PASSWORD")),
            database=getattr(settings, "database", os.getenv("MYSQL_DATABASE")),
            charset="utf8mb4",
            connect_timeout=int(getattr(settings, "connect_timeout", 10)),
            cursorclass=DictCursor,
            autocommit=True,
        )

    def search(
        self,
        query: str,
        *,
        knowledge_type: str | None = None,
        top_k: int = 10,
        include_system_fields: bool = False,
    ) -> list[RetrievalHit]:
        terms = _query_terms(query)
        if not terms:
            return []
        types = [knowledge_type] if knowledge_type else list(MYSQL_FORM_CONTRACTS)
        types = [item for item in types if item in MYSQL_FORM_CONTRACTS and MYSQL_FORM_CONTRACTS[item].get("table")]
        connection = self._connect()
        try:
            hits: list[RetrievalHit] = []
            for item in types:
                hits.extend(self._search_table(connection, item, terms, top_k, include_system_fields))
            return sorted(hits, key=lambda hit: hit.score, reverse=True)[: max(1, top_k)]
        finally:
            connection.close()

    def list_for_sync(self, knowledge_type: str, *, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """读取一张业务表的完整行；仅供索引同步，绝不写回 MySQL。"""
        contract = MYSQL_FORM_CONTRACTS.get(knowledge_type)
        if not contract or not contract.get("table"):
            raise ValueError(f"不支持同步的 MySQL 表单类型：{knowledge_type}")
        if not contract.get("public_fields"):
            raise ValueError(f"{knowledge_type} 没有可公开检索字段，不建立 MySQL 向量索引")
        connection = self._connect()
        try:
            table = str(contract["table"])
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM `{table}` WHERE `id` > %s ORDER BY `id` ASC LIMIT %s",
                    [max(0, int(after_id)), max(1, min(int(limit), 1000))],
                )
                return list(cursor.fetchall() or [])
        finally:
            connection.close()

    def count_for_sync(self, knowledge_type: str) -> int:
        contract = MYSQL_FORM_CONTRACTS.get(knowledge_type)
        if not contract or not contract.get("table") or not contract.get("public_fields"):
            raise ValueError(f"不支持同步的 MySQL 表单类型：{knowledge_type}")
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS total FROM `{contract['table']}`", [])
                row = cursor.fetchone() or {}
                return int(row.get("total") or 0)
        finally:
            connection.close()


    def fetch_by_ids(
        self,
        knowledge_type: str,
        record_ids: list[str],
        *,
        include_system_fields: bool = False,
    ) -> dict[str, RetrievalHit]:
        """按 Milvus 已选出的主键补充 MySQL 权威字段，不参与相关度检索。"""
        ids = list(dict.fromkeys(str(value) for value in record_ids if str(value).strip()))
        contract = MYSQL_FORM_CONTRACTS.get(knowledge_type)
        if not ids or not contract or not contract.get("table"):
            return {}
        connection = self._connect()
        try:
            table = str(contract["table"])
            fields = list(all_mysql_fields(knowledge_type) if include_system_fields else business_fields(knowledge_type))
            placeholders = ", ".join(["%s"] * len(ids))
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT * FROM `{table}` WHERE `id` IN ({placeholders})", ids)
                rows = cursor.fetchall() or []
            return {
                str(row["id"]): self._row_to_hit(row, knowledge_type, fields)
                for row in rows
                if row.get("id") is not None
            }
        finally:
            connection.close()

    def records_for_sync(self, knowledge_type: str, *, after_id: int = 0, limit: int = 500) -> list[Any]:
        """把 MySQL 行转换成非系统字段索引记录，保留原表 id。"""
        from core.contracts.knowledge import CleanedKnowledgeRecord

        contract = MYSQL_FORM_CONTRACTS[knowledge_type]
        indexed_fields = set(business_fields(knowledge_type))
        table = str(contract["table"])
        records: list[CleanedKnowledgeRecord] = []
        for row in self.list_for_sync(knowledge_type, after_id=after_id, limit=limit):
            record_id = row.get("id")
            values = {field: row.get(field) for field in ordered_fields(knowledge_type) if field in indexed_fields and row.get(field) not in (None, "")}
            text = mysql_fields_to_text(knowledge_type, values, self._comments_for_sync(knowledge_type))
            if record_id is None or not text:
                continue
            records.append(CleanedKnowledgeRecord(
                record_id=str(record_id),
                mysql_id=str(record_id),
                text=text,
                fields=values,
                source_locator=f"mysql:{table}|type:record|id:{record_id}",
                metadata={"storage": "mysql", "mysql_id": str(record_id), "chunk_role": "mysql", "field_names": list(values)},
            ))
        return records

    def next_record_for_sync(self, knowledge_type: str, *, after_id: int = 0) -> tuple[Any | None, int | None]:
        """读取下一条原始 MySQL 行并转换；空业务内容也返回其 id，避免提前结束同步。"""
        rows = self.list_for_sync(knowledge_type, after_id=after_id, limit=1)
        if not rows:
            return None, None
        row = rows[0]
        record_id = row.get("id")
        if record_id is None:
            return None, None
        from core.contracts.knowledge import CleanedKnowledgeRecord

        contract = MYSQL_FORM_CONTRACTS[knowledge_type]
        indexed_fields = set(business_fields(knowledge_type))
        values = {field: row.get(field) for field in ordered_fields(knowledge_type) if field in indexed_fields and row.get(field) not in (None, "")}
        text = mysql_fields_to_text(knowledge_type, values, self._comments_for_sync(knowledge_type))
        if not text:
            return None, int(record_id)
        return CleanedKnowledgeRecord(
            record_id=str(record_id), mysql_id=str(record_id), text=text, fields=values,
            source_locator=f"mysql:{contract['table']}|type:record|id:{record_id}",
            metadata={"storage": "mysql", "mysql_id": str(record_id), "chunk_role": "mysql", "field_names": list(values)},
        ), int(record_id)

    def _search_table(
        self,
        connection: Any,
        knowledge_type: str,
        terms: list[str],
        top_k: int,
        include_system_fields: bool = False,
    ) -> list[RetrievalHit]:
        from agents.retrieval_agent.schemas import RetrievalHit

        table = str(MYSQL_FORM_CONTRACTS[knowledge_type]["table"])
        fields = list(all_mysql_fields(knowledge_type) if include_system_fields else business_fields(knowledge_type))
        predicates: list[str] = []
        params: list[str] = []
        score_parts: list[str] = []
        for term in terms:
            term_score_parts: list[str] = []
            for field in fields:
                predicates.append(f"CAST(`{field}` AS CHAR) LIKE %s")
                params.append(f"%{term}%")
                term_score_parts.append(f"CASE WHEN CAST(`{field}` AS CHAR) LIKE %s THEN 1 ELSE 0 END")
            score_parts.append("(" + " + ".join(term_score_parts) + ")")
        where = " OR ".join(predicates)
        score_params = [f"%{term}%" for term in terms for _ in fields]
        limit = max(1, min(int(top_k), 50))
        sql = f"SELECT * FROM `{table}` WHERE {where} ORDER BY ({' + '.join(score_parts)}) DESC, `id` ASC LIMIT %s"
        with connection.cursor() as cursor:
            cursor.execute(sql, params + score_params + [limit])
            rows = cursor.fetchall() or []

        hits: list[RetrievalHit] = []
        for row in rows:
            hit = self._row_to_hit(row, knowledge_type, fields)
            matched = sum(term.casefold() in hit.metadata["chunk_text"].casefold() for term in terms)
            hits.append(RetrievalHit(
                record_id=hit.record_id,
                score=matched / max(1, len(terms)),
                source=hit.source,
                metadata=hit.metadata,
            ))
        return hits

    @staticmethod
    def _row_to_hit(row: dict[str, Any], knowledge_type: str, fields: list[str]) -> RetrievalHit:
        from agents.retrieval_agent.schemas import RetrievalHit

        record_id = str(row.get("id") or row.get("src_id") or "")
        table = str(MYSQL_FORM_CONTRACTS[knowledge_type]["table"])
        values: dict[str, Any] = {}
        seen_values: set[str] = set()
        for field in fields:
            value = row.get(field)
            if value in (None, ""):
                continue
            key = _field_value_key(value)
            if key and key in seen_values:
                continue
            if key:
                seen_values.add(key)
            values[field] = value
        text = "\n".join(f"{field}：{value}" for field, value in values.items())
        document_id = f"mysql:{knowledge_type}:{record_id}"
        return RetrievalHit(
            record_id=document_id,
            score=0.0,
            source="mysql",
            metadata={
                "document_id": document_id,
                "mysql_id": record_id,
                "chunk_text": text,
                "source_locator": f"mysql:{table}|id:{record_id}",
                "knowledge_type": knowledge_type,
                "storage": "mysql",
                "publication_status": "PUBLISHED",
                "fields": values,
            },
        )


def _query_terms(query: str) -> list[str]:
    text = str(query or "").strip()
    if not text:
        return []
    terms = [part for part in re.split(r"[\s,，。；;、/]+", text) if part]
    return list(dict.fromkeys(terms))


def _field_value_key(value: Any) -> str:
    plain = re.sub(r"<[^>]*>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", "", plain).casefold()
