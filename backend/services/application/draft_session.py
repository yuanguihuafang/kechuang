from __future__ import annotations

import logging
from typing import Any

from core.context_window import limit_draft_context

logger = logging.getLogger(__name__)


def load_session_draft_context(
    runtime: Any, *, thread_id: str, user_id: str, user_role: str | None = None,
) -> dict[str, Any] | None:
    """Read the current owner's authoritative draft; lookup errors must propagate."""
    session = runtime.require_conversation().session_store.get(thread_id)
    if not session or str(session.get("user_id") or "") != user_id:
        return None
    external_user = (user_role or session.get("user_role")) == "external"
    flow = (session.get("state") or {}).get("chat_flow") or {}
    draft_id = str(flow.get("active_draft_id") or "")
    if not draft_id and not isinstance(flow.get("draft_index"), list):
        return None
    store = runtime.require_draft_store()
    row = store.get(draft_id, user_id=user_id) if draft_id else None
    if row and row.get("status") == "DELETED":
        row = None
    list_for_thread = getattr(store, "list_for_thread", None)
    rows = list_for_thread(thread_id) if callable(list_for_thread) else ([row] if row else [])
    rows = [
        item for item in rows
        if str(item.get("user_id") or "") == user_id
    ]
    rows = _order_draft_rows(rows)
    if row and not any(item.get("draft_id") == draft_id for item in rows):
        rows.append(row)
    if not rows and not isinstance(flow.get("draft_index"), list):
        return None
    draft_index = merge_session_draft_index(
        flow.get("draft_index"), rows, complete=callable(list_for_thread)
    )
    if draft_index != flow.get("draft_index"):
        session_store = runtime.require_conversation().session_store
        session_store.upsert(
            thread_id,
            user_id=user_id,
            user_role=session.get("user_role"),
            state={"chat_flow": {**flow, "draft_index": draft_index}},
        )
    active = None
    if row and not (external_user and row.get("draft_type") == "customer_visit"):
        active = limit_draft_context({
            **(row.get("draft") if isinstance(row.get("draft"), dict) else {}),
            "draft_id": row["draft_id"],
            "draft_type": row["draft_type"],
            "status": row["status"],
            "version": row["version"],
        })
    if active is None:
        active = {}
    active["session_drafts"] = [
        item
        for item in draft_index
        if item.get("status") != "DELETED"
        and not (external_user and item.get("draft_type") == "customer_visit")
    ]
    return active if active.get("draft_id") or active["session_drafts"] else None


def _draft_reference(item: dict[str, Any], ordinal: int) -> dict[str, Any] | None:
    draft = item.get("draft") if isinstance(item.get("draft"), dict) else item
    draft_id = str(item.get("draft_id") or draft.get("draft_id") or "").strip()
    if not draft_id:
        return None
    fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
    mysql_fields = draft.get("mysql_fields") if isinstance(draft.get("mysql_fields"), dict) else {}
    title = str(
        item.get("draft_title") or draft.get("title") or fields.get("title")
        or mysql_fields.get("title") or fields.get("enterprise_name") or ""
    ).strip()
    reference = {
        "draft_id": draft_id,
        "ordinal": ordinal,
        "draft_type": item.get("draft_type") or draft.get("draft_type"),
        "status": str(item.get("status") or draft.get("status") or "DRAFT").upper(),
        "version": item.get("version") or draft.get("version"),
    }
    if title:
        reference["title"] = title
    return {key: value for key, value in reference.items() if value not in (None, "")}


def merge_session_draft_index(
    existing: Any, drafts: list[dict[str, Any]], *, complete: bool = False,
) -> list[dict[str, Any]]:
    """Keep stable session ordinals while refreshing only compact draft references."""
    index: list[dict[str, Any]] = []
    by_id: dict[str, int] = {}
    next_ordinal = 1
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                continue
            draft_id = str(item.get("draft_id") or "").strip()
            if not draft_id or draft_id in by_id:
                continue
            try:
                ordinal = max(1, int(item.get("ordinal") or next_ordinal))
            except (TypeError, ValueError):
                ordinal = next_ordinal
            while any(entry["ordinal"] == ordinal for entry in index):
                ordinal += 1
            reference = {**item, "draft_id": draft_id, "ordinal": ordinal}
            by_id[draft_id] = len(index)
            index.append(reference)
            next_ordinal = max(next_ordinal, ordinal + 1)

    current_ids: set[str] = set()
    for item in drafts:
        if not isinstance(item, dict):
            continue
        draft = item.get("draft") if isinstance(item.get("draft"), dict) else item
        draft_id = str(item.get("draft_id") or draft.get("draft_id") or "").strip()
        if not draft_id:
            continue
        current_ids.add(draft_id)
        if str(item.get("status") or draft.get("status") or "").upper() == "DELETED":
            if draft_id in by_id:
                reference = _draft_reference(item, index[by_id[draft_id]]["ordinal"])
                if reference is not None:
                    index[by_id[draft_id]] = reference
            continue
        existing_index = by_id.get(draft_id)
        ordinal = (
            index[existing_index]["ordinal"]
            if existing_index is not None
            else next_ordinal
        )
        reference = _draft_reference(item, ordinal)
        if reference is not None:
            if existing_index is not None:
                index[existing_index] = reference
            else:
                reference["ordinal"] = next_ordinal
                next_ordinal += 1
                by_id[draft_id] = len(index)
                index.append(reference)

    if complete:
        for reference in index:
            if reference["draft_id"] not in current_ids:
                reference["status"] = "DELETED"
    return index


def _order_draft_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda item: str(item.get("draft_id") or ""))
    ordered.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return sorted(ordered, key=lambda item: str(item.get("created_at") or ""))


def load_session_draft_cards(
    runtime: Any, *, thread_id: str, user_id: str, user_role: str
) -> list[dict[str, Any]]:
    """Return owner-scoped session drafts newest first for the conversation card pager."""
    session = runtime.require_conversation().session_store.get(thread_id)
    if not session or str(session.get("user_id") or "") != user_id:
        return []
    store = runtime.require_draft_store()
    list_for_thread = getattr(store, "list_for_thread", None)
    if not callable(list_for_thread):
        return []
    rows = list_for_thread(thread_id)
    owned_rows = [
        row for row in rows
        if str(row.get("user_id") or "") == user_id
    ]
    flow = (session.get("state") or {}).get("chat_flow") or {}
    draft_index = merge_session_draft_index(
        flow.get("draft_index"), _order_draft_rows(owned_rows), complete=True
    )
    if draft_index != flow.get("draft_index"):
        runtime.require_conversation().session_store.upsert(
            thread_id,
            user_id=user_id,
            user_role=session.get("user_role"),
            state={"chat_flow": {**flow, "draft_index": draft_index}},
        )
    ordinals = {
        item["draft_id"]: item["ordinal"]
        for item in draft_index
        if item.get("status") != "DELETED"
    }
    visible = [
        row for row in owned_rows
        if row.get("status") != "DELETED"
        and (row.get("draft_type") != "customer_visit" or user_role in {"internal", "admin"})
    ]
    visible.sort(
        key=lambda row: (str(row.get("updated_at") or ""), str(row.get("draft_id") or "")),
        reverse=True,
    )
    return [
        {
            **(row.get("draft") if isinstance(row.get("draft"), dict) else {}),
            "draft_id": row.get("draft_id"),
            "draft_type": row.get("draft_type"),
            "status": row.get("status"),
            "version": row.get("version"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "ordinal": ordinals.get(row.get("draft_id")),
        }
        for row in visible[:100]
    ]


def sync_session_draft(
    runtime: Any,
    thread_id: str | None,
    draft: dict[str, Any] | None,
    *,
    user_id: str | None = None,
    user_role: str | None = None,
    status: str | None = None,
) -> None:
    """Keep an unpublished draft edit visible to the next turn in its session."""
    if not thread_id or not isinstance(draft, dict):
        return

    draft_id = str(draft.get("draft_id") or "").strip()
    if not draft_id:
        return

    try:
        store = getattr(runtime.require_conversation(), "session_store", None)
        if store is None:
            return

        session = store.get(thread_id) or {}
        state = dict(session.get("state") or {})
        flow = dict(state.get("chat_flow") or {})
        next_draft = limit_draft_context(
            {
                **draft,
                "draft_id": draft_id,
                **({"status": status} if status else {}),
            }
        )

        request_context = dict(flow.get("request_context") or {})
        draft_context = dict(request_context.get("draft_context") or {})
        draft_context.update(
            {
                "draft_id": draft_id,
                "draft_type": next_draft.get("draft_type"),
                "fields": next_draft.get("fields") or {},
                "mysql_fields": next_draft.get("mysql_fields") or {},
                "attachments": next_draft.get("attachments") or [],
                "source_blocks": next_draft.get("source_blocks") or [],
            }
        )
        for key in ("text", "summary", "inquiry_summary", "match_direction"):
            if next_draft.get(key) not in (None, ""):
                draft_context[key] = next_draft[key]

        draft_rows = [next_draft]
        draft_store_loader = getattr(runtime, "require_draft_store", None)
        index_owner_id = str(user_id or session.get("user_id") or "").strip()
        index_complete = False
        try:
            draft_store = draft_store_loader() if callable(draft_store_loader) else None
            list_for_thread = getattr(draft_store, "list_for_thread", None)
            if thread_id and index_owner_id and callable(list_for_thread):
                draft_rows = [
                    item for item in list_for_thread(thread_id)
                    if str(item.get("user_id") or "") == index_owner_id
                ]
                if not any(item.get("draft_id") == draft_id for item in draft_rows):
                    draft_rows.append(next_draft)
                draft_rows = _order_draft_rows(draft_rows)
                index_complete = True
        except Exception:
            logger.warning("读取会话草稿索引失败 thread_id=%s", thread_id, exc_info=True)
            draft_rows = [next_draft]

        flow.update(
            {
                "active_draft_id": draft_id,
                "active_draft": next_draft,
                "draft_id": draft_id,
                "draft_type": next_draft.get("draft_type"),
                "draft_status": str(next_draft.get("status") or status or "DRAFT").upper(),
                "draft_index": merge_session_draft_index(
                    flow.get("draft_index"),
                    draft_rows,
                    complete=index_complete,
                ),
                "request_context": {
                    **request_context,
                    "draft_context": draft_context,
                },
            }
        )
        store.upsert(
            thread_id,
            user_id=user_id,
            user_role=user_role,
            state={"chat_flow": flow},
        )
    except Exception:
        logger.warning("同步会话草稿失败 thread_id=%s", thread_id, exc_info=True)
