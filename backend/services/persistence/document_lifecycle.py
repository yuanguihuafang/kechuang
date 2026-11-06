"""基于 PostgreSQL 的当前文档目录。"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, func, or_, select, text

from .sqlalchemy import get_engine, platform_documents

DOCUMENT_STATUSES = {
    "DRAFT",
    "UPLOADED",
    "PARSING",
    "WAITING_REVIEW",
    "READY_TO_PUBLISH",
    "PUBLISHED",
    "INDEXING",
    "INDEXED",
    "UNPUBLISHED",
    "EXPIRED",
    "RECYCLED",
    "RESTORED",
    "PURGED",
    "FAILED",
}
PUBLICATION_STATUSES = {"PUBLISHED", "UNPUBLISHED", "EXPIRED"}
INDEX_STATUSES = {"NOT_INDEXED", "INDEXING", "INDEXED", "DELETED", "FAILED"}


def publication_status_label(version: dict[str, Any]) -> str:
    """根据 PostgreSQL 中的版本状态生成统一的展示状态。"""
    status = str(version.get("status") or "").upper()
    publication = str(version.get("publication_status") or "").upper()
    if status == "RECYCLED":
        return "已回收"
    if status == "PURGED":
        return "已删除"
    if publication == "PUBLISHED" or status == "PUBLISHED":
        return "可检索"
    if publication == "EXPIRED" or status == "EXPIRED":
        return "已过期"
    if (
        status == "UNPUBLISHED"
        or version.get("unpublished_at")
        or version.get("unpublish_reason_code")
    ):
        return "已下架"
    return "未发布"


def _with_publication_label(
    version: dict[str, Any], *, is_current: bool | None = None
) -> dict[str, Any]:
    result = {**version, "publication_label": publication_status_label(version)}
    if is_current is not None:
        result["_is_current"] = is_current
    return result


BEIJING = ZoneInfo("Asia/Shanghai")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _beijing_date(value: Any):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING).date()


class DocumentLifecycleStore:
    """一条当前文档记录；替换上传保留旧版本来源直到显式清理。"""

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn and engine is None:
            raise ValueError("DATABASE_URL 未配置，文档目录必须使用 PostgreSQL")
        self.engine = engine or get_engine(self.dsn)

    def create_version(
        self,
        *,
        document_id: str,
        source_name: str,
        storage_key: str,
        scope_id: str,
        user_id: str | None,
        version_number: int,
        knowledge_type: str,
        version_id: str | None = None,
        status: str = "UPLOADED",
        artifact_refs: dict[str, Any] | None = None,
        publication_status: str = "UNPUBLISHED",
        index_status: str = "NOT_INDEXED",
    ) -> dict[str, Any]:
        self._validate_status(status)
        self._validate_publication_status(publication_status)
        self._validate_index_status(index_status)
        if version_number < 1:
            raise ValueError("版本号必须为正整数")
        version_id = version_id or f"{document_id}:v{version_number}"
        existing_identity = self.get_version(version_id)
        if existing_identity and existing_identity["document_id"] != document_id:
            raise ValueError("版本 ID 已属于其他文档")
        now = _now()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    platform_documents.select()
                    .where(
                        platform_documents.c.document_id == document_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row:
                versions = [self._row_to_version(row, document_id, is_current=True)] + [
                    _with_publication_label(item, is_current=False)
                    for item in (row["version_history"] or [])
                ]
                existing = next((item for item in versions if item["version_id"] == version_id), None)
                if existing:
                    if existing["status"] == "PURGED" or any(
                        existing.get(key) != value for key, value in {
                            "storage_key": storage_key, "version_number": version_number,
                            "scope_id": scope_id, "user_id": user_id,
                        }.items()
                    ):
                        raise ValueError("版本 ID 已存在，拒绝替换原件或复活已删除版本")
                    return existing
                if version_number <= max(int(item["version_number"]) for item in versions):
                    raise ValueError("新版本号必须大于已有版本号")
            values = {
                "document_id": document_id,
                "source_name": source_name,
                "storage_key": storage_key,
                "scope_id": scope_id,
                "user_id": user_id,
                "knowledge_type": knowledge_type,
                "status": status,
                "active_version_id": version_id,
                "version_number": version_number,
                "version_id": version_id,
                "publication_status": publication_status,
                "index_status": index_status,
                "artifact_refs": artifact_refs or {},
                "parsed_block_count": 0,
                "chunk_count": 0,
                "indexed_count": 0,
                "index_time": None,
                "error": None,
                "version_history": list(row["version_history"] or []) if row else [],
                "unpublish_reason_code": None,
                "unpublish_reason_text": None,
                "unpublished_by": None,
                "unpublished_at": None,
                "created_at": row["created_at"] if row else now,
                "updated_at": now,
            }
            if row:
                if row["version_id"] and row["version_id"] != version_id:
                    previous = self._row_to_version(row, document_id, is_current=False)
                    values["version_history"] = [
                        item
                        for item in values["version_history"]
                        if item.get("version_id") != previous["version_id"]
                    ] + [previous]
                conn.execute(
                    platform_documents.update()
                    .where(
                        platform_documents.c.document_id == document_id,
                    )
                    .values(**values)
                )
            else:
                conn.execute(platform_documents.insert().values(**values))
        return self.get_version(version_id) or {}

    def bind_draft_attachments(
        self, attachments: list[dict[str, Any]], *, user_id: str, knowledge_type: str
    ) -> list[str]:
        """补齐先上传后确认用途的分类；不替换原件、版本或已有分类。"""
        if knowledge_type not in {"demand", "solution", "achievement"}:
            return []
        updated = []
        with self.engine.begin() as conn:
            for attached in attachments:
                document_id = str(attached.get("document_id") or "")
                if not document_id:
                    continue
                version_id = str(attached.get("version_id") or "")
                row = conn.execute(
                    platform_documents.select().where(
                        platform_documents.c.document_id == document_id,
                    ).with_for_update()
                ).mappings().first()
                if (
                    not row or not version_id
                    or row["version_id"] != version_id or row["active_version_id"] != version_id
                    or row["user_id"] != user_id or row["scope_id"] != "public"
                    or row["status"] in {"RECYCLED", "PURGED"}
                    or (attached.get("storage_key") and attached["storage_key"] != row["storage_key"])
                ):
                    raise ValueError(f"附件 {document_id} 的归属或版本已变化，请重新选择附件")
                if row["knowledge_type"] == knowledge_type:
                    continue
                if row["knowledge_type"]:
                    raise ValueError(f"附件 {document_id} 已归类为 {row['knowledge_type']}，不能覆盖分类")
                if (
                    row["publication_status"] != "UNPUBLISHED"
                    or row["index_status"] != "NOT_INDEXED"
                    or any(v.get("publication_status") in {"PUBLISHED", "WITHDRAWN"}
                           for v in row["version_history"] or [])
                ):
                    raise ValueError(f"附件 {document_id} 已有发布或索引记录，不能自动补齐分类")
                conn.execute(
                    platform_documents.update().where(
                        platform_documents.c.document_id == document_id,
                    ).values(knowledge_type=knowledge_type, updated_at=_now())
                )
                updated.append(document_id)
        return updated

    def update_version(
        self, version_id: str, *, status: str | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        expected_version = fields.pop("expected_version", None)
        if status is not None:
            self._validate_status(status)
        if "publication_status" in fields:
            self._validate_publication_status(fields["publication_status"])
        if "index_status" in fields:
            self._validate_index_status(fields["index_status"])
        allowed = {
            "artifact_refs",
            "parsed_block_count",
            "chunk_count",
            "indexed_count",
            "index_time",
            "error",
            "publication_status",
            "index_status",
            "unpublish_reason_code",
            "unpublish_reason_text",
            "unpublished_by",
            "unpublished_at",
        }
        changes = {key: value for key, value in fields.items() if key in allowed}
        if status is not None:
            changes["status"] = status
        now = _now()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    platform_documents.select()
                    .where(
                        platform_documents.c.version_id == version_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                # ponytail: 历史版本低频读取时扫描 JSON；数据量大后改 JSONB 索引查询。
                candidates = (
                    conn.execute(
                        platform_documents.select()
                        .where(
                            platform_documents.c.version_history != [],
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .all()
                )
                row = next(
                    (
                        item
                        for item in candidates
                        if any(
                            v.get("version_id") == version_id
                            for v in (item["version_history"] or [])
                        )
                    ),
                    None,
                )
            if row is None:
                return None
            document_id = row["document_id"]
            current = (
                self._row_to_version(row, document_id, is_current=True)
                if row["version_id"] == version_id
                else next(
                    _with_publication_label(item, is_current=False)
                    for item in row["version_history"]
                    if item.get("version_id") == version_id
                )
            )
            if expected_version is not None and current != expected_version:
                raise RuntimeError(f"版本 {version_id} 的来源已变化，拒绝写入")
            if current.get("status") == "PURGED" and (
                status not in {None, "PURGED"}
                or changes.get("publication_status", "UNPUBLISHED") != "UNPUBLISHED"
                or changes.get("index_status", "DELETED") != "DELETED"
            ):
                raise ValueError("已删除版本不可恢复或重新发布")
            if "artifact_refs" in changes:
                changes["artifact_refs"] = {
                    **(current.get("artifact_refs") or {}),
                    **changes["artifact_refs"],
                }
            if row["version_id"] == version_id:
                changes["updated_at"] = now
                conn.execute(
                    platform_documents.update()
                    .where(
                        platform_documents.c.document_id == document_id,
                    )
                    .values(**changes)
                )
            else:
                history = list(row["version_history"] or [])
                for item in history:
                    if item.get("version_id") == version_id:
                        item.update(changes)
                        item["updated_at"] = _iso(now)
                conn.execute(
                    platform_documents.update()
                    .where(
                        platform_documents.c.document_id == document_id,
                    )
                    .values(version_history=history, updated_at=now)
                )
        return self.get_version(version_id)

    def list_documents(
        self,
        *,
        scope_id: str | None = None,
        user_id: str | None = None,
        knowledge_type: str | None = None,
        status: str | None = None,
        limit: int = 200,
        include_purged: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10000))
        query = platform_documents.select().with_only_columns(
            platform_documents.c.document_id,
            platform_documents.c.source_name,
            platform_documents.c.scope_id,
            platform_documents.c.user_id,
            platform_documents.c.knowledge_type,
            platform_documents.c.status,
            platform_documents.c.active_version_id,
            platform_documents.c.created_at,
            platform_documents.c.updated_at,
        )
        if scope_id:
            query = query.where(platform_documents.c.scope_id == scope_id)
        if user_id:
            query = query.where(platform_documents.c.user_id == user_id)
        if knowledge_type:
            query = query.where(platform_documents.c.knowledge_type == knowledge_type)
        if status:
            query = query.where(platform_documents.c.status == status)
        if not include_purged:
            query = query.where(platform_documents.c.status != "PURGED")
        with self.engine.connect() as conn:
            rows = (
                conn.execute(query.order_by(platform_documents.c.updated_at.desc()).limit(limit))
                .mappings()
                .all()
            )
        result = []
        for row in rows:
            document = {
                "document_id": row["document_id"],
                "source_name": row["source_name"],
                "scope_id": row["scope_id"],
                "user_id": row["user_id"],
                "knowledge_type": row["knowledge_type"],
                "status": row["status"],
                "active_version_id": row["active_version_id"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            document["active_version"] = (
                self.get_version(row["active_version_id"]) if row["active_version_id"] else None
            )
            document["versions"] = self.list_versions(row["document_id"])
            result.append(document)
        return result

    def list_documents_page(
        self,
        *,
        scope_id: str | None = None,
        user_id: str | None = None,
        knowledge_type: str | None = None,
        status: str | None = None,
        search: str | None = None,
        published_or_admin_only: bool = False,
        page: int = 1,
        page_size: int = 20,
        include_purged: bool = False,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        filters = []
        if scope_id:
            filters.append(platform_documents.c.scope_id == scope_id)
        if user_id:
            filters.append(platform_documents.c.user_id == user_id)
        if knowledge_type:
            filters.append(platform_documents.c.knowledge_type == knowledge_type)
        if status:
            filters.append(platform_documents.c.status == status)
        if published_or_admin_only:
            filters.append(
                or_(
                    platform_documents.c.user_id == "admin",
                    platform_documents.c.publication_status == "PUBLISHED",
                )
            )
        search = str(search or "").strip()
        if search:
            escaped_search = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            pattern = f"%{escaped_search}%"
            filters.append(
                or_(
                    platform_documents.c.source_name.ilike(pattern, escape="\\"),
                    platform_documents.c.document_id.ilike(pattern, escape="\\"),
                )
            )
        if not include_purged:
            filters.append(platform_documents.c.status != "PURGED")
        filters.append(
            or_(
                platform_documents.c.active_version_id.is_not(None),
                platform_documents.c.version_history != [],
            )
        )
        columns = (
            platform_documents.c.document_id,
            platform_documents.c.source_name,
            platform_documents.c.scope_id,
            platform_documents.c.user_id,
            platform_documents.c.knowledge_type,
            platform_documents.c.status,
            platform_documents.c.active_version_id,
            platform_documents.c.created_at,
            platform_documents.c.updated_at,
            platform_documents.c.storage_key,
            platform_documents.c.version_number,
            platform_documents.c.version_id,
            platform_documents.c.publication_status,
            platform_documents.c.index_status,
            platform_documents.c.artifact_refs,
            platform_documents.c.parsed_block_count,
            platform_documents.c.chunk_count,
            platform_documents.c.indexed_count,
            platform_documents.c.index_time,
            platform_documents.c.error,
            platform_documents.c.unpublish_reason_code,
            platform_documents.c.unpublish_reason_text,
            platform_documents.c.unpublished_by,
            platform_documents.c.unpublished_at,
        )
        with self.engine.connect() as conn:
            total = int(
                conn.execute(
                    select(func.count()).select_from(platform_documents).where(*filters)
                ).scalar_one()
            )
            rows = (
                conn.execute(
                    platform_documents.select()
                    .with_only_columns(*columns)
                    .where(*filters)
                    .order_by(
                        platform_documents.c.updated_at.desc(), platform_documents.c.document_id
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .mappings()
                .all()
            )
        documents = []
        for row in rows:
            document = {
                "document_id": row["document_id"],
                "source_name": row["source_name"],
                "scope_id": row["scope_id"],
                "user_id": row["user_id"],
                "knowledge_type": row["knowledge_type"],
                "status": row["status"],
                "active_version_id": row["active_version_id"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            document["active_version"] = (
                self._row_to_version(row, row["document_id"], is_current=True)
                if row["active_version_id"] and row["version_id"] == row["active_version_id"]
                else None
            )
            documents.append(document)
        return {
            "documents": documents,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def get_document(
        self, document_id: str, *, user_id: str | None = None, include_purged: bool = False
    ) -> dict[str, Any] | None:
        """读取单份文档及版本；传入 user_id 时强制按归属过滤。"""
        query = platform_documents.select().where(platform_documents.c.document_id == document_id)
        if user_id:
            query = query.where(platform_documents.c.user_id == user_id)
        if not include_purged:
            query = query.where(platform_documents.c.status != "PURGED")
        with self.engine.connect() as conn:
            row = conn.execute(query.limit(1)).mappings().first()
        if row is None:
            return None
        document = {
            "document_id": row["document_id"],
            "source_name": row["source_name"],
            "scope_id": row["scope_id"],
            "user_id": row["user_id"],
            "knowledge_type": row["knowledge_type"],
            "status": row["status"],
            "active_version_id": row["active_version_id"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        document["active_version"] = (
            self.get_version(row["active_version_id"]) if row["active_version_id"] else None
        )
        document["versions"] = self.list_versions(document_id)
        return document

    def is_storage_key_referenced(
        self, storage_key: str, exclude_version_ids: tuple[str, ...] = ()
    ) -> bool:
        """完整检查活动与历史原件引用，不受目录分页或 PURGED 当前行影响。"""
        history = ("jsonb_array_elements" if self.engine.dialect.name == "postgresql" else "json_each")
        query = text(f"""
            SELECT EXISTS (
                SELECT 1 FROM platform_documents
                WHERE storage_key = :key AND status <> 'PURGED'
                  AND version_id NOT IN :excluded
            ) OR EXISTS (
                SELECT 1 FROM platform_documents,
                    {history}(platform_documents.version_history) AS history
                WHERE history.value->>'storage_key' = :key
                  AND COALESCE(history.value->>'status', '') <> 'PURGED'
                  AND history.value->>'version_id' NOT IN :excluded
            )
        """).bindparams(bindparam("excluded", expanding=True))
        with self.engine.connect() as conn:
            return bool(conn.execute(query, {"key": storage_key, "excluded": exclude_version_ids}).scalar_one())

    def list_storage_references(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        """一次性读取存储引用，供后台孤儿扫描使用，避免逐文档加载版本。"""

        limit = max(1, min(int(limit), 10000))
        query = (
            platform_documents.select()
            .with_only_columns(
                platform_documents.c.document_id,
                platform_documents.c.storage_key,
                platform_documents.c.version_history,
            )
            .where(platform_documents.c.status != "PURGED")
        )
        with self.engine.connect() as conn:
            rows = (
                conn.execute(query.order_by(platform_documents.c.updated_at.desc()).limit(limit))
                .mappings()
                .all()
            )
        return [
            {
                "document_id": row["document_id"],
                "storage_key": row["storage_key"],
                "versions": row["version_history"] or [],
            }
            for row in rows
        ]

    def summary(self) -> dict[str, Any]:
        """返回管理员总览所需的服务端聚合，避免前端只统计当前分页。"""
        with self.engine.connect() as conn:
            rows = conn.execute(platform_documents.select()).mappings().all()
        active = [row for row in rows if row["status"] != "PURGED"]
        today = datetime.now(BEIJING).date()
        yesterday = today - timedelta(days=1)
        today_count = sum(_beijing_date(row["created_at"]) == today for row in active)
        yesterday_count = sum(_beijing_date(row["created_at"]) == yesterday for row in active)
        type_rows: dict[str, int] = {}
        scope_rows: dict[str, int] = {}
        for row in active:
            if row["knowledge_type"]:
                type_rows[row["knowledge_type"]] = type_rows.get(row["knowledge_type"], 0) + 1
            scope_rows[row["scope_id"] or "public"] = (
                scope_rows.get(row["scope_id"] or "public", 0) + 1
            )
        return {
            "total": len(active),
            "indexed": sum(row["status"] in {"INDEXED", "PUBLISHED"} for row in active),
            "waiting": sum(
                row["status"]
                in {
                    "WAITING_REVIEW",
                    "WAITING_CONFIRM",
                    "WAITING_DRAFT_CONFIRM",
                    "WAITING_INTENT_CONFIRM",
                    "UPLOADED",
                }
                for row in active
            ),
            "failed": sum(row["status"] == "FAILED" for row in active),
            "by_knowledge_type": type_rows,
            "by_scope": scope_rows,
            "comparison": {
                "timezone": "Asia/Shanghai",
                "today": today_count,
                "yesterday": yesterday_count,
                "delta": today_count - yesterday_count,
            },
        }

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    platform_documents.select().where(
                        platform_documents.c.version_id == version_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                # ponytail: 历史版本低频读取时扫描 JSON；数据量大后改 JSONB 索引查询。
                rows = conn.execute(platform_documents.select()).mappings().all()
                row = next(
                    (
                        item
                        for item in rows
                        if any(
                            v.get("version_id") == version_id
                            for v in (item["version_history"] or [])
                        )
                    ),
                    None,
                )
        if row is None:
            return None
        if row["version_id"] == version_id:
            return self._row_to_version(row, row["document_id"], is_current=True)
        for item in list(row["version_history"] or []):
            if item.get("version_id") == version_id:
                return _with_publication_label(item, is_current=False)
        return None

    def list_versions(self, document_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                platform_documents.select()
                .with_only_columns(
                    platform_documents.c.active_version_id,
                    platform_documents.c.version_history,
                )
                .where(platform_documents.c.document_id == document_id)
            ).first()
        if row is None:
            return []
        current = self.get_version(row.active_version_id) if row.active_version_id else None
        values = ([current] if current else []) + [
            _with_publication_label(item, is_current=False)
            for item in list(row.version_history or [])
        ]
        return sorted(values, key=lambda item: int(item.get("version_number") or 0), reverse=True)

    def purge_document(self, document_id: str) -> bool:
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(
                platform_documents.select()
                .with_only_columns(
                    platform_documents.c.version_history,
                )
                .where(platform_documents.c.document_id == document_id)
                .with_for_update()
            ).first()
            if row is None:
                return False
            history = list(row.version_history or [])
            for item in history:
                item.update(
                    status="PURGED",
                    publication_status="UNPUBLISHED",
                    index_status="DELETED",
                    updated_at=_iso(now),
                )
            result = conn.execute(
                platform_documents.update()
                .where(
                    platform_documents.c.document_id == document_id,
                )
                .values(
                    status="PURGED",
                    publication_status="UNPUBLISHED",
                    index_status="DELETED",
                    version_history=history,
                    updated_at=now,
                )
            )
        return result.rowcount > 0

    def expire_document(self, document_id: str) -> bool:
        """标记文档过期，但保留目录记录和已发布字段草稿。"""

        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(
                platform_documents.select()
                .with_only_columns(
                    platform_documents.c.version_history,
                )
                .where(platform_documents.c.document_id == document_id)
                .with_for_update()
            ).first()
            if row is None:
                return False
            history = list(row.version_history or [])
            for item in history:
                item.update(status="EXPIRED", publication_status="EXPIRED", updated_at=_iso(now))
            result = conn.execute(
                platform_documents.update()
                .where(
                    platform_documents.c.document_id == document_id,
                )
                .values(
                    status="EXPIRED",
                    publication_status="EXPIRED",
                    version_history=history,
                    updated_at=now,
                )
            )
        return result.rowcount > 0

    @staticmethod
    def _row_to_version(row: Any, document_id: str, is_current: bool = False) -> dict[str, Any]:
        version = {
            "version_id": row["version_id"],
            "document_id": document_id,
            "version_number": row["version_number"],
            "source_name": row["source_name"],
            "storage_key": row["storage_key"],
            "scope_id": row["scope_id"],
            "user_id": row["user_id"],
            "status": row["status"],
            "publication_status": row["publication_status"],
            "index_status": row["index_status"],
            "artifact_refs": row["artifact_refs"] or {},
            "parsed_block_count": row["parsed_block_count"] or 0,
            "chunk_count": row["chunk_count"] or 0,
            "indexed_count": row["indexed_count"] or 0,
            "index_time": _iso(row["index_time"]) if row["index_time"] else None,
            "error": row["error"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "knowledge_type": row["knowledge_type"],
            "unpublish_reason_code": row["unpublish_reason_code"],
            "unpublish_reason_text": row["unpublish_reason_text"],
            "unpublished_by": row["unpublished_by"],
            "unpublished_at": _iso(row["unpublished_at"]) if row["unpublished_at"] else None,
            "_is_current": is_current,
        }
        return _with_publication_label(version)

    def _validate_status(self, status: str) -> None:
        if status not in DOCUMENT_STATUSES:
            raise ValueError(f"不支持的文档状态：{status}")

    @staticmethod
    def _validate_publication_status(status: str) -> None:
        if status not in PUBLICATION_STATUSES:
            raise ValueError(f"不支持的发布状态：{status}")

    @staticmethod
    def _validate_index_status(status: str) -> None:
        if status not in INDEX_STATUSES:
            raise ValueError(f"不支持的索引状态：{status}")
