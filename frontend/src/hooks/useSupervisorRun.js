import { useRef, useState } from 'react';
import { api } from '../api.js';

export function useSupervisorRun({ identity, sessionId, setSessionId, draftIntake, intentHint, uploadFile, assistantText, loadConversationList, loadMemoryCandidates, setMessages }) {
  const abortRef = useRef(null);
  const [requestPreview, setRequestPreview] = useState(null);
  const [requestId, setRequestId] = useState('req-chat-001');
  const [taskId, setTaskId] = useState('task-chat-001');
  const [traceId, setTraceId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [responseState, setResponse] = useState(null);
  const [recoveryNote, setRecoveryNote] = useState('');
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [recoveryMessage, setRecoveryMessage] = useState('');

  async function sendMessage(text, overrides = {}) {
    const normalizedText = text.trim();
    if (!normalizedText) { setError('请输入你的问题或需求。'); return; }
    if (abortRef.current) return;
    const activeDraft = responseState?.draft && ['DRAFT_GENERATED', 'DRAFT_SAVED'].includes(responseState.status) ? responseState.draft : null;
    const controller = new AbortController();
    const payload = {
      message: normalizedText, user_role: identity.user_role, user_id: identity.user_id,
      user_type: identity.user_type || undefined, company: identity.company || undefined,
      contact_name: identity.name || identity.contact_name || undefined,
      contact_phone: identity.phone || identity.linkphone || identity.telphone || undefined,
      knowledge_type: uploadFile ? (overrides.knowledge_type || undefined) : undefined,
      thread_id: sessionId.trim() || undefined,
      intent_hint: activeDraft?.draft_type || intentHint || draftIntake?.intent || undefined,
      intent_confirmation: activeDraft || draftIntake ? 'confirmed' : undefined,
      draft_context: activeDraft ? { draft_type: activeDraft.draft_type, draft_id: responseState.draft_id, parsed_draft: activeDraft, text: normalizedText }
        : draftIntake ? { draft_type: draftIntake.intent, demand_type: draftIntake.demandType } : undefined,
      ...overrides,
    };
    abortRef.current = controller; setRequestPreview(payload); setError(''); setBusy(true);
    const messageId = `${Date.now()}-${Math.random()}`;
    setMessages((items) => [...items, { id: `${messageId}-user`, role: 'user', text: normalizedText }, { id: `${messageId}-assistant`, role: 'assistant', pending: true }]);
    try {
      const result = await api.chatStream(payload, {
        signal: controller.signal,
        onStart: ({ thread_id: startedSessionId }) => {
          if (startedSessionId) {
            setSessionId(startedSessionId);
            localStorage.setItem(`kechuang-chat-session-id:${identity.user_id}`, startedSessionId);
          }
        },
        onToken: (token) => {
          setMessages((items) => items.map((item) => item.id === `${messageId}-assistant`
            ? { ...item, pending: false, text: `${item.text || ''}${token}` }
            : item));
        },
        onEvent: (event, data) => {
          if (event !== 'progress') return;
          setMessages((items) => items.map((item) => item.id === `${messageId}-assistant` && item.pending
            ? { ...item, progress: data?.text || '正在处理，请稍候…' }
            : item));
        },
      });
      const elapsedMs = Number.isFinite(Number(result?.processing_elapsed_ms))
        ? Number(result.processing_elapsed_ms) : undefined;
      setResponse(result);
      if (result.thread_id) { setSessionId(result.thread_id); localStorage.setItem(`kechuang-chat-session-id:${identity.user_id}`, result.thread_id); }
      await loadConversationList?.();
      setMessages((items) => items.map((item) => item.id === `${messageId}-assistant` ? { ...item, pending: false, response: result, text: assistantText(result), elapsedMs } : item));
      await loadMemoryCandidates?.();
      return result;
    } catch (requestError) {
      const aborted = controller.signal.aborted || requestError?.name === 'AbortError';
      const textValue = aborted ? '本次回答已中断，可以继续输入新的问题。' : (requestError.message || '问答任务失败。');
      setResponse(null); setError(aborted ? '本次回答已中断，可以继续输入新的问题。' : requestError.message);
      setMessages((items) => items.map((item) => item.id === `${messageId}-assistant` ? { ...item, pending: false, error: requestError.message, text: textValue } : item));
      return null;
    } finally { if (abortRef.current === controller) abortRef.current = null; setBusy(false); }
  }

  async function recoverTask() {
    const recoveryTaskId = responseState?.task_id;
    if (!recoveryTaskId) return;
    setRecoveryBusy(true); setRecoveryMessage('正在提交人工恢复…');
    try {
      const result = await api.recoverSupervisorTask(recoveryTaskId, { operator_id: identity.user_id, decision_note: recoveryNote.trim() || '前端人工确认后恢复' });
      setResponse(result); setRecoveryMessage(result.status === 'SUCCESS' ? '恢复执行成功，任务已完成。' : '恢复后仍未完成，请查看新的处理结果。');
      setMessages((items) => items.map((item) => item.role === 'assistant' && !item.pending ? { ...item, response: result, text: assistantText(result) } : item));
    } catch (errorValue) { setRecoveryMessage(`恢复失败：${errorValue.message}`); }
    finally { setRecoveryBusy(false); }
  }

  return { abortRef, requestPreview, requestId, setRequestId, taskId, setTaskId, traceId, setTraceId, busy, error, setError, responseState, setResponse, sendMessage, interruptChat: () => abortRef.current?.abort(), recoveryNote, setRecoveryNote, recoveryBusy, recoveryMessage, recoverTask };
}
