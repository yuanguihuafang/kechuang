import { useRef, useState } from 'react';

export function useDocumentTasks({ knowledgeType, sessionId, message, setMessage, uploadBusy, onUploadAndParse, setResponse, setMessages }) {
  const uploadInputRef = useRef(null);
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadMessage, setUploadMessage] = useState('');

  async function submitUpload() {
    if (!uploadFile || !onUploadAndParse) return;
    setUploadMessage('正在上传并解析…');
    try {
      const result = await onUploadAndParse(uploadFile, knowledgeType, sessionId.trim(), message.trim());
      setUploadMessage(`材料解析完成，已识别 ${result.blocks?.length || 0} 个内容块，正在生成草稿预览…`);
      if (!result.draft) throw new Error('材料解析完成，但没有生成可编辑草稿');
      setResponse(result);
      setMessages((items) => [...items, {
        id: `${Date.now()}-draft`, role: 'assistant', response: result,
        text: result.answer || '已根据上传材料生成草稿，请在当前消息中查看并修改。',
        elapsedMs: result.processing_elapsed_ms,
      }]);
      setUploadMessage('解析完成，已生成草稿预览。请在聊天卡片中修改后确认。');
      setUploadFile(null);
      return result;
    } catch (error) { setUploadMessage(`处理失败：${error.message}`); return null; }
  }

  return { uploadInputRef, uploadFile, setUploadFile, uploadMessage, setUploadMessage, submitUpload, uploadBusy };
}
