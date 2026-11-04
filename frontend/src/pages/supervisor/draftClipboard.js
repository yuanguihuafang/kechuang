import { displayDraftField } from '../../draftFieldLabels.js';

export function formatDraftForClipboard(draft, fields = []) {
  if (!draft) return '';

  const title = String(draft.title || '').trim();
  const summary = String(
    draft.requirement_summary || draft.technical_summary || draft.achievement_summary || '',
  ).trim();
  const fieldLines = fields
    .map(([key, value]) => {
      const text = String(value ?? '').trim();
      return text ? `${displayDraftField(key, draft.draft_type)}：${text}` : '';
    })
    .filter(Boolean);

  return [title, summary, fieldLines.join('\n')].filter(Boolean).join('\n\n');
}

export async function copyDraftText(text, {
  clipboard = globalThis.navigator?.clipboard,
  documentRef = globalThis.document,
} = {}) {
  const value = String(text || '').trim();
  if (!value) throw new Error('草稿暂无可复制内容。');

  let clipboardError;
  if (typeof clipboard?.writeText === 'function') {
    try {
      await clipboard.writeText(value);
      return;
    } catch (error) {
      clipboardError = error;
    }
  }

  if (typeof documentRef?.execCommand !== 'function' || !documentRef.body) {
    throw clipboardError || new Error('当前浏览器不支持复制草稿内容。');
  }

  const textarea = documentRef.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  textarea.style.pointerEvents = 'none';
  documentRef.body.appendChild(textarea);
  let copied = false;
  try {
    textarea.select();
    copied = documentRef.execCommand('copy');
  } catch (error) {
    clipboardError ||= error;
  } finally {
    textarea.remove();
  }

  if (!copied) throw clipboardError || new Error('浏览器未允许复制草稿内容。');
}
