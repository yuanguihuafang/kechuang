"""Map public conversation IDs to DeerFlow-safe internal IDs."""

from __future__ import annotations

import hashlib
import re

_DEERFLOW_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_deerflow_thread_id(external_thread_id: str, user_id: str) -> str:
    """Return a stable DeerFlow ID without changing the public conversation ID.

    Valid legacy IDs remain unchanged. Invalid IDs use a deterministic,
    user-namespaced digest; the external ID remains the only ID returned to
    the frontend, so no reverse lookup table is needed for this boundary.
    """

    external = str(external_thread_id or "").strip()
    user = str(user_id or "").strip()
    if not external:
        raise ValueError("thread_id 不能为空")
    if not user:
        raise ValueError("user_id 不能为空")
    if _DEERFLOW_THREAD_ID.fullmatch(external):
        return external

    digest = hashlib.sha256(f"{user}\0{external}".encode("utf-8")).hexdigest()
    return f"kc-{digest[:56]}"
