function stringifyDraft(draft) {
  if (typeof draft === 'string') return draft;
  if (!draft || typeof draft !== 'object') return '';

  const sections = [
    draft.title && `标题：${draft.title}`,
    draft.summary && `摘要：${draft.summary}`,
    draft.content && (typeof draft.content === 'string' ? draft.content : JSON.stringify(draft.content, null, 2)),
  ].filter(Boolean);
  return sections.join('\n\n') || JSON.stringify(draft, null, 2);
}

/**
 * 会话历史中的 response 可能是完整 Supervisor 响应对象，不能直接作为 React 子节点。
 * 统一转换成可展示文本，同时保留原始 response 给证据/草稿卡片使用。
 */
export function chatContentText(content) {
  if (typeof content === 'string') return content;
  if (content === null || content === undefined) return '';
  if (typeof content !== 'object') return String(content);

  if (typeof content.answer === 'string' && content.answer.trim()) return content.answer;
  if (content.draft) return stringifyDraft(content.draft);
  if (typeof content.error === 'string' && content.error.trim()) return content.error;
  if (typeof content.message === 'string' && content.message.trim()) return content.message;
  if (content.result && typeof content.result === 'object') {
    const resultText = chatContentText(content.result);
    if (resultText) return resultText;
  }
  if (Array.isArray(content.evidences)) {
    return content.evidences.length
      ? `已完成检索，找到 ${content.evidences.length} 条相关证据。`
      : '本次检索没有找到匹配证据。';
  }
  return JSON.stringify(content, null, 2);
}

export function chatMessageText(message) {
  if (!message) return '';
  if (message.role === 'user') return chatContentText(message.content);
  return chatContentText(message.response || message.content);
}
