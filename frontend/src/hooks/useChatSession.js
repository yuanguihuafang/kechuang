import { useEffect, useState } from 'react';
import { api } from '../api.js';

const CHAT_SESSION_KEY = 'kechuang-chat-session-id';

function sessionKey(userId) {
  return `${CHAT_SESSION_KEY}:${userId || 'anonymous'}`;
}

function storedSessionId(userId) {
  const value = localStorage.getItem(sessionKey(userId)) || '';
  return value.startsWith('session-chat-') ? '' : value;
}

export function useChatSession({ identity, isExternalUser, assistantText }) {
  const [sessionId, setSessionId] = useState(() => storedSessionId(identity?.user_id));
  const [messages, setMessages] = useState([]);
  const [conversationList, setConversationList] = useState([]);
  const [conversationMenuOpen, setConversationMenuOpen] = useState(false);
  const [sessionActionId, setSessionActionId] = useState('');
  const [renamingSessionId, setRenamingSessionId] = useState('');
  const [renameValue, setRenameValue] = useState('');

  async function loadConversationList() {
    if (!identity?.user_id) return;
    try {
      const result = await api.myConversations(identity.user_id);
      setConversationList(Array.isArray(result?.sessions) ? result.sessions : []);
    } catch { setConversationList([]); }
  }

  useEffect(() => { void loadConversationList(); }, [identity?.user_id, isExternalUser]);

  useEffect(() => {
    if (!sessionId.trim() || !identity?.user_id) return undefined;
    localStorage.setItem(sessionKey(identity.user_id), sessionId.trim());
    let cancelled = false;
    api.chatHistory(sessionId.trim(), identity.user_id, 20).then((history) => {
      if (cancelled || !Array.isArray(history) || history.length === 0) return;
      setMessages(history.map((item) => ({
        id: item.message_id,
        role: item.role,
        text: item.role === 'assistant' && item.response?.status === 'FAILED'
          ? '当前检索服务暂不可用，请检查服务状态后重试。'
          : item.role === 'assistant' && item.response ? assistantText(item.response)
            : typeof item.content === 'string' ? item.content : '',
        response: item.role === 'assistant' && item.response && typeof item.response === 'object' ? item.response : null,
      })));
    }).catch(() => {});
    return () => { cancelled = true; };
  // 会话读取只随用户或会话变化；assistantText 是纯展示函数，不参与请求条件。
  // ponytail: 不引入 useCallback，只避免无意义的历史会话重复请求。
  }, [identity?.user_id, sessionId]);

  useEffect(() => {
    if (!identity?.user_id) return;
    setSessionId(storedSessionId(identity.user_id));
    setMessages([]);
  }, [identity?.user_id]);

  function openConversation(item) {
    if (!item?.thread_id) return;
    setSessionId(item.thread_id);
    localStorage.setItem(sessionKey(identity.user_id), item.thread_id);
    setMessages([]);
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

  async function saveConversationRename(event, item, onError) {
    event.preventDefault();
    event.stopPropagation();
    const title = renameValue.trim();
    if (!title || !identity?.user_id) return;
    try {
      setSessionActionId(item.thread_id);
      await api.renameChatSession(item.thread_id, identity.user_id, title);
      setRenamingSessionId(''); setRenameValue('');
      await loadConversationList();
    } catch (error) { onError?.(error); }
    finally { setSessionActionId(''); }
  }

  async function removeConversation(event, item, onError, onCurrent) {
    event.stopPropagation();
    if (!identity?.user_id || !window.confirm('删除后将清除该会话的历史消息，确定继续吗？')) return;
    try {
      setSessionActionId(item.thread_id);
      await api.deleteChatSession(item.thread_id, identity.user_id);
      if (item.thread_id === sessionId) onCurrent?.();
      await loadConversationList();
    } catch (error) { onError?.(error); }
    finally { setSessionActionId(''); }
  }

  function createConversation() {
    localStorage.removeItem(sessionKey(identity?.user_id));
    setSessionId(''); setMessages([]); setConversationMenuOpen(false);
  }

  return {
    sessionId, setSessionId, messages, setMessages,
    conversationList, conversationMenuOpen, setConversationMenuOpen,
    sessionActionId, renamingSessionId, renameValue, setRenameValue,
    loadConversationList, openConversation, beginRenameConversation,
    cancelRenameConversation, saveConversationRename, removeConversation, createConversation,
  };
}
