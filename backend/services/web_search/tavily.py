"""Tavily Search API 的最小异步适配器。"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from runtime.config import Settings, get_settings

_MAX_RESULTS = 20
_DEFAULT_RESULTS = 5


class TavilySearchClient:
    """只负责 Tavily HTTP 调用和结果标准化，不参与 Supervisor 编排。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport

    async def extract(self, url: str) -> dict[str, Any]:
        """Extract one public web page through Tavily, never by fetching the URL locally."""
        url = str(url or "").strip()

        def failure(code: str, message: str) -> dict[str, Any]:
            return {
                "status": "FAILED",
                "error_code": code,
                "error": message,
                "url": url,
                "content": "",
            }

        if len(url) > 2048 or not _public_http_url(url):
            return failure("WEB_FETCH_INVALID_URL", "网页地址无效或不可访问。")
        api_key = str(getattr(self.settings, "tavily_api_key", "") or "").strip()
        if not api_key:
            return failure("WEB_FETCH_NOT_CONFIGURED", "联网搜索服务未配置。")
        timeout = httpx.Timeout(
            float(getattr(self.settings, "tavily_timeout_seconds", 30)),
            connect=float(getattr(self.settings, "llm_connect_timeout_seconds", 30)),
        )
        base_url = str(
            getattr(self.settings, "tavily_base_url", "https://api.tavily.com")
            or "https://api.tavily.com"
        ).rstrip("/")
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self.transport, trust_env=False
            ) as client:
                response = await client.post(
                    f"{base_url}/extract",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"urls": url, "extract_depth": "basic"},
                )
            if response.status_code >= 400:
                return failure("WEB_FETCH_UPSTREAM_ERROR", "网页提取服务请求失败。")
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                return failure("WEB_FETCH_INVALID_RESPONSE", "网页提取服务返回格式异常。")
            for item in body["results"]:
                if isinstance(item, dict) and item.get("url") == url:
                    content = item.get("raw_content")
                    if not isinstance(content, str):
                        return failure("WEB_FETCH_INVALID_RESPONSE", "网页提取服务返回格式异常。")
                    return {"status": "SUCCESS", "url": url, "content": content[:8000]}
            return failure("WEB_FETCH_UPSTREAM_ERROR", "网页提取失败。")
        except httpx.RequestError:
            return failure("WEB_FETCH_NETWORK_ERROR", "网页提取服务暂时不可用。")
        except (ValueError, TypeError):
            return failure("WEB_FETCH_INVALID_RESPONSE", "网页提取服务返回格式异常。")

    async def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        search_depth: str | None = None,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {
                "status": "FAILED",
                "error_code": "WEB_SEARCH_QUERY_REQUIRED",
                "error": "联网搜索需要提供查询内容。",
                "query": "",
                "results": [],
            }
        api_key = str(getattr(self.settings, "tavily_api_key", "") or "").strip()
        if not api_key:
            return {
                "status": "FAILED",
                "error_code": "WEB_SEARCH_NOT_CONFIGURED",
                "error": "联网搜索服务未配置。",
                "query": query,
                "results": [],
            }
        limit = min(max(int(max_results or _DEFAULT_RESULTS), 1), _MAX_RESULTS)
        depth = (
            str(search_depth or getattr(self.settings, "tavily_search_depth", "basic") or "basic")
            .strip()
            .lower()
        )
        if depth not in {"basic", "advanced", "fast", "ultra-fast"}:
            depth = "basic"
        timeout = httpx.Timeout(
            float(getattr(self.settings, "tavily_timeout_seconds", 30)),
            connect=float(getattr(self.settings, "llm_connect_timeout_seconds", 30)),
        )
        base_url = str(
            getattr(self.settings, "tavily_base_url", "https://api.tavily.com")
            or "https://api.tavily.com"
        ).rstrip("/")
        payload = {
            "query": query,
            "search_depth": depth,
            "max_results": limit,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self.transport, trust_env=False
            ) as client:
                response = await client.post(
                    f"{base_url}/search",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if response.status_code >= 400:
                return {
                    "status": "FAILED",
                    "error_code": "WEB_SEARCH_UPSTREAM_ERROR",
                    "error": f"联网搜索服务请求失败（HTTP {response.status_code}）。",
                    "query": query,
                    "results": [],
                }
            body = response.json()
        except (httpx.TimeoutException, httpx.NetworkError):
            return {
                "status": "FAILED",
                "error_code": "WEB_SEARCH_NETWORK_ERROR",
                "error": "联网搜索服务暂时不可用。",
                "query": query,
                "results": [],
            }
        except (ValueError, TypeError):
            return {
                "status": "FAILED",
                "error_code": "WEB_SEARCH_INVALID_RESPONSE",
                "error": "联网搜索服务返回格式异常。",
                "query": query,
                "results": [],
            }
        results = _normalize_results(body.get("results") if isinstance(body, dict) else [], limit)
        return {
            "status": "SUCCESS" if results else "NO_EVIDENCE",
            "query": query,
            "results": results,
        }


def _normalize_results(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_content: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _canonical_url(item.get("url"))
        title = _clean_text(item.get("title"), 500)
        content = _clean_text(item.get("content"), 6000)
        if not url or not content:
            continue
        content_key = re.sub(r"\W+", "", content, flags=re.UNICODE).casefold()
        if url in seen_urls or (content_key and content_key in seen_content):
            continue
        seen_urls.add(url)
        if content_key:
            seen_content.add(content_key)
        score = item.get("score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        normalized.append(
            {
                "title": title or url,
                "url": url,
                "content": content,
                "score": score,
                "published_date": _clean_text(item.get("published_date"), 64) or None,
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def _canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )


def _public_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()
