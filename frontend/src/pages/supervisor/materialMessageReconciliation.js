export function appendMaterialUploadMessages(messages, messageId, fileName) {
  return [
    ...messages,
    { id: `${messageId}-user`, role: 'user', text: `我上传了一份资料：${fileName}。` },
    { id: messageId, role: 'assistant', text: '正在解析文档，请稍候。' },
  ];
}

export function reconcileAssistantMessage(
  messages,
  streamedMessageIds,
  finalMessage,
) {
  let matched = false;
  const reconciled = messages.map((message) => {
    if (!streamedMessageIds.has(message.id)) return message;
    matched = true;
    return { ...message, ...finalMessage, id: message.id };
  });
  return matched ? reconciled : [...messages, finalMessage];
}
