import { download, request } from "./api/request.js";

const SERVER_API_BASE = (
  import.meta.env.VITE_API_BASE_URL ||
  `${window.location.protocol}//${window.location.hostname}:8793`
).replace(/\/+$/, "");

const API = {
  GATEWAY: SERVER_API_BASE,
  OVERALL: SERVER_API_BASE,
};

export const api = {
  health: () =>
    request(`${API.OVERALL}/api/v1/health`, {
      skipAuth: true,
      timeoutMs: 10000,
    }),
  authLogin: (payload) =>
    request(`${API.GATEWAY}/api/v1/auth/login`, {
      skipAuth: true,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  authMe: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/auth/me?user_id=${encodeURIComponent(userId)}`,
    ),
  authLogout: () =>
    request(`${API.GATEWAY}/api/v1/auth/logout`, { method: "POST" }),
  listTestUsers: () =>
    request(`${API.GATEWAY}/api/v1/identity/test-users`, { skipAuth: true }),
  myProfile: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/profile?user_id=${encodeURIComponent(userId)}`,
    ),
  myDocuments: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/documents?user_id=${encodeURIComponent(userId)}`,
    ),
  myDocumentDetail: (userId, documentId) =>
    request(
      `${API.GATEWAY}/api/v1/me/documents/${encodeURIComponent(documentId)}?user_id=${encodeURIComponent(userId)}`,
    ),
  myDocumentParseResult: (documentId) =>
    request(
      `${API.GATEWAY}/api/v1/perception/files/${encodeURIComponent(documentId)}/parse-result`,
    ),
  myConversations: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/conversations?user_id=${encodeURIComponent(userId)}`,
    ),
  myDrafts: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/drafts?user_id=${encodeURIComponent(userId)}`,
    ),
  myDraftDetail: (draftId) =>
    request(`${API.GATEWAY}/api/v1/drafts/${encodeURIComponent(draftId)}`),
  myDraftDelete: (draftId, userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/drafts/${encodeURIComponent(draftId)}?user_id=${encodeURIComponent(userId)}`,
      { method: "DELETE" },
    ),
  myVisitRecords: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/visit-records?user_id=${encodeURIComponent(userId)}`,
    ),
  deleteVisitRecord: (recordId, userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/visit-records/${encodeURIComponent(recordId)}?user_id=${encodeURIComponent(userId)}`,
      { method: "DELETE" },
    ),
  myMatchResults: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/match-results?user_id=${encodeURIComponent(userId)}`,
    ),
  myDraftUnpublish: (draftId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/me/drafts/${encodeURIComponent(draftId)}/unpublish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminDraftUnpublish: (draftId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/drafts/${encodeURIComponent(draftId)}/unpublish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminDraftDelete: (draftId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/drafts/${encodeURIComponent(draftId)}`,
      { method: "DELETE" },
    ),
  myDraftRepublish: (draftId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/me/drafts/${encodeURIComponent(draftId)}/republish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  myUnpublish: (documentId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/me/documents/${encodeURIComponent(documentId)}/unpublish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  myPublish: (documentId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/me/documents/${encodeURIComponent(documentId)}/publish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  chatStream: async (payload, options = {}) => {
    const token = sessionStorage.getItem("kechuang-agent-token");
    const response = await fetch(`${API.GATEWAY}/api/v1/chat/stream`, {
      signal: options.signal,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-Stream-Protocol": options.streamProtocol || "events-v1",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers || {}),
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok || !response.body) {
      const error = new Error(`问答流式请求失败 (${response.status})`);
      error.status = response.status;
      error.retryAfter = Number(response.headers.get("Retry-After")) || 0;
      throw error;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    let streamThreadId = "";
    const consume = (block) => {
      const event = block.match(/^event:\s*(.+)$/m)?.[1]?.trim();
      const data = block.match(/^data:\s*(.+)$/m)?.[1];
      if (!data) return;
      const payloadData = JSON.parse(data);
      options.onEvent?.(event, payloadData);
      if (event === "start") {
        streamThreadId = String(payloadData.thread_id || "").trim();
        options.onStart?.(payloadData);
      }
      if (event === "token" || event === "llm_token")
        options.onToken?.(payloadData.text || "");
      if (event === "done") {
        result = {
          ...(payloadData.result || {}),
          thread_id:
            payloadData.result?.thread_id || streamThreadId || undefined,
        };
      }
      if (event === "error")
        throw new Error(payloadData.message || "问答流式请求失败");
    };
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value || new Uint8Array(), {
        stream: !chunk.done,
      });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      blocks.filter(Boolean).forEach(consume);
      if (chunk.done) break;
    }
    if (buffer.trim()) consume(buffer);
    if (!result) throw new Error("问答流未返回完整结果");
    return result;
  },
  chatMaterialPrepare: (count, userId, threadId = "", intentHint = "") => {
    const form = new FormData();
    form.append("count", String(count));
    form.append("user_id", userId);
    if (threadId) form.append("thread_id", threadId);
    if (intentHint) form.append("intent_hint", intentHint);
    return request(`${API.GATEWAY}/api/v1/chat/materials/prepare`, {
      method: "POST",
      body: form,
    });
  },
  chatMaterialParse: (
    file,
    userId,
    threadId = "",
    message = "",
    intentHint = "",
    attachmentMode = "new",
    knowledgeType = "",
    options = {},
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("user_id", userId);
    if (threadId) form.append("thread_id", threadId);
    if (message) form.append("message", message);
    if (intentHint) form.append("intent_hint", intentHint);
    if (knowledgeType) form.append("knowledge_type", knowledgeType);
    form.append("attachment_mode", attachmentMode);
    if (options.debug) form.append("debug", "true");
    const headers = { Accept: "text/event-stream" };
    const token = sessionStorage.getItem("kechuang-agent-token");
    if (token) headers.Authorization = `Bearer ${token}`;
    return fetch(`${API.GATEWAY}/api/v1/chat/materials/parse/stream`, {
      method: "POST",
      headers,
      body: form,
      signal: options.signal,
    }).then(async (response) => {
      if (!response.ok || !response.body) {
        const error = new Error(`文件解析请求失败 (${response.status})`);
        error.status = response.status;
        error.retryAfter = Number(response.headers.get("Retry-After")) || 0;
        throw error;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let result = null;
      let streamThreadId = "";
      const consume = (block) => {
        const event = block.match(/^event:\s*(.+)$/m)?.[1]?.trim();
        const data = block.match(/^data:\s*(.+)$/m)?.[1];
        if (!data) return;
        const payload = JSON.parse(data);
        options.onEvent?.(event, payload);
        if (event === "start")
          streamThreadId = String(payload.thread_id || "").trim();
        if (event === "token" || event === "llm_token")
          options.onToken?.(payload.text || "", payload);
        if (event === "error") {
          const error = new Error(payload.message || "文件处理失败");
          error.status = payload.code;
          error.stage = payload.stage;
          throw error;
        }
        if (event === "done") {
          result = {
            ...(payload.result || {}),
            thread_id: payload.result?.thread_id || streamThreadId || undefined,
          };
        }
      };
      while (true) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(), {
          stream: !chunk.done,
        });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        blocks.filter(Boolean).forEach(consume);
        if (chunk.done) break;
      }
      if (buffer.trim()) consume(buffer);
      if (!result) throw new Error("文件解析流未返回完整结果");
      return result;
    });
  },
  contact: (payload) =>
    request(`${API.GATEWAY}/api/v1/me/contact`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  chatHistory: (threadId, userId, limit = 20) =>
    request(
      `${API.GATEWAY}/api/v1/chat/sessions/${encodeURIComponent(threadId)}/messages?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
    ),
  renameChatSession: (threadId, userId, title) =>
    request(
      `${API.GATEWAY}/api/v1/chat/sessions/${encodeURIComponent(threadId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, title }),
      },
    ),
  deleteChatSession: (threadId, userId) =>
    request(
      `${API.GATEWAY}/api/v1/chat/sessions/${encodeURIComponent(threadId)}?user_id=${encodeURIComponent(userId)}`,
      {
        method: "DELETE",
      },
    ),
  createDraft: (payload) =>
    request(`${API.GATEWAY}/api/v1/drafts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  listDrafts: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/me/drafts?user_id=${encodeURIComponent(userId)}`,
    ),
  checkDraftSimilarity: (payload) =>
    request(`${API.GATEWAY}/api/v1/drafts/similarity-check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateDraft: (draftId, payload) =>
    request(`${API.GATEWAY}/api/v1/drafts/${encodeURIComponent(draftId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  removeDraftAttachment: (draftId, attachment) =>
    request(`${API.GATEWAY}/api/v1/drafts/${encodeURIComponent(draftId)}/attachments/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document_id: attachment?.document_id || null,
        storage_key: attachment?.document_id ? null : attachment?.storage_key || null,
      }),
    }),
  transitionDraft: (draftId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/drafts/${encodeURIComponent(draftId)}/transition`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  publishDraft: (draftId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/drafts/${encodeURIComponent(draftId)}/publish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  listMemoryCandidates: (userId) =>
    request(
      `${API.GATEWAY}/api/v1/memory/candidates?user_id=${encodeURIComponent(userId)}`,
    ),
  confirmMemory: (memoryId, userId) =>
    request(
      `${API.GATEWAY}/api/v1/memory/${encodeURIComponent(memoryId)}/confirm?user_id=${encodeURIComponent(userId)}`,
      { method: "POST" },
    ),
  rejectMemory: (memoryId, userId) =>
    request(
      `${API.GATEWAY}/api/v1/memory/${encodeURIComponent(memoryId)}/reject?user_id=${encodeURIComponent(userId)}`,
      { method: "POST" },
    ),
  adminOverview: () => request(`${API.GATEWAY}/api/v1/admin/overview`),
  adminDocuments: (params = {}) => {
    const query = new URLSearchParams();
    if (params.scope_id) query.set("scope_id", params.scope_id);
    if (params.user_id) query.set("user_id", params.user_id);
    if (params.knowledge_type)
      query.set("knowledge_type", params.knowledge_type);
    if (params.status) query.set("status", params.status);
    if (params.search) query.set("search", params.search);
    if (params.published_or_admin_only)
      query.set("published_or_admin_only", "true");
    if (params.page) query.set("page", String(params.page));
    if (params.page_size) query.set("page_size", String(params.page_size));
    return request(
      `${API.GATEWAY}/api/v1/admin/documents${query.toString() ? `?${query}` : ""}`,
    );
  },
  adminTasks: (params = {}) => {
    const query = new URLSearchParams();
    if (params.operation) query.set("operation", params.operation);
    if (params.document_id) query.set("document_id", params.document_id);
    if (params.page) query.set("page", String(params.page));
    if (params.page_size) query.set("page_size", String(params.page_size));
    return request(
      `${API.GATEWAY}/api/v1/admin/tasks${query.toString() ? `?${query}` : ""}`,
    );
  },
  adminKnowledgeUpdates: ({
    page = 1,
    page_size = 20,
    published_only = true,
    knowledge_type,
  } = {}) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(page_size),
      published_only: String(published_only),
    });
    if (knowledge_type) params.set("knowledge_type", knowledge_type);
    return request(`${API.GATEWAY}/api/v1/admin/knowledge-updates?${params}`);
  },
  adminKnowledgeUpdateDetail: (recordId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge-updates/${encodeURIComponent(recordId)}`,
    ),
  adminConfirmKnowledgeUpdate: (recordId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge-updates/${encodeURIComponent(recordId)}/confirm`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminBusinessRecords: (recordType = "") =>
    request(
      `${API.GATEWAY}/api/v1/admin/business-records${recordType ? `?record_type=${encodeURIComponent(recordType)}` : ""}`,
    ),
  saveBusinessRecord: (payload) =>
    request(`${API.GATEWAY}/api/v1/business/records`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  confirmBusinessRecord: (payload) =>
    request(`${API.GATEWAY}/api/v1/business/records/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  adminDocument: (documentId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/documents/${encodeURIComponent(documentId)}`,
    ),
  adminFieldPolicies: () =>
    request(`${API.GATEWAY}/api/v1/admin/field-policies`),
  adminAuditEvents: (page = 1, pageSize = 20, service = "", filters = {}) => {
    const params = new URLSearchParams({ page, page_size: pageSize });
    if (service) params.set("service", service);
    Object.entries(filters).forEach(
      ([key, value]) => value && params.set(key, value),
    );
    return request(`${API.GATEWAY}/api/v1/admin/audit-events?${params}`);
  },
  adminAuditEventDetail: (eventId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/audit-events/${encodeURIComponent(eventId)}`,
    ),
  adminOperations: () => request(`${API.GATEWAY}/api/v1/admin/operations`),
  adminRuntimeEvidence: () =>
    request(`${API.GATEWAY}/api/v1/admin/runtime-evidence`),
  adminStartRuntimeEvidence: (payload) =>
    request(`${API.GATEWAY}/api/v1/admin/runtime-evidence/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  adminResetRuntimeEvidence: (payload) =>
    request(`${API.GATEWAY}/api/v1/admin/runtime-evidence/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  adminRuntimeEvidenceExport: (format = "json") =>
    download(
      `${API.GATEWAY}/api/v1/admin/runtime-evidence/export?format=${encodeURIComponent(format)}`,
    ),
  adminRunRetention: (payload) =>
    request(`${API.GATEWAY}/api/v1/admin/operations/retention`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  metrics: () => request(`${API.GATEWAY}/api/v1/metrics`),
  adminMetrics: () => request(`${API.GATEWAY}/api/v1/admin/metrics`),
  adminRetrieve: (payload) =>
    request(`${API.GATEWAY}/api/v1/admin/knowledge/retrieve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  adminSyncMysql: (knowledgeType, batchSize = 64) =>
    request(`${API.GATEWAY}/api/v1/admin/knowledge/sync-mysql`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        knowledge_type: knowledgeType,
        batch_size: batchSize,
      }),
    }),
  adminMysqlSyncStatus: (taskId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge/sync-mysql/${encodeURIComponent(taskId)}`,
    ),
  adminStopMysqlSync: (taskId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge/sync-mysql/${encodeURIComponent(taskId)}/stop`,
      { method: "POST" },
    ),
  adminMysqlCounts: (refresh = false) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge/mysql-counts?refresh=${refresh ? "true" : "false"}`,
      { timeoutMs: 15000 },
    ),
  adminRebuildCollection: (knowledgeType) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge/collections/${encodeURIComponent(knowledgeType)}/rebuild?confirm=true`,
      { method: "POST" },
    ),
  adminRestoreCollection: (knowledgeType, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge/collections/${encodeURIComponent(knowledgeType)}/restore`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminActivateCollection: (knowledgeType, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge/collections/${encodeURIComponent(knowledgeType)}/activate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminRebuildMysqlData: (knowledgeType) =>
    request(
      `${API.GATEWAY}/api/v1/admin/knowledge/collections/${encodeURIComponent(knowledgeType)}/rebuild-mysql?confirm=true`,
      { method: "POST" },
    ),
  persistentMetrics: (windowHours = 24) =>
    request(
      `${API.GATEWAY}/api/v1/metrics/persistent?window_hours=${encodeURIComponent(windowHours)}`,
    ),
  adminConversations: ({ page = 1, page_size = 20 } = {}) =>
    request(
      `${API.GATEWAY}/api/v1/admin/conversations?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(page_size)}`,
    ),
  adminConversation: (sessionId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/conversations/${encodeURIComponent(sessionId)}`,
    ),
  adminDeleteConversation: (sessionId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/conversations/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    ),
  adminTask: (taskId) =>
    request(`${API.GATEWAY}/api/v1/admin/tasks/${encodeURIComponent(taskId)}`),
  documentFileUrl: (documentId, identity, inline = false) => {
    const query = new URLSearchParams();
    if (identity?.user_id) query.set("user_id", identity.user_id);
    if (inline) query.set("inline", "true");
    return `${API.GATEWAY}/api/v1/knowledge/documents/${encodeURIComponent(documentId)}/file?${query.toString()}`;
  },
  adminRecoverTask: (taskId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/tasks/${encodeURIComponent(taskId)}/recover`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminUpload: (
    file,
    scopeId,
    userId,
    knowledgeType,
    idempotencyKey,
    documentId = "",
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("scope_id", scopeId);
    // 管理员上传统一归属平台管理员，避免默认测试用户出现在知识文档归属中。
    form.append("user_id", "admin");
    form.append("user_role", "admin");
    form.append("knowledge_type", knowledgeType);
    if (idempotencyKey) form.append("idempotency_key", idempotencyKey);
    if (documentId) form.append("document_id", documentId);
    return request(`${API.GATEWAY}/api/v1/admin/files/upload`, {
      method: "POST",
      body: form,
    });
  },
  adminParseTask: (taskId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/tasks/${encodeURIComponent(taskId)}/parse`,
      { method: "POST" },
    ),
  adminExtractFields: (taskId, knowledgeType) =>
    request(
      `${API.GATEWAY}/api/v1/admin/tasks/${encodeURIComponent(taskId)}/extract-fields${knowledgeType ? `?knowledge_type=${encodeURIComponent(knowledgeType)}` : ''}`,
      { method: "POST" },
    ),
  adminConfirmTask: (taskId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/tasks/${encodeURIComponent(taskId)}/confirm`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  retrievalChunkPreview: (payload) =>
    request(`${API.GATEWAY}/api/v1/knowledge/chunk-preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  retrievalChunkDetail: (payload) =>
    request(`${API.GATEWAY}/api/v1/knowledge/chunk-detail`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  adminDocumentVersions: (documentId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/documents/${encodeURIComponent(documentId)}/versions`,
    ),
  adminParseResult: (documentId) =>
    request(
      `${API.GATEWAY}/api/v1/admin/documents/${encodeURIComponent(documentId)}/parse-result`,
    ),
  adminPublishVersion: (versionId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/versions/${encodeURIComponent(versionId)}/publish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminUnpublishVersion: (versionId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/versions/${encodeURIComponent(versionId)}/unpublish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminRecycleVersion: (versionId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/versions/${encodeURIComponent(versionId)}/recycle`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminRestoreVersion: (versionId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/versions/${encodeURIComponent(versionId)}/restore`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminPurgeVersion: (versionId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/versions/${encodeURIComponent(versionId)}/purge`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  adminDeleteDocument: (documentId, payload) =>
    request(
      `${API.GATEWAY}/api/v1/admin/documents/${encodeURIComponent(documentId)}`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  parseResult: (documentId) =>
    request(
      `${API.GATEWAY}/api/v1/perception/files/${encodeURIComponent(documentId)}/parse-result`,
    ),
  saveParseResult: (documentId, rawText, options = {}) =>
    request(
      `${API.GATEWAY}/api/v1/perception/files/${encodeURIComponent(documentId)}/parse-result`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_text: rawText, ...options }),
      },
    ),
  perceptionAssetUrl: (assetUrl) => {
    const value = String(assetUrl || "");
    if (/^https?:\/\//i.test(value)) return value;
    return `${API.GATEWAY}/${value.replace(/^\/+/, "")}`;
  },
  index: (payload) =>
    request(`${API.GATEWAY}/api/v1/knowledge/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  search: (payload) =>
    request(`${API.GATEWAY}/api/v1/knowledge/retrieve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  deleteStoredFile: (document, userId) =>
    request(
      `${API.GATEWAY}/api/v1/perception/files?storage_key=${encodeURIComponent(document.storage_key)}&document_id=${encodeURIComponent(document.document_id)}&user_id=${encodeURIComponent(userId || document.user_id || "")}`,
      {
        method: "DELETE",
      },
    ),
};
