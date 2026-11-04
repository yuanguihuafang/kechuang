import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api.js';
import { PageJump } from './PageJump.jsx';
import { errorMessage } from '../../api/request.js';
import { AuthenticatedDownload } from '../documents/AuthenticatedDownload.jsx';

function dateText(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; }
function roleText(value) { return value === 'admin' ? '管理员' : value === 'internal' ? '内部人员' : value === 'external' ? '外部人员' : value || '未知'; }
const FIELD_LABELS = { draft_type: '草稿类型', title: '标题', company: '公司名称', enterprise_name: '企业名称', user_name: '专家姓名', description: '描述', analysis: '分析', analyse_contect: '需求分析', contect: '需求描述', contact: '联系人', link_name: '联系人', phone: '联系电话', link_tel: '联系电话', category: '分类', product_type: '产品类型', authentication_image: '营业执照照片', avatar: '用户头像', logo: 'Logo', image: '图片', first_img_url: '封面图片', attachments: '附件' };
const IMAGE_FIELD_KEYS = new Set(['authentication_image', 'avatar', 'logo', 'image', 'first_img_url']);
const SESSION_PAGE_SIZE = 20;
const FLOW_STEP_LABELS = {
  IDLE: '待处理', INTENT_CONFIRM: '确认意图', INFO_REQUIRED: '补充信息', DRAFT_EDITING: '完善草稿',
  DRAFT_GENERATING: '生成草稿', RECORDING: '记录中', MATCHING: '匹配中', QUERY_REQUIRED: '补充检索内容',
  WAITING_PUBLISH: '等待发布', PUBLISHING: '发布中', INDEXED: '已写入知识库', PUBLISH_FAILED: '发布失败',
  MATCH_CONFIRM: '确认匹配方向', EXECUTING: '执行中', COMPLETED: '已完成', INTERRUPTED: '已中断',
  ROLLED_BACK: '已回退', SWITCHING: '切换流程', ERROR: '处理异常',
};
function imageUrl(key, value) {
  if (!IMAGE_FIELD_KEYS.has(key) || typeof value !== 'string') return '';
  const text = value.trim();
  const markdown = text.match(/^\[[^\]]*\]\((https?:\/\/[^)]+)\)$/i)?.[1] || text;
  return /^https?:\/\/[^\s"'<>]+$/i.test(markdown) ? markdown : '';
}
function draftLines(draft) {
  const lines = Object.entries(draft || {}).filter(([key, value]) => key !== 'attachments' && value != null && String(value).trim()).map(([key, value]) => ({ key, label: FIELD_LABELS[key] || key, value: typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value) }));
  const attachments = (draft?.attachments || []).filter((item) => item && typeof item === 'object');
  if (attachments.length) {
    lines.push({
      key: 'attachments',
      label: '关联文档',
      value: attachments.map((item) => String(item.file_name || item.document_id || '未命名附件')).join('、'),
    });
  }
  return lines;
}
function currentStepText(session) {
  const state = session?.state || {};
  const step = state.stage || state.flow_state || state.active?.stage || state.active_flow?.stage || '';
  return FLOW_STEP_LABELS[step] || step || '—';
}

export function SessionManager() {
  const [sessions, setSessions] = useState([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [detail, setDetail] = useState(null);
  const [detailMode, setDetailMode] = useState('conversation');
  const refresh = useCallback(async () => {
    setLoading(true); setError('');
    try {
      const result = await api.adminConversations({ page, page_size: SESSION_PAGE_SIZE });
      setSessions(Array.isArray(result?.items) ? result.items : []);
      setTotal(Number(result?.total || 0));
      setTotalPages(Number(result?.total_pages || 0));
      if (page > Math.max(1, Number(result?.total_pages || 0))) setPage(Math.max(1, Number(result?.total_pages || 0)));
    }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setLoading(false); }
  }, [page]);
  useEffect(() => { void refresh(); }, [refresh]);
  async function remove(session) {
    if (!window.confirm(`确定删除会话“${session.title || session.session_id}”？\n会话消息、草稿（包括已发布）、附件，以及不再被其他活动草稿引用的 MySQL、Milvus、MinIO 数据都会删除。此操作不可恢复。`)) return;
    setBusy(session.session_id); setError('');
    try { await api.adminDeleteConversation(session.session_id); await refresh(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(''); }
  }
  async function openDetail(session, mode) {
    setBusy(`detail:${session.session_id}`); setError('');
    try { setDetailMode(mode); setDetail(await api.adminConversation(session.session_id)); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(''); }
  }
  return <section className="task-center" aria-labelledby="session-manager-title">
    <div className="task-center-head"><div><h1 id="session-manager-title">会话管理</h1><p className="overline">POSTGRESQL / USER SESSIONS</p><p>查看全部用户会话。删除会同步清理会话关联的草稿、附件、MySQL 业务记录和 Milvus 向量。</p></div><button type="button" className="secondary" onClick={() => void refresh()} disabled={loading || Boolean(busy)}>{loading ? '刷新中…' : '刷新会话'}</button></div>
    {error && <div className="task-error" role="alert">{error}</div>}
    {loading && !sessions.length ? <div className="task-empty" role="status">正在读取用户会话…</div> : null}
    {!loading && !sessions.length ? <div className="task-empty" role="status">暂无用户会话。</div> : null}
    {!!sessions.length && <>
      <div className="task-list" role="list">{sessions.map((session) => <article className="task-card" role="listitem" key={session.session_id}><div className="task-card-main"><div className="task-card-title"><strong>{session.title || '未命名会话'}</strong><span className="task-status task-status-succeeded">{roleText(session.user_role)}</span></div><p className="task-card-kind">用户：{session.user_name || session.user_id || '—'}{session.company_name ? ` · ${session.company_name}` : ''}（ID：{session.user_id || '—'}）</p><div className="task-card-meta"><span>当前步骤：{currentStepText(session)}</span><span>消息 {session.message_count ?? 0} 条</span><span>草稿 {session.draft_count ?? 0} 条</span><span>匹配记录 {session.match_count ?? 0} 条</span><span>更新时间：{dateText(session.updated_at)}</span></div></div><div className="task-card-actions"><button type="button" className="text-button" onClick={() => void openDetail(session, 'conversation')} disabled={Boolean(busy)}>查看会话</button><button type="button" className="text-button" onClick={() => void openDetail(session, 'draft')} disabled={Boolean(busy)}>查看草稿</button><button type="button" className="text-button" onClick={() => void openDetail(session, 'match')} disabled={Boolean(busy) || !session.match_count}>查看匹配结果</button><button type="button" className="text-button task-delete-button" onClick={() => void remove(session)} disabled={Boolean(busy)}>删除会话</button></div></article>)}</div>
      {totalPages > 1 && <nav className="task-pagination" aria-label="会话列表分页"><span>第 {page} / {totalPages} 页，共 {total} 条</span><div><button type="button" className="secondary" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={page <= 1 || loading}>上一页</button><PageJump page={page} totalPages={totalPages} onJump={setPage} disabled={loading} label="会话列表" /><button type="button" className="secondary" onClick={() => setPage((value) => Math.min(totalPages, value + 1))} disabled={page >= totalPages || loading}>下一页</button></div></nav>}
    </>}
      {detail && <div className="draft-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setDetail(null); }}><section className="draft-modal admin-session-detail-modal" role="dialog" aria-modal="true" aria-labelledby="admin-session-detail-title"><header className="draft-modal-head"><div><span>{detailMode === 'draft' ? 'USER DRAFT' : detailMode === 'match' ? 'MATCH RESULTS' : 'USER SESSION'}</span><h2 id="admin-session-detail-title">{detail.session?.title || '未命名会话'}</h2><small>用户：{detail.session?.user_name || detail.session?.user_id || '—'}{detail.session?.company_name ? ` · ${detail.session.company_name}` : ''}（ID：{detail.session?.user_id || '—'}） · {roleText(detail.session?.user_role)} · 当前步骤：{currentStepText(detail.session)}</small></div><button type="button" className="draft-modal-close" onClick={() => setDetail(null)} aria-label="关闭">×</button></header><div className="draft-modal-body">{detailMode === 'conversation' ? <><h3>会话记录（{detail.messages?.length || 0}）</h3>{detail.messages?.length ? <div className="admin-session-messages">{detail.messages.map((message) => <article key={message.message_id}><strong>{message.role === 'user' ? '用户' : message.role === 'assistant' ? '助手' : '系统'}</strong><p>{message.content || '—'}</p><small>{dateText(message.created_at)}</small></article>)}</div> : <p>暂无会话消息。</p>}</> : detailMode === 'match' ? <><h3>匹配结果（{(detail.match_results || []).reduce((total, batch) => total + (batch.items?.length || 0), 0)} 条）</h3>{detail.match_results?.length ? detail.match_results.map((batch, index) => <details key={batch.result_id || index} open><summary>第 {index + 1} 次匹配 · {dateText(batch.created_at)}</summary><p><strong>检索内容：</strong>{batch.query || '—'}</p>{batch.items?.length ? <div className="admin-draft-fields">{batch.items.map((item, itemIndex) => <article key={item.id || item.mysql_id || itemIndex}><h4>匹配结果 {itemIndex + 1}：{item.title || item.name || item.requirement_name || '未命名结果'}</h4>{draftLines(item).map(({ key, label, value }) => <p key={key}><strong>{label}：</strong>{value}</p>)}</article>)}</div> : <p>本次没有返回具体匹配结果。</p>}</details>) : <p>暂无匹配结果。</p>}</> : <><h3>关联草稿（{detail.drafts?.length || 0}）</h3>{detail.drafts?.length ? detail.drafts.map((item) => <details key={item.draft_id} open><summary>{item.draft_title || item.draft_id}</summary><div className="admin-draft-fields">{draftLines(item.draft).map(({ key, label, value }) => { const url = imageUrl(key, value); return <p className={url ? 'admin-draft-image-field' : undefined} key={key}><strong>{label}：</strong>{url ? <a href={url} target="_blank" rel="noreferrer"><img src={url} alt={label} loading="lazy" /></a> : value}</p>; })}</div></details>) : <p>暂无关联草稿。</p>}<h3>附件（{detail.attachments?.length || 0}）</h3>{detail.attachments?.length ? <ul>{detail.attachments.map((item) => <li key={item.document_id}><span>{item.file_name}</span><AuthenticatedDownload url={item.file_url ? api.perceptionAssetUrl(item.file_url) : ''} filename={item.file_name} label="下载附件" className="text-button" /></li>)}</ul> : <p>暂无附件。</p>}</>}</div><footer className="draft-modal-foot"><button type="button" className="secondary" onClick={() => setDetail(null)}>关闭</button></footer></section></div>}
  </section>;
}

export const TaskCenter = SessionManager;
