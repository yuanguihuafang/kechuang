"""将 Perception/Retrieval 入口封装为 Supervisor 工具。"""

from __future__ import annotations

from typing import Any

from core.contracts.agent import AgentContext
from core.contracts.knowledge import DocumentUnpublishRequest
from core.contracts.tools import ToolContext
from services.observability.metrics import metrics
from services.persistence.retention import serialize_storage_references


class InProcessToolAdapter:
    """单端口模式的 Tool Adapter，直接调用两个 Agent 的入口函数。

    工具调用发生在同一 Python 进程内，不经过子 Agent HTTP 端口。
    """

    def __init__(
        self,
        perception_module: Any,
        retrieval_module: Any,
        lifecycle_store: Any | None = None,
        web_search_module: Any | None = None,
        draft_store: Any | None = None,
    ) -> None:
        self.perception = perception_module
        self.retrieval = retrieval_module
        self.lifecycle_store = lifecycle_store
        self.web_search = web_search_module
        self.draft_store = draft_store

    @staticmethod
    def _tool_context(
        arguments: dict[str, Any],
        context: ToolContext | dict[str, Any] | None = None,
        *,
        require_identity: bool = False,
        legacy_role: str = "internal",
    ) -> ToolContext:
        if context is not None:
            return (
                context if isinstance(context, ToolContext) else ToolContext.model_validate(context)
            )
        identity = arguments.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        user_id = str(arguments.get("user_id") or identity.get("user_id") or "").strip()
        user_role = str(
            arguments.get("user_role") or identity.get("user_role") or identity.get("role") or ""
        ).strip()
        if require_identity and (not user_id or not user_role):
            raise ValueError("Tool requires backend-injected user identity")
        return ToolContext(
            user_id=user_id or "legacy-tool",
            user_role=user_role or legacy_role,
            thread_id=arguments.get("thread_id"),
            trace_id=str(arguments.get("trace_id") or arguments.get("request_id") or "tool-trace"),
        )

    @classmethod
    def _context(
        cls,
        arguments: dict[str, Any],
        agent: str,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> AgentContext:
        tool_context = cls._tool_context(arguments, context, require_identity=True)
        return AgentContext(
            request_id=str(arguments.get("request_id") or "tool-request"),
            trace_id=tool_context.trace_id,
            session_id=arguments.get("session_id"),
            task_id=arguments.get("task_id"),
            thread_id=tool_context.thread_id,
            agent=agent,
            run_id=arguments.get("run_id"),
            identity=tool_context.identity(),
        )

    @staticmethod
    def _unwrap(result: Any) -> dict[str, Any]:
        """把统一 AgentResult 解包为 Supervisor Tool 的业务负载。

        Supervisor Tool 继续接收业务字典，但错误和无数据结果保留完整
        envelope，避免解包时吞掉错误码和链路元数据。
        """

        data = getattr(result, "data", None)
        if data is not None and hasattr(data, "model_dump"):
            body = data.model_dump()
            meta = getattr(result, "meta", None)
            if meta is not None:
                body["meta"] = (
                    meta.model_dump(mode="json") if hasattr(meta, "model_dump") else dict(meta)
                )
            return body
        if data is not None and isinstance(data, dict):
            body = dict(data)
            meta = getattr(result, "meta", None)
            if meta is not None:
                body["meta"] = (
                    meta.model_dump(mode="json") if hasattr(meta, "model_dump") else dict(meta)
                )
            return body
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    @classmethod
    def _envelope(cls, result: Any, context: ToolContext) -> dict[str, Any]:
        body = cls._unwrap(result)
        if not isinstance(body, dict):
            body = {"value": body}
        status = str(getattr(result, "status", None) or body.get("status") or "SUCCESS")
        error = getattr(result, "error_message", None) or body.get("error")
        return {
            **body,
            "status": status,
            "data": body,
            "error": error,
            "trace_id": context.trace_id,
        }

    @classmethod
    def _retrieval_arguments(
        cls,
        arguments: dict[str, Any],
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在平台边界把管理员测试身份映射为 Retrieval 可识别的业务身份。

        ``admin`` 是平台治理角色，不属于流程图中的三类业务用户；它不能直接
        进入 Retrieval 的 ``ManualIdentity``。管理员仍保留在 Supervisor 的原始
        请求和审计上下文中，这里只为 Agent 契约生成执行身份。
        """

        normalized = dict(arguments)
        identity = dict(arguments.get("identity") or {})
        tool_context = cls._tool_context(arguments, context, require_identity=True)
        if context is None and (not identity.get("user_id") or not identity.get("user_role")):
            raise ValueError("检索必须提供已解析的用户身份")
        normalized["identity"] = tool_context.identity()
        return normalized

    async def upload_document(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_context = self._tool_context(arguments, context)
        result = await self.perception.upload_document(
            file=arguments["file"],
            scope_id=arguments.get("scope_id", "public"),
            user_id=tool_context.user_id,
            document_id=arguments.get("document_id"),
            new_version=bool(arguments.get("new_version", False)),
        )
        return self._envelope(result, tool_context)

    async def parse_document(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_context = self._tool_context(arguments, context)
        # Supervisor 自己负责任务状态；这里调用 Agent 的同步业务函数，
        # 不能拿 Agent 的 202 提交响应当作解析结果。
        result = await self.perception.parse_stored_document(
            document_id=arguments["document_id"],
            storage_key=arguments["storage_key"],
            file_name=arguments.get("file_name", ""),
            knowledge_type=arguments.get("knowledge_type", ""),
        )
        return self._envelope(result, tool_context)

    async def search_knowledge(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_context = self._tool_context(arguments, context, require_identity=True)
        request = self.retrieval.RetrievalRequest.model_validate(
            self._retrieval_arguments(arguments, tool_context)
        )
        target = getattr(self.retrieval, "agent", self.retrieval)
        with metrics.stage("retrieval.total"):
            if hasattr(target, "invoke"):
                result = await target.invoke(
                    self._context(arguments, "retrieval", tool_context), request
                )
            else:
                result = await self.retrieval.retrieve(request)
        return self._envelope(result, tool_context)

    async def search_tavily(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_context = self._tool_context(arguments, context)
        if self.web_search is None:
            from services.web_search import TavilySearchClient

            self.web_search = TavilySearchClient()
        result = await self.web_search.search(
            str(arguments.get("query") or ""),
            max_results=arguments.get("max_results"),
            search_depth=arguments.get("search_depth"),
        )
        return self._envelope(result, tool_context)

    async def index_document(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_context = self._tool_context(arguments, context, require_identity=True)
        request = self.retrieval.IndexRequest.model_validate(
            self._retrieval_arguments(arguments, tool_context)
        )
        # 分块、Embedding、旧向量删除和 Milvus upsert 由 Retrieval Agent
        # 的 LangGraph index 节点一次完成；Supervisor 不重复实现这些细节。
        retrieval_agent = getattr(self.retrieval, "agent", self.retrieval)
        if hasattr(retrieval_agent, "invoke"):
            result = await retrieval_agent.invoke(
                self._context(arguments, "retrieval", tool_context), request
            )
            return self._envelope(result, tool_context)
        result = await retrieval_agent.index(request)
        return self._envelope(result, tool_context)

    async def delete_document(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_context = self._tool_context(arguments, context, legacy_role="admin")
        normalized = dict(arguments)
        normalized["user_id"] = tool_context.user_id
        normalized["identity"] = tool_context.identity()
        try:
            result = await self._delete_document(normalized)
        except Exception as exc:  # noqa: BLE001 - 工具边界会返回结构化错误
            result = {"status": "FAILED", "error": str(exc)}
        return self._envelope(
            {**result, "thread_id": tool_context.thread_id},
            tool_context,
        )

    async def _delete_document(self, arguments: dict[str, Any]) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        unpublish_request = DocumentUnpublishRequest.model_validate(
            {
                "scope_id": arguments.get("scope_id", "public"),
                "knowledge_type": arguments.get("knowledge_type"),
                "identity": {
                    "user_id": arguments.get("user_id", ""),
                    "user_role": (arguments.get("identity") or {}).get("user_role", "admin"),
                },
                "remove_vectors": True,
            }
        )
        unpublish = await self.retrieval.unpublish_document(document_id, unpublish_request)
        unpublish_body = unpublish.model_dump()
        if unpublish_body.get("status") not in {"UNPUBLISHED", "SUCCESS"}:
            return {"status": "FAILED", "stage": "retrieval", "retrieval": unpublish_body}
        deleted = await self.perception.delete_stored_document_internal(
            storage_key=arguments["storage_key"],
            document_id=document_id,
        )
        if self.lifecycle_store is not None:
            self.lifecycle_store.purge_document(document_id)
        return {"status": "DELETED", "retrieval": unpublish_body, "perception": deleted}

    @serialize_storage_references
    async def delete_version(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_context = self._tool_context(arguments, context, legacy_role="admin")
        normalized = dict(arguments)
        normalized["user_id"] = tool_context.user_id
        normalized["identity"] = tool_context.identity()
        try:
            result = await self._delete_version(normalized)
        except Exception as exc:  # noqa: BLE001 - 工具边界会返回结构化错误
            result = {"status": "FAILED", "error": str(exc)}
        return self._envelope(
            {**result, "thread_id": tool_context.thread_id},
            tool_context,
        )

    async def _delete_version(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """替换版本时只清理指定旧版本，不影响新版本索引。"""

        document_id = str(arguments["document_id"])
        version_id = str(arguments["version_id"])
        store = self.lifecycle_store
        if store is None or arguments.get("preserve_current_vectors") is not True:
            raise ValueError("旧版本清理必须核实活动版本并保留当前索引")
        version = store.get_version(version_id)
        replacement_id = str(arguments.get("replacement_version_id") or "")
        replacement = store.get_version(replacement_id) if replacement_id else None
        document = store.get_document(document_id, include_purged=True)
        key = str(arguments.get("storage_key") or "").strip()
        if (not document or not version or version.get("document_id") != document_id
                or not key or version.get("storage_key") != key
                or not replacement or replacement.get("document_id") != document_id
                or document.get("active_version_id") != replacement_id
                or version_id == replacement_id or replacement.get("status") not in {"PUBLISHED", "INDEXED"}
                or replacement.get("publication_status") != "PUBLISHED"
                or replacement.get("storage_key") != arguments.get("replacement_storage_key")):
            raise ValueError("旧版本、原件或已发布活动替代版本不匹配")
        if version.get("status") == "PURGED":
            return {"status": "DELETED", "already_purged": True}
        if store.is_storage_key_referenced(key, exclude_version_ids=(version_id,)):
            return {"status": "SKIPPED", "reason": "原件仍被其他版本引用"}
        drafts = self.draft_store
        if drafts is None:
            from services.persistence.postgres import PostgresDraftStore

            drafts = PostgresDraftStore()
            self.draft_store = drafts
        if any(row.get("status") != "DELETED" for row in drafts.list_storage_key_references(key)):
            return {"status": "SKIPPED", "reason": "原件仍被草稿引用"}
        for row in drafts.list_for_document(document_id):
            if row.get("status") != "PUBLISHED":
                continue
            attachments = [item for item in (row.get("draft") or {}).get("attachments") or []
                           if isinstance(item, dict) and item.get("document_id") == document_id]
            if not attachments or any(item.get("version_id") in {None, "", version_id}
                                      for item in attachments):
                return {"status": "SKIPPED", "reason": "旧版本仍有发布引用"}
        delete_original = getattr(self.perception, "delete_original_file", None)
        if delete_original is None:
            return {"status": "FAILED", "error": "Perception 未提供旧原文件清理接口"}
        deleted = await delete_original(storage_key=key)
        return {"status": "DELETED", "perception": deleted}
