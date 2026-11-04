import { useState } from 'react';
import { chatMessageText } from '../../utils/chatContent.js';
import { MineruInlinePreview } from '../documents/MineruInlinePreview.jsx';
import { displayDraftField } from '../../draftFieldLabels.js';
import { api } from '../../api.js';
import { download } from '../../api/request.js';

const REASONS = [
  ['NEEDS_REVISION', '内容需要修改'],
  ['OUTDATED', '内容已过期'],
  ['DUPLICATE', '内容重复'],
  ['PROJECT_ENDED', '项目已结束'],
  ['NO_LONGER_PUBLIC', '不希望继续公开'],
  ['UPLOAD_ERROR', '上传错误'],
  ['OTHER', '其他'],
];

const PAGE_SIZE = 5;
const IMAGE_URL_PATTERN = /https?:\/\/[^\s<>"'，。；、）)]+?\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s<>"'，。；、）)]*)?/gi;

const statusLabels = {
  PUBLISHED: '可检索',
  UNPUBLISHED: '已下架',
  WITHDRAWN: '已下架',
  RECYCLED: '回收站',
  INDEXED: '已建立索引',
  WAITING_REVIEW: '待确认',
  PARSING: '解析中',
  FAILED: '处理失败',
};

function formatTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
}

function draftAttachments(record) {
  const draft = record?.draft || {};
  const state = record?.state || {};
  return [...(Array.isArray(record?.attachments) ? record.attachments : []), ...(Array.isArray(draft.attachments) ? draft.attachments : []), ...(Array.isArray(state.attachments) ? state.attachments : [])]
    .filter((item) => item && typeof item === 'object')
    .filter((item, index, list) => list.findIndex((candidate) => (
      (candidate.document_id && candidate.document_id === item.document_id)
      || (candidate.file_url && candidate.file_url === item.file_url)
      || (candidate.storage_key && candidate.storage_key === item.storage_key)
    )) === index);
}

function sessionIdOf(record) {
  return record?.session_id || record?.thread_id || record?.state?.session_id || record?.state?.thread_id || null;
}

export function matchAttachments(item, response) {
  if (Array.isArray(item.attachments)) return item.attachments;
  if (Array.isArray(item.documents)) return item.documents;
  const documentId = item.document_id || item.source?.document_id;
  const sourceId = item.source_id || item.source?.source_id;
  return (Array.isArray(response.documents) ? response.documents : []).filter(document => (
    (documentId && document.document_id === documentId)
    || (sourceId && document.source_id === sourceId)
  ));
}

function isVisitDraft(record) {
  const draft = record?.draft || record || {};
  return String(record?.draft_type || draft.draft_type || '').trim() === 'customer_visit';
}

function publicationStatus(document) {
  return document.active_version?.publication_status || document.publication_status || document.status || '—';
}

function titleForConversation(item) {
  const title = item.title || item.state?.title || item.state?.summary || item.state?.last_question;
  if (title) return title;

  // PostgreSQL 中保留真实 session_id 用于恢复；界面统一显示，避免暴露历史命名前缀。
  const sessionId = String(item.thread_id || '');
  const diagnose = sessionId.match(/^diagnose-(\d{8})(\d{6})$/);
  if (diagnose) {
    const [, date, time] = diagnose;
    return `会话 · ${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)} ${time.slice(0, 2)}:${time.slice(2, 4)}`;
  }
  return `会话 · ${formatTime(item.updated_at)}`;
}

function recordText(value) {
  if (value === null || value === undefined || value === '') return '暂无记录';
  if (Array.isArray(value)) return value.map((item) => recordText(item)).join('、');
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}：${recordText(item)}`).join('；');
  return String(value);
}

function matchHistoryTitle(record) {
  const query = String(record?.query || '').replace(/\s+/g, ' ').trim();
  if (!query) return '供需匹配';

  const labeled = query.match(/(?:需求名称|项目名称|标题|需求类型|企业名称)\s*[:：]\s*([^，,。；;\n]+?)(?=\s+user\s*[:：]|\s+内容描述\s*[:：]|$)/i);
  const title = labeled?.[1]?.trim();
  if (title) return title.length > 32 ? `${title.slice(0, 32)}…` : title;

  const compact = query.replace(/\buser\s*:\s*/gi, '').replace(/请立即进行方案匹配[。！!]?/g, '').trim();
  return compact.length > 32 ? `${compact.slice(0, 32)}…` : compact || '供需匹配';
}

function extractImageUrls(text) {
  return [...String(text || '').matchAll(IMAGE_URL_PATTERN)].map(([url]) => url).filter((url, index, list) => list.indexOf(url) === index);
}

function resolveMatchImages(text, item, extraImageUrls = []) {
  const imageUrls = [...new Set([
    ...(Array.isArray(item?.image_urls) ? item.image_urls : []),
    ...(Array.isArray(extraImageUrls) ? extraImageUrls : []),
    ...extractImageUrls(text),
  ].filter(Boolean))];
  let imageIndex = 0;
  let resolvedText = String(text || '').replace(
    /!\[([^\]]*)\]\(([^)\s]+)(?:\s+['"][^)]*['"])?\)/g,
    (markup, alt, source) => {
      if (!/^(?:https?:\/\/|\.?\/)?images\//i.test(source) || !imageUrls[imageIndex]) return markup;
      const url = api.perceptionAssetUrl(imageUrls[imageIndex++]);
      return `![${alt}](${url})`;
    },
  );
  const missingImages = [];
  for (const source of imageUrls) {
    const url = api.perceptionAssetUrl(source);
    if (resolvedText.includes(`](${source})`) || resolvedText.includes(`](${url})`) || resolvedText.includes(`src="${source}"`) || resolvedText.includes(`src="${url}"`)) continue;
    if (resolvedText.includes(source)) {
      resolvedText = resolvedText.split(source).join(`![图片](${url})`);
    } else {
      missingImages.push(`![图片](${url})`);
    }
  }
  if (missingImages.length) resolvedText += `\n\n${missingImages.join('\n')}`;
  return { text: resolvedText, images: imageUrls.map((url) => ({ url })) };
}

function renderMatchDetail(detail, item) {
  if (!detail) return null;
  const draftChunks = Array.isArray(detail.draft_chunks) ? detail.draft_chunks : [];
  const sourceChunks = Array.isArray(detail.source_chunks) ? detail.source_chunks : [];
  const showSourceContext = ['agent_doc', 'admin_doc'].includes(item?.chunk_role);
  const imageUrls = [
    ...(Array.isArray(detail.image_urls) ? detail.image_urls : []),
    ...draftChunks.flatMap((chunk) => chunk.image_urls || []),
    ...sourceChunks.flatMap((chunk) => chunk.image_urls || []),
  ];
  const draftText = detail.draft || draftChunks.map((chunk) => chunk.text || '').filter(Boolean).join('\n\n');
  const sourceText = detail.cleaned_source_text || sourceChunks.map((chunk) => chunk.text || '').filter(Boolean).join('\n\n');
  const rawText = detail.text || detail.details || item.detail || item.summary || '';
  const organizedText = detail.formatted_text ? resolveMatchImages(detail.formatted_text, item, imageUrls).text : '';
  const organizedSections = organizedText.split(/(?=^#{1,3}\s+)/m).map((section) => {
    const heading = section.match(/^#{1,3}\s+(.+?)(?:\r?\n|$)/);
    return {
      title: heading?.[1]?.trim(),
      text: (heading ? section.slice(heading[0].length) : section).trim(),
    };
  }).filter((section) => section.title || section.text);
  const visibleSourceText = showSourceContext ? String(sourceText || '').trim() : '';
  const hasOriginal = Boolean(draftText || visibleSourceText || (!draftText && !visibleSourceText && rawText));
  return <>
    {organizedSections.length > 0
      ? <div className="recommendation-detail-organized">{organizedSections.map((section, index) => <section key={`${section.title || 'content'}-${index}`}>
        {section.title && <h4>{section.title}</h4>}
        {section.text && <MineruInlinePreview text={section.text} images={imageUrls.map((url) => ({ url }))} />}
      </section>)}</div>
      : <p className="recommendation-detail-text">暂未生成分段整理内容，完整资料可展开查看。</p>}
    {hasOriginal && <details className="recommendation-detail-source">
      <summary>查看完整草稿与原文</summary>
      {draftText && <section><h4>草稿原文</h4><MineruInlinePreview text={draftText} images={imageUrls.map((url) => ({ url }))} /></section>}
      {visibleSourceText && <section><h4>解析原文上下文</h4><MineruInlinePreview text={visibleSourceText} images={imageUrls.map((url) => ({ url }))} /></section>}
      {!draftText && !visibleSourceText && rawText && <section><h4>检索原文</h4><MineruInlinePreview text={rawText} images={imageUrls.map((url) => ({ url }))} /></section>}
    </details>}
  </>;
}

function Pagination({ page, total, onChange }) {
  const pageCount = Math.ceil(total / PAGE_SIZE);
  if (pageCount <= 1) return null;
  return <nav className="personal-pagination" aria-label="分页">
    <span>第 {page} / {pageCount} 页 · 共 {total} 条</span>
    <div>
      <button type="button" className="secondary compact" onClick={() => onChange(page - 1)} disabled={page <= 1}>上一页</button>
      <button type="button" className="secondary compact" onClick={() => onChange(page + 1)} disabled={page >= pageCount}>下一页</button>
    </div>
  </nav>;
}

/** 个人中心：展示资料、文档、草稿、历史会话、匹配记录和拜访记录；数据操作由页面回调提供。 */
export function PersonalCenter({
  identity,
  profile,
  documents,
  conversations,
  drafts,
  visitRecords = [],
  matchResults = [],
  notice,
  loading,
  busy,
  conversationMessages,
  conversationLoading,
  selectedConversationId,
  documentDetail,
  documentDetailLoading,
  onRefresh,
  onUnpublish,
  onRepublish,
  onOpenDocument,
  onCloseDocument,
  onOpenConversation,
  draftDetail,
  onOpenDraft,
  onCloseDraft,
  onDeleteDraft,
  onUnpublishDraft,
  onRepublishDraft,
  onDeleteDraftAttachment,
  onDeleteVisitRecord,
}) {
  const [matchPage, setMatchPage] = useState(1);
  const [activityPage, setActivityPage] = useState(1);
  const [documentPage, setDocumentPage] = useState(1);
  const [standaloneDraftPage, setStandaloneDraftPage] = useState(1);
  const [expandedMatchRecord, setExpandedMatchRecord] = useState(null);
  const [expandedMatchItems, setExpandedMatchItems] = useState({});
  const [matchDetails, setMatchDetails] = useState({});
  const [matchDetailLoading, setMatchDetailLoading] = useState({});
  const [matchDetailErrors, setMatchDetailErrors] = useState({});
  const activeDocuments = documents.filter((item) => publicationStatus(item) === 'PUBLISHED').length;
  const unpublishedDocuments = documents.filter((item) => publicationStatus(item) === 'UNPUBLISHED').length;
  const uniqueDrafts = drafts.filter((draft, index, list) => list.findIndex((candidate) => (
    candidate.draft_id === draft.draft_id
  )) === index);
  const activeDrafts = uniqueDrafts.filter((item) => !['PUBLISHED', 'WITHDRAWN'].includes(item.status)).length;
  const currentUserId = profile?.user_id || identity.user_id || '—';
  const isInnerUser = profile?.is_inner_user ?? identity.user_role === 'internal';
  const userTypeLabel = String(profile?.user_type ?? '') === '1' ? '企业人员' : (profile?.user_type || '—');
  const linkedDraftIds = new Set(documents.flatMap((document) => uniqueDrafts.filter((draft) => (
    draft.source_document_id === document.document_id
      || draftAttachments(draft).some((attachment) => attachment.document_id === document.document_id)
  )).map((draft) => draft.draft_id)));
  const draftsForDocument = (document) => uniqueDrafts.filter((draft) => {
    const linked = draft.source_document_id === document.document_id
      || draftAttachments(draft).some((attachment) => attachment.document_id === document.document_id);
    return linked;
  });
  const visitRecordIds = new Set(visitRecords.flatMap((record) => [
    record?.record_id, record?.draft_id, record?.mysql_record_id, record?.business_record_id,
  ].filter(Boolean).map(String)));
  const visitSessionIds = new Set([
    ...visitRecords.map(sessionIdOf),
    ...uniqueDrafts.filter(isVisitDraft).map(sessionIdOf),
  ].filter(Boolean).map(String));
  const standaloneDrafts = uniqueDrafts.filter((draft) => !linkedDraftIds.has(draft.draft_id) && !isVisitDraft(draft));
  const activity = conversations.filter((conversation) => !visitSessionIds.has(String(sessionIdOf(conversation) || ''))).map((conversation) => {
    const sessionId = sessionIdOf(conversation);
    const sessionDrafts = uniqueDrafts.filter((draft) => sessionId && sessionIdOf(draft) === sessionId);
    const attachments = [...draftAttachments(conversation), ...sessionDrafts.flatMap((draft) => draftAttachments(draft))]
      .filter((item, index, list) => list.findIndex((candidate) => (
        (candidate.document_id && candidate.document_id === item.document_id)
        || (candidate.file_url && candidate.file_url === item.file_url)
        || (candidate.storage_key && candidate.storage_key === item.storage_key)
        || (candidate.file_name && candidate.file_name === item.file_name)
      )) === index);
    const documentIds = new Set(attachments.map((item) => item.document_id).filter(Boolean));
    const sessionDocuments = documents.filter((document) => documentIds.has(document.document_id) || sessionIdOf(document) === sessionId);
    return { ...conversation, sessionDrafts, attachments, sessionDocuments };
  });
  const activityDocumentIds = new Set(activity.flatMap((item) => item.sessionDocuments.map((document) => document.document_id)));
  const activityDraftIds = new Set(activity.flatMap((item) => item.sessionDrafts.map((draft) => draft.draft_id)));
  const visitDocumentIds = new Set([
    ...documents.filter((document) => visitSessionIds.has(String(sessionIdOf(document) || ''))).map((document) => document.document_id),
    ...uniqueDrafts.filter(isVisitDraft).flatMap((draft) => draftAttachments(draft)).map((attachment) => attachment.document_id),
  ].filter(Boolean));
  const unlinkedDocuments = documents.filter((document) => !activityDocumentIds.has(document.document_id) && !visitDocumentIds.has(document.document_id));
  const unlinkedActivityDrafts = standaloneDrafts.filter((draft) => !activityDraftIds.has(draft.draft_id));
  const matchHistory = matchResults.map((record) => {
    const response = record.result || {};
    const items = (response.ui_blocks || []).filter((block) => block?.type === 'recommendation_list').flatMap((block) => block.data?.items || []);
    return { ...record, response, items: items.length ? items : (response.match_results || []) };
  }).filter((record) => record.items.length > 0
    && !visitRecordIds.has(String(record.draft_id || record.response?.draft_id || ''))
    && !visitSessionIds.has(String(record.session_id || '')));
  const pageSlice = (items, page) => items.slice((Math.min(page, Math.max(1, Math.ceil(items.length / PAGE_SIZE))) - 1) * PAGE_SIZE, Math.min(page, Math.max(1, Math.ceil(items.length / PAGE_SIZE))) * PAGE_SIZE);
  const visibleMatches = pageSlice(matchHistory, matchPage);
  const visibleActivity = pageSlice(activity, activityPage);
  const visibleDocuments = pageSlice(unlinkedDocuments, documentPage);
  const visibleStandaloneDrafts = pageSlice(unlinkedActivityDrafts, standaloneDraftPage);

  async function viewMatchDetail(item, itemKey) {
    if (matchDetails[itemKey]) {
      setExpandedMatchItems((current) => ({ ...current, [itemKey]: !current[itemKey] }));
      return;
    }
    setExpandedMatchItems((current) => ({ ...current, [itemKey]: true }));
    setMatchDetailLoading((current) => ({ ...current, [itemKey]: true }));
    setMatchDetailErrors((current) => ({ ...current, [itemKey]: '' }));
    if (!item?.chunk_role || !Number.isInteger(item.chunk_index) || !identity?.user_id) {
      setMatchDetailErrors((current) => ({ ...current, [itemKey]: '该匹配结果缺少二次检索定位信息。' }));
      setMatchDetailLoading((current) => ({ ...current, [itemKey]: false }));
      return;
    }
    try {
      const detail = await api.retrievalChunkDetail({
        user_id: identity.user_id,
        mysql_id: item.mysql_id || null,
        source_id: item.source_id || null,
        document_id: item.document_id || item.source?.document_id || null,
        chunk_role: item.chunk_role,
        chunk_index: item.chunk_index,
        knowledge_type: item.knowledge_type || null,
      });
      setMatchDetails((current) => ({ ...current, [itemKey]: detail }));
    } catch (error) {
      setMatchDetailErrors((current) => ({ ...current, [itemKey]: error.message || '详情加载失败' }));
    } finally {
      setMatchDetailLoading((current) => ({ ...current, [itemKey]: false }));
    }
  }

  return <section className="personal-center" aria-labelledby="personal-center-title">
    <header className="personal-heading">
      <div>
        <p className="overline">PERSONAL CENTER</p>
        <h1 id="personal-center-title">个人中心</h1>
        <p>统一查看个人资料、历史对话、已提交材料与待处理草稿。数量和分页仅统计当前已加载记录。</p>
      </div>
      <div className="personal-heading-actions">
        <button className="secondary compact" type="button" onClick={onRefresh} disabled={loading}>{loading ? '读取中…' : '刷新数据'}</button>
      </div>
    </header>
    {notice && <div className="personal-notice" role="status">{notice}</div>}

    <section className="personal-profile" aria-label="用户基本信息">
      <div><span>用户 ID</span><strong>{currentUserId}</strong></div>
      <div><span>身份</span><strong>{isInnerUser ? '内部人员' : '企业人员'}</strong></div>
      <div><span>用户类型</span><strong>{userTypeLabel}</strong></div>
      <div><span>所属公司</span><strong>{profile?.company || '—'}</strong></div>
    </section>

    <section className="personal-stats" aria-label="个人资料统计">
      <article><span>可检索资料</span><strong>{activeDocuments}</strong><small>当前用户可检索</small></article>
      <article><span>已下架资料</span><strong>{unpublishedDocuments}</strong><small>停止检索，保留处置记录</small></article>
      <article><span>待处理草稿</span><strong>{activeDrafts}</strong><small>由对话生成，可继续完善</small></article>
      <article><span>历史会话</span><strong>{conversations.length}</strong><small>可查看本人最近对话</small></article>
    </section>

    {isInnerUser && <section className="panel personal-visits" aria-labelledby="personal-visits-title">
      <div className="section-head">
        <div><p className="overline">CUSTOMER VISITS</p><h2 id="personal-visits-title">拜访记录</h2><p>按次记录客户拜访内容并形成客户拜访记录。</p></div>
        <span className="personal-readonly">本人记录 · {visitRecords.length} 次</span>
      </div>
      {loading ? <p className="table-empty">正在读取拜访记录…</p> : !visitRecords.length ? <p className="table-empty">暂无拜访记录。完成并确认一次客户拜访后，会显示在这里。</p> : <div className="personal-visit-list">
        {visitRecords.map((record, index) => {
          const data = record?.draft || record?.structured_data || {};
          const fields = Object.entries(data).filter(([key]) => !['draft_type', 'attachments', 'demand_matches', 'solution_evidences'].includes(key));
          const attachments = Array.isArray(record.attachments) ? record.attachments : (Array.isArray(data.attachments) ? data.attachments : []);
          const matches = Array.isArray(record.match_results) ? record.match_results : (data.demand_matches || data.solution_evidences || []);
          const threadId = record.thread_id || record.session_id || data.chat_session_id;
          return <article className="personal-visit-card" key={record.record_id || record.business_record_id || index}>
            <header><div><span className="overline">CUSTOMER VISIT · {index + 1}</span><h3>{data.enterprise_name || '未填写拜访企业'}</h3></div><div className="personal-card-head-actions"><small>{formatTime(record.updated_at || record.created_at)}</small>{threadId && <button type="button" className="secondary compact" onClick={() => onOpenConversation?.(threadId)}>查看会话</button>}<button type="button" className="text-button danger-text" onClick={() => onDeleteVisitRecord?.(record)} disabled={busy}>删除</button></div></header>
            <dl className="personal-detail-meta"><div><dt>记录 ID</dt><dd>{record.record_id || data.record_id || '—'}</dd></div><div><dt>MySQL 记录</dt><dd>{record.mysql_record_id || data.mysql_record_id || record.business_record_id || '—'}</dd></div><div><dt>关联草稿</dt><dd>{record.draft_id || data.draft_id || '—'}</dd></div><div><dt>来源会话</dt><dd>{record.thread_id || record.session_id || data.chat_session_id || '—'}</dd></div></dl>
            <details open><summary>草稿字段（{fields.length}）</summary><div className="personal-visit-fields">{fields.map(([key, value]) => <div key={key}><strong>{displayDraftField(key)}</strong><p>{recordText(value)}</p></div>)}</div></details>
            <details><summary>附件（{attachments.length}）</summary>{!attachments.length ? <p className="table-empty">无长期保存附件。</p> : <ul className="personal-draft-attachments">{attachments.map((item, attachmentIndex) => <li key={item.document_id || item.file_url || item.storage_key || attachmentIndex}><span>{attachmentDisplayName(item) || `附件 ${attachmentIndex + 1}`}</span><AttachmentDownload attachment={item} identity={identity} label="下载附件" /></li>)}</ul>}</details>
            <details><summary>匹配结果（{matches.length}）</summary>{!matches.length ? <p className="table-empty">暂无关联匹配结果。</p> : <div className="personal-visit-fields">{matches.map((item, matchIndex) => <div key={item.record_id || item.source_locator || matchIndex}><strong>{item.title || item.demand || `匹配结果 ${matchIndex + 1}`}</strong><p>{recordText(item.adapt_solution || item.summary || item.text)}</p>{(item.source_locator || item.document_id) && <small>来源：{item.source_locator || item.document_id}</small>}</div>)}</div>}</details>
          </article>;
        })}
      </div>}
    </section>}

    <section className="panel personal-visits" aria-labelledby="personal-matches-title">
      <div className="section-head"><div><p className="overline">MATCH HISTORY</p><h2 id="personal-matches-title">匹配结果</h2><p>按次保存供需匹配结果，点击查看后再展开具体结果。</p></div><span className="personal-readonly">共 {matchHistory.length} 次</span></div>
      {loading ? <p className="table-empty">正在读取匹配结果…</p> : !matchHistory.length ? <p className="table-empty">暂无已保存的匹配结果。</p> : <>
        <div className="personal-visit-list">{visibleMatches.map((record, index) => {
          const { response, items } = record;
          const recordKey = record.result_id || `${record.created_at}-${index}`;
          const expanded = expandedMatchRecord === recordKey;
              return <article className="personal-visit-card" key={recordKey}><header><div><span className="overline">MATCH · {(Math.min(matchPage, Math.max(1, Math.ceil(matchHistory.length / PAGE_SIZE))) - 1) * PAGE_SIZE + index + 1}</span><h3 title={record.query || '供需匹配'}>{matchHistoryTitle(record)}</h3></div><div className="personal-card-head-actions"><small>{formatTime(record.created_at)}</small><button type="button" className="secondary compact" onClick={() => setExpandedMatchRecord(expanded ? null : recordKey)}>{expanded ? '收起' : '查看'}</button></div></header>
            {expanded && <div className="personal-match-items">{items.map((item, itemIndex) => {
              const source = item.source || item.evidence || {};
              const documents = matchAttachments(item, response);
              const itemKey = `${recordKey}-${item.record_id || item.document_id || itemIndex}`;
              const itemExpanded = Boolean(expandedMatchItems[itemKey]);
              const detail = matchDetails[itemKey];
              const detailLoading = Boolean(matchDetailLoading[itemKey]);
              const detailError = matchDetailErrors[itemKey];
              const previewText = recordText(item.summary || item.text || item.adapt_solution);
              const previewContent = resolveMatchImages(previewText, item, extractImageUrls(item.details));
              return <article className="personal-match-item" key={itemKey}><div className="personal-match-item-head"><strong>{item.title || item.demand || `匹配项 ${itemIndex + 1}`}</strong><button type="button" className="secondary compact" onClick={() => viewMatchDetail(item, itemKey)} disabled={detailLoading} aria-busy={detailLoading}>{detailLoading ? '检索中…' : itemExpanded ? '收起详情' : '查看详情'}</button></div>{itemExpanded && <div className="personal-match-item-body">{detailLoading && <p className="recommendation-detail-text">正在二次检索完整详情…</p>}{detailError && <p className="recommendation-detail-text">{detailError}</p>}{!detailLoading && !detailError && detail && renderMatchDetail(detail, item)}{!detailLoading && !detailError && !detail && <p className="personal-match-item-preview">暂无详情内容</p>}<small>首次召回来源：{source.source_locator || item.source_locator || item.document_id || '—'}</small>{documents.length > 0 && <ul className="personal-draft-attachments">{documents.map((document, documentIndex) => <li key={document.document_id || documentIndex}><span>{document.source_name || document.file_name || document.document_id}</span>{item.knowledge_type !== 'demand' && source.knowledge_type !== 'demand' && document.can_download && <AttachmentDownload attachment={document} identity={identity} label="查看附件" />}</li>)}</ul>}</div>}{!itemExpanded && <div className="personal-match-item-preview"><MineruInlinePreview text={previewContent.text || '暂无可显示内容'} images={previewContent.images} /></div>}</article>;
            })}</div>}
          </article>;
        })}</div>
        <Pagination page={matchPage} total={matchHistory.length} onChange={setMatchPage} />
      </>}
    </section>

    <section className="panel personal-activity">
        <div className="section-head">
          <div><p className="overline">MY ACTIVITY</p><h2>我的资料与对话</h2><p>按对话查看最近消息、生成的草稿和上传的附件。</p></div>
          <span className="personal-readonly">本人资料</span>
        </div>
        <p className="personal-help">下架会立即停止该资料参与检索；不删除原始文件和历史记录，如需彻底删除请联系管理员。</p>
        {loading ? <p className="table-empty">正在读取资料和对话…</p> : !activity.length && !unlinkedDocuments.length && !unlinkedActivityDrafts.length ? <p className="table-empty">暂无资料或对话记录。可前往智能问答上传材料或描述需求。</p> : <>
          {!!activity.length && <><div className="personal-activity-list">{visibleActivity.map((item) => <article className={`personal-conversation-card ${selectedConversationId === item.thread_id ? 'is-selected' : ''}`} key={item.thread_id}>
            <button className={`personal-session ${selectedConversationId === item.thread_id ? 'active' : ''}`} type="button" onClick={() => onOpenConversation(item.thread_id)}>
              <span className="personal-session-title" title={item.thread_id}>{titleForConversation(item)}</span>
              {!item.sessionDrafts.length && !item.attachments.length && !item.sessionDocuments.length && <span className="personal-session-empty-state">这条对话暂未生成草稿或上传附件。</span>}
              <small>{formatTime(item.updated_at)}</small>
            </button>
            <div className={`personal-conversation-resources${item.sessionDrafts.length && item.attachments.length ? ' has-draft-attachments' : ''}`}>
              {!!item.sessionDrafts.length && <div><span className="personal-resource-title">草稿</span><div className="personal-draft-grid">{item.sessionDrafts.map((draft) => <PersonalDraftCard key={draft.draft_id} item={draft} onOpenDraft={onOpenDraft} />)}</div></div>}
              {!!item.attachments.length && <div><span className="personal-resource-title">附件</span><ul className="personal-attachment-list">{item.attachments.map((attachment, index) => <li key={attachment.document_id || attachment.file_url || attachment.storage_key || index}><span title={attachment.source_name || attachment.file_name || attachment.name}>{attachmentDisplayName(attachment)}</span><AttachmentDownload attachment={attachment} identity={identity} label="查看附件" /></li>)}</ul></div>}
              {!!item.sessionDocuments.length && <div><span className="personal-resource-title">已提交资料</span>{item.sessionDocuments.map((document) => <PersonalDocument key={document.document_id} document={document} relatedDrafts={[]} busy={busy} onUnpublish={onUnpublish} onRepublish={onRepublish} onOpenDocument={onOpenDocument} onOpenDraft={onOpenDraft} />)}</div>}
            </div>
            {selectedConversationId === item.thread_id && <ConversationPreview messages={conversationMessages} loading={conversationLoading} selected={selectedConversationId} />}
          </article>)}</div><Pagination page={activityPage} total={activity.length} onChange={setActivityPage} /></>}
          {!!unlinkedDocuments.length && <div className="personal-unlinked-materials"><h3>未关联对话的资料</h3>{visibleDocuments.map((document) => <PersonalDocument key={document.document_id} document={document} relatedDrafts={draftsForDocument(document)} busy={busy} onUnpublish={onUnpublish} onRepublish={onRepublish} onOpenDocument={onOpenDocument} onOpenDraft={onOpenDraft} />)}<Pagination page={documentPage} total={unlinkedDocuments.length} onChange={setDocumentPage} /></div>}
          {!!unlinkedActivityDrafts.length && <div className="personal-unlinked-materials"><h3>未关联对话的草稿</h3><div className="personal-draft-grid">{visibleStandaloneDrafts.map((item) => <PersonalDraftCard key={item.draft_id} item={item} onOpenDraft={onOpenDraft} />)}</div><Pagination page={standaloneDraftPage} total={unlinkedActivityDrafts.length} onChange={setStandaloneDraftPage} /></div>}
        </>}
      </section>

    {documentDetail && <PersonalDocumentDetail document={documentDetail.document || documentDetail} parseResult={documentDetail.parseResult} parseError={documentDetail.parseError} loading={documentDetailLoading} onClose={onCloseDocument} />}
    {draftDetail && <PersonalDraftDetail draft={draftDetail} identity={identity} busy={busy} onClose={onCloseDraft} onDelete={onDeleteDraft} onUnpublish={onUnpublishDraft} onRepublish={onRepublishDraft} onDeleteAttachment={onDeleteDraftAttachment} />}
  </section>;
}

const draftStatusLabels = {
  CONFIRMED: '已确认', PENDING_REVIEW: '待确认', PUBLISHED: '可检索',
  WITHDRAWN: '已下架', DRAFT_GENERATED: '待完善', WAITING_DRAFT_CONFIRM: '等待草稿确认',
};

export function PersonalDraftDetail({ draft: record, identity, busy, onClose, onDelete, onUnpublish, onRepublish, onDeleteAttachment, readOnly = false }) {
  const draft = record?.draft || {};
  const fields = Object.entries(draft.fields || {});
  const attachments = draftAttachments(record);
  const summary = draft.requirement_summary || draft.technical_summary || draft.achievement_summary || draft.summary;
  const published = record?.status === 'PUBLISHED';

  return <div className="draft-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose?.(); }}>
    <section className="draft-modal personal-draft-modal" role="dialog" aria-modal="true" aria-labelledby="personal-draft-detail-title">
      <header className="draft-modal-head"><div><span>MY DRAFT</span><h2 id="personal-draft-detail-title">草稿详情</h2><p>{record?.draft_title || draft.title || draft.fields?.enterprise_name || draft.fields?.user_name || record?.draft_id || '—'}</p></div><button type="button" className="draft-modal-close" onClick={onClose} aria-label="关闭草稿详情">×</button></header>
      <div className="draft-modal-body personal-draft-detail-body">
        <dl className="personal-detail-meta"><div><dt>草稿状态</dt><dd>{draftStatusLabels[record?.status] || record?.status || '—'}</dd></div><div><dt>资料类型</dt><dd>{record?.draft_type || '—'}</dd></div><div><dt>更新时间</dt><dd>{formatTime(record?.updated_at || record?.created_at)}</dd></div></dl>
        {summary && <><h3>内容摘要</h3><p className="personal-draft-summary">{summary}</p></>}
        <h3>草稿字段</h3>
        {!fields.length ? <p className="table-empty">暂无结构化字段。</p> : <div className="personal-draft-fields">{fields.map(([key, value]) => <div key={key}><span>{displayDraftField(key)}</span><p>{typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value || '—')}</p></div>)}</div>}
        <h3>附件</h3>
        {!attachments.length ? <p className="table-empty">无附件。</p> : <ul className="personal-draft-attachments">{attachments.map((item, index) => { const key = item.document_id || item.storage_key || item.file_name || item.name; return <li key={key || index}><span title={item.source_name || item.file_name || item.name}>{attachmentDisplayName(item)}</span><span className="personal-attachment-actions"><AttachmentDownload attachment={item} identity={identity} />{!readOnly && <button type="button" className="text-button danger-text draft-file-action" onClick={() => onDeleteAttachment?.(record, item)} disabled={busy || record?.status === 'PUBLISHED'} title={record?.status === 'PUBLISHED' ? '已发布草稿不可修改，请先下架' : '从草稿中移除附件'}>删除</button>}</span></li>; })}</ul>}
      </div>
      <footer className="draft-modal-foot"><div className="draft-modal-foot-actions">{!readOnly && onDelete && <button type="button" className="secondary danger-border" onClick={() => onDelete(record)} disabled={busy || published} title={published ? '请先下架草稿，再删除' : '从个人中心逻辑删除草稿'}>删除草稿</button>}{!readOnly && published && onUnpublish && <button type="button" className="secondary" onClick={() => onUnpublish(record)} disabled={busy}>下架</button>}{!readOnly && record?.status === 'WITHDRAWN' && onRepublish && <button type="button" onClick={() => onRepublish(record)} disabled={busy}>重新上架</button>}</div><div className="draft-modal-foot-actions"><button type="button" className="secondary" onClick={() => copyDraft(record)}>复制草稿</button><button type="button" className="secondary" onClick={onClose}>关闭</button></div></footer>
    </section>
  </div>;
}

function PersonalDraftCard({ item, onOpenDraft }) {
  const draft = item.draft || {};
  const title = item.draft_title || draft.title || draft.fields?.enterprise_name || draft.fields?.user_name || item.draft_id || '未命名草稿';
  return <div className="personal-draft"><button className="personal-draft-open" type="button" onClick={() => onOpenDraft?.(item)}><span><strong>{title}</strong><small>{item.draft_type || '资料草稿'} · {formatTime(item.updated_at || item.created_at)}</small></span><em>{draftStatusLabels[item.status] || item.status || '待处理'}</em></button></div>;
}

function attachmentDownloadUrl(item, identity) {
  if (item?.document_id) return api.documentFileUrl(item.document_id, identity);
  return item?.file_url ? api.perceptionAssetUrl(item.file_url) : '';
}

function AttachmentDownload({ attachment, identity, label = '下载' }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const url = attachmentDownloadUrl(attachment, identity);
  if (!url) return null;
  async function handleDownload() {
    if (busy) return;
    setBusy(true);
    setError('');
    let objectUrl;
    try {
      const skipAuth = new URL(url).origin !== new URL(api.perceptionAssetUrl('/')).origin;
      const blob = await download(url, { skipAuth });
      objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = attachment.file_name || attachment.source_name || attachment.name || '附件';
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (failure) {
      setError(failure.message || '附件下载失败');
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setBusy(false);
    }
  }
  return <span><button type="button" className="draft-file-action" onClick={() => void handleDownload()} disabled={busy}>{busy ? '下载中…' : label}</button>{error && <small role="alert">{error}</small>}</span>;
}

function attachmentDisplayName(item) {
  return item?.source_type === 'audio_transcript'
    ? '语音转写文本'
    : (item?.file_name || item?.name || item?.document_id || '未命名附件');
}

function copyDraft(record) {
  const draft = record?.draft || record || {};
  const fields = draft.fields && typeof draft.fields === 'object' ? draft.fields : draft;
  const excluded = new Set(['draft_type', 'attachments', 'source_blocks', 'demand_matches', 'solution_evidences']);
  const text = Object.entries(fields)
    .filter(([key, value]) => !excluded.has(key) && value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${displayDraftField(key)}：${recordText(value)}`)
    .join('\n');
  void navigator.clipboard?.writeText(text || '暂无可复制的草稿内容');
}

function PersonalDocument({ document, relatedDrafts, busy, onUnpublish, onRepublish, onOpenDocument, onOpenDraft }) {
  const [reason, setReason] = useState('NEEDS_REVISION');
  const [detail, setDetail] = useState('');
  const status = publicationStatus(document);
  const published = status === 'PUBLISHED';
  const fileName = document.source_name || document.file_name || document.document_id;

  return <article className="personal-document-card">
    <div className="personal-document-main">
      <div className="personal-document-title"><strong title={fileName}>{fileName}</strong><span className={`personal-status personal-status-${String(status).toLowerCase()}`}>{statusLabels[status] || status}</span></div>
      <span>{document.knowledge_type || '未指定'} · {document.active_version?.version_no || document.version_no || '当前版本'}</span>
      <small>最近更新：{formatTime(document.updated_at)}</small>
      {document.unpublish_reason_code && <em>下架原因：{document.unpublish_reason_text || REASONS.find(([code]) => code === document.unpublish_reason_code)?.[1] || document.unpublish_reason_code}</em>}
    </div>
    <div className="personal-document-actions">
      <button className="text-button personal-view-document" type="button" onClick={() => onOpenDocument?.(document)} disabled={busy}>查看资料</button>
      {published ? <div className="personal-unpublish">
        <label>下架原因<select value={reason} onChange={(event) => setReason(event.target.value)} aria-label={`${fileName}的下架原因`}>{REASONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        {reason === 'OTHER' && <label>原因说明<textarea value={detail} onChange={(event) => setDetail(event.target.value)} placeholder="填写下架原因（必填）" rows="2" /></label>}
        <button className="secondary danger-border" type="button" onClick={() => onUnpublish?.(document, reason, detail)} disabled={busy}>申请下架</button>
      </div> : <div className="personal-document-state"><span>{status === 'UNPUBLISHED' ? '已停止检索' : '当前不可下架'}</span><small>{status === 'UNPUBLISHED' ? '可由本人重新上架，管理员也可继续处置。' : '资料完成建库后可申请下架。'}</small>{status === 'UNPUBLISHED' && <button className="secondary" type="button" onClick={() => onRepublish?.(document)} disabled={busy}>重新上架</button>}</div>}
    </div>
    {!!relatedDrafts?.length && <div className="personal-linked-drafts"><span>关联草稿</span>{relatedDrafts.map((item) => <PersonalDraftCard key={item.draft_id} item={item} onOpenDraft={onOpenDraft} />)}</div>}
  </article>;
}

function PersonalDocumentDetail({ document, parseResult, parseError, loading, onClose }) {
  const versions = Array.isArray(document?.versions) ? document.versions : [];
  const activeVersionId = document?.active_version_id || document?.active_version?.version_id;
  const status = publicationStatus(document);
  return <div className="draft-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose?.(); }}>
    <section className="draft-modal personal-document-modal" role="dialog" aria-modal="true" aria-labelledby="personal-document-detail-title">
      <header className="draft-modal-head">
        <div><span>MY DOCUMENT</span><h2 id="personal-document-detail-title">资料详情</h2><p>{document?.source_name || document?.file_name || document?.document_id || '—'}</p></div>
        <button type="button" className="draft-modal-close" onClick={onClose} aria-label="关闭资料详情">×</button>
      </header>
      <div className="draft-modal-body personal-document-detail-body">
        {loading ? <p className="table-empty">正在读取资料详情…</p> : <>
          <dl className="personal-detail-meta">
            <div><dt>资料状态</dt><dd>{statusLabels[status] || status}</dd></div>
            <div><dt>知识类型</dt><dd>{document?.knowledge_type || '未指定'}</dd></div>
            <div><dt>知识范围</dt><dd>{document?.scope_id || 'public'}</dd></div>
            <div><dt>文档 ID</dt><dd>{document?.document_id || '—'}</dd></div>
          </dl>
          {document?.unpublish_reason_code && <div className="personal-detail-notice">下架原因：{document.unpublish_reason_text || document.unpublish_reason_code}<br /><small>资料已停止参与检索，但原始资料和历史版本仍保留。</small></div>}
          <h3>解析原文</h3>
          {parseError && <p className="table-error" role="alert">{parseError}</p>}
          {parseResult ? <div className="personal-source-preview">
            {parseResult.raw_text ? <MineruInlinePreview text={parseResult.raw_text} pageTexts={parseResult.page_texts} pageManifest={parseResult.metadata?.pages || []} images={parseResult.metadata?.images || parseResult.images || []} /> : (parseResult.blocks || []).map((block, index) => <p key={block.block_id || index}>{block.text || '（空块）'}</p>)}
          </div> : !parseError && <p className="table-empty">暂无已保存的解析原文。</p>}
          <h3>版本记录</h3>
          {!versions.length ? <p className="table-empty">暂无版本记录。</p> : <div className="personal-version-list">{versions.map((version) => <article key={version.version_id || version.version_number}>
            <div><strong>v{version.version_number ?? '—'}</strong><span>{version.version_id || '—'}</span></div>
            <p>检索状态：{statusLabels[version.publication_status] || version.publication_status || '—'} · 索引状态：{version.index_status || '—'}</p>
            <small>{version.version_id === activeVersionId ? '当前版本 · ' : ''}更新时间：{formatTime(version.updated_at || version.created_at)}</small>
          </article>)}</div>}
        </>}
      </div>
      <footer className="draft-modal-foot"><button type="button" className="secondary" onClick={onClose}>关闭</button></footer>
    </section>
  </div>;
}

function ConversationPreview({ messages, loading, selected }) {
  if (!selected) return <p className="personal-conversation-empty">选择一条会话以查看最近记录。</p>;
  if (loading) return <p className="personal-conversation-empty">正在读取对话记录…</p>;
  if (!messages.length) return <p className="personal-conversation-empty">该会话暂无可显示的保存消息。</p>;
  return <div className="personal-conversation-preview" aria-live="polite">{messages.map((message) => {
    const isUser = message.role === 'user';
    const content = chatMessageText(message);
    return <article className={`personal-message ${isUser ? 'is-user' : 'is-assistant'}`} key={message.message_id || `${message.created_at}-${content}`}>
      <span>{isUser ? '我' : '科创服务助手'}</span><p>{content || '—'}</p>
    </article>;
  })}</div>;
}
