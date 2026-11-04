import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { displayDraftField } from '../draftFieldLabels.js';

function draftText(draft) {
  if (!draft) return '';
  return draft.requirement_summary || draft.technical_summary || draft.achievement_summary || '';
}

function draftLabel(type) {
  return { demand: '需求', solution: '技术方案', achievement: '科研成果' }[type] || type || '未分类';
}

export function DraftList({ userId, userRole = 'internal_market', refreshKey = 0 }) {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  async function load() {
    if (!userId) return;
    setError('');
    try {
      const drafts = await api.listDrafts(userId);
      setItems(drafts);
      setSelected((current) => {
        if (!current) return drafts[0] || null;
        return drafts.find((item) => item.draft_id === current.draft_id) || drafts[0] || null;
      });
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  useEffect(() => { void load(); }, [userId, refreshKey]);

  function updateField(key, value) {
    if (selected?.status === 'PUBLISHED') return;
    setSelected((current) => current ? { ...current, draft: { ...current.draft, [key]: value } } : current);
  }

  async function save() {
    if (!selected || selected.status === 'PUBLISHED') return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await api.updateDraft(selected.draft_id, {
        user_id: userId,
        draft: selected.draft,
        status: selected.status || 'CONFIRMED',
      });
      setItems((current) => current.map((item) => item.draft_id === result.draft_id ? result : item));
      setSelected(result);
      setNotice('草稿已保存。');
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function transition(targetStatus) {
    if (!selected) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await api.transitionDraft(selected.draft_id, {
        user_id: userId,
        target_status: targetStatus,
        operator_id: userId,
        operator_role: userRole,
      });
      setItems((current) => current.map((item) => item.draft_id === result.draft_id ? result : item));
      setSelected(result);
      setNotice(`草稿已${targetStatus === 'PENDING_REVIEW' ? '提交审核' : targetStatus === 'PUBLISHED' ? '发布' : '撤回'}。`);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function publish() {
    if (!selected) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await api.publishDraft(selected.draft_id, {
        user_id: userId,
        operator_id: userId,
        operator_role: userRole,
        scope_id: selected.draft?.scope_id || 'public',
      });
      const published = result.draft;
      setItems((current) => current.map((item) => item.draft_id === published.draft_id ? published : item));
      setSelected(published);
      setNotice(`已直接写入公开知识库，共 ${result.index_result?.indexed_count || 0} 个向量块。`);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  return <section className="panel draft-list-panel" aria-label="已保存草稿">
    <div className="panel-title">
      <span>06</span>
      <div><h2>已保存草稿</h2><p>当前测试用户的草稿，可继续修改；暂不代表正式发布。</p></div>
      <button type="button" className="secondary" onClick={() => void load()} disabled={busy}>刷新</button>
    </div>
    {error && <p className="supervisor-error" role="alert">{error}</p>}
    {items.length === 0 ? <p className="empty-state">暂无已保存草稿</p> : <div className="draft-list-layout">
      <div className="draft-list-items" role="list" aria-label="草稿列表">
        {items.map((item) => <button type="button" role="listitem" className={`draft-list-item${selected?.draft_id === item.draft_id ? ' is-selected' : ''}`} key={item.draft_id} onClick={() => { setSelected(item); setNotice(''); }}>
          <strong>{item.draft?.title || '未命名草稿'}</strong>
          <span>{draftLabel(item.draft_type)} · {item.status}</span>
        </button>)}
      </div>
      {selected && <div className="draft-editor">
        <label htmlFor="saved-draft-title">标题<input id="saved-draft-title" disabled={selected.status === 'PUBLISHED'} value={selected.draft?.title || ''} onChange={(event) => updateField('title', event.target.value)} /></label>
        <label htmlFor="saved-draft-summary">内容摘要<textarea id="saved-draft-summary" rows="5" value={draftText(selected.draft)} onChange={(event) => {
          const key = selected.draft_type === 'demand' ? 'requirement_summary' : selected.draft_type === 'solution' ? 'technical_summary' : 'achievement_summary';
          updateField(key, event.target.value);
        }} disabled={selected.status === 'PUBLISHED'} /></label>
        {(selected.draft?.flow_definition?.fields || Object.keys(selected.draft?.fields || {}).map((key) => ({ key }))).map((field) => <label key={field.key}>{displayDraftField(field)}<input disabled={selected.status === 'PUBLISHED'} value={selected.draft?.fields?.[field.key] || ''} onChange={(event) => updateField('fields', { ...selected.draft.fields, [field.key]: event.target.value })} /></label>)}
        <div className="draft-editor-actions">
          {selected.status !== 'PUBLISHED' && <button type="button" onClick={() => void save()} disabled={busy}>{busy ? '处理中…' : '保存修改'}</button>}
          {(selected.status === 'CONFIRMED' || selected.status === 'WITHDRAWN') && <button type="button" onClick={() => void publish()} disabled={busy}>确认并建立公开索引</button>}
          {selected.status === 'PENDING_REVIEW' && <button type="button" onClick={() => void publish()} disabled={busy}>建立公开索引</button>}
          {selected.status === 'PUBLISHED' && <button type="button" className="secondary" onClick={() => void transition('WITHDRAWN')} disabled={busy}>撤回发布</button>}
        </div>
      </div>}
    </div>}
    {notice && <p className="success-message" role="status">{notice}</p>}
  </section>;
}
