"""工作流 PostgreSQL 仓储：保存业务草稿。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from typing import Any

from ..postgres import (
    _draft_text,
    _draft_version,
    _iso,
    _merge_draft,
    _now,
    _transition_metadata,
    _validate_draft_transition,
    _validate_draft_transition_permission,
    _version_mismatch,
    memory_content_similarity,
)
from ..sqlalchemy import sqlalchemy_connection as _pooled_connection


class PostgresDraftStore:
    """需求、方案、成果、拜访记录共用的草稿仓储。"""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")

    @staticmethod
    def _draft_title(draft: dict[str, Any]) -> str:
        draft_type = str(draft.get("draft_type") or "")
        fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
        mysql_fields = (
            draft.get("mysql_fields") if isinstance(draft.get("mysql_fields"), dict) else {}
        )
        if draft_type == "customer_visit":
            return str(
                fields.get("company")
                or fields.get("enterprise_name")
                or draft.get("title")
                or "未命名拜访记录"
            ).strip()[:160]
        if draft_type == "enterprise":
            return str(
                fields.get("enterprise_name") or mysql_fields.get("enterprise_name") or "未命名企业"
            ).strip()[:160]
        if draft_type == "expert":
            return str(
                fields.get("user_name") or mysql_fields.get("user_name") or "未命名专家"
            ).strip()[:160]
        return str(
            draft.get("title") or fields.get("title") or mysql_fields.get("title") or "未命名草稿"
        ).strip()[:160]

    @staticmethod
    def _record(row: tuple[Any, ...]) -> dict[str, Any]:
        draft = dict(row[8] or {})
        version = _draft_version(draft)
        draft.pop("_draft_version", None)
        return {
            "draft_id": row[0],
            "user_id": row[1],
            "user_role": row[2],
            "draft_type": row[3],
            "status": row[4],
            "thread_id": row[5],
            "source_document_id": row[6],
            "draft_title": row[7],
            "draft": draft,
            "created_at": _iso(row[9]),
            "updated_at": _iso(row[10]),
            "version": version,
            "revision": version,
        }

    def create(
        self,
        *,
        user_id: str,
        user_role: str,
        draft: dict[str, Any],
        thread_id: str | None = None,
        reuse_existing_thread_draft: bool = False,
    ) -> dict[str, Any]:
        draft_to_save = _merge_draft({}, draft)
        draft_to_save["_draft_version"] = 1
        draft_type = str(draft_to_save.get("draft_type") or "unknown")
        title = self._draft_title(draft_to_save)
        now = _now()
        if thread_id and reuse_existing_thread_draft:
            with _pooled_connection(self.dsn) as conn:
                existing = conn.exec_driver_sql(
                    """SELECT draft_id FROM platform_drafts
                       WHERE user_id=%s AND thread_id=%s AND draft_type=%s AND status <> 'DELETED'
                       ORDER BY updated_at DESC LIMIT 1""",
                    (user_id, thread_id, draft_type),
                ).fetchone()
            if existing:
                return self.update(existing[0], user_id=user_id, draft=draft)
        draft_id = f"draft_{uuid.uuid4().hex}"
        with _pooled_connection(self.dsn) as conn:
            conn.exec_driver_sql(
                """INSERT INTO platform_drafts
                (draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,draft_title,content,created_at,updated_at)
                VALUES (%s,%s,%s,%s,'CONFIRMED',%s,%s,%s,%s::jsonb,%s,%s)""",
                (
                    draft_id,
                    user_id,
                    user_role,
                    draft_type,
                    thread_id,
                    (draft_to_save.get("attachments") or [{}])[0].get("document_id"),
                    title,
                    json.dumps(draft_to_save, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get(draft_id, user_id=user_id)  # type: ignore[return-value]

    def create_many(
        self,
        *,
        user_id: str,
        user_role: str,
        drafts: list[dict[str, Any]],
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Create an extracted document batch atomically, preserving extraction order."""
        if not drafts:
            return []

        now = _now()
        rows: list[dict[str, Any]] = []
        with _pooled_connection(self.dsn) as conn:
            for index, draft in enumerate(drafts):
                draft_to_save = _merge_draft({}, draft)
                draft_to_save["_draft_version"] = 1
                draft_type = str(draft_to_save.get("draft_type") or "unknown")
                draft_id = f"draft_{uuid.uuid4().hex}"
                draft_title = self._draft_title(draft_to_save)
                created_at = now
                # The first extracted item is also the active/newest draft in the UI.
                updated_at = now + timedelta(microseconds=len(drafts) - index - 1)
                attachments = draft_to_save.get("attachments") or []
                first_attachment = attachments[0] if attachments else {}
                source_document_id = (
                    first_attachment.get("document_id")
                    if isinstance(first_attachment, dict)
                    else None
                )
                conn.exec_driver_sql(
                    """INSERT INTO platform_drafts
                    (draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,draft_title,content,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,'CONFIRMED',%s,%s,%s,%s::jsonb,%s,%s)""",
                    (
                        draft_id,
                        user_id,
                        user_role,
                        draft_type,
                        thread_id,
                        source_document_id,
                        draft_title,
                        json.dumps(draft_to_save, ensure_ascii=False),
                        created_at,
                        updated_at,
                    ),
                )
                rows.append(
                    {
                        "draft_id": draft_id,
                        "user_id": user_id,
                        "user_role": user_role,
                        "draft_type": draft_type,
                        "status": "CONFIRMED",
                        "thread_id": thread_id,
                        "source_document_id": source_document_id,
                        "draft_title": draft_title,
                        "draft": {key: value for key, value in draft_to_save.items() if key != "_draft_version"},
                        "created_at": _iso(created_at),
                        "updated_at": _iso(updated_at),
                        "version": 1,
                        "revision": 1,
                    }
                )
        return rows

    def get(self, draft_id: str, *, user_id: str) -> dict[str, Any] | None:
        with _pooled_connection(self.dsn) as conn:
            row = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts WHERE draft_id=%s AND user_id=%s""",
                (draft_id, user_id),
            ).fetchone()
        return self._record(row) if row else None

    def list(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts WHERE user_id=%s
                   AND status <> 'DELETED'
                   ORDER BY updated_at DESC LIMIT %s""",
                (user_id, limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_for_document(self, document_id: str) -> list[dict[str, Any]]:
        """完整读取来源文档或附件关联的未删除草稿。"""
        attachment_filter = json.dumps({"attachments": [{"document_id": document_id}]})
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts
                   WHERE status <> 'DELETED'
                     AND (source_document_id=%s OR content @> %s::jsonb)
                   ORDER BY updated_at DESC, draft_id DESC""",
                (document_id, attachment_filter),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_business_references(
        self, draft_type: str, mysql_record_id: str, *, user_id: str | None
    ) -> list[dict[str, Any]]:
        """按业务类型/ID 完整读取活动引用；私有拜访记录强制限定归属。"""
        query = """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts
                   WHERE status <> 'DELETED' AND draft_type=%s
                     AND content->>'mysql_record_id'=%s"""
        params: tuple[Any, ...] = (draft_type, mysql_record_id)
        if draft_type == "customer_visit":
            if not user_id or not user_id.strip():
                raise ValueError("私有草稿引用查询需要 user_id")
            query += " AND user_id=%s"
            params += (user_id,)
        query += " ORDER BY updated_at DESC, draft_id DESC"
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(query, params).fetchall()
        return [self._record(row) for row in rows]

    def list_storage_key_references(self, storage_key: str) -> list[dict[str, Any]]:
        """跨归属和类型完整读取对象键的活动附件引用，不要求附件含文档 ID。"""
        attachment_filter = json.dumps({"attachments": [{"storage_key": storage_key}]})
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts
                   WHERE status <> 'DELETED' AND content @> %s::jsonb
                   ORDER BY updated_at DESC, draft_id DESC""",
                (attachment_filter,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_all(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts"""
        params: list[Any] = []
        if status:
            query += " WHERE status=%s"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(query, tuple(params)).fetchall()
        return [self._record(row) for row in rows]

    def list_page(
        self,
        *,
        status: str | None = None,
        draft_types: list[str] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status=%s")
            params.append(status)
        if draft_types:
            conditions.append("draft_type IN (" + ",".join("%s" for _ in draft_types) + ")")
            params.extend(draft_types)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        columns = "draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,draft_title,content,created_at,updated_at"
        with _pooled_connection(self.dsn) as conn:
            total = int(
                conn.exec_driver_sql(
                    f"SELECT COUNT(*) FROM platform_drafts{where}",
                    tuple(params),
                ).scalar_one()
            )
            rows = conn.exec_driver_sql(
                f"SELECT {columns} FROM platform_drafts{where} "
                "ORDER BY updated_at DESC, draft_id DESC LIMIT %s OFFSET %s",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return {
            "items": [self._record(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def get_admin(self, draft_id: str) -> dict[str, Any] | None:
        with _pooled_connection(self.dsn) as conn:
            row = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts WHERE draft_id=%s""",
                (draft_id,),
            ).fetchone()
        return self._record(row) if row else None

    def list_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts
                   WHERE thread_id=%s
                   ORDER BY updated_at DESC, draft_id DESC""",
                (thread_id,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_for_source_id(self, source_id: str) -> list[dict[str, Any]]:
        source_id = str(source_id or "").strip()
        if not source_id:
            return []
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts
                   WHERE content->>'source_id'=%s
                   ORDER BY updated_at DESC, draft_id DESC""",
                (source_id,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def owner_hints_for_threads(self, thread_ids: list[str]) -> dict[str, dict[str, str]]:
        thread_ids = list(dict.fromkeys(str(value) for value in thread_ids if str(value)))
        if not thread_ids:
            return {}
        placeholders = ",".join("%s" for _ in thread_ids)
        with _pooled_connection(self.dsn) as conn:
            rows = conn.exec_driver_sql(
                f"""SELECT DISTINCT ON (thread_id) thread_id,user_id,user_role
                    FROM platform_drafts WHERE thread_id IN ({placeholders})
                      AND status <> 'DELETED' AND user_id IS NOT NULL AND user_id <> ''
                    ORDER BY thread_id, updated_at DESC, draft_id DESC""",
                tuple(thread_ids),
            ).fetchall()
        return {
            str(row[0]): {"user_id": str(row[1]), "user_role": str(row[2] or "")} for row in rows
        }

    def find_similar(
        self, *, user_id: str, draft: dict[str, Any], threshold: float = 0.72
    ) -> list[dict[str, Any]]:
        draft_type = str(draft.get("draft_type") or "unknown")
        candidate_text = _draft_text(draft)
        current_draft_id = str(draft.get("draft_id") or "").strip()
        matches = []
        for item in self.list(user_id=user_id, limit=100):
            if current_draft_id and str(item.get("draft_id") or "") == current_draft_id:
                continue
            if str(item.get("draft_type") or "unknown") != draft_type:
                continue
            score = memory_content_similarity(candidate_text, _draft_text(item.get("draft") or {}))
            if score >= threshold:
                matches.append(
                    {
                        "draft_id": item["draft_id"],
                        "status": item["status"],
                        "draft_type": item["draft_type"],
                        "title": (item.get("draft") or {}).get("title"),
                        "similarity": round(score, 4),
                        "updated_at": item.get("updated_at"),
                    }
                )
        return sorted(matches, key=lambda item: item["similarity"], reverse=True)

    def update(
        self,
        draft_id: str,
        *,
        user_id: str,
        draft: dict[str, Any],
        status: str | None = None,
        expected_version: int | str | None = None,
        replace_source_blocks: bool = False,
        replace_fields: bool = False,
    ) -> dict[str, Any] | None:
        current = self.get(draft_id, user_id=user_id)
        if current is None:
            return None
        current_version = int(current.get("version", 1))
        if _version_mismatch(current_version, expected_version):
            raise ValueError("draft version mismatch")
        draft_to_save = _merge_draft(
            current.get("draft") or {}, draft, replace_source_blocks=replace_source_blocks,
            replace_fields=replace_fields,
        )
        draft_to_save["_draft_version"] = current_version + 1
        query = """UPDATE platform_drafts SET content=%s::jsonb,draft_type=%s,draft_title=%s,
                   source_document_id=%s,status=COALESCE(%s,status),updated_at=%s
                   WHERE draft_id=%s AND user_id=%s"""
        params: list[Any] = [
            json.dumps(draft_to_save, ensure_ascii=False),
            str(draft_to_save.get("draft_type") or "unknown"),
            self._draft_title(draft_to_save),
            (draft_to_save.get("attachments") or [{}])[0].get("document_id"),
            status,
            _now(),
            draft_id,
            user_id,
        ]
        if expected_version is not None:
            query += " AND COALESCE((content->>'_draft_version')::integer, 1)=%s"
            params.append(current_version)
        with _pooled_connection(self.dsn) as conn:
            result = conn.exec_driver_sql(query, tuple(params))
        if expected_version is not None and result.rowcount == 0:
            raise ValueError("draft version mismatch")
        return self.get(draft_id, user_id=user_id)

    def remove_attachment(
        self, draft_id: str, *, user_id: str, document_id: str | None = None,
        storage_key: str | None = None,
    ) -> dict[str, Any] | None:
        with _pooled_connection(self.dsn) as conn:
            row = conn.exec_driver_sql(
                """SELECT draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                   draft_title,content,created_at,updated_at FROM platform_drafts
                   WHERE draft_id=%s AND user_id=%s FOR UPDATE""",
                (draft_id, user_id),
            ).fetchone()
            if row is None:
                return None
            current = self._record(row)
            if current.get("status") == "PUBLISHED":
                raise ValueError("已发布草稿不能删除附件，请先下架")
            draft = current["draft"]
            attachments = draft.get("attachments") or []
            kept = [attachment for attachment in attachments if not (
                (document_id and attachment.get("document_id") == document_id)
                or (not document_id and storage_key and attachment.get("storage_key") == storage_key)
            )]
            if len(kept) == len(attachments):
                return current
            stored_draft = dict(row[8] or {})
            stored_draft["attachments"] = kept
            stored_draft["source_blocks"] = []
            stored_draft["_draft_version"] = current["version"] + 1
            updated = conn.exec_driver_sql(
                """UPDATE platform_drafts
                   SET content=%s::jsonb, source_document_id=%s, updated_at=%s
                   WHERE draft_id=%s AND user_id=%s
                   RETURNING draft_id,user_id,user_role,draft_type,status,thread_id,source_document_id,
                             draft_title,content,created_at,updated_at""",
                (
                    json.dumps(stored_draft, ensure_ascii=False),
                    next((a.get("document_id") for a in kept if a.get("document_id")), None),
                    _now(),
                    draft_id,
                    user_id,
                ),
            ).fetchone()
        return self._record(updated) if updated else None

    def logical_delete(
        self,
        draft_id: str,
        *,
        user_id: str,
        operator_id: str | None = None,
        allow_published: bool = False,
    ) -> dict[str, Any] | None:
        current = self.get(draft_id, user_id=user_id)
        if current is None:
            return None
        if current.get("status") == "PUBLISHED" and not allow_published:
            raise ValueError("已发布草稿请先下架后再删除")
        deleted_at = _now()
        draft_to_save = _merge_draft(
            current.get("draft") or {},
            {"deleted_by": operator_id or user_id, "deleted_at": deleted_at.isoformat()},
        )
        draft_to_save["_draft_version"] = int(current.get("version", 1)) + 1
        with _pooled_connection(self.dsn) as conn:
            conn.exec_driver_sql(
                """UPDATE platform_drafts
                   SET status='DELETED', content=%s::jsonb, updated_at=%s
                   WHERE draft_id=%s AND user_id=%s""",
                (json.dumps(draft_to_save, ensure_ascii=False), deleted_at, draft_id, user_id),
            )
        return self.get(draft_id, user_id=user_id)

    def purge_deleted(self, draft_id: str) -> bool:
        """物理删除已逻辑删除的草稿目录记录，避免误删仍在使用的草稿。"""
        with _pooled_connection(self.dsn) as conn:
            result = conn.exec_driver_sql(
                "DELETE FROM platform_drafts WHERE draft_id=%s AND status='DELETED'",
                (draft_id,),
            )
        return result.rowcount > 0

    def transition(
        self,
        draft_id: str,
        *,
        user_id: str,
        target_status: str,
        operator_id: str | None = None,
        operator_role: str = "internal",
        note: str | None = None,
        expected_version: int | str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get(draft_id, user_id=user_id)
        if current is None:
            return None
        _validate_draft_transition(current["status"], target_status)
        _validate_draft_transition_permission(
            current,
            target_status=target_status,
            operator_id=operator_id,
            operator_role=operator_role,
        )
        current_version = int(current.get("version", 1))
        if _version_mismatch(current_version, expected_version):
            raise ValueError("draft version mismatch")
        draft_to_save = _merge_draft(
            current.get("draft") or {},
            _transition_metadata(target_status, operator_id, operator_role, note),
        )
        if target_status == "PUBLISHED":
            draft_to_save.pop("status_note", None)
        draft_to_save["_draft_version"] = current_version + 1
        query = """UPDATE platform_drafts SET status=%s,content=%s::jsonb,
                   updated_at=%s WHERE draft_id=%s AND user_id=%s"""
        params = [target_status, json.dumps(draft_to_save, ensure_ascii=False),
                  _now(), draft_id, user_id]
        if expected_version is not None:
            query += " AND COALESCE((content->>'_draft_version')::integer, 1)=%s"
            params.append(current_version)
        with _pooled_connection(self.dsn) as conn:
            result = conn.exec_driver_sql(query, tuple(params))
        if expected_version is not None and result.rowcount == 0:
            raise ValueError("draft version mismatch")
        return self.get(draft_id, user_id=user_id)
