"""统一用户身份解析。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException
from starlette.requests import Request

from runtime.config import get_settings




def require_request_user_id(request: Request) -> str:
    """Return the authenticated user from request state; never trust request data."""

    user_id = str(getattr(request.state, "user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_REQUIRED",
                "message": "请先注册并登录后使用智能助手",
                "details": {"login_required": True},
            },
        )
    return user_id




def issue_access_token(user_id: str) -> str:
    """签发智能体自己的会话令牌；原系统令牌不落库。"""
    settings = get_settings()
    payload = {"user_id": user_id, "exp": int(time.time()) + settings.auth_token_ttl_seconds}
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(
        settings.auth_token_secret.encode(), encoded.encode(), hashlib.sha256
    ).digest()
    return f"{encoded}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def verify_access_token(token: str) -> str:
    """校验智能体会话令牌并返回 user_id。"""
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(
            get_settings().auth_token_secret.encode(), encoded.encode(), hashlib.sha256
        ).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(actual, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if (
            int(payload.get("exp", 0)) <= int(time.time())
            or not str(payload.get("user_id") or "").strip()
        ):
            raise ValueError
        return str(payload["user_id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="智能体登录已失效，请重新登录") from None
