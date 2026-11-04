import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api } from '../../api.js';
import { BusinessBlocks } from './BusinessBlocks.jsx';
import { DraftList } from './DraftList.jsx';
import { VoiceInput } from './VoiceInput.jsx';
import { AgentDebugPanel } from './AgentDebugPanel.jsx';
import {
  appendMaterialUploadMessages,
  reconcileAssistantMessage,
} from './materialMessageReconciliation.js';
import { matchDraftActionPayload } from './matchDraftAction.js';
import { updateDraftPublicationState } from './draftPresentation.js';
import { examples, externalQuickIntents } from '../../config/quickIntents.js';
import { getChatComposerSize } from './chatComposerSizing.js';
import { submitChatOnEnter } from './chatComposerKeyboard.js';

const formatJson = (value) => JSON.stringify(value, null, 2);
const isPersistedDraftId = (value) => Boolean(value && !String(value).startsWith('draft_session_'));

function cleanAssistantText(value) {
  const lines = String(value || '').split(/\r?\n/).map((line) => {
    const value = line.trim();
    if (!value || /^-+$/.test(value) || /^\|\s*-/.test(value)) return '';
    if (/^#{1,6}\s+/.test(value)) return value.replace(/^#{1,6}\s+/, '');
    if (/^[-*]\s+/.test(value)) return `• ${value.replace(/^[-*]\s+/, '')}`;
    if (/^\|.*\|$/.test(value)) return value.slice(1, -1).split('|').map((cell) => cell.trim()).join('　');
    return value;
  }).filter(Boolean);
  for (let index = 1; index < lines.length; index += 1) {
    if (/^[。！？；，、,.!?;:：]+$/.test(lines[index])) {
      lines[index - 1] += lines[index];
      lines.splice(index, 1);
      index -= 1;
    }
  }
  return lines.join('\n').replace(/\*\*([^*]+)\*\*/g, '$1');
}

function responseElapsedMs(result, preferRetrieval = false) {
  const rawValue = preferRetrieval ? result?.retrieval_elapsed_ms : result?.processing_elapsed_ms;
  if (rawValue == null || rawValue === '') return undefined;
  const value = Number(rawValue);
  return Number.isFinite(value) ? value : undefined;
}

function formatDocumentSummary(value) {
  const prefix = '已从文档提取以下内容：';
  let summary = String(value || '');
  if (summary.startsWith(prefix)) summary = summary.slice(prefix.length);
  summary = summary.replace(/^\s*LLM总结\s*[:：]\s*/, '');
  summary = cleanAssistantText(summary);
  return `${prefix}\nLLM总结：${summary ? `\n${summary}` : ''}`;
}

function parseDocumentSummaryMessage(content) {
  const raw = String(content || '');
  const match = raw.match(/\n+\s*耗时\s*[：:]\s*(?:(\d+(?:\.\d+)?)\s*(?:s|秒)|-)\s*$/i);
  if (!match) return { text: formatDocumentSummary(raw) };
  return {
    text: formatDocumentSummary(raw.slice(0, match.index).trimEnd()),
    elapsedMs: match[1] == null ? undefined : Number(match[1]) * 1000,
  };
}

function intentName(value) {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return '';
  const name = value.name || value.business_intent || value.intent || '';
  return name;
}

function formatElapsed(ms) {
  if (!Number.isFinite(ms)) return '';
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} ms`;
  return `${(ms / 1000).toFixed(2)} 秒`;
}

const friendlyStreamProgress = {
  session: '正在恢复会话…',
  generating: '正在组织回答…',
  retrieval: '正在检索相关资料…',
  confirmation: '正在等待您的确认…',
  draft: '草稿已生成，正在整理展示…',
  completed: '处理完成…',
  processing: '正在处理，请稍候…',
};

const quickIntents = [
  {
    intent: 'enterprise_profile',
    label: '企业画像',
    code: '01',
    description: '了解企业能力、行业背景与潜在需求',
    prompt: '帮我了解这家企业的基本情况、技术能力和潜在需求',
  },
  {
    intent: 'demand_discovery',
    label: '需求发现',
    code: '02',
    description: '查询客户当前的需求与合作诉求',
    prompt: '帮我查询当前客户的需求和待解决问题',
  },
  {
    intent: 'supply_demand_match',
    label: '供需匹配',
    code: '03',
    description: '查询企业、专家、方案和科研成果',
    prompt: '帮我查询与当前需求相关的企业、专家、方案和科研成果',
  },
  {
    intent: 'customer_visit',
    label: '客户拜访',
    code: '04',
    description: '记录交流内容并整理拜访后的跟进方向',
    prompt: '我正在拜访客户，请帮我记录交流内容并整理企业画像、需求和跟进方向',
  },
];

const CHAT_SESSION_KEY = 'kechuang-chat-session-id';

function responseError(response) {
  return response?.error
    || response?.result?.error
    || response?.result?.message
    || response?.result?.detail
    || '本次请求未完成，请检查服务状态和依赖配置。';
}

function ResponseSummary({ response }) {
  if (!response) return <div className="response-empty">暂无响应</div>;
  if (response.status !== 'FAILED') return <pre className="json-viewer">{formatJson(response)}</pre>;

  return <div className="response-failure" role="alert">
    <div className="response-failure-mark">!</div>
    <div className="response-failure-body">
      <strong>本次请求未完成</strong>
      <p>{responseError(response)}</p>
      {response.error_code && <small>错误码：{response.error_code}</small>}
      {response.selected_tool && <small>调用工具：{response.selected_tool}</small>}
      <span>请先检查右上角服务状态，再重新提交问题。</span>
    </div>
  </div>;
}

function chatSessionKey(userId) {
  return `${CHAT_SESSION_KEY}:${userId || 'anonymous'}`;
}

function storedSessionId(userId) {
  const value = localStorage.getItem(chatSessionKey(userId)) || '';
  // 这是旧版前端生成的标识，不代表 PostgreSQL 中的正式会话。
  return value.startsWith('session-chat-') ? '' : value;
}

function identityOptionLabel(user) {
  const id = String(user?.user_id || '');
  const name = String(user?.name || '').trim();
  const person = name && name !== id ? `${name} · ${id}` : (name || id);
  return `${person} · ${user?.is_inner_user ? '内部人员' : '外部人员'}`;
}

/** 问答工作台：管理会话与流式请求，并呈现快捷入口、消息、输入区和调试侧栏。 */
export function SupervisorDebug({
  identity,
  identityOptions = [],
  identityLoading = false,
  identityError = '',
  selectedIdentity,
  allowedScopes = 'public',
  onIdentityChange,
  knowledgeType,
  onKnowledgeTypeChange,
  onUploadAndParse,
  uploadBusy = false,
  showInspector = false,
}) {
  const chatThreadRef = useRef(null);
  const messageInputRef = useRef(null);
  const uploadInputRef = useRef(null);
  const chatAbortControllerRef = useRef(null);
  const historyRequestVersionRef = useRef(0);
  const [message, setMessage] = useState('');
  useLayoutEffect(() => {
    const input = messageInputRef.current;
    if (!input) return;

    const resizeInput = () => {
      input.style.height = 'auto';
      input.style.maxHeight = 'none';
      input.style.overflowY = 'hidden';
      const computed = window.getComputedStyle(input);
      const number = (value) => Number.parseFloat(value) || 0;
      const fontSize = number(computed.fontSize);
      const lineHeight = computed.lineHeight === 'normal'
        ? fontSize * 1.45
        : number(computed.lineHeight);
      const size = getChatComposerSize({
        scrollHeight: input.scrollHeight,
        lineHeight,
        paddingTop: number(computed.paddingTop),
        paddingBottom: number(computed.paddingBottom),
        borderTopWidth: number(computed.borderTopWidth),
        borderBottomWidth: number(computed.borderBottomWidth),
      });

      input.style.height = `${size.height}px`;
      input.style.minHeight = `${size.minHeight}px`;
      input.style.maxHeight = `${size.maxHeight}px`;
      input.style.overflowY = size.overflowY;
    };

    resizeInput();
    window.addEventListener('resize', resizeInput);
    return () => window.removeEventListener('resize', resizeInput);
  }, [message]);
  const [requestId, setRequestId] = useState('req-chat-001');
  const [sessionId, setSessionId] = useState(() => {
    const userId = identity?.user_id || 'anonymous';
    // 新会话由后端创建并写入 PostgreSQL；这里只恢复已有的真实会话 ID。
    return storedSessionId(userId);
  });
  const sessionIdRef = useRef(sessionId);
  const matchContextRef = useRef(null);
  const [taskId, setTaskId] = useState('task-chat-001');
  const [traceId, setTraceId] = useState('');
  const [response, setResponse] = useState(null);
  const [streamEvents, setStreamEvents] = useState([]);
  const [messages, setMessages] = useState([]);
  const [requestPreview, setRequestPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [uploadFile, setUploadFile] = useState(null);
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [uploadConfirmationPending, setUploadConfirmationPending] = useState(false);
  const [intentHint, setIntentHint] = useState('');
  const selectedIntentRef = useRef('');
  const [draftIntake, setDraftIntake] = useState(null);
  const [uploadMessage, setUploadMessage] = useState('');
  const [memoryCandidates, setMemoryCandidates] = useState([]);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [memoryError, setMemoryError] = useState('');
  const [userIdSearch, setUserIdSearch] = useState('');
  const [userIdSearchError, setUserIdSearchError] = useState('');
  const [draftRefreshKey, setDraftRefreshKey] = useState(0);
  const [conversationList, setConversationList] = useState([]);
  const [conversationMenuOpen, setConversationMenuOpen] = useState(false);
  const [sessionActionId, setSessionActionId] = useState('');
  const [renamingSessionId, setRenamingSessionId] = useState('');
  const [renameValue, setRenameValue] = useState('');

  function adoptSessionId(value) {
    const nextSessionId = String(value || '').trim();
    if (!nextSessionId) return;
    sessionIdRef.current = nextSessionId;
    setSessionId(nextSessionId);
    if (identity?.user_id) localStorage.setItem(chatSessionKey(identity.user_id), nextSessionId);
  }

  async function searchAndSwitchUser(event) {
    event.preventDefault();
    const userId = userIdSearch.trim();
    if (!userId) return;
    setUserIdSearchError('');
    try {
      await onIdentityChange?.(userId);
    } catch (error) {
      setUserIdSearchError(error.message || '用户不存在或读取失败');
    }
  }
  const [externalFlow, setExternalFlow] = useState('');
  const isExternalUser = identity?.user_role === 'external';

  async function loadConversationList() {
    if (!identity?.user_id) return;
    try {
      const result = await api.myConversations(identity.user_id);
      setConversationList(Array.isArray(result?.sessions) ? result.sessions : []);
    } catch {
      setConversationList([]);
    }
  }

  useEffect(() => {
    loadConversationList();
  }, [identity?.user_id, isExternalUser]);

  useEffect(() => {
    const thread = chatThreadRef.current;
    if (!thread || messages.length === 0) return;
    requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  }, [messages]);

  async function loadMemoryCandidates() {
    if (!identity?.user_id) return;
    try {
      setMemoryError('');
      setMemoryCandidates(await api.listMemoryCandidates(identity.user_id));
    } catch (requestError) {
      setMemoryError(requestError.message);
    }
  }

  useEffect(() => {
    if (showInspector) loadMemoryCandidates();
  }, [identity?.user_id, showInspector]);

  useEffect(() => {
    if (!sessionId.trim() || !identity?.user_id) return undefined;
    if (chatAbortControllerRef.current) return undefined;
    localStorage.setItem(chatSessionKey(identity.user_id), sessionId.trim());
    let cancelled = false;
    const requestVersion = ++historyRequestVersionRef.current;
    api.chatHistory(sessionId.trim(), identity.user_id, 20).then((history) => {
      if (
        cancelled
        || requestVersion !== historyRequestVersionRef.current
        || !Array.isArray(history)
        || history.length === 0
      ) return;
      const assistantResponses = history
        .filter((item) => item.role === 'assistant' && item.response && typeof item.response === 'object')
        .map((item) => item.response);
      const latestResponse = assistantResponses.at(-1) || null;
      const latestIntent = assistantResponses.slice().reverse().map((item) => (
        intentName(item.intent) || intentName(item.internal_flow)
      )).find(Boolean) || intentName(latestResponse?.intent) || selectedIntentRef.current;
      const restoredFlow = latestIntent === 'demand' ? 'demand' : (
        ['solution', 'achievement', 'supply_draft'].includes(latestIntent) ? 'solution' : ''
      );
      selectedIntentRef.current = latestIntent;
      setIntentHint(latestIntent);
      setExternalFlow(restoredFlow);
      if (restoredFlow) onKnowledgeTypeChange?.(restoredFlow);
      setResponse(latestResponse);
      if (latestResponse?.status === 'DRAFT_INFO_REQUIRED') {
        setDraftIntake({
          intent: latestIntent || latestResponse.intent?.name || '',
          demandType: latestResponse.intent?.demand_type || undefined,
          missingFields: latestResponse.intent?.missing_fields || [],
        });
      } else if (latestResponse?.status === 'DRAFT_GENERATED' || latestResponse?.status === 'DRAFT_SAVED' || latestResponse?.status === 'PUBLISHED') {
        setDraftIntake(null);
      }
      setMessages(history.map((item) => {
        const documentSummary = item.role === 'assistant'
          && typeof item.content === 'string'
          && item.content.startsWith('已从文档提取以下内容：')
          ? parseDocumentSummaryMessage(item.content)
          : null;
        const response = item.role === 'assistant' && item.response && typeof item.response === 'object' ? item.response : null;
        return {
          id: item.message_id,
          role: item.role,
          text: item.role === 'assistant' && item.response?.status === 'FAILED'
            ? '当前检索服务暂不可用，请检查服务状态后重试。'
            : documentSummary
              ? documentSummary.text
              : item.role === 'assistant' && response
                ? assistantText(response)
                : typeof item.content === 'string' ? item.content : '',
          response,
          elapsedMs: documentSummary?.elapsedMs ?? responseElapsedMs(response),
        };
      }));
    }).catch(() => { /* 服务未启动时保持空白会话，发送消息仍会给出明确错误 */ });
    return () => { cancelled = true; };
  }, [identity?.user_id, sessionId]);

  useEffect(() => {
    if (!identity?.user_id) return;
    const restoredSessionId = storedSessionId(identity.user_id);
    sessionIdRef.current = restoredSessionId;
    setSessionId(restoredSessionId);
    setMessages([]);
    setStreamEvents([]);
    chatAbortControllerRef.current?.abort();
  }, [identity?.user_id]);

  useEffect(() => () => {
    chatAbortControllerRef.current?.abort();
  }, []);

  async function sendMessage(text, overrides = {}) {
    const normalizedText = text.trim();
    if (!normalizedText) { setError('请输入你的问题或需求。'); return; }
    if (chatAbortControllerRef.current) { setError('上一条请求仍在处理中，请稍候。'); return; }
    const controller = new AbortController();
    const payload = {
      message: normalizedText,
      user_id: identity.user_id,
      thread_id: sessionIdRef.current.trim() || undefined,
      // 快捷入口和控制按钮可通过 overrides 传入；普通追问由后端按会话状态继承当前意图。
      intent_hint: Object.prototype.hasOwnProperty.call(overrides, 'intent_hint')
        ? overrides.intent_hint
        : undefined,
      draft_context: draftIntake ? { draft_type: draftIntake.intent, demand_type: draftIntake.demandType } : undefined,
      debug: true,
      ...overrides,
    };
    chatAbortControllerRef.current = controller;
    setRequestPreview(payload); setError(''); setBusy(true); setMessage(''); setIntentHint(''); setStreamEvents([]);
    // Do not let an in-flight history read overwrite the optimistic messages.
    historyRequestVersionRef.current += 1;
    const messageId = `${Date.now()}-${Math.random()}`;
    setMessages((items) => [...items, { id: `${messageId}-user`, role: 'user', text: normalizedText }, { id: `${messageId}-assistant`, role: 'assistant', pending: true, progress: '正在为您处理，请稍候…' }]);
    try {
      const result = await api.chatStream(payload, {
        signal: controller.signal,
        onStart: ({ thread_id: startedSessionId }) => {
          adoptSessionId(startedSessionId);
        },
        onToken: (token) => {
          setMessages((items) => items.map((item) => item.id === `${messageId}-assistant`
            ? { ...item, pending: false, text: `${item.text || ''}${token}` }
            : item));
        },
        onEvent: (event, data) => {
          if (event !== 'token' && event !== 'llm_token') {
            setStreamEvents((items) => [...items, { event, data, at: new Date().toISOString() }]);
          }
          const progress = event === 'progress'
            ? (data?.text || friendlyStreamProgress[data?.stage] || friendlyStreamProgress.processing)
            : friendlyStreamProgress[event];
          if (progress) setMessages((items) => items.map((item) => item.id === `${messageId}-assistant`
            ? { ...item, progress }
            : item));
        },
      });
      const elapsedMs = responseElapsedMs(result);
      setResponse(result);
      if (result.status === 'DRAFT_INFO_REQUIRED') {
        setDraftIntake({
          intent: result.intent?.name || draftIntake?.intent || intentHint,
          demandType: draftIntake?.demandType || undefined,
          missingFields: result.intent?.missing_fields || [],
        });
      } else if (result.status === 'DRAFT_GENERATED' || result.status === 'DRAFT_SAVED' || result.status === 'PUBLISHED') {
        setDraftIntake(null);
      }
      if (result.thread_id) {
        adoptSessionId(result.thread_id);
      }
      await loadConversationList();
      setMessages((items) => items.map((item) => item.id === `${messageId}-assistant` ? { ...item, pending: false, response: result, text: cleanAssistantText(assistantText(result)), elapsedMs } : item));
      await loadMemoryCandidates();
    }
    catch (requestError) {
      if (controller.signal.aborted || requestError?.name === 'AbortError') {
        setResponse(null);
        setError('本次回答已中断，可以继续输入新的问题。');
        setMessages((items) => items.map((item) => item.id === `${messageId}-assistant` ? { ...item, pending: false, error: '本次回答已中断', text: '本次回答已中断，可以继续输入新的问题。' } : item));
        return;
      }
      setResponse(null); setError(requestError.message);
      setMessages((items) => items.map((item) => item.id === `${messageId}-assistant` ? { ...item, pending: false, error: requestError.message, text: requestError.message || '问答任务失败。' } : item));
    }
    finally {
      if (chatAbortControllerRef.current === controller) chatAbortControllerRef.current = null;
      setBusy(false);
    }
  }

  function interruptChat() {
    const controller = chatAbortControllerRef.current;
    if (controller) controller.abort();
  }

  async function submit(event) {
    event.preventDefault();
    const pendingFiles = selectedFiles.length ? selectedFiles : (uploadFile ? [uploadFile] : []);
    const typed = message.trim();
    const typedUploadIntent = pendingFiles.length && /(?:整理|生成|提取|解析).*(需求|方案|成果)/.test(typed)
      ? (typed.includes('需求') ? 'demand' : typed.includes('成果') ? 'achievement' : 'solution')
      : '';
    if (typedUploadIntent) {
      selectedIntentRef.current = typedUploadIntent;
      setDraftIntake({ intent: typedUploadIntent });
      setMessages((items) => [...items, { id: `${Date.now()}-upload-intent-text`, role: 'user', text: typed }]);
      setMessage('');
      setUploadConfirmationPending(false);
      await processSelectedFiles('new', typed);
      return;
    }
    if (pendingFiles.length && /补充(当前|刚才|上一个)?(草稿|材料|文件)/.test(typed)) {
      setMessages((items) => [...items, { id: `${Date.now()}-attachment-choice`, role: 'user', text: typed }]);
      setMessage('');
      setUploadConfirmationPending(false);
      await processSelectedFiles('supplement', '补充当前草稿');
      return;
    }
    if (pendingFiles.length && /(另一个|新的)(需求|方案|成果)|新建(需求|方案|成果)/.test(typed)) {
      setMessages((items) => [...items, { id: `${Date.now()}-attachment-choice`, role: 'user', text: typed }]);
      setMessage('');
      setUploadConfirmationPending(false);
      await processSelectedFiles('new', '新建草稿');
      return;
    }
    const customDemandType = typed.match(/^需求类型\s*[:：]\s*[^；;。]+[；;]\s*具体类型(?:是)?\s*[:：]?\s*([^；;。]+)$/);
    if (draftIntake?.intent === 'demand' && customDemandType?.[1]?.trim()) {
      setMessage('');
      await sendMessage(typed, {
        intent_hint: 'demand',
        intent_confirmation: 'confirmed',
        draft_context: { draft_type: 'demand', demand_type: customDemandType[1].trim() },
      });
      return;
    }
    await sendMessage(message);
  }

  function assistantText(result) {
    if (result?.status === 'FAILED') {
      const detail = result.error || result.result?.error || '';
      const code = result.error_code || result.result?.error_code || '';
      return `本次请求未完成${code ? `（${code}）` : ''}${detail ? `：${detail}` : '，请检查服务状态后重试。'}`;
    }
    if (result?.answer) {
      return cleanAssistantText(result.answer);
    }
    const payload = result?.result || {};
    if (payload.answer) return cleanAssistantText(payload.answer);
    if (Array.isArray(payload.match_results)) return payload.match_results.length
      ? `已完成匹配，找到 ${payload.match_results.length} 条相关结果。`
      : '本次没有找到匹配结果，请换一种描述或扩大知识范围。';
    return result?.intent_reason || '已完成意图识别，当前工具已返回结构化结果。';
  }

  async function decideMemory(memoryId, action) {
    setMemoryBusy(true); setMemoryError('');
    try {
      if (action === 'confirm') await api.confirmMemory(memoryId, identity.user_id);
      else await api.rejectMemory(memoryId, identity.user_id);
      await loadMemoryCandidates();
    } catch (requestError) {
      setMemoryError(requestError.message);
    } finally { setMemoryBusy(false); }
  }

  const hasEditableDraft = (item) => Boolean(
    item
      && item.status !== 'PUBLISHED'
      && item.draft?.status !== 'PUBLISHED'
      && (item.draft || item.drafts?.length),
  );
  const hasCurrentDraft = Boolean(
    hasEditableDraft(response)
      || messages.some((item) => item.role === 'assistant' && hasEditableDraft(item.response)),
  );

  function clearSelectedFiles() {
    setUploadFile(null);
    setSelectedFiles([]);
    setUploadConfirmationPending(false);
    if (uploadInputRef.current) uploadInputRef.current.value = '';
  }

  function appendUserNotice(text, suffix = 'notice') {
    setMessages((items) => [...items, {
      id: `${Date.now()}-${suffix}`,
      role: 'user',
      text,
    }]);
  }

  function removeSelectedFile(target) {
    const files = (selectedFiles.length ? selectedFiles : (uploadFile ? [uploadFile] : []))
      .filter((file) => file !== target);
    setSelectedFiles(files);
    setUploadFile(files[0] || null);
    setUploadConfirmationPending(false);
    if (!files.length && uploadInputRef.current) uploadInputRef.current.value = '';
  }

  function handleFileSelection(event) {
    const incoming = Array.from(event.target.files || []);
    const files = incoming.slice(0, 1);
    setSelectedFiles(files);
    setUploadFile(files[0] || null);
    setUploadConfirmationPending(false);
    setUploadMessage('');
  }

  async function processSelectedFiles(attachmentMode = 'new', actionLabel = '') {
    const files = selectedFiles.length ? selectedFiles : (uploadFile ? [uploadFile] : []);
    if (!files.length || !onUploadAndParse) return;
    const uploadIntent = ['demand', 'solution', 'achievement', 'customer_visit', 'enterprise_profile', 'demand_discovery', 'supply_demand_match'].includes(selectedIntentRef.current)
      ? selectedIntentRef.current
      : (['demand', 'solution', 'achievement', 'customer_visit', 'enterprise_profile', 'demand_discovery', 'supply_demand_match'].includes(intentName(response?.intent))
        ? intentName(response.intent)
        : (['demand', 'solution', 'achievement', 'customer_visit', 'enterprise_profile', 'demand_discovery', 'supply_demand_match'].includes(draftIntake?.intent)
          ? draftIntake.intent
          : ''));
    setError('');
    const parseMessageId = `${Date.now()}-parse-start`;
    const streamedAnswerIds = new Set();
    setMessages((items) => appendMaterialUploadMessages(items, parseMessageId, files[0].name));
    setUploadMessage(`${actionLabel ? `${actionLabel}，` : ''}文件正在上传并解析，请稍候。`);
    try {
      const parseOne = (file) => {
        const tokenMessageIds = {};
        const appendToken = (token, data) => {
          const phase = data?.phase === 'document_summary' ? 'document_summary' : 'answer';
          let messageId = tokenMessageIds[phase];
          if (!messageId) {
            messageId = `${Date.now()}-${phase}-${Math.random()}`;
            tokenMessageIds[phase] = messageId;
            if (phase === 'answer') streamedAnswerIds.add(messageId);
            setMessages((items) => [...items, {
              id: messageId,
              role: 'assistant',
              text: phase === 'document_summary' ? formatDocumentSummary('') : '',
            }]);
          }
          setMessages((items) => items.map((item) => item.id === messageId
            ? { ...item, text: phase === 'document_summary'
              ? formatDocumentSummary(`${item.text || ''}${token || ''}`)
              : `${item.text || ''}${token || ''}` }
            : item));
        };
        return onUploadAndParse(
        file, sessionIdRef.current.trim(), attachmentMode === 'supplement' ? '补充当前草稿' : actionLabel, uploadIntent, attachmentMode,
        uploadIntent === 'supply_demand_match' ? knowledgeType : '',
        {
          onToken: appendToken,
          onEvent: (event, data) => {
            if (event === 'progress' && data?.summary) {
              const messageId = tokenMessageIds.document_summary || `${Date.now()}-document-summary`;
              tokenMessageIds.document_summary = messageId;
              setMessages((items) => items.some((item) => item.id === messageId)
                ? items.map((item) => item.id === messageId ? {
                  ...item,
                  text: formatDocumentSummary(data.summary),
                  elapsedMs: Number.isFinite(Number(data.extraction_elapsed_ms)) ? Number(data.extraction_elapsed_ms) : undefined,
                } : item)
                : [...items, {
                  id: messageId,
                  role: 'assistant',
                  text: formatDocumentSummary(data.summary),
                  elapsedMs: Number.isFinite(Number(data.extraction_elapsed_ms)) ? Number(data.extraction_elapsed_ms) : undefined,
                }]);
            }
            if (event === 'progress' && data?.stage === 'retrieval') {
              setMessages((items) => [...items, {
                id: `${Date.now()}-retrieval-started`, role: 'assistant',
                text: data?.text || '正在根据解析内容检索知识库…',
              }]);
            }
          }
        },
        );
      };
      // 新建草稿的文件并行；补充当前草稿顺序处理，避免同一草稿并发覆盖。
      const results = attachmentMode === 'supplement'
        ? await files.reduce(async (chain, file) => [...(await chain), await parseOne(file)], Promise.resolve([]))
        : await Promise.all(files.map(parseOne));
      const result = results.length === 1 ? results[0] : {
        ...results[results.length - 1],
        answer: results.map((item) => item.answer).filter(Boolean).join('\n') || '文件解析完成，已生成草稿。',
        blocks: results.flatMap((item) => item.blocks || []),
        drafts: results.flatMap((item) => item.drafts || (item.draft ? [item.draft] : [])),
        draft: results[0]?.draft || results[0]?.drafts?.[0] || null,
      };
      const elapsedMs = responseElapsedMs(result, selectedIntentRef.current === 'supply_demand_match');
      if (result.draft_context) matchContextRef.current = result.draft_context;
      if (result.document_summary || result.extracted_query) {
        matchContextRef.current = {
          ...(matchContextRef.current || {}),
          text: result.document_summary || result.extracted_query,
        };
      }
      adoptSessionId(result.thread_id);
      if (selectedIntentRef.current === 'supply_demand_match') {
        setResponse(result);
        setMessages((items) => reconcileAssistantMessage(items, streamedAnswerIds, {
          id: `${Date.now()}-match`, role: 'assistant', response: result,
          text: result.answer || '资料解析完成，正在进行匹配。', elapsedMs,
        }));
        clearSelectedFiles();
        setUploadMessage(result.answer || '资料解析完成，已返回匹配结果。');
        return;
      }
      if (result.draft || result.drafts?.length) {
        const draft = result.draft || result.drafts[0];
        const draftResult = {
          ...result,
          status: result.status || 'DRAFT_GENERATED',
          answer: '草稿已生成，请在下方预览并编辑。',
          intent: result.intent || { name: draft.draft_type, confirmed: true },
          actions: result.actions?.length ? result.actions : [
            { id: 'save_draft', type: 'SAVE_DRAFT', label: '保存', enabled: true },
            { id: 'publish_draft', type: 'PUBLISH_DRAFT', label: '发布', enabled: true },
          ],
          ui_blocks: result.ui_blocks?.length ? result.ui_blocks : [{ type: 'draft_card', data: { draft } }],
        };
        setResponse(draftResult);
        setMessages((items) => reconcileAssistantMessage(items, streamedAnswerIds, {
          id: `${Date.now()}-draft`, role: 'assistant', response: draftResult,
          text: draftResult.answer, elapsedMs,
        }));
        clearSelectedFiles();
        setUploadMessage('解析完成，已生成草稿预览。修改后确认即可直接写入公开知识库。');
        return;
      }
      if (result.status === 'UPLOAD_INTENT_REQUIRED') {
        setResponse(result);
        setMessages((items) => reconcileAssistantMessage(items, streamedAnswerIds, {
          id: `${Date.now()}-material-intent`, role: 'assistant', response: result, text: result.answer, elapsedMs,
        }));
        setUploadMessage(result.answer || '请先选择资料处理方向。');
        return;
      }
      setResponse(result);
      setMessages((items) => reconcileAssistantMessage(items, streamedAnswerIds, {
        id: `${Date.now()}-material-result`,
        role: 'assistant',
        response: result,
        text: assistantText(result),
        elapsedMs,
      }));
      clearSelectedFiles();
    } catch (uploadError) {
      setUploadMessage(`处理失败：${uploadError.message}`);
      const errorTitle = uploadError.stage === 'parse'
        ? '文件解析失败'
        : uploadError.stage === 'processing'
          ? '文件解析已完成，后续处理失败'
          : '文件处理失败';
      setMessages((items) => [...items, {
        id: `${Date.now()}-parse-error`, role: 'assistant',
        text: `${errorTitle}：${uploadError.message || '请稍后重试。'}`,
      }]);
      clearSelectedFiles();
    }
  }

  async function submitUpload() {
    const files = selectedFiles.length ? selectedFiles : (uploadFile ? [uploadFile] : []);
    if (!files.length || !onUploadAndParse) return;
    const currentIntent = String(intentName(response?.intent) || draftIntake?.intent || selectedIntentRef.current || '').toLowerCase();
    const currentDraft = response?.draft || response?.drafts?.[0];
    const isUnfinishedVisit = currentIntent === 'customer_visit'
      && response?.status !== 'PUBLISHED'
      && currentDraft?.draft_type === 'customer_visit';
    if (!currentIntent) {
      setUploadMessage('正在解析文档并自动判断处理方式…');
      await processSelectedFiles('new');
      return;
    }
    await processSelectedFiles((hasCurrentDraft || isUnfinishedVisit) ? 'supplement' : 'new');
  }

  async function handleBusinessAction(action) {
    if (busy) return;
    if (action.type === 'SUBMIT_HUMAN_INPUT') {
      return sendMessage(action.message || '');
    }
    if (action.type === 'MATCH_RESULT_CONFIRM') {
      if (action.value !== 'start_draft') return sendMessage('否');
      const draftType = action.draft_type || 'demand';
      const currentDraft = response?.draft || response?.drafts?.[0] || {};
      selectedIntentRef.current = draftType;
      setDraftIntake({ intent: draftType, ...(draftType === 'demand' ? { demandType: currentDraft.fields?.category } : {}) });
      setExternalFlow(draftType === 'demand' ? 'demand' : 'solution');
      onKnowledgeTypeChange?.(draftType);
      return sendMessage(action.message || '立即进入需求草稿完善流程', {
        ...matchDraftActionPayload(action, currentDraft),
      });
    }
    if (action.type === 'START_DEMAND_DRAFT') {
      selectedIntentRef.current = 'demand';
      setDraftIntake({ intent: 'demand' });
      setExternalFlow('demand');
      onKnowledgeTypeChange?.('demand');
      const currentDraft = response?.draft || response?.drafts?.[0] || {};
      return sendMessage(action.message || '补充需求并重新匹配', {
        intent_hint: 'demand',
        intent_confirmation: 'confirmed',
        draft_context: { ...currentDraft, draft_type: 'demand', match_follow_up: true },
      });
    }
    if (action.type === 'SELECT_UPLOAD_INTENT') {
      selectedIntentRef.current = action.value;
      setDraftIntake({ intent: action.value });
      setMessages((items) => [...items, { id: `${Date.now()}-intent-choice`, role: 'user', text: action.label }]);
      setUploadMessage(`已选择“${action.label}”，正在开始解析。`);
      await processSelectedFiles('new', action.label);
      return;
    }
    if (action.type === 'SELECT_MATCH_INTENT') {
      selectedIntentRef.current = 'supply_demand_match';
      setDraftIntake({ intent: 'supply_demand_match' });
      setMessages((items) => [...items, { id: `${Date.now()}-intent-choice`, role: 'user', text: action.label }]);
      setUploadMessage(`已选择“${action.label}”，正在解析文档并提取摘要。`);
      await processSelectedFiles('new', action.label);
      return;
    }
    if (action.type === 'SUPPLEMENT_CURRENT_DRAFT' || action.type === 'CREATE_NEW_DRAFT') {
      const attachmentMode = action.type === 'SUPPLEMENT_CURRENT_DRAFT' ? 'supplement' : 'new';
      const userText = action.type === 'SUPPLEMENT_CURRENT_DRAFT' ? '补充当前草稿' : action.label;
      setMessages((items) => [...items, { id: `${Date.now()}-attachment-choice`, role: 'user', text: userText }]);
      setUploadConfirmationPending(false);
      await processSelectedFiles(attachmentMode, action.label);
      return;
    }
    if (action.type === 'SKIP_MATCHED_DEMAND_SAVE') {
      setUploadMessage('本次需求暂不保存，匹配结果仍保留在当前会话。');
      return;
    }
    if (action.type === 'SELECT_SUPPLY_TYPE') {
      const draftType = action.value === '科研成果' ? 'achievement' : 'solution';
      selectedIntentRef.current = draftType;
      const nextIntake = { ...(draftIntake || {}), intent: draftType, supplyType: draftType };
      setDraftIntake(nextIntake);
      if (uploadFile && response?.status === 'UPLOAD_INTENT_REQUIRED' && onUploadAndParse) {
        setUploadMessage(`已选择${action.value}，正在解析文件并生成草稿。`);
        try {
          const result = await onUploadAndParse(uploadFile, sessionIdRef.current.trim(), `请根据文件整理${action.value}草稿`, draftType);
          adoptSessionId(result.thread_id);
          setResponse(result);
          setMessages((items) => [...items, { id: `${Date.now()}-draft`, role: 'assistant', response: result, text: result.answer || '文件解析完成，已生成草稿。', elapsedMs: responseElapsedMs(result) }]);
          setUploadFile(null);
          setUploadMessage(result.answer || '文件解析完成，已生成草稿。');
        } catch (error) { setUploadMessage(`处理失败：${error.message}`); }
        return;
      }
      void sendMessage(`资料类型：${action.value}`, {
        intent_hint: draftType,
        intent_confirmation: 'confirmed',
        draft_context: { draft_type: draftType },
      });
      return;
    }
    if (action.type === 'SELECT_DEMAND_TYPE') {
      selectedIntentRef.current = 'demand';
      const nextIntake = { ...(draftIntake || {}), intent: 'demand', demandType: action.value };
      setDraftIntake(nextIntake);
      if (uploadFile && response?.status === 'UPLOAD_INTENT_REQUIRED' && onUploadAndParse) {
        setUploadMessage('已选择需求，正在解析文件并生成草稿。');
        try {
          const result = await onUploadAndParse(uploadFile, sessionIdRef.current.trim(), '请根据文件整理需求草稿', 'demand');
          adoptSessionId(result.thread_id);
          setResponse(result);
          setMessages((items) => [...items, { id: `${Date.now()}-draft`, role: 'assistant', response: result, text: result.answer || '文件解析完成，已生成需求草稿。', elapsedMs: responseElapsedMs(result) }]);
          setUploadFile(null);
          setUploadMessage(result.answer || '文件解析完成，已生成需求草稿。');
        } catch (error) { setUploadMessage(`处理失败：${error.message}`); }
        return;
      }
      if (action.value === '其他') {
        setMessage('需求类型：其他；具体类型是：');
        return;
      }
      void sendMessage(`需求类型：${action.value}`, {
        intent_hint: 'demand',
        intent_confirmation: 'confirmed',
        draft_context: { draft_type: 'demand', demand_type: action.value },
      });
      return;
    }
    if (action.type === 'SELECT_MATCH_DIRECTION') {
      const nextKnowledgeType = action.value === 'demand_to_supply' ? 'solution' : 'demand';
      const directionMessage = action.value === 'demand_to_supply'
        ? '我有需求，想匹配相关的技术方案、科研成果或服务方。'
        : '我有技术方案或科研成果，想匹配相关需求。';
      const currentMatchContext = matchContextRef.current || response?.draft_context || response?.draft || {};
      onKnowledgeTypeChange?.(nextKnowledgeType);
      selectedIntentRef.current = 'supply_demand_match';
      void sendMessage(directionMessage, {
        intent_hint: 'supply_demand_match',
        knowledge_type: nextKnowledgeType,
        draft_context: { ...currentMatchContext, match_direction: action.value },
      });
      return;
    }
    if (action.type === 'SELECT_KNOWLEDGE_TYPE') {
      const labels = {
        enterprise: '企业', demand: '需求', solution: '技术方案',
        achievement: '科研成果', expert: '专家',
      };
      const knowledgeType = action.value;
      const label = labels[knowledgeType] || '相关知识';
      void sendMessage(`查询${label}`, {
        intent_hint: response?.intent?.business_intent || 'knowledge_search',
        intent_confirmation: 'confirmed',
        knowledge_type: knowledgeType,
      });
      return;
    }
    if (action.type === 'CONFIRM_INTERNAL_INTENT' && response?.intent?.internal_flow?.name) {
      void sendMessage('确认按当前方向处理', {
        intent_hint: response.intent.internal_flow.name,
        intent_confirmation: 'confirmed',
      });
      return;
    }
    if (action.type === 'CHANGE_INTERNAL_INTENT') {
      setMessage('我想从企业画像、潜在需求、适配方案、供应商匹配中选择处理方向');
      void sendMessage('我想更换处理方向', { flow_action: 'switch', intent_hint: undefined, intent_confirmation: undefined, draft_context: undefined });
      return;
    }
    if (action.type === 'CONFIRM_INTENT' && response?.intent?.name) {
      const labels = { demand: '需求', solution: '技术方案', achievement: '科研成果', supply_draft: '技术方案 / 科研成果' };
      void sendMessage(`确认整理为${labels[response.intent.name] || '当前类型'}`, {
        intent_hint: response.intent.name,
        intent_confirmation: 'confirmed',
      });
      return;
    }
    if (action.type === 'CHANGE_INTENT') {
      setDraftIntake(null);
      void sendMessage('我想更换整理类型', { flow_action: 'switch', intent_hint: undefined, intent_confirmation: undefined, draft_context: undefined });
      return;
    }
    if (action.type === 'BACK_FLOW') {
      void sendMessage('返回上一步', { flow_action: 'back', intent_hint: undefined, intent_confirmation: undefined, draft_context: undefined });
      return;
    }
    if (action.type === 'SWITCH_FLOW') {
      setDraftIntake(null);
      void sendMessage('我想切换意图', { flow_action: 'switch', intent_hint: undefined, intent_confirmation: undefined, draft_context: undefined });
      return;
    }
    if (action.type === 'UPLOAD_MATERIAL') {
      document.querySelector('.composer-attach input')?.click();
      return;
    }
    if (action.type === 'GENERATE_INQUIRY_DRAFT') {
      setUploadMessage('正在整理已记录的拜访内容…');
      void sendMessage('整理生成客户拜访记录草稿', {
        intent_hint: 'customer_visit',
        intent_confirmation: 'confirmed',
      });
      return;
    }
    if (action.type === 'MATCH_VISIT_SOLUTIONS') {
      void sendMessage('生成适配方案', {
        intent_hint: 'customer_visit',
        intent_confirmation: 'confirmed',
      });
      return;
    }
    if (action.type === 'CONTINUE_CUSTOMER_VISIT') {
      void sendMessage('继续补充拜访内容', {
        intent_hint: 'customer_visit',
        intent_confirmation: 'confirmed',
      });
      return;
    }
    if (action.type === 'START_MATCHING') {
      setUploadMessage('正在进入供需匹配…');
      void sendMessage(action.message || '开始供需匹配', {
        ...(action.request || {}),
        intent_hint: action.request?.intent_hint || 'supply_demand_match',
        intent_confirmation: 'confirmed',
        knowledge_type: action.request?.knowledge_type || action.knowledge_type,
        draft_context: action.request?.draft_context || (action.draft ? {
          draft_id: action.draft_id,
          draft_type: action.draft.draft_type,
          text: action.draft.text || action.draft.summary,
          fields: action.draft.fields || {},
          mysql_fields: action.draft.mysql_fields || {},
          attachments: action.draft.attachments || [],
          source_blocks: action.draft.source_blocks || [],
        } : undefined),
      });
      return;
    }
    if (action.type === 'IMMEDIATE_MATCH') {
      const intent = response?.intent?.business_intent || response?.intent?.name;
      const matchContext = action.draft_context || response?.draft_context || response?.draft;
      void sendMessage(action.label || '立即匹配', {
        intent_hint: intent === 'demand_discovery' ? 'demand_discovery' : 'supply_demand_match',
        intent_confirmation: 'confirmed',
        flow_action: 'immediate_match',
        draft_context: matchContext,
      });
      return;
    }
    if (action.type === 'NEXT_MATCH_BATCH') {
      setUploadMessage('正在换一批匹配结果…');
      return sendMessage(action.message || '换一批匹配结果', {
        intent_hint: action.intent_hint || action.business_intent || 'supply_demand_match',
        intent_confirmation: 'confirmed',
        flow_action: 'next_match_batch',
      });
    }
    if (['SAVE_DRAFT', 'PUBLISH_DRAFT', 'CONFIRM_DRAFT', 'SAVE_MATCHED_DEMAND'].includes(action.type)) {
      if (!action.draft) return;
      if (['CONFIRM_DRAFT', 'PUBLISH_DRAFT'].includes(action.type) && action.draft.draft_type === 'customer_visit') {
        const isPublish = action.type === 'PUBLISH_DRAFT';
        setUploadMessage(isPublish ? '正在保存并发布拜访记录…' : '正在保存拜访记录…');
        api.confirmBusinessRecord({
          user_id: identity.user_id,
          record_type: 'customer_visit',
          confirmed: true,
          confirmed_by: identity.user_id,
          raw_text: action.draft.raw_text || action.draft.fields?.enterprise_portrait || null,
          structured_data: action.draft,
          draft_id: action.draft.draft_id || response?.draft_id || null,
          thread_id: sessionId.trim() || null,
        }).then((saved) => {
          const status = isPublish ? 'PUBLISHED' : 'DRAFT_SAVED';
          const savedDraft = { ...action.draft, status };
          const nextResponse = isPublish ? {
            status: 'CUSTOMER_VISIT_SAVED',
            answer: '拜访记录已保存。是否根据本次记录中的潜在需求，匹配相关方案和供应商？',
            draft: savedDraft,
            business_record: saved.business_record || saved,
            visit_record: saved.visit_record,
            actions: [{
              id: 'match_after_customer_visit',
              type: 'START_MATCHING',
              label: '匹配相关供应商',
              message: '根据本次客户拜访记录中的潜在需求匹配相关方案和供应商',
              knowledge_type: 'solution',
              draft: savedDraft,
              enabled: true,
            }],
          } : { status, actions: [], draft: savedDraft, business_record: saved.business_record || saved, visit_record: saved.visit_record };
          setUploadMessage(`${isPublish ? '拜访记录已发布' : '拜访记录已保存'}：${saved.business_record?.business_record_id || saved.business_record_id || '成功'}`);
          setResponse(nextResponse);
          setMessages((items) => {
            const draftId = savedDraft.draft_id || response?.draft_id;
            const isSavedVisit = (candidate) => candidate?.draft_type === 'customer_visit'
              && (draftId ? candidate.draft_id === draftId || (!candidate.draft_id && candidate.title === savedDraft.title) : candidate.title === savedDraft.title);
            const updated = items.map((item) => {
              const candidates = [item.response?.draft, ...(item.response?.drafts || [])];
              if (!candidates.some(isSavedVisit)) return item;
              const updatedResponse = {
                ...item.response,
                status,
                ...(item.response.draft ? { draft: isSavedVisit(item.response.draft) ? savedDraft : item.response.draft } : {}),
                ...(item.response.drafts ? { drafts: item.response.drafts.map((candidate) => isSavedVisit(candidate) ? savedDraft : candidate) } : {}),
                actions: (item.response.actions || []).filter((candidate) => !['PUBLISH_DRAFT', 'CONFIRM_DRAFT'].includes(candidate.type)),
              };
              return { ...item, response: updatedResponse };
            });
            if (!isPublish) return updated;
            return [...updated, {
              id: `${Date.now()}-visit-match-confirmation`,
              role: 'assistant',
              text: nextResponse.answer,
              response: nextResponse,
            }];
          });
        }).catch((requestError) => setUploadMessage(`拜访记录${isPublish ? '发布' : '保存'}失败：${requestError.message}`));
        return;
      }
      const shouldPublish = !['SAVE_DRAFT', 'SAVE_MATCHED_DEMAND'].includes(action.type);
      setUploadMessage(shouldPublish ? '正在保存并发布草稿…' : '正在保存草稿…');
      const currentDraftIndex = Number.isInteger(action.draft_index) ? action.draft_index : 0;
      const responseDraftId = Array.isArray(response?.draft_ids)
        ? response.draft_ids[currentDraftIndex]
        : response?.draft_id;
      const targetDraftId = action.draft.draft_id
        || (!response?.drafts?.length ? responseDraftId : null);
      const saveOrCreate = async () => {
        const existingDraftId = isPersistedDraftId(targetDraftId) ? targetDraftId : null;
        if (externalFlow === 'solution') {
          const similarity = await api.checkDraftSimilarity({
            user_id: identity.user_id, draft: action.draft, draft_id: existingDraftId,
          });
          if (similarity.decision_required) {
            const first = similarity.matches[0];
            const overwrite = window.confirm(`发现相似草稿“${first.title || first.draft_id}”。确定覆盖旧草稿吗？取消则新建。`);
            if (overwrite) {
              await api.transitionDraft(first.draft_id, {
                user_id: identity.user_id, target_status: 'WITHDRAWN',
                note: '供给侧相似成果选择覆盖旧草稿',
              });
            }
          }
        }
        const saved = existingDraftId
        ? await api.updateDraft(existingDraftId, {
          user_id: identity.user_id,
          thread_id: sessionId.trim() || undefined,
          draft: action.draft,
        })
        : await api.createDraft({
          user_id: identity.user_id,
          thread_id: sessionId.trim() || undefined,
          draft: action.draft,
        });
        if (!shouldPublish) return saved;
        return api.publishDraft(saved.draft_id || targetDraftId, {
          user_id: identity.user_id,
          thread_id: sessionId.trim() || undefined,
          scope_id: 'public',
        });
      };
      return saveOrCreate().then(async (result) => {
        const draftId = result.draft_id || targetDraftId;
        const savedDraft = {
          ...action.draft,
          draft_id: draftId,
          version: result.version ?? action.draft.version,
          status: shouldPublish ? 'PUBLISHED' : 'DRAFT_SAVED',
          attachments: (action.draft.attachments || []).map(({ temporary, expires_at, ...attachment }) => attachment),
        };
        const sourceDrafts = Array.isArray(response?.drafts) && response.drafts.length
          ? response.drafts : (response?.draft ? [response.draft] : []);
        const previousDraftStatuses = Array.isArray(response?.draft_statuses)
          ? response.draft_statuses
          : sourceDrafts.map((item) => item?.status || (response?.status === 'PUBLISHED' ? 'PUBLISHED' : 'DRAFT_GENERATED'));
        const { drafts: nextDrafts, statuses: nextDraftStatuses } = updateDraftPublicationState(
          sourceDrafts,
          previousDraftStatuses,
          targetDraftId,
          currentDraftIndex,
          shouldPublish ? 'PUBLISHED' : 'DRAFT_SAVED',
        );
        const nextDraftIds = nextDrafts.length
          ? nextDrafts.map((item) => item.draft_id || null)
          : undefined;
        const allDraftsPublished = nextDrafts.length > 0 && nextDraftStatuses.every((status) => status === 'PUBLISHED');
        const nextResponse = {
          ...response, draft_id: draftId, draft: savedDraft, draft_index: currentDraftIndex,
          status: nextDrafts.length ? (allDraftsPublished ? 'PUBLISHED' : 'DRAFT_SAVED') : (shouldPublish ? 'PUBLISHED' : 'DRAFT_SAVED'),
          ...(nextDrafts.length ? { drafts: nextDrafts, draft_ids: nextDraftIds, draft_statuses: nextDraftStatuses } : {}),
        };
        setResponse(nextResponse);
        // 同一会话中的初版草稿、匹配结果草稿必须共用最新字段，避免编辑一个仍显示另一个旧版本。
        setMessages((items) => items.map((item) => {
          if (item.role !== 'assistant' || !item.response || !draftId) return item;
          const responseDrafts = Array.isArray(item.response.drafts) && item.response.drafts.length
            ? item.response.drafts : (item.response.draft ? [item.response.draft] : []);
          const hasDraft = item.response.draft_id === draftId
            || responseDrafts.some((candidate) => candidate?.draft_id === draftId);
          if (!hasDraft) return item;
          const previousStatuses = Array.isArray(item.response.draft_statuses)
            ? item.response.draft_statuses
            : responseDrafts.map((candidate) => candidate?.status || 'DRAFT_GENERATED');
          const { drafts: currentDrafts, statuses: updatedStatuses } = updateDraftPublicationState(
            responseDrafts,
            previousStatuses,
            draftId,
            0,
            shouldPublish ? 'PUBLISHED' : 'DRAFT_SAVED',
          );
          const activeDraft = item.response.draft_id === draftId ? savedDraft : (item.response.draft || currentDrafts[0]);
          const debugContext = item.response.debug?.context;
          const savedDraftContext = {
            ...(debugContext?.draft_context || {}),
            draft_id: draftId,
            draft_type: savedDraft.draft_type,
            status: savedDraft.status,
            ...(savedDraft.draft_type === 'demand' && savedDraft.fields?.category
              ? { demand_type: savedDraft.fields.category } : {}),
          };
          const updatedResponse = {
            ...item.response,
            status: updatedStatuses.length
              ? (updatedStatuses.every((status) => status === 'PUBLISHED') ? 'PUBLISHED' : 'DRAFT_SAVED')
              : (shouldPublish ? 'PUBLISHED' : 'DRAFT_SAVED'),
            intent: {
              ...(item.response.intent || {}),
              name: item.response.intent?.name || savedDraft.draft_type,
              operation: shouldPublish ? 'draft_publish' : 'draft_save',
            },
            draft: item.response.draft_id === draftId ? savedDraft : item.response.draft,
            drafts: currentDrafts,
            draft_ids: currentDrafts.map((candidate) => candidate?.draft_id || null),
            draft_statuses: updatedStatuses,
            actions: (item.response.actions || []).map((candidate) => (
              candidate.type === 'EDIT_DRAFT' && (candidate.draft?.draft_id === draftId || item.response.draft_id === draftId)
                ? { ...candidate, draft: savedDraft } : candidate
            )),
            ...(item.response.debug ? { debug: {
              ...item.response.debug,
              context: {
                ...(debugContext || {}),
                draft: activeDraft,
                active_draft: savedDraft,
                draft_context: savedDraftContext,
              },
            } } : {}),
          };
          return { ...item, response: updatedResponse };
        }));
        if (shouldPublish) {
          appendUserNotice(`已保存并发布到知识库：${savedDraft.title || action.draft.title || '当前草稿'}`, 'published');
        }
        setUploadMessage(shouldPublish
          ? `草稿已发布并写入公开知识库：${result.index_result?.indexed_count || 0} 个向量块`
          : `草稿已保存：${draftId}`);
        setDraftRefreshKey((value) => value + 1);
        if (shouldPublish && result.next_step) {
          setMessages((items) => [...items, {
            id: `${draftId}-next-step-${Date.now()}`,
            role: 'assistant',
            response: result.next_step,
            text: result.next_step.answer,
          }]);
        }
      }).catch((requestError) => {
        setUploadMessage(`草稿发布失败：${requestError.message}`);
        throw requestError;
      });
    }
    if (action.type === 'MATCH_DRAFT' || action.type === 'REMATCH_DRAFT') {
      if (!action.draft) return;
      const draftText = Object.entries(action.draft || {}).map(([key, value]) => `${key}：${typeof value === 'object' ? JSON.stringify(value) : value}`).join('\n');
      setUploadMessage('正在保存需求并匹配相关知识…');
      const saveAndMatch = async () => {
        const saved = isPersistedDraftId(response?.draft_id)
          ? await api.updateDraft(response.draft_id, {
            user_id: identity.user_id,
            thread_id: sessionId.trim() || undefined,
            draft: action.draft,
          })
          : await api.createDraft({
            user_id: identity.user_id,
            thread_id: sessionId.trim() || undefined,
            draft: action.draft,
          });
        const draftId = saved.draft_id || response?.draft_id;
        const published = await api.publishDraft(draftId, {
          user_id: identity.user_id,
          thread_id: sessionId.trim() || undefined,
          scope_id: 'public',
        });
        setDraftRefreshKey((value) => value + 1);
        await sendMessage('请根据当前需求匹配相关技术方案和科研成果。', {
          intent_hint: 'supply_demand_match',
          intent_confirmation: 'confirmed',
          draft_context: { ...action.draft, text: draftText, draft_id: draftId },
        });
        return published;
      };
      saveAndMatch().then((published) => {
        setUploadMessage(`需求已保存并发布，正在展示匹配结果（${published.index_result?.indexed_count || 0} 个向量块）。`);
      }).catch((requestError) => {
        setUploadMessage(`保存并匹配失败：${requestError.message}`);
      });
      return;
    }
    // 浏览器测试界面没有微信小程序原生客服控件，因此通过接口展示客服动作结果；
    // 小程序正式接入时应把 CONTACT_WECOM 绑定到 <button open-type="contact">，不要照搬此请求。
    if (action.type === 'CONTACT_WECOM') {
      api.contact({
        user_id: identity.user_id,
        query: action.query || response?.query || message || '当前检索请求',
        document_ids: action.document_ids || (response?.documents || []).map((item) => item.document_id).filter(Boolean),
      }).then((result) => setUploadMessage(result.message)).catch((requestError) => {
        setUploadMessage(`企业微信联系请求失败：${requestError.message}`);
      });
      return;
    }
    console.info('business_action_selected', action);
  }

  function chooseQuickIntent(intent, label) {
    if (busy) return;
    selectedIntentRef.current = intent;
    // 快速入口是进入流程，不把示例描述直接当作检索问题发送。
    void sendMessage(intent === 'supply_demand_match' ? '开始供需匹配' : (label || intent), {
      intent_hint: intent,
      intent_confirmation: 'confirmed',
    });
  }

  function sendExampleQuestion(prompt) {
    if (busy) return;
    void sendMessage(prompt);
  }

  function startExternalFlow(item) {
    if (busy) return;
    if (item.intent === 'demand') {
      selectedIntentRef.current = item.intent;
      setExternalFlow('demand');
      onKnowledgeTypeChange?.('demand');
      clearSelectedFiles();
      setUploadMessage('');
      setError('');
      void sendMessage('我有需求', {
        thread_id: undefined,
        intent_hint: 'demand',
        draft_context: undefined,
        intent_confirmation: 'confirmed',
      });
      return;
    }
    if (item.intent === 'supply_demand_match') {
      selectedIntentRef.current = item.intent;
      setExternalFlow(null);
      setDraftIntake(null);
      onKnowledgeTypeChange?.('');
      clearSelectedFiles();
      setUploadMessage('');
      setError('');
      void sendMessage('开始供需匹配', {
        intent_hint: 'supply_demand_match',
        draft_context: undefined,
        intent_confirmation: 'confirmed',
      });
      return;
    }
    chooseQuickIntent(item.intent, item.label);
  }

  function openConversation(item) {
    if (!item?.thread_id) return;
    const restoredIntent = intentName(item.state?.last_intent) || intentName(item.state?.chat_flow?.last_intent);
    selectedIntentRef.current = restoredIntent;
    setIntentHint(restoredIntent);
    sessionIdRef.current = item.thread_id;
    setSessionId(item.thread_id);
    localStorage.setItem(chatSessionKey(identity.user_id), item.thread_id);
    setMessages([]);
    setResponse(null);
    setError('');
    setConversationMenuOpen(false);
  }

  function beginRenameConversation(event, item) {
    event.stopPropagation();
    setRenamingSessionId(item.thread_id);
    setRenameValue(String(item.title || item.state?.title || item.state?.summary || '未命名会话'));
  }

  function cancelRenameConversation(event) {
    event.stopPropagation();
    setRenamingSessionId('');
    setRenameValue('');
  }

  async function saveConversationRename(event, item) {
    event.preventDefault();
    event.stopPropagation();
    const title = renameValue.trim();
    if (!title || !identity?.user_id) return;
    try {
      setSessionActionId(item.thread_id);
      await api.renameChatSession(item.thread_id, identity.user_id, title);
      setRenamingSessionId('');
      setRenameValue('');
      await loadConversationList();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setSessionActionId('');
    }
  }

  async function removeConversation(event, item) {
    event.stopPropagation();
    if (!identity?.user_id || !window.confirm('删除后将清除该会话的历史消息，确定继续吗？')) return;
    try {
      setSessionActionId(item.thread_id);
      await api.deleteChatSession(item.thread_id, identity.user_id);
      if (item.thread_id === sessionId) createConversation();
      await loadConversationList();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setSessionActionId('');
    }
  }

  function createConversation() {
    localStorage.removeItem(chatSessionKey(identity.user_id));
    sessionIdRef.current = '';
    matchContextRef.current = null;
    setSessionId('');
    setMessage('');
    setIntentHint('');
    selectedIntentRef.current = '';
    setDraftIntake(null);
    clearSelectedFiles();
    setMessages([]);
    setResponse(null);
    setRequestPreview(null);
    setStreamEvents([]);
    setError('');
    setConversationMenuOpen(false);
  }

  function handleVoiceTranscript(text) {
    setMessage((current) => {
      const existing = current.trim();
      return existing ? `${existing}${existing.endsWith('。') ? '' : '。'} ${text}` : text;
    });
    setUploadMessage('语音识别完成，文字已填入输入框。');
  }

  const visibleQuickIntents = isExternalUser
    ? externalQuickIntents
    : quickIntents.filter((item) => item.intent !== 'customer_visit' || ['internal', 'admin'].includes(identity.user_role));

  return <section className={`supervisor-page${isExternalUser ? ' external-supervisor-page' : ''}`} aria-label="科创智能问答">
    <div className="chat-workspace">
      <section className="panel chat-panel">
        <header className="external-chat-header">
          <div className="external-user-summary">
            <span className="external-user-avatar">{(identity.name || identity.user_id || 'U').slice(0, 1).toUpperCase()}</span>
            <div><strong>{identity.name || identity.user_id}</strong><small>{identity.company || '当前企业'} · {identity.user_role === 'admin' ? '管理员' : identity.user_role === 'internal' ? '内部市场人员' : '外部用户'}</small></div>
          </div>
          <div className="conversation-control">
            <button type="button" className="conversation-trigger" aria-expanded={conversationMenuOpen} onClick={() => { setConversationMenuOpen((open) => !open); loadConversationList(); }}>
              <span aria-hidden="true">◷</span> 历史会话
            </button>
            {conversationMenuOpen && <div className="conversation-menu" role="menu">
              <div className="conversation-menu-heading"><strong>历史会话</strong><small>{conversationList.length} 个会话</small></div>
              <div className="conversation-menu-list">
                {!conversationList.length && <p>暂无历史会话</p>}
                {conversationList.map((item) => {
                  const title = String(item.title || item.state?.title || item.state?.summary || '未命名会话');
                  const isRenaming = renamingSessionId === item.thread_id;
                  const isActing = sessionActionId === item.thread_id;
                  return <div className="conversation-item" key={item.thread_id}>
                    {isRenaming ? <form className="conversation-rename-form" onSubmit={(event) => saveConversationRename(event, item)}>
                      <input className="conversation-rename-input" value={renameValue} maxLength={80} autoFocus onChange={(event) => setRenameValue(event.target.value)} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => { if (event.key === 'Escape') cancelRenameConversation(event); }} />
                      <div className="conversation-item-actions"><button type="submit" className="conversation-action primary" disabled={isActing}>保存</button><button type="button" className="conversation-action" onClick={cancelRenameConversation}>取消</button></div>
                    </form> : <>
                      <button type="button" className="conversation-item-main" role="menuitem" onClick={() => openConversation(item)}>
                        <strong>{title}</strong>
                        <small>{item.updated_at ? new Date(item.updated_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</small>
                      </button>
                      <div className="conversation-item-actions"><button type="button" className="conversation-action" onClick={(event) => beginRenameConversation(event, item)} disabled={isActing}>重命名</button><button type="button" className="conversation-action danger" onClick={(event) => removeConversation(event, item)} disabled={isActing}>删除</button></div>
                    </>}
                  </div>;
                })}
              </div>
              <button type="button" className="conversation-new" onClick={createConversation}>＋ 新建会话</button>
            </div>}
          </div>
        </header>
        {/* 首屏显示快捷入口；会话中显示消息，并将结构化结果交给 BusinessBlocks。 */}
        <div ref={chatThreadRef} className={`chat-thread${messages.length === 0 ? ' chat-thread-empty' : ''}`} aria-live="polite">
          {messages.length === 0 ? <>
            <article className="chat-message chat-message-assistant">
              <div className="chat-bubble chat-welcome-bubble"><strong>你好，我是科创服务助手</strong><span>你可以直接提问，也可以先选择要整理的内容类型。</span></div>
            </article>
            <div className="chat-examples">
              <span>快速开始</span>
              <div className="quick-intents-grid">
              {visibleQuickIntents.map((item) => <button
                type="button"
                className="example-chip quick-intent"
                key={item.code || item.intent}
                title={item.prompt}
                onClick={() => isExternalUser
                  ? startExternalFlow(item)
                  : chooseQuickIntent(item.intent, item.label)}
              ><span className="quick-intent-code">{item.code}</span><strong>{item.label}</strong><small>{item.description}</small><i>→</i></button>)}
              </div>
              <span className="chat-example-divider">也可以直接提问</span>
              {examples.map((example) => <button type="button" className="example-chip" key={example} onClick={() => sendExampleQuestion(example)}>{example}</button>)}
            </div>
          </> : messages.map((item) => <article className={`chat-message chat-message-${item.role}`} key={item.id}><span className={`chat-message-label${item.role === 'assistant' ? ' assistant-label' : ''}`}>{item.role === 'assistant' && <span className="assistant-avatar" aria-hidden="true"><i /></span>}<span>{item.role === 'assistant' ? '科创服务助手' : '我'}</span></span><div className={`chat-bubble${item.pending ? ' is-pending' : ''}${item.error ? ' has-error' : ''}`}>{item.pending ? (item.progress || '正在为您处理，请稍候…') : item.text}</div>{item.role === 'assistant' && !item.pending && Number.isFinite(item.elapsedMs) && <small className="chat-message-elapsed">本次耗时：{formatElapsed(item.elapsedMs)}</small>}{item.role === 'assistant' && !item.pending && item.response?.status !== 'FAILED' && <BusinessBlocks response={item.response} identity={selectedIdentity || identity} busy={busy} onAction={handleBusinessAction} onUploadRequest={() => uploadInputRef.current?.click()} />}</article>)}
        </div>
        {/* 输入区统一处理文本发送、附件解析、语音转写和中断当前请求。 */}
        {(selectedFiles.length > 0 || uploadFile) && <div className="chat-attachment-row"><div className={`chat-upload-files chat-upload-files-${(selectedFiles.length ? selectedFiles : [uploadFile]).length}`} role="list" aria-label="待解析附件">{(selectedFiles.length ? selectedFiles : [uploadFile]).map((file) => <div className="chat-upload-file" role="listitem" key={`${file.name}-${file.size}-${file.lastModified}`}><span className="chat-upload-name" title={file.name}>{file.name}</span><button type="button" className="chat-attachment-remove" onClick={() => removeSelectedFile(file)} disabled={uploadBusy} aria-label={`删除附件 ${file.name}`} title="删除附件">×</button></div>)}</div><button type="button" onClick={submitUpload} disabled={uploadBusy}>{uploadBusy ? '处理中…' : '开始解析'}</button></div>}
        <form className="chat-form" onSubmit={submit}>
          <label htmlFor="supervisor-message">输入你的问题或需求</label>
          <div className="composer-row">
            <label className="composer-attach" title="添加材料"><input ref={uploadInputRef} type="file" accept=".pdf,.pptx,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.md,.txt,.wav,.mp3,.m4a,.aac,.ogg,.webm,audio/*" onChange={handleFileSelection} /><span aria-hidden="true">＋</span></label>
            <VoiceInput onTranscript={handleVoiceTranscript} disabled={busy || uploadBusy} />
            <textarea ref={messageInputRef} className="chat-message-input" id="supervisor-message" value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={submitChatOnEnter} placeholder="请输入你的需求" rows="1" />
            <button className={`composer-send${busy ? ' composer-cancel' : ''}`} type={busy ? 'button' : 'submit'} onClick={busy ? interruptChat : undefined} aria-label={busy ? '中断回答' : '发送问题'} title={busy ? '中断回答' : '发送问题'}>{busy ? '中断' : '➤'}</button>
          </div>
          <div className="chat-actions"><span>当前身份：{identity.name || identity.user_id} · {identity.user_role}</span><button type="button" className="secondary" onClick={createConversation} disabled={busy}>新建会话</button></div>
          </form>
        {error && <p className="supervisor-error" role="alert">{error}</p>}
      </section>
      <AgentDebugPanel messages={messages} streamEvents={streamEvents} />
        {/* 身份信息与角色入口；右侧调试面板仅供 Web 联调查看。 */}
        <aside className="chat-sidebar" aria-label="当前身份与可用知识范围">
        <div className="chat-sidebar-panel">
        <section className="chat-sidebar-card identity-sidebar-card">
          <div className="chat-sidebar-heading"><span className="sidebar-icon">◎</span><h2>当前身份</h2></div>
          <div className="identity-sidebar-profile">
            <span className="identity-avatar">{(selectedIdentity?.name || identity.name || selectedIdentity?.user_id || identity.user_id || 'U').slice(0, 1).toUpperCase()}</span>
            <div><strong>{selectedIdentity?.name || identity.name || selectedIdentity?.user_type || identity.user_type || (identity.user_role === 'internal' ? '内部市场人员' : '外部用户')}</strong><small>{selectedIdentity?.company || identity.company || `用户 ID：${selectedIdentity?.user_id || identity.user_id}`}</small></div>
          </div>
          <label className="identity-switcher">切换测试身份
            <select value={selectedIdentity?.user_id || ''} onChange={(event) => onIdentityChange?.(event.target.value)} disabled={identityLoading || identityOptions.length === 0}>
              {identityOptions.length === 0 && <option value="">{identityLoading ? '正在读取用户信息…' : '未读取到用户'}</option>}
              {identityOptions.map((user) => <option value={user.user_id} key={user.user_id}>{identityOptionLabel(user)}</option>)}
            </select>
          </label>
          <form className="identity-id-search" onSubmit={searchAndSwitchUser}>
            <label htmlFor="identity-user-id">按 user_id 切换</label>
            <div><input id="identity-user-id" value={userIdSearch} onChange={(event) => setUserIdSearch(event.target.value)} placeholder="输入 user_id" maxLength={64} /><button type="submit" disabled={!userIdSearch.trim() || identityLoading}>切换</button></div>
          </form>
          <p className="identity-scope">可访问：{allowedScopes}</p>
          {(identityError || userIdSearchError) && <p className="identity-error" role="alert">{userIdSearchError ? `用户信息读取失败：${userIdSearchError}` : `测试身份列表刷新失败，当前身份仍可用：${identityError}`}</p>}
        </section>
        <section className="chat-sidebar-card knowledge-sidebar-card">
          <div className="chat-sidebar-heading"><span className="sidebar-icon">◇</span><h2>{isExternalUser ? '快捷入口' : '可用知识范围'}</h2></div>
          {isExternalUser ? <div className="external-shortcuts">
            <p>快速进入</p>
            {externalQuickIntents.map((item) => <button type="button" key={item.intent} onClick={() => startExternalFlow(item)}><span>{item.code}</span><strong>{item.label}</strong><small>{item.description}</small><i>→</i></button>)}
          </div> : <ul className="knowledge-scope-list">
            <li><span className="scope-icon">▤</span><div><strong>公共知识</strong><small>政策、行业与公开资料</small></div><em>可检索</em></li>
            <li><span className="scope-icon">▱</span><div><strong>企业信息</strong><small>企业能力与基础画像</small></div><em>可检索</em></li>
            <li><span className="scope-icon">▣</span><div><strong>需求与方案</strong><small>已确认需求和技术方案</small></div><em>可检索</em></li>
            <li><span className="scope-icon">◈</span><div><strong>科研成果</strong><small>可公开的成果资料</small></div><em>可检索</em></li>
          </ul>}
          <p className="knowledge-scope-note">
            {isExternalUser ? '外部用户仅访问脱敏后的公开知识。' : '范围会随登录身份、数据状态和权限策略变化。'}
          </p>
        </section>
        </div>
      </aside>
      {showInspector && <aside className="supervisor-inspector">
        <section className="panel"><div className="panel-title"><span>01</span><div><h2>执行状态</h2><p>当前阶段先展示意图路由和工具响应。</p></div></div><div className={`route-status ${response?.status === 'FAILED' ? 'route-failed' : ''}`}><strong>{response ? response.status : '等待提问'}</strong><span>{response?.selected_tool || 'Supervisor 尚未调用工具'}</span></div>{response?.error_code && <p className="supervisor-error">{response.error_code}：{response.error}</p>}</section>
        <section className="panel"><div className="panel-title"><span>02</span><div><h2>任务计划</h2><p>展示 Supervisor 当前识别出的意图和执行步骤。</p></div></div><div className="plan-summary"><strong>{response?.intent_operation || response?.intent?.operation || '等待识别意图'}</strong>{response?.plan?.steps?.length ? <ol>{response.plan.steps.map((step, index) => <li key={step.id || index}>{step.description || step.tool || '待执行步骤'}</li>)}</ol> : <span>提交问题后显示任务步骤。</span>}</div></section>
        <section className="panel"><div className="panel-title"><span>03</span><div><h2>证据 / 响应</h2><p>{response?.status === 'FAILED' ? '请求未完成，以下显示可读错误信息。' : response ? '结构化结果，便于审计和后续生成答案。' : '提交问题后显示结果。'}</p></div></div><ResponseSummary response={response} /></section>
        <section className="panel"><div className="panel-title"><span>05</span><div><h2>事件流</h2><p>管理和调试用途，显示本次请求收到的全部阶段事件。</p></div></div>{streamEvents.length === 0 ? <p className="empty-state">暂无事件</p> : <div className="json-viewer">{streamEvents.map((item, index) => <details key={`${item.at}-${index}`}><summary>{item.event}</summary><pre>{formatJson(item.data)}</pre></details>)}</div>}</section>
        <section className="panel memory-candidates" aria-label="待确认记忆"><div className="panel-title"><span>04</span><div><h2>待确认记忆</h2><p>仅显示当前用户候选，不会自动生效。</p></div><button type="button" className="secondary" onClick={loadMemoryCandidates} disabled={memoryBusy}>刷新</button></div>{memoryError && <p className="supervisor-error" role="alert">{memoryError}</p>}{memoryCandidates.length === 0 ? <p className="empty-state">暂无待确认记忆</p> : <div className="memory-candidate-list">{memoryCandidates.map((item) => <article className="memory-candidate" key={item.memory_id}><div><strong>{item.memory_type}</strong><p>{item.content}</p></div><div className="memory-candidate-actions"><button type="button" onClick={() => decideMemory(item.memory_id, 'confirm')} disabled={memoryBusy}>确认</button><button type="button" className="secondary" onClick={() => decideMemory(item.memory_id, 'reject')} disabled={memoryBusy}>拒绝</button></div></article>)}</div>}</section>
        <DraftList userId={identity.user_id} refreshKey={draftRefreshKey} />
        <details className="panel inspector-details"><summary>查看请求上下文</summary><pre className="json-viewer">{requestPreview ? formatJson(requestPreview) : '暂无请求'}</pre></details>
        <details className="panel inspector-details"><summary>调试标识</summary><div className="supervisor-fields"><label>Request ID<input value={requestId} onChange={(event) => setRequestId(event.target.value)} /></label><label>Session ID<input value={sessionId} onChange={(event) => setSessionId(event.target.value)} /></label><label>Task ID<input value={taskId} onChange={(event) => setTaskId(event.target.value)} /></label><label>Trace ID<input value={traceId} onChange={(event) => setTraceId(event.target.value)} placeholder="留空自动生成" /></label></div></details>
      </aside>}
    </div>
  </section>;
}
