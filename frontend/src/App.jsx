import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api.js';
import { errorMessage } from './api/request.js';
import { SupervisorPage } from './pages/supervisor/SupervisorPage.jsx';
import { displayDraftFieldLabel } from './draftFieldLabels.js';
import { draftFailureMessage } from './pages/documents/draftFailure.js';
import { buildSavedChunksParseUpdate, removeBlankLines } from './pages/documents/parseResultSave.js';
import { reparseResetState } from './pages/documents/reparseState.js';
import { createUuid } from './uuid.js';

const displayError = (value, fallback = '未知错误') => errorMessage(value, fallback);

const IndexPreviewModal = lazy(() => import('./pages/documents/IndexPreviewModal.jsx').then(({ IndexPreviewModal }) => ({ default: IndexPreviewModal })));
const AdminDocumentsPage = lazy(() => import('./pages/documents/AdminDocumentsPage.jsx').then(({ AdminDocumentsPage }) => ({ default: AdminDocumentsPage })));
const AdminUploadPage = lazy(() => import('./pages/documents/AdminUploadPage.jsx').then(({ AdminUploadPage }) => ({ default: AdminUploadPage })));
const PersonalPage = lazy(() => import('./pages/personal/PersonalPage.jsx').then(({ PersonalPage }) => ({ default: PersonalPage })));
const AdminFieldPolicy = lazy(() => import('./pages/admin/AdminFieldPolicy.jsx').then(({ AdminFieldPolicy }) => ({ default: AdminFieldPolicy })));
const AdminGovernance = lazy(() => import('./pages/admin/AdminGovernance.jsx').then(({ AdminGovernance }) => ({ default: AdminGovernance })));
const AdminOverview = lazy(() => import('./pages/admin/AdminOverview.jsx').then(({ AdminOverview }) => ({ default: AdminOverview })));
const TaskCenter = lazy(() => import('./pages/admin/TaskCenter.jsx').then(({ TaskCenter }) => ({ default: TaskCenter })));
const RetrievalPage = lazy(() => import('./pages/retrieval/RetrievalPage.jsx').then(({ RetrievalPage }) => ({ default: RetrievalPage })));

const RECORD_KEY = 'kechuang-workbench-records-v2';
const CHAT_SESSION_KEY = 'kechuang-chat-session-id';
const DOCUMENT_PAGE_SIZE = 20;
const ADMIN_PAGE_SIZE = 20;
const CACHE_FIELDS = [
  'document_id', 'version_id', 'supervisor_task_id', 'storage_key', 'file_name',
  'scope_id', 'user_id', 'knowledge_type', 'status', 'stage', 'progress',
  'indexed_count', 'block_count', 'error_code', 'error', 'updated_at', 'idempotency_key',
];
const knowledgeTypes = [
  ['demand', '需求库'], ['solution', '方案库'], ['achievement', '成果库'],
  ['enterprise', '企业库'], ['expert', '专家库'],
];

function displayKnowledgeType(item) {
  const persisted = String(item?.knowledge_type || '').trim();
  if (persisted && ['enterprise', 'demand', 'achievement', 'expert', 'solution'].includes(persisted)) {
    return persisted;
  }
  return 'solution';
}

function loadRecords() {
  try {
    const stored = JSON.parse(localStorage.getItem(RECORD_KEY) || '{}');
    if (!stored || typeof stored !== 'object') return {};
    // 旧版本只保存了“已上传”状态，没有 MinIO 对象地址，不能继续解析或删除。
    // 隐藏这类本地残留记录，避免用户再次点到无效文档。
    return Object.fromEntries(Object.entries(stored).filter(([, document]) => document?.storage_key));
  } catch { return {}; }
}

function sourceLocatorLegacy(block, fallbackName, ordinal) {
  const sourceName = block.source_name || fallbackName || 'unknown';
  const blockType = block.block_type || 'text';
  const metadata = block.metadata || {};
  if (metadata.sheet_name && metadata.row_number != null) {
    return `${sourceName}|type:${blockType}|sheet:${metadata.sheet_name}|row:${metadata.row_number}`;
  }
  if (metadata.slide_start != null && metadata.slide_end != null) {
    return `${sourceName}|type:${blockType}|slides:${metadata.slide_start}-${metadata.slide_end}|ordinal:${ordinal + 1}`;
  }
  if (metadata.page_start != null && metadata.page_end != null) {
    return metadata.page_start === metadata.page_end
      ? `${sourceName}|type:${blockType}|page:${metadata.page_start}|ordinal:${ordinal + 1}`
      : `${sourceName}|type:${blockType}|pages:${metadata.page_start}-${metadata.page_end}|ordinal:${ordinal + 1}`;
  }
  const page = block.page_number == null ? 'unknown' : block.page_number;
  return `${sourceName}|type:${blockType}|page:${page}|ordinal:${ordinal + 1}`;
}

function healthIssues(health) {
  if (!health || health.status === 'ok') return [];
  const issues = Object.entries(health.services || {}).flatMap(([serviceName, service]) => {
    if (service?.error) return [{ serviceName, checkName: '服务', error: service.error }];
    const checks = service?.checks || {};
    return Object.entries(checks)
      .filter(([, check]) => check?.status !== 'ok')
      .map(([checkName, check]) => ({
        serviceName,
        checkName,
        error: check?.error || check?.error_type || '不可用',
      }));
  });
  return issues.length > 0
    ? issues
    : (health.error ? [{ serviceName: '健康检查', checkName: '请求', error: health.error }] : []);
}

function HealthStatus({ health }) {
  const issues = healthIssues(health);
  const label = !health ? '检查服务中' : health.status === 'ok' ? '服务运行正常' : '部分服务未就绪';
  return <div className={`health health-${health?.status || 'unknown'}`} title={issues.map((item) => `${item.serviceName}/${item.checkName}: ${item.error}`).join('\n')}>
    <span />
    <strong>{label}</strong>
    {issues.length > 0 && <details className="health-details">
      <summary>查看原因</summary>
      <ul>{issues.map((item) => <li key={`${item.serviceName}-${item.checkName}`}><b>{item.checkName}</b>：{item.error}</li>)}</ul>
    </details>}
  </div>;
}

function blockLocationLabel(block) {
  const metadata = block.metadata || {};
  const sourceLocator = String(block.source_locator || '');
  const range = sourceLocator.match(/(?:^|\|)(?:pages|slides):(\d+)-(\d+)/);
  if (range) return `第 ${range[1]}-${range[2]} 页`;
  const single = sourceLocator.match(/(?:^|\|)page:(\d+)/);
  if (single && metadata.page_start == null && metadata.slide_start == null) return `第 ${single[1]} 页`;
  if (metadata.page_start != null && metadata.page_end != null) {
    return metadata.page_start === metadata.page_end
      ? `第 ${metadata.page_start} 页`
      : `第 ${metadata.page_start}-${metadata.page_end} 页`;
  }
  if (metadata.slide_start != null && metadata.slide_end != null) {
    return metadata.slide_start === metadata.slide_end
      ? `第 ${metadata.slide_start} 页`
      : `第 ${metadata.slide_start}-${metadata.slide_end} 页`;
  }
  return `第 ${block.page_number ?? '—'} 页`;
}

function restoreSavedChunks(chunks) {
  return Array.isArray(chunks) ? chunks.filter((chunk) => chunk && typeof chunk === 'object').map((chunk, index) => ({
    block_id: chunk.block_id || chunk.source_chunk_id || `saved:chunk:${index}`,
    text: chunk.text || chunk.chunk_text || '',
    page_number: chunk.page_number ?? chunk.metadata?.page_start,
    metadata: chunk.metadata || {},
    source_chunk_id: chunk.source_chunk_id,
    proposed_visibility: chunk.proposed_visibility || chunk.visibility || 'public',
    source_locator: chunk.source_locator,
  })) : [];
}

function toIndexChunk(block, index, documentId = 'document') {
  const metadata = block.metadata || {};
  return {
    chunk_index: index,
    chunk_text: block.text,
    parent_id: metadata.parent_id || undefined,
    parent_text: metadata.parent_text || undefined,
    child_index: metadata.child_index ?? 0,
    source_locator: block.source_locator,
    metadata: { ...metadata, preview_confirmed: true },
    visibility: block.proposed_visibility || metadata.proposed_visibility || 'public',
    source_chunk_id: block.source_chunk_id || `${documentId}:chunk:${index}`,
  };
}

function splitPreviewText(value, maxLength = 600) {
  const text = String(value || '').trim();
  if (!text) return [''];
  const paragraphs = text.split(/\n{2,}/).map((item) => item.trim()).filter(Boolean);
  const segments = [];
  for (const paragraph of (paragraphs.length ? paragraphs : [text])) {
    if (paragraph.length <= maxLength) {
      segments.push(paragraph);
      continue;
    }
    let remainder = paragraph;
    while (remainder.length > maxLength) {
      let cut = remainder.lastIndexOf('。', maxLength);
      if (cut < Math.floor(maxLength * 0.55)) cut = remainder.lastIndexOf('；', maxLength);
      if (cut < Math.floor(maxLength * 0.55)) cut = remainder.lastIndexOf(' ', maxLength);
      if (cut < Math.floor(maxLength * 0.55)) cut = maxLength;
      segments.push(remainder.slice(0, cut + (cut < maxLength ? 1 : 0)).trim());
      remainder = remainder.slice(cut + (cut < maxLength ? 1 : 0)).trim();
    }
    if (remainder) segments.push(remainder);
  }
  return segments.length ? segments : [''];
}

function imageIdsByPage(images = []) {
  const result = new Map();
  for (const image of Array.isArray(images) ? images : []) {
    const page = Number(image?.pageNumber ?? image?.page_number ?? image?.page);
    const imageId = String(image?.image_id || '').trim();
    if (!Number.isInteger(page) || page <= 0 || !imageId) continue;
    const values = result.get(page) || [];
    if (!values.includes(imageId)) values.push(imageId);
    result.set(page, values);
  }
  return result;
}

function imageRefsByPage(images = []) {
  const result = new Map();
  for (const image of Array.isArray(images) ? images : []) {
    const page = Number(image?.pageNumber ?? image?.page_number ?? image?.page);
    const imageId = String(image?.image_id || '').trim();
    if (!Number.isInteger(page) || page <= 0 || !imageId) continue;
    const sources = [image?.url, image?.image_url, image?.relativePath, image?.relative_path, image?.path]
      .map((value) => String(value || '').replaceAll('\\', '/').trim())
      .filter(Boolean);
    const values = result.get(page) || [];
    if (!values.some((item) => item.image_id === imageId)) values.push({ image_id: imageId, sources });
    result.set(page, values);
  }
  return result;
}

function applyEditedTextToSolutionBlocks(blocks, rawText, pageTexts = {}, images = []) {
  const text = String(rawText || '');
  if (!text.trim()) return blocks;
  const recordBlocks = (blocks || []).filter((block) => block.block_type === 'record');
  if (recordBlocks.length) {
    const records = text.split(/\n{2,}/).map((item) => item.trim()).filter(Boolean);
    if (records.length === recordBlocks.length) {
      return records.map((record, index) => ({ ...recordBlocks[index], text: record }));
    }
  }
  const pageBlocks = new Map((blocks || []).map((block) => [block.page_number, block]));
  const pages = Object.entries(pageTexts || {})
    .map(([page, value]) => ({ page: Number(page), text: String(value || '').trim() }))
    .filter((item) => Number.isFinite(item.page) && item.text)
    .sort((left, right) => left.page - right.page);
  const positions = [];
  let cursor = 0;
  for (const page of pages) {
    const position = text.indexOf(page.text, cursor);
    if (position < 0) continue;
    positions.push({ ...page, position });
    cursor = position + page.text.length;
  }
  const pageImageIds = imageIdsByPage(images);
  const pageImageRefs = imageRefsByPage(images);
  if (positions.length > 1) {
    const blockForPage = (page) => (blocks || []).find((block) => (
      Number(block?.page_number) === page
      || (Number(block?.metadata?.slide_start) <= page && Number(block?.metadata?.slide_end) >= page)
    )) || blocks[0] || {};
    const scopedBlock = (template, page, value) => ({
      ...template,
      text: value,
      page_number: page,
      metadata: {
        ...(template.metadata || {}),
        page_start: page,
        page_end: page,
        slide_start: page,
        slide_end: page,
        ...(pageImageIds.size ? { image_ids: pageImageIds.get(page) || [] } : {}),
        ...(pageImageRefs.size ? { image_refs: pageImageRefs.get(page) || [] } : {}),
      },
    });
    const segments = positions.map((page, index) => scopedBlock(
      blockForPage(page.page),
      page.page,
      text.slice(page.position, positions[index + 1]?.position ?? text.length).trim(),
    )).filter((block) => block.text);
    const firstPage = blockForPage(positions[0].page);
    const prefix = text.slice(0, positions[0].position).trim();
    if (prefix) segments.unshift(scopedBlock(firstPage, firstPage.page_number || 1, prefix));
    return segments;
  }
  if (!positions.length) return [{
    ...(blocks[0] || {}),
    block_type: 'text',
    text,
    metadata: {
      ...(blocks[0]?.metadata || {}),
      field_policy_managed: false,
      field_values: undefined,
    },
  }];
  const firstPage = pageBlocks.get(positions[0].page) || blocks[0] || {};
  return [{
    ...firstPage,
    text,
    metadata: {
      ...(firstPage.metadata || {}),
      ...(pageImageIds.size ? { image_ids: pageImageIds.get(positions[0].page) || [] } : {}),
      ...(pageImageRefs.size ? { image_refs: pageImageRefs.get(positions[0].page) || [] } : {}),
    },
  }];
}

function buildSolutionChunks(blocks, fallbackText, documentId = 'solution') {
  const targetChars = 1000;
  const sourceBlocks = (blocks || []).filter((block) => String(block.text || '').trim());
  if (!sourceBlocks.length) {
    return splitPreviewText(fallbackText, targetChars).map((text, index) => ({
      block_id: `solution:chunk:${index + 1}`,
      block_type: 'text',
      text,
      source_name: 'unknown',
      metadata: { page_start: null, page_end: null, image_ids: [] },
    }));
  }

  const chunks = [];
  let current = null;
  const flush = () => {
    if (current?.text?.trim()) chunks.push(current);
    current = null;
  };
  for (const block of sourceBlocks) {
    const metadata = block.metadata || {};
    const pageStart = metadata.page_start ?? metadata.slide_start ?? block.page_number ?? null;
    const pageEnd = metadata.page_end ?? metadata.slide_end ?? block.page_number ?? null;
    const isTable = String(block.block_type || '').includes('table') || metadata.contains_table;
    // 表格/图片关联块优先保持完整；超长表格由后端 Perception 的表格子块负责拆分。
    const pieces = isTable && String(block.text).length <= 1500
      ? [String(block.text).trim()]
      : splitPreviewText(block.text, targetChars);
    for (const text of pieces) {
      const cleanText = String(text || '').trim();
      if (!cleanText) continue;
      const nextStart = current?.metadata.page_start ?? pageStart;
      const nextEnd = pageEnd;
      const candidate = current ? `${current.text}\n\n${cleanText}` : cleanText;
      if (current && candidate.length > targetChars) flush();
      if (!current) {
        current = {
          ...block,
          text: cleanText,
          block_id: `${block.block_id || 'solution'}:chunk:${chunks.length + 1}`,
          metadata: {
            ...metadata,
            page_start: nextStart,
            page_end: nextEnd,
            image_ids: [...(metadata.image_ids || [])],
            parent_id: `${documentId}:pages:${nextStart || 'unknown'}-${nextEnd || 'unknown'}`,
            parent_text: cleanText,
          },
        };
        continue;
      }
      current.text = `${current.text}\n\n${cleanText}`;
      current.metadata.page_end = nextEnd;
      current.metadata.parent_text = current.text;
      current.metadata.image_ids = [...new Set([
        ...(current.metadata.image_ids || []), ...(metadata.image_ids || []),
      ])];
    }
  }
  flush();
  return chunks.map((chunk, index) => ({
    ...chunk,
    generated_segment: index,
    metadata: { ...chunk.metadata, field_policy_managed: false },
  }));
}

function fieldsToPreviewText(fields) {
  return Object.entries(fields || {})
    .filter(([, value]) => value !== null && value !== undefined && String(value).trim())
    .map(([name, value]) => `${displayDraftFieldLabel(name)}：${value}`)
    .join('\n');
}

function buildFieldVisibilityBlocks(blocks, knowledgeType) {
  return (blocks || []).flatMap((block, sourceBlockIndex) => {
    const metadata = block.metadata || {};
    const fields = metadata.field_values;
    if (!metadata.field_policy_managed || !fields || typeof fields !== 'object') {
      return [{ ...block, preview_source_block_index: sourceBlockIndex }];
    }

    const preview = metadata.field_visibility_preview || {};
    const overrides = metadata.field_visibility_overrides || {};
    const grouped = { public: {}, internal: {} };
    for (const [name, value] of Object.entries(fields)) {
      if (value === null || value === undefined || !String(value).trim()) continue;
      const visibility = overrides[name] || preview[name] || 'internal';
      (visibility === 'public' ? grouped.public : grouped.internal)[name] = value;
    }

    const sourceId = block.source_chunk_id || block.block_id || `${knowledgeType || 'knowledge'}:${sourceBlockIndex}`;
    return Object.entries(grouped)
      .filter(([, selectedFields]) => Object.keys(selectedFields).length > 0)
      .map(([visibility, selectedFields]) => ({
        ...block,
        block_id: `${block.block_id || sourceBlockIndex}:${visibility}`,
        text: fieldsToPreviewText(selectedFields),
        proposed_visibility: visibility === 'public' ? 'public' : 'internal_supplement',
        preview_source_block_index: sourceBlockIndex,
        metadata: {
          ...metadata,
          field_values: selectedFields,
          field_names: Object.keys(selectedFields),
          field_visibility: visibility,
          field_visibility_preview: Object.fromEntries(
            Object.keys(selectedFields).map((name) => [name, visibility]),
          ),
          field_visibility_overrides: Object.fromEntries(
            Object.entries(overrides).filter(([name]) => Object.prototype.hasOwnProperty.call(selectedFields, name)),
          ),
        },
        source_chunk_id: `${sourceId}:${visibility}`,
      }));
  });
}

function catalogStage(document, task) {
  if (task?.stage) return task.stage;
  const status = String(document.active_version?.status || document.status || '').toUpperCase();
  if (status === 'FAILED') return 'failed';
  if (status === 'PARSING') return 'parse';
  if (['WAITING_REVIEW', 'READY_TO_PUBLISH'].includes(status)) return 'confirm';
  if (['INDEXING', 'INDEXED', 'PUBLISHED', 'UNPUBLISHED'].includes(status)) return 'index';
  if (['RECYCLED', 'PURGED'].includes(status)) return 'deleted';
  return 'upload';
}

export default function App() {
  const [identity, setIdentity] = useState(() => ({
    user_role: window.location.pathname.startsWith('/admin') ? 'admin' : 'external',
    user_id: 'demo-user',
    name: '测试用户',
  }));
  const [testUsers, setTestUsers] = useState([]);
  const [testUsersLoading, setTestUsersLoading] = useState(false);
  const [testUsersError, setTestUsersError] = useState('');
  const [scopeId, setScopeId] = useState('public');
  const [searchScopeId, setSearchScopeId] = useState('public');
  const [knowledgeType, setKnowledgeType] = useState('demand');
  const [catalogKnowledgeType, setCatalogKnowledgeType] = useState('');
  const [documentSearch, setDocumentSearch] = useState('');
  const [documentPage, setDocumentPage] = useState(1);
  const [documentTotal, setDocumentTotal] = useState(0);
  const [documentTotalPages, setDocumentTotalPages] = useState(0);
  const [overviewUpdatePage, setOverviewUpdatePage] = useState(1);
  const [overviewUpdateTotal, setOverviewUpdateTotal] = useState(0);
  const [overviewUpdateTotalPages, setOverviewUpdateTotalPages] = useState(0);
  const [draftCatalogPage, setDraftCatalogPage] = useState(1);
  const [draftCatalogTotal, setDraftCatalogTotal] = useState(0);
  const [draftCatalogTotalPages, setDraftCatalogTotalPages] = useState(0);
  const [searchKnowledgeType, setSearchKnowledgeType] = useState('');
  const [file, setFile] = useState(null);
  const fileInputRef = useRef(null);
  const [uploadTarget, setUploadTarget] = useState(null);
  const [uploaded, setUploaded] = useState(null);
  const [parsed, setParsed] = useState(null);
  const [parsedDirty, setParsedDirty] = useState(false);
  const parsedDraftRef = useRef('');
  const [generatedChunks, setGeneratedChunks] = useState([]);
  const [indexPreview, setIndexPreview] = useState(null);
  const [records, setRecords] = useState(loadRecords);
  const [files, setFiles] = useState([]);
  const [versions, setVersions] = useState([]);
  const [versionsDocument, setVersionsDocument] = useState(null);
  const [documentDetails, setDocumentDetails] = useState({});
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsBusy, setVersionsBusy] = useState(false);
  const [supervisorTasks, setSupervisorTasks] = useState([]);
  const [adminOverview, setAdminOverview] = useState(null);
  const [knowledgeUpdates, setKnowledgeUpdates] = useState([]);
  const [draftCatalog, setDraftCatalog] = useState([]);
  const [knowledgeUpdateDetail, setKnowledgeUpdateDetail] = useState(null);
  const [knowledgeUpdateDetailLoading, setKnowledgeUpdateDetailLoading] = useState(false);
  const [mysqlSync, setMysqlSync] = useState({});
  const [mysqlCounts, setMysqlCounts] = useState({});
  const [adminFieldPolicies, setAdminFieldPolicies] = useState(null);
  const [myProfile, setMyProfile] = useState(null);
  const [myDocuments, setMyDocuments] = useState([]);
  const [myConversations, setMyConversations] = useState([]);
  const [myDrafts, setMyDrafts] = useState([]);
  const [myVisitRecords, setMyVisitRecords] = useState([]);
  const [myMatchResults, setMyMatchResults] = useState([]);
  const [personalDraftDetail, setPersonalDraftDetail] = useState(null);
  const [personalDocumentDetail, setPersonalDocumentDetail] = useState(null);
  const [personalDocumentLoading, setPersonalDocumentLoading] = useState(false);
  const [personalLoading, setPersonalLoading] = useState(false);
  const [personalConversationId, setPersonalConversationId] = useState('');
  const [personalConversationMessages, setPersonalConversationMessages] = useState([]);
  const [personalConversationLoading, setPersonalConversationLoading] = useState(false);
  const personalRefreshRequestRef = useRef(0);
  const personalDocumentRequestRef = useRef(0);
  const mysqlStopInFlightRef = useRef(new Set());
  const [selected, setSelected] = useState(null);
  const [fileLoading, setFileLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('选择文件后按步骤处理；建立索引前可查看 Perception 解析结果。');
  const [health, setHealth] = useState(null);
  const healthRequestRef = useRef(0);
  const healthInFlightRef = useRef(null);
  const adminDocumentContextRef = useRef(null);
  const filesRefreshInFlightRef = useRef(null);
  const filesRefreshQueuedRef = useRef(null);
  const filesRefreshQueuedOptionsRef = useRef(null);
  const [query, setQuery] = useState('');
  const [searchResult, setSearchResult] = useState(null);
  const [page, setPage] = useState(() => window.location.pathname.startsWith('/admin') ? 'overview' : 'supervisor');
  const [route, setRoute] = useState(() => window.location.pathname.startsWith('/admin') ? '/admin' : '/');

  // 正在处理的上传对象优先；selected 可能仍是文件表中的旧快照。
  const current = uploaded || selected;
  const currentStage = current?.stage || 'idle';
  const previewBlocks = useMemo(
    () => buildFieldVisibilityBlocks(parsed?.blocks, current?.knowledge_type || parsed?.metadata?.knowledge_type),
    [parsed?.blocks, parsed?.metadata?.knowledge_type, current?.knowledge_type],
  );
  useEffect(() => {
    setGeneratedChunks(restoreSavedChunks(parsed?.saved_chunks));
    parsedDraftRef.current = parsed?.raw_text || '';
  }, [parsed?.document_id, parsed?.saved_chunks]);
  // 当前阶段用 URL 区分两个前端工作区；真正的管理员鉴权由后端认证接入后负责。
  const isAdminPage = route === '/admin';
  const isAdminUser = identity.user_role === 'admin';
  const selectedTestUser = testUsers.find((user) => user.user_id === identity.user_id);
  const allowedScopes = useMemo(() => ({
    admin: 'public · internal · private', internal: 'public · internal', external: 'public',
  }[identity.user_role]), [identity.user_role]);
  const scopeOptions = useMemo(() => {
    const publicScope = [{ value: 'public', label: '公共知识（public）' }];
    if (identity.user_role === 'internal' || isAdminUser) {
      return [...publicScope, { value: 'internal', label: '内部市场知识（internal）' }];
    }
    return publicScope;
  }, [identity.user_role, isAdminUser]);

  const searchScopeOptions = useMemo(() => {
    const publicOption = { value: 'public', label: '公共知识库（五类公开库）' };
    const accessibleOption = { value: 'accessible', label: '可访问知识库（按当前身份）' };
    if (identity.user_role === 'internal' || isAdminUser) {
      return [
        publicOption,
        { value: 'internal', label: '我的内部拜访库（inner）' },
        { value: 'accessible', label: '公共库 + 我的私有库' },
      ];
    }
    return [publicOption, accessibleOption];
  }, [identity.user_role, isAdminUser, scopeOptions]);

  function navigate(path, nextPage = 'supervisor') {
    window.history.pushState({}, '', path);
    setRoute(path);
    setPage(nextPage);
    setIdentity((currentIdentity) => {
      if (path === '/admin') return { ...currentIdentity, user_role: 'admin' };
      if (currentIdentity.user_role === 'admin') {
        return { ...currentIdentity, user_role: 'internal' };
      }
      return currentIdentity;
    });
  }

  useEffect(() => {
    setTestUsersLoading(true);
    setTestUsersError('');
    api.listTestUsers()
      .then((users) => {
        if (!Array.isArray(users) || users.length === 0) return;
        setTestUsers(users);
        setIdentity((currentIdentity) => {
          if (isAdminPage) {
            return {
              ...currentIdentity,
              user_id: 'admin',
              name: '管理员',
              user_role: 'admin',
              user_type: 'admin',
              company: '平台管理员',
              phone: null,
            };
          }
          const current = users.find((user) => user.user_id === currentIdentity.user_id) || (isAdminPage ? null : users[0]);
          if (!current) return currentIdentity;
          const nextRole = current.is_inner_user ? 'internal' : 'external';
          return {
            ...currentIdentity,
            user_id: current.user_id,
            name: current.name || current.user_id,
            user_role: nextRole,
            user_type: current.user_type || null,
            company: current.company || null,
            phone: current.phone || null,
          };
        });
      })
      .catch((error) => {
        setTestUsersError(error.message || '无法读取 MySQL 用户表');
      })
      .finally(() => setTestUsersLoading(false));
    return undefined;
  }, [isAdminPage]);

  useEffect(() => {
    if (!scopeOptions.some((option) => option.value === scopeId)) setScopeId(scopeOptions[0].value);
  }, [scopeId, scopeOptions]);
  useEffect(() => {
    if (!searchScopeOptions.some((option) => option.value === searchScopeId)) {
      setSearchScopeId(searchScopeOptions[0].value);
    }
  }, [searchScopeId, searchScopeOptions]);

  useEffect(() => {
    const handlePopState = () => {
      const nextRoute = window.location.pathname.startsWith('/admin') ? '/admin' : '/';
      setRoute(nextRoute);
      setIdentity((currentIdentity) => {
        if (nextRoute === '/admin') return { ...currentIdentity, user_role: 'admin' };
        if (currentIdentity.user_role === 'admin') {
          return { ...currentIdentity, user_role: 'internal' };
        }
        return currentIdentity;
      });
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  function persist(next) {
    setRecords(next);
    // 解析正文、blocks、MinerU 产物和任务响应都可能很大，只保存在当前页面内存。
    // localStorage 只保存文件列表所需的轻量字段，采用白名单避免新增大字段再次超额。
    const compact = Object.fromEntries(Object.entries(next).map(([id, document]) => [
      id,
      Object.fromEntries(CACHE_FIELDS
        .filter((field) => document[field] !== undefined && document[field] !== null)
        .map((field) => [field, document[field]])),
    ]));
    try {
      localStorage.setItem(RECORD_KEY, JSON.stringify(compact));
    } catch (error) {
      // 清理本应用旧版本留下的同名大缓存后再尝试一次，不能影响上传/解析请求。
      try {
        localStorage.removeItem(RECORD_KEY);
        localStorage.setItem(RECORD_KEY, JSON.stringify(compact));
      } catch {
        console.warn('无法保存本地文件状态缓存，将仅保留当前页面状态。', error);
  }
}

  }

  function updateRecord(document, patch) {
    const id = document.document_id;
    const next = { ...records, [id]: { ...records[id], ...document, ...patch } };
    persist(next);
    const updated = next[id];
    // 当前进度面板绑定 selected/uploaded；操作过程中同步替换，
    // 不需要用户再次点击“查看”才能看到最新状态。
    setSelected((currentDocument) => currentDocument?.document_id === id ? updated : currentDocument);
    setUploaded((currentDocument) => currentDocument?.document_id === id ? updated : currentDocument);
    return updated;
  }

  async function refreshFilesNow({ refreshCounts = false, refreshTasks = false } = {}) {
    setFileLoading(true);
    try {
      const taskRequest = page === 'documents' && refreshTasks
        ? api.adminTasks({ page: 1, page_size: 100 }).catch(() => ({ items: supervisorTasks }))
        : Promise.resolve(null);
      const updatesRequest = page === 'overview'
        ? api.adminKnowledgeUpdates({ page: overviewUpdatePage, page_size: ADMIN_PAGE_SIZE, published_only: false })
        : Promise.resolve(null);
      const draftCatalogRequest = page === 'documents'
        ? api.adminKnowledgeUpdates({ page: draftCatalogPage, page_size: ADMIN_PAGE_SIZE, published_only: false, knowledge_type: catalogKnowledgeType || undefined })
        : Promise.resolve(null);
      const [response, overview, updatesResponse, draftCatalogResponse, policyResponse, taskResponse] = await Promise.all([
        api.adminDocuments({
          knowledge_type: catalogKnowledgeType || undefined,
          search: ['documents', 'upload'].includes(page) ? documentSearch || undefined : undefined,
          published_or_admin_only: page === 'upload',
          page: documentPage,
          page_size: DOCUMENT_PAGE_SIZE,
        }),
        api.adminOverview().catch(() => null), updatesRequest,
        draftCatalogRequest,
        api.adminFieldPolicies().catch(() => null),
        taskRequest,
      ]);
      setDocumentTotal(Number(response.total || 0));
      setDocumentTotalPages(Number(response.total_pages || 0));
      const lastDocumentPage = Math.max(1, Number(response.total_pages || 0));
      if (documentPage > lastDocumentPage) {
        setDocumentPage(lastDocumentPage);
      }
      const allTasks = Array.isArray(taskResponse?.items) ? taskResponse.items : [];
      if (taskResponse) {
        // 同步状态单独按需查询；暂停或完成后每个知识库只保留最新一条。
        const shownSyncTypes = new Set();
        setSupervisorTasks(allTasks.filter((task) => {
          if (task.operation !== 'mysql_sync') return true;
          if (!['STOPPED', 'SUCCESS', 'SUCCEEDED'].includes(task.status)) return false;
          if (shownSyncTypes.has(task.knowledge_type)) return false;
          shownSyncTypes.add(task.knowledge_type);
          return true;
        }));
        const syncByKnowledgeType = {};
        for (const task of allTasks) {
          if (task.operation === 'mysql_sync' && task.knowledge_type && !syncByKnowledgeType[task.knowledge_type]) {
            syncByKnowledgeType[task.knowledge_type] = task;
          }
        }
        setMysqlSync(syncByKnowledgeType);
      }
      setAdminOverview(overview);
      if (updatesResponse) {
        setKnowledgeUpdates(Array.isArray(updatesResponse.items) ? updatesResponse.items : []);
        setOverviewUpdateTotal(Number(updatesResponse.total || 0));
        setOverviewUpdateTotalPages(Number(updatesResponse.total_pages || 0));
        if (overviewUpdatePage > Math.max(1, Number(updatesResponse.total_pages || 0))) {
          setOverviewUpdatePage(Math.max(1, Number(updatesResponse.total_pages || 0)));
        }
      }
      if (draftCatalogResponse) {
        setDraftCatalog(Array.isArray(draftCatalogResponse.items) ? draftCatalogResponse.items : []);
        setDraftCatalogTotal(Number(draftCatalogResponse.total || 0));
        setDraftCatalogTotalPages(Number(draftCatalogResponse.total_pages || 0));
        if (draftCatalogPage > Math.max(1, Number(draftCatalogResponse.total_pages || 0))) {
          setDraftCatalogPage(Math.max(1, Number(draftCatalogResponse.total_pages || 0)));
        }
      }
      if (Array.isArray(policyResponse?.policies)) setAdminFieldPolicies(policyResponse.policies);
      if (refreshCounts) {
        setMysqlCounts({});
        try {
          const counts = await api.adminMysqlCounts(true);
          setMysqlCounts(counts?.counts || {});
        } catch (error) {
          setMysqlCounts({ __error: error?.message || 'MySQL 统计读取失败' });
        }
      }
      const taskByDocument = new Map();
      for (const task of allTasks) {
        // Supervisor 接口按 updated_at 倒序返回。同一文档可能存在历史任务，
        // 只保留列表中最先出现的最新任务，不能被旧 FAILED 任务覆盖。
        if (task.document_id && !taskByDocument.has(task.document_id)) {
          taskByDocument.set(task.document_id, task);
        }
      }
      const nextFiles = (response.documents || []).map((item) => {
        const task = taskByDocument.get(item.document_id) || null;
        const resolvedTask = task || records[item.document_id]?.supervisor_task || null;
        return {
          ...(records[item.document_id] || {}),
          ...item,
          ...item.active_version,
          knowledge_type: catalogKnowledgeType
            ? displayKnowledgeType({
              ...item,
              knowledge_type: item.knowledge_type || records[item.document_id]?.knowledge_type,
              ...item.active_version,
            })
            : String(item.knowledge_type || item.active_version?.knowledge_type || records[item.document_id]?.knowledge_type || ''),
          file_name: item.source_name || item.active_version?.source_name,
          file_size: item.active_version?.file_size,
          stage: catalogStage(item, resolvedTask),
          supervisor_task: resolvedTask,
        };
      });
      setFiles(nextFiles);
      const currentIds = new Set(nextFiles.map((item) => item.document_id));
      setSelected((current) => current && currentIds.has(current.document_id)
        ? { ...current, ...nextFiles.find((item) => item.document_id === current.document_id) }
        : null);
      setUploaded((current) => current && currentIds.has(current.document_id)
        ? { ...current, ...nextFiles.find((item) => item.document_id === current.document_id) }
        : null);
      setVersionsDocument((current) => current && currentIds.has(current.document_id)
        ? { ...current, ...nextFiles.find((item) => item.document_id === current.document_id) }
        : null);
    } catch (error) {
      setFiles([]);
      if (page === 'overview') setKnowledgeUpdates([]);
      if (page === 'documents') setDraftCatalog([]);
      setNotice(`无法读取管理台分页数据：${error.message}`);
    } finally { setFileLoading(false); }
  }

  async function refreshFiles(options = {}) {
    if (filesRefreshInFlightRef.current) {
      const queuedOptions = filesRefreshQueuedOptionsRef.current || {};
      filesRefreshQueuedOptionsRef.current = {
        refreshCounts: Boolean(queuedOptions.refreshCounts || options.refreshCounts),
        refreshTasks: Boolean(queuedOptions.refreshTasks || options.refreshTasks),
      };
      filesRefreshQueuedRef.current = () => refreshFiles(filesRefreshQueuedOptionsRef.current || {});
      return filesRefreshInFlightRef.current;
    }
    const request = refreshFilesNow(options);
    filesRefreshInFlightRef.current = request;
    try {
      return await request;
    } finally {
      if (filesRefreshInFlightRef.current === request) {
        filesRefreshInFlightRef.current = null;
        const queuedRefresh = filesRefreshQueuedRef.current;
        filesRefreshQueuedRef.current = null;
        if (queuedRefresh) {
          filesRefreshQueuedOptionsRef.current = null;
          void queuedRefresh();
        }
      }
    }
  }

  async function refreshHealth() {
    if (healthInFlightRef.current) return healthInFlightRef.current;
    const requestId = ++healthRequestRef.current;
    const request = (async () => {
      let error;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          const result = await api.health();
          if (requestId === healthRequestRef.current) setHealth(result);
          return;
        } catch (currentError) {
          error = currentError;
          if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 500));
        }
      }
      if (requestId === healthRequestRef.current) {
        setHealth({ status: 'degraded', services: {}, error: error?.message || '健康接口请求失败' });
      }
    })();
    healthInFlightRef.current = request;
    try {
      return await request;
    } finally {
      if (healthInFlightRef.current === request) healthInFlightRef.current = null;
    }
  }

  async function openKnowledgeUpdate(update) {
    setPage('documents');
    setKnowledgeUpdateDetailLoading(true);
    try {
      setKnowledgeUpdateDetail(await api.adminKnowledgeUpdateDetail(update.record_id));
      if (update.primary?.document_id) await toggleDocumentDetails(update.primary);
    } catch (error) {
      setNotice(`知识库更新详情读取失败：${error.message}`);
    } finally { setKnowledgeUpdateDetailLoading(false); }
  }

  useEffect(() => {
    personalRefreshRequestRef.current += 1;
    personalDocumentRequestRef.current += 1;
    setMyProfile(null);
    setMyDocuments([]);
    setMyConversations([]);
    setMyDrafts([]);
    setMyVisitRecords([]);
    setMyMatchResults([]);
    setPersonalDraftDetail(null);
    setPersonalDocumentDetail(null);
    setPersonalDocumentLoading(false);
    setPersonalLoading(false);
    setPersonalConversationId('');
    setPersonalConversationMessages([]);
  }, [identity.user_id]);

  useEffect(() => {
    if (route === '/' && page === 'personal') return;
    personalDocumentRequestRef.current += 1;
    setPersonalDocumentDetail(null);
    setPersonalDocumentLoading(false);
  }, [route, page]);

  useEffect(() => {
    const previous = adminDocumentContextRef.current;
    const enteringDocuments = route === '/admin' && page === 'documents'
      && (!previous || previous.route !== route || previous.page !== page || previous.userId !== identity.user_id);
    adminDocumentContextRef.current = { route, page, userId: identity.user_id };
    if (route === '/admin' && ['overview', 'documents', 'upload'].includes(page)) refreshFiles({ refreshCounts: enteringDocuments, refreshTasks: enteringDocuments });
    if (route === '/' && page === 'personal') refreshPersonal();
  }, [route, page, identity.user_id, documentPage, documentSearch, catalogKnowledgeType, overviewUpdatePage, draftCatalogPage]);

  function searchAdminDocuments(value) {
    setDocumentSearch(value);
    setDocumentPage(1);
  }

  useEffect(() => {
    if (route === '/admin') refreshHealth();
  }, [route, identity.user_id]);

  async function refreshPersonal() {
    if (!identity.user_id || isAdminPage) return;
    const requestId = ++personalRefreshRequestRef.current;
    setPersonalLoading(true);
    try {
      const canViewVisitRecords = ['internal', 'admin'].includes(identity.user_role);
      const sections = [
        ['个人资料', () => api.myProfile(identity.user_id), setMyProfile],
        ['文档', () => api.myDocuments(identity.user_id), value => setMyDocuments(value.documents || [])],
        ['会话', () => api.myConversations(identity.user_id), value => {
          const sessions = value.sessions || [];
          setMyConversations(sessions);
          setPersonalConversationId(currentId => sessions.some(item => item.thread_id === currentId) ? currentId : '');
          if (!sessions.length) setPersonalConversationMessages([]);
        }],
        ['草稿', () => api.myDrafts(identity.user_id), setMyDrafts],
        ['拜访记录', () => canViewVisitRecords ? api.myVisitRecords(identity.user_id) : Promise.resolve([]), setMyVisitRecords],
        ['匹配记录', () => api.myMatchResults(identity.user_id), setMyMatchResults],
      ];
      const results = await Promise.allSettled(sections.map(([, load]) => load()));
      if (requestId !== personalRefreshRequestRef.current) return;
      const failures = [];
      results.forEach((result, index) => {
        const [label, , update] = sections[index];
        if (result.status === 'fulfilled') update(result.value);
        else failures.push(`${label}：${result.reason?.message || '读取失败'}`);
      });
      if (failures.length) setNotice(`部分区域刷新失败，保留上次数据。${failures.join('；')}`);
      else setNotice(current => /^(部分区域刷新失败|个人中心读取失败)/.test(current || '') ? '' : current);
    } catch (error) {
      if (requestId === personalRefreshRequestRef.current) setNotice(`个人中心读取失败：${error.message}`);
    } finally {
      if (requestId === personalRefreshRequestRef.current) setPersonalLoading(false);
    }
  }

  async function syncMysqlTables(knowledgeType) {
    setBusy('mysql-sync');
    setNotice('正在读取 MySQL 业务表并重建公开检索索引，MySQL 原表不会被修改…');
    try {
      const result = await api.adminSyncMysql(knowledgeType);
      setMysqlSync((current) => ({ ...current, [knowledgeType]: { task_id: result.task_id, status: result.status || 'QUEUED', processed: result.processed || 0, total: result.total || 0, indexed_count: result.indexed_count || 0, batch_size: result.batch_size || 64 } }));
      setNotice(`MySQL ${knowledgeType} 表同步已开始，后端正在按批次处理。`);
      let status = null;
      for (let attempt = 0; attempt < 360; attempt += 1) {
        try {
          status = await api.adminMysqlSyncStatus(result.task_id);
        } catch (error) {
          if (error.status !== 429) throw error;
          const retryAfter = Math.max(1, Number(error.retryAfter) || 1);
          await new Promise((resolve) => setTimeout(resolve, retryAfter * 1000));
          continue;
        }
        setMysqlSync((current) => ({ ...current, [knowledgeType]: status }));
        setMysqlCounts((current) => ({
          ...current,
          [knowledgeType]: {
            ...current[knowledgeType],
            milvus_count: status.indexed_count ?? current[knowledgeType]?.milvus_count,
          },
        }));
        if (['SUCCESS', 'FAILED', 'STOPPED'].includes(status.status)) break;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      setNotice(status?.status === 'SUCCESS' ? `MySQL ${knowledgeType} 表同步完成，共处理 ${status.processed} 条，写入 ${status.indexed_count} 条向量。` : status?.status === 'STOPPED' ? `MySQL ${knowledgeType} 表已暂停，可继续同步。` : `MySQL ${knowledgeType} 表同步失败：${displayError(status?.error, '任务超时')}`);
      await refreshFiles({ refreshCounts: true });
    } catch (error) {
      setNotice(`MySQL 表同步失败：${error.message}`);
    } finally {
      setBusy('');
    }
  }

  async function stopMysqlSync(knowledgeType) {
    const taskId = mysqlSync?.[knowledgeType]?.task_id;
    if (!taskId || mysqlStopInFlightRef.current.has(knowledgeType)) return;
    mysqlStopInFlightRef.current.add(knowledgeType);
    setMysqlSync((current) => ({ ...current, [knowledgeType]: { ...current[knowledgeType], status: 'STOPPING' } }));
    setNotice(`已请求停止 MySQL ${knowledgeType} 表同步，当前记录完成后停止。`);
    try {
      const result = await api.adminStopMysqlSync(taskId);
      setMysqlSync((current) => ({ ...current, [knowledgeType]: { ...current[knowledgeType], ...result } }));
    } catch (error) {
      setMysqlSync((current) => ({ ...current, [knowledgeType]: { ...current[knowledgeType], status: 'RUNNING' } }));
      setNotice(`停止同步失败：${error.message}`);
    } finally {
      mysqlStopInFlightRef.current.delete(knowledgeType);
    }
  }

  async function rebuildMysqlData(knowledgeType) {
    if (!window.confirm(`确定只清理 ${knowledgeType} 的 MySQL 同步数据吗？用户和管理台文档会保留。`)) return;
    try {
      await api.adminRebuildMysqlData(knowledgeType);
      setNotice(`${knowledgeType} 的 MySQL 同步数据已清理，请手动重新同步。`);
      await refreshFiles({ refreshCounts: true });
    } catch (error) { setNotice(`清理 MySQL 同步数据失败：${error.message}`); }
  }

  function openPersonalConversation(sessionId) {
    if (!sessionId || !identity.user_id) return;
    localStorage.setItem(`${CHAT_SESSION_KEY}:${identity.user_id}`, sessionId);
    setPersonalConversationId(sessionId);
    setPersonalConversationMessages([]);
    navigate('/', 'supervisor');
  }

  async function openPersonalDocument(document) {
    if (!document?.document_id || !identity.user_id) return;
    const requestId = ++personalDocumentRequestRef.current;
    setPersonalDocumentLoading(true);
    setPersonalDocumentDetail({ document, parseResult: null });
    try {
      const [detail, parsed] = await Promise.allSettled([
        api.myDocumentDetail(identity.user_id, document.document_id),
        api.myDocumentParseResult(document.document_id),
      ]);
      if (requestId !== personalDocumentRequestRef.current) return;
      if (detail.status === 'rejected') throw detail.reason;
      const parseError = parsed.status === 'rejected' && parsed.reason?.status !== 404
        ? `解析结果读取失败：${parsed.reason?.message || '请求失败'}` : '';
      setPersonalDocumentDetail({ ...detail.value, parseResult: parsed.status === 'fulfilled' ? parsed.value : null, parseError });
    } catch (error) {
      if (requestId !== personalDocumentRequestRef.current) return;
      setNotice(`资料详情读取失败：${error.message}`);
      setPersonalDocumentDetail(null);
    } finally {
      if (requestId === personalDocumentRequestRef.current) setPersonalDocumentLoading(false);
    }
  }

  function closePersonalDocument() {
    personalDocumentRequestRef.current += 1;
    setPersonalDocumentDetail(null);
    setPersonalDocumentLoading(false);
  }

  async function unpublishMyDocument(document, reasonCode, reasonText) {
    if (reasonCode === 'OTHER' && !String(reasonText || '').trim()) {
      setNotice('选择其他时必须填写下架原因。');
      return;
    }
    if (!window.confirm(`确认下架“${document.source_name || document.file_name}”吗？`)) return;
    setBusy('unpublish');
    try {
      await api.myUnpublish(document.document_id, {
        user_id: identity.user_id, reason_code: reasonCode, reason_text: String(reasonText || '').trim(),
      });
      setNotice('资料已下架，管理员可以查看下架原因。');
      await refreshPersonal();
    } catch (error) { setNotice(`下架失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function republishMyDocument(document) {
    if (!document?.document_id || !window.confirm(`确认重新上架“${document.source_name || document.file_name}”吗？`)) return;
    setBusy('publish');
    try {
      await api.myPublish(document.document_id, { user_id: identity.user_id, note: '用户重新上架资料' });
      setNotice('资料已重新上架，可以参与检索。');
      await refreshPersonal();
    } catch (error) { setNotice(`重新上架失败：${error.message}`); }
    finally { setBusy(''); }
  }

  function openPersonalDraft(draft) {
    setPersonalDraftDetail(draft);
  }

  async function deletePersonalDraftAttachment(record, attachment) {
    if (!record?.draft_id || !attachment || record.status === 'PUBLISHED') return;
    if (!window.confirm(`确认从草稿中删除附件“${attachment.file_name || attachment.name || '未命名附件'}”吗？`)) return;
    setBusy('draft-attachment');
    try {
      const result = await api.removeDraftAttachment(record.draft_id, attachment);
      setPersonalDraftDetail(result);
      setNotice('附件已从当前草稿移除，原文件保留；其他草稿不受影响。');
      await refreshPersonal();
    } catch (error) { setNotice(`删除附件失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function deletePersonalDraft(record) {
    if (record?.status === 'PUBLISHED') {
      setNotice('已发布草稿必须先下架，才能删除。');
      return;
    }
    if (!record?.draft_id || !window.confirm(`确认从个人中心删除草稿“${record.draft_title || record.draft?.title || record.draft_id}”吗？删除后草稿和附件入口立即隐藏，关联资源由系统暂存，管理员可在管理台查看并按需彻底删除。`)) return;
    setBusy('draft-delete');
    try {
      await api.myDraftDelete(record.draft_id, identity.user_id);
      setPersonalDraftDetail(null);
      setNotice('草稿和附件入口已从个人中心隐藏；关联资源由系统暂存，管理员可在管理台查看并按需彻底删除。');
      await refreshPersonal();
    } catch (error) { setNotice(`删除草稿失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function deletePersonalVisitRecord(record) {
    const recordId = record?.business_record_id || record?.record_id;
    if (!recordId || !window.confirm('确认删除这条拜访记录吗？删除后将不能参与检索。')) return;
    setBusy('visit-delete');
    try {
      await api.deleteVisitRecord(recordId, identity.user_id);
      setNotice('拜访记录及其私密检索向量已删除。');
      await refreshPersonal();
    } catch (error) { setNotice(`删除拜访记录失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function unpublishMyDraft(draft, reasonCode, reasonText) {
    const selectedReason = reasonCode || 'NEEDS_REVISION';
    if (reasonCode === 'OTHER' && !String(reasonText || '').trim()) {
      setNotice('选择其他时必须填写下架原因。');
      return;
    }
    if (!window.confirm(`确认下架草稿“${draft?.draft?.title || draft?.draft_id}”吗？`)) return;
    setBusy('unpublish');
    try {
      const result = await api.myDraftUnpublish(draft.draft_id, {
        user_id: identity.user_id,
        reason_code: selectedReason,
        reason_text: String(reasonText || '').trim(),
      });
      const withdrawnDraft = { ...(result.draft || draft), status: 'WITHDRAWN' };
      setPersonalDraftDetail(withdrawnDraft);
      setMyDrafts((current) => current.map((item) => (
        item.draft_id === draft.draft_id ? { ...item, ...withdrawnDraft, status: 'WITHDRAWN' } : item
      )));
      setNotice('下架成功：已下架，当前不再参与检索。');
      await refreshPersonal();
    } catch (error) { setNotice(`草稿下架失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function republishMyDraft(draft) {
    if (!draft?.draft_id || !window.confirm(`确认重新上架草稿“${draft?.draft?.title || draft.draft_id}”吗？`)) return;
    setBusy('publish');
    try {
      const result = await api.myDraftRepublish(draft.draft_id, { user_id: identity.user_id });
      const publishedDraft = result.draft || { ...draft, status: 'PUBLISHED' };
      setPersonalDraftDetail(publishedDraft);
      setMyDrafts((current) => current.map((item) => (
        item.draft_id === draft.draft_id ? { ...item, ...publishedDraft, status: 'PUBLISHED' } : item
      )));
      setNotice('草稿已重新上架，当前可以参与检索。');
      await refreshPersonal();
    } catch (error) { setNotice(`重新上架失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function unpublishAdminDraft(draft) {
    if (!draft?.draft_id || !window.confirm(`确认下架草稿“${draft.title || draft.draft_id}”吗？`)) return;
    setBusy('unpublish');
    try {
      const result = await api.adminDraftUnpublish(draft.draft_id, {
        user_id: draft.user_id || knowledgeUpdateDetail?.user_id || identity.user_id,
        operator_id: identity.user_id,
        reason_code: 'NEEDS_REVISION',
        reason_text: '管理控制台下架',
      });
      const updated = result.draft || { ...draft, status: 'WITHDRAWN' };
      setKnowledgeUpdateDetail((current) => current ? {
        ...current,
        status: 'WITHDRAWN',
        drafts: (current.drafts || []).map((item) => item.draft_id === draft.draft_id ? { ...item, ...updated, status: 'WITHDRAWN' } : item),
      } : current);
      setNotice('草稿已下架，相关向量已停止检索。');
      await refreshFiles();
    } catch (error) { setNotice(`草稿下架失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function refreshAdminKnowledgeUpdate() {
    await refreshFiles();
    if (knowledgeUpdateDetail?.record_id) {
      setKnowledgeUpdateDetail(await api.adminKnowledgeUpdateDetail(knowledgeUpdateDetail.record_id));
    }
  }

  async function republishAdminDraft(draft) {
    if (!draft?.draft_id || !window.confirm(`确认重新上架草稿“${draft.title || draft.draft_id}”吗？`)) return;
    setBusy('publish');
    try {
      const result = await api.myDraftRepublish(draft.draft_id, { user_id: draft.user_id || knowledgeUpdateDetail?.user_id || identity.user_id, operator_id: identity.user_id });
      const updated = result.draft || { ...draft, status: 'PUBLISHED' };
      setKnowledgeUpdateDetail((current) => current ? {
        ...current,
        status: 'PUBLISHED',
        drafts: (current.drafts || []).map((item) => item.draft_id === draft.draft_id ? { ...item, ...updated, status: 'PUBLISHED' } : item),
      } : current);
      setNotice('草稿已重新上架，当前可以参与检索。');
      await refreshFiles();
    } catch (error) { setNotice(`草稿重新上架失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function deleteAdminDraft(draft) {
    if (!draft?.draft_id || !window.confirm(`确认删除草稿“${draft.title || draft.draft_id}”及其关联数据吗？`)) return;
    setBusy('draft-delete');
    try {
      await api.adminDraftDelete(draft.draft_id);
      setKnowledgeUpdateDetail(null);
      setNotice('草稿及其关联 PostgreSQL、Milvus、MinIO 数据已清理。');
      await refreshFiles();
    } catch (error) { setNotice(`草稿删除失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function purgeAdminDraft(draft) {
    if (!draft?.draft_id || !window.confirm(`确认删除草稿记录“${draft.title || draft.draft_title || draft.draft_id}”吗？删除后不可恢复。`)) return;
    setBusy('draft-purge');
    try {
      await api.adminDraftDelete(draft.draft_id);
      setKnowledgeUpdateDetail(null);
      setNotice('草稿记录已删除。');
      await refreshFiles();
    } catch (error) { setNotice(`草稿记录删除失败：${error.message}`); }
    finally { setBusy(''); }
  }

  function chooseFile(event) {
    const selectedFile = event.target.files?.[0] || null;
    setFile(selectedFile);
    setUploaded(null); setParsed(null); setParsedDirty(false);
    if (!uploadTarget) setSelected(null);
    if (selectedFile && uploadTarget) setNotice(`已选择替换文件“${selectedFile.name}”。点击“上传替换版本”后将重新解析并建立索引。`);
  }

  function clearDocumentSelection() {
    setSelected(null);
    setUploaded(null);
    setParsed(null);
    setParsedDirty(false);
    setIndexPreview(null);
    setUploadTarget(null);
    setFile(null);
    setVersionsDocument(null);
    setVersions([]);
  }

  function switchCatalog(nextKnowledgeType) {
    if (nextKnowledgeType === catalogKnowledgeType) return;
    setKnowledgeUpdateDetail(null);
    setKnowledgeUpdateDetailLoading(false);
    setCatalogKnowledgeType(nextKnowledgeType);
    setDocumentPage(1);
    setDraftCatalogPage(1);
    if (nextKnowledgeType) setKnowledgeType(nextKnowledgeType);
    clearDocumentSelection();
    setNotice(nextKnowledgeType
      ? `已切换到${knowledgeTypes.find(([value]) => value === nextKnowledgeType)?.[1] || '当前'}，仅显示该库文档。`
      : '已切换到全部知识库文档。');
  }

  async function upload() {
    if (!file) return setNotice('请先选择一份文件。');
    const replacingDocument = uploadTarget?.document_id ? { ...uploadTarget } : null;
    const uploadKnowledgeType = knowledgeType || catalogKnowledgeType;
    setBusy('upload'); setNotice('正在保存原始文件到 MinIO…');
    try {
      const response = await api.adminUpload(file, scopeId, identity.user_id, uploadKnowledgeType, createUuid(), replacingDocument?.document_id || '');
      if (!response.task_id || !response.storage_key) throw new Error(response.error || '上传成功但未返回 Supervisor 任务或 MinIO 对象地址');
      const document = updateRecord(
        { ...response, supervisor_task_id: response.task_id, scope_id: scopeId, user_id: identity.user_id, knowledge_type: uploadKnowledgeType },
        { stage: 'upload', status: '已上传，等待解析' },
      );
      setUploaded(document); setSelected(document); setUploadTarget(null);
      setFiles((current) => [document, ...current.filter((item) => item.document_id !== document.document_id)]);
      setNotice(replacingDocument ? '新版本上传完成，正在重新解析…' : '上传完成。下一步可调用 Perception 解析。');
      await refreshFiles();
      if (replacingDocument) await parse(document);
    } catch (error) { setNotice(`上传失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function selectStoredFile(document) {
    if (document.knowledge_type) setKnowledgeType(document.knowledge_type);
    const task = document.supervisor_task || supervisorTasks.find((item) => item.document_id === document.document_id);
    const selectedDocument = document.storage_key
      ? { ...document, ...(task || {}), ...(task ? taskStage(task) : {}), supervisor_task: task || document.supervisor_task, supervisor_task_id: task?.task_id || document.supervisor_task_id }
      : { ...document, stage: 'legacy', status: '需重新上传原文件' };
    setSelected(selectedDocument); setUploaded(null); void loadVersions(selectedDocument);
    const localParsed = document.parsed_blocks?.length ? {
      status: 'SUCCESS',
      document_id: document.document_id,
      source_name: document.file_name,
      blocks: document.parsed_blocks,
      raw_text: document.parsed_raw_text || '',
      metadata: document.parsed_metadata || {},
      edited: Boolean(document.parsed_edited),
    } : null;
    setParsed(localParsed);
    setParsedDirty(false);
    setFile(null);
    if (localParsed || task?.parsed_result) {
      if (!localParsed) setParsed(task.parsed_result);
      if (task?.status === 'FAILED') {
        setNotice(`已选择 ${document.file_name}。上次任务失败：${task.error_code || 'UNKNOWN'} ${displayError(task.error, '未提供错误详情')}。可修正后重试。`);
      } else {
        setNotice(`已选择 ${document.file_name}。解析结果已恢复，可编辑后重新建立索引。`);
      }
      return;
    }
    if (['WAITING_CONFIRM', 'WAITING_DRAFT_CONFIRM', 'INDEXING', 'SUCCEEDED'].includes(task?.status)) {
      try {
        const parsedResponse = await api.adminParseResult(document.document_id);
        const restored = updateRecord(selectedDocument, {
          ...task,
          supervisor_task_id: task.task_id,
          ...taskStage(task),
          block_count: parsedResponse.blocks?.length || 0,
          parsed_blocks: parsedResponse.blocks || [],
          parsed_raw_text: parsedResponse.raw_text || '',
          parsed_metadata: parsedResponse.metadata || {},
          parsed_edited: Boolean(parsedResponse.edited),
        });
        setParsed(parsedResponse);
        setParsedDirty(false);
        setSelected(restored);
        setNotice(task.status === 'SUCCEEDED'
          ? `已选择 ${document.file_name}。索引已完成，解析结果已恢复，共 ${parsedResponse.blocks?.length || 0} 个文档块。`
          : `已选择 ${document.file_name}。解析已完成，共 ${parsedResponse.blocks?.length || 0} 个文档块，请确认后建立索引。`);
        return;
      } catch (error) {
        setNotice(`解析已完成，但读取解析结果失败：${error.message}`);
        return;
      }
    }
    if (!document.storage_key) {
      setNotice('该记录缺少 MinIO 对象地址，是旧记录，请重新上传该文件。');
      return;
    }
    setNotice(task ? `已选择 ${document.file_name}。可以继续 Supervisor 任务：开始解析。` : `该文件没有关联的 Supervisor 任务，请重新上传后再处理。`);
  }

  async function toggleDocumentDetails(document) {
    if (versionsDocument?.document_id === document.document_id) {
      clearDocumentSelection();
      return;
    }
    navigate('/admin', 'upload');
    setVersionsDocument(document);
    await openDocumentForUpdate(document);
  }

  async function openDocumentForUpdate(document) {
    await selectStoredFile(document);
    window.requestAnimationFrame(() => window.document.querySelector('.preview-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  }

  async function openIndexPreview(document) {
    setIndexPreview({ document, parsed: null, loading: true, error: '' });
    try {
      // 先恢复当前文档的选择状态和版本信息，再读取服务端保存的完整解析结果。
      await selectStoredFile(document);
      const parsedResponse = await api.adminParseResult(document.document_id);
      if (!parsedResponse?.blocks) throw new Error('当前文档没有可用的解析块');
      setParsed(parsedResponse);
      setParsedDirty(false);
      setIndexPreview({ document, parsed: parsedResponse, loading: false, error: '' });
    } catch (error) {
      setIndexPreview({ document, parsed: null, loading: false, error: error.message });
      setNotice(`读取 Perception 解析预览失败：${error.message}`);
    }
  }

  async function uploadAndParseFromChat(selectedFile, chatSessionId = '', message = '', intentHint = '', attachmentMode = 'new', knowledgeType = '', options = {}) {
    if (!selectedFile) throw new Error('请先选择要解析的文件。');
    setBusy('chat-upload');
    setNotice('正在通过用户会话入口上传材料…');
    const onMaterialEvent = (event, data) => {
      options.onEvent?.(event, data);
      if (event === 'file_received') setNotice(`已上传：${data?.file_name || selectedFile.name}`);
      else if (event === 'file_parse_started') setNotice('正在解析文档，请稍候…');
      else if (event === 'text_cleaning_started') setNotice('正在清洗原文，请稍候…');
      else if (event === 'text_cleaned') setNotice('原文清洗完成，正在生成草稿…');
      else if (event === 'document_summary') {
        setNotice('文档内容已提取，正在检索需求库…');
      }
      else if (event === 'retrieval_started') setNotice('正在根据解析内容检索知识库…');
      else if (event === 'processing') setNotice('正在处理文档，请稍候…');
    };
    try {
      const parsedResponse = await api.chatMaterialParse(
        selectedFile, identity.user_id, chatSessionId, message, intentHint, attachmentMode, knowledgeType,
        { onEvent: onMaterialEvent, onToken: options.onToken, debug: true },
      );
      setNotice(`材料解析完成，已分 ${parsedResponse.segment_count || 0} 段交给 LLM，草稿预览将显示在当前聊天中。`);
      return parsedResponse;
    } catch (error) {
      setNotice(`上传或解析失败：${error.message}`);
      throw error;
    } finally { setBusy(''); }
  }

  async function resolveSupervisorDocument(document) {
    if (!document?.document_id) return document;
    const currentTaskId = document.supervisor_task?.task_id || document.supervisor_task_id;
    if (currentTaskId) {
      try {
        const currentTask = await api.adminTask(currentTaskId);
        const resolved = { ...document, ...currentTask, supervisor_task: currentTask, supervisor_task_id: currentTask.task_id };
        if (document.draft_previews?.length) {
          resolved.draft_previews = document.draft_previews;
          resolved.draft_preview = document.draft_preview || document.draft_previews[0];
        }
        setSelected((item) => item?.document_id === document.document_id ? resolved : item);
        setUploaded((item) => item?.document_id === document.document_id ? resolved : item);
        return resolved;
      } catch {
        // 任务可能在清库、重启或保留期清理后失效，按文档重新找最新任务。
      }
    }
    try {
      const taskCatalog = await api.adminTasks({ document_id: document.document_id, page: 1, page_size: 100 });
      const task = (Array.isArray(taskCatalog.items) ? taskCatalog.items : [])
        .find((item) => item.document_id === document.document_id);
      if (!task) return document;
      const resolved = { ...document, ...task, supervisor_task: task, supervisor_task_id: task.task_id };
      if (document.draft_previews?.length) {
        resolved.draft_previews = document.draft_previews;
        resolved.draft_preview = document.draft_preview || document.draft_previews[0];
      }
      setSelected((item) => item?.document_id === document.document_id ? resolved : item);
      setUploaded((item) => item?.document_id === document.document_id ? resolved : item);
      return resolved;
    } catch (error) {
      throw new Error(`无法读取当前 Supervisor 任务：${error.message}`);
    }
  }

  async function parse(documentOverride = null) {
    let documentToParse = documentOverride || uploaded || selected;
    if (documentToParse?.document_id) {
      try { documentToParse = await resolveSupervisorDocument(documentToParse); }
      catch (error) { return setNotice(error.message); }
    }
    if (!documentToParse?.document_id || !documentToParse?.storage_key || !documentToParse?.supervisor_task_id) return setNotice('当前文件没有可用的 Supervisor 任务，请重新上传该文件后再解析。');
    setParsed(null);
    setParsedDirty(false);
    parsedDraftRef.current = '';
    setGeneratedChunks([]);
    setIndexPreview(null);
    setBusy('parse'); setNotice('Perception 正在解析原文…');
    documentToParse = updateRecord(documentToParse, {
      ...reparseResetState(),
      stage: 'parse',
      status: 'Perception 解析中',
    });
    const startedAt = performance.now();
    try {
      const response = await parseSupervisorTask(documentToParse);
      const parseDurationMs = Math.round(performance.now() - startedAt);
      const completedDocument = updateRecord(documentToParse, { parse_duration_ms: parseDurationMs });
      setUploaded(completedDocument); setSelected(completedDocument);
      setNotice(`解析原文已完成（${response.blocks?.length || 0} 个内容块），可继续点击“字段提取”。`);
    } catch (error) {
      const parseDurationMs = Math.round(performance.now() - startedAt);
      const failedDocument = updateRecord(documentToParse, { parse_duration_ms: parseDurationMs });
      setUploaded(failedDocument); setSelected(failedDocument);
      const message = displayError(error);
      const document = updateRecord(failedDocument, { stage: 'failed', status: '解析失败', error: message });
      setUploaded(document); setSelected(document); setNotice(`解析失败：${message}`);
    } finally { setBusy(''); }
  }

  async function extractFields() {
    if (parsedDirty) return setNotice('请先点击“保存原文”或“保存分块”，再提取字段。');
    let documentToExtract = uploaded || selected;
    if (documentToExtract?.document_id) {
      try { documentToExtract = await resolveSupervisorDocument(documentToExtract); }
      catch (error) { return setNotice(error.message); }
    }
    const taskId = documentToExtract?.supervisor_task?.task_id || documentToExtract?.supervisor_task_id;
    if (!documentToExtract?.document_id || !taskId || !parsed) return setNotice('请先点击“开始解析”完成原文解析。');
    const extractionKnowledgeType = documentToExtract.knowledge_type || documentToExtract.supervisor_task?.knowledge_type;
    updateRecord(documentToExtract, {
      draft_status: 'RUNNING',
      draft_id: null,
      draft_ids: [],
      draft_preview: null,
      draft_previews: [],
      draft_error_code: null,
      draft_error: null,
      draft_duration_ms: null,
      result_status: null,
    });
    setBusy('fields'); setNotice('LLM 正在提取字段…');
    const startedAt = performance.now();
    try {
      await api.adminExtractFields(taskId, extractionKnowledgeType);
      const task = await waitForSupervisorTask(taskId, ['WAITING_DRAFT_CONFIRM', 'WAITING_CONFIRM', 'FAILED'], documentToExtract);
      if (task.status === 'FAILED') throw new Error(displayError(task.error, '字段提取失败'));
      const ready = await pollAdminDraft(taskId, documentToExtract);
      if (ready?.draft_status === 'FAILED') throw new Error(draftFailureMessage(ready));
      const updated = updateRecord(documentToExtract, { ...ready, supervisor_task_id: taskId, draft_duration_ms: Math.round(performance.now() - startedAt) });
      setUploaded(updated); setSelected(updated);
      if (ready?.draft_status === 'EMPTY') return setNotice('未指定知识类型，字段保持为空。');
      setNotice('LLM 字段草稿已生成，请检查后确认写入。');
    } catch (error) {
      setNotice(`字段提取失败：${displayError(error)}`);
    } finally { setBusy(''); }
  }

  async function confirmIndex(documentOverride = null, forceReindex = false, confirmMode = 'all') {
    let documentToIndex = documentOverride || uploaded || selected;
    if (documentToIndex?.document_id) {
      try { documentToIndex = await resolveSupervisorDocument(documentToIndex); }
      catch (error) { return setNotice(error.message); }
    }
    // supervisor_task 是服务端按更新时间选出的当前任务，优先于旧缓存里的 task_id。
    const taskId = documentToIndex?.supervisor_task?.task_id || documentToIndex?.supervisor_task_id;
    if (!parsed || !documentToIndex || !taskId) return setNotice('请先通过 Supervisor 完成当前文件的解析。');
    if (parsedDirty) return setNotice('请先点击“保存原文”或“保存分块”，再发布草稿或写入索引。');
    const taskDrafts = documentToIndex?.supervisor_task || {};
    const draftPreviews = documentToIndex?.draft_previews || documentToIndex?.draft_preview
      ? (documentToIndex.draft_previews || [documentToIndex.draft_preview])
      : (taskDrafts.draft_previews || (taskDrafts.draft_preview ? [taskDrafts.draft_preview] : []));
    const shouldWriteDrafts = confirmMode !== 'chunks';
    // 字段确认是唯一正式写入入口；若用户已保存分块预览，随草稿一起提交，
    // 由后端先写 MySQL 取得真实 mysql_id，再统一写入 Milvus。
    const shouldWriteChunks = confirmMode !== 'draft' || generatedChunks.length > 0;
    if (shouldWriteDrafts && !draftPreviews.length) return setNotice('请先生成 LLM 字段草稿。');
    if (shouldWriteChunks && confirmMode === 'chunks' && !generatedChunks.length) return setNotice('请先保存解析原文并生成分块。');
    const draftPublished = draftPreviews.length > 0 && (
      draftPreviews.every((draft) => draft.status === 'PUBLISHED' || draft._ui_published)
      || documentToIndex.result_status === 'PUBLISHED'
      || documentToIndex.supervisor_task?.result_status === 'PUBLISHED'
    );
    const draftDirty = draftPreviews.some((draft) => draft._ui_dirty);
    if (confirmMode === 'draft' && draftPublished && !draftDirty) {
      return setNotice('当前草稿已发布，修改内容后才能再次发布。');
    }
    if (confirmMode === 'all' && currentStage === 'index' && !parsedDirty && !forceReindex) {
      setNotice('当前草稿没有修改，无需更新索引。');
      return;
    }
    const chunks = shouldWriteChunks ? generatedChunks.map((block, index) => toIndexChunk(block, index, documentToIndex.document_id)) : [];
    const drafts = shouldWriteDrafts ? draftPreviews.map(({ _ui_published, _ui_dirty, ...draft }) => draft) : [];
    if (!chunks.length && !drafts.length) return setNotice('当前没有可写入的草稿或文本块。');
    setBusy('index'); setNotice(confirmMode === 'draft'
      ? `正在提交 ${drafts.length} 条字段草稿，准备写入 MySQL + Milvus…`
      : `正在提交 ${chunks.length} 个文档块，准备写入 Milvus…`);
    updateRecord(documentToIndex, { stage: 'index', status: 'Retrieval 索引中' });
    try {
      const accepted = await api.adminConfirmTask(taskId, {
        chunks,
        drafts,
        images: parsed?.metadata?.images || [],
        images_confirmed: true,
        identity,
      });
      if (accepted.status === 'FAILED') {
        const code = accepted.error_code ? `[${accepted.error_code}] ` : '';
        throw new Error(`${code}${displayError(accepted.error, '索引任务提交失败')}`);
      }
      const response = await waitForSupervisorTask(taskId, ['SUCCEEDED', 'FAILED']);
      if (response.status !== 'SUCCEEDED') {
        const code = response.error_code ? `[${response.error_code}] ` : '';
        throw new Error(`${code}${displayError(response.error, '索引失败')}`);
      }
      const persistedChunks = response.indexed_chunks || response.index_result?.indexed_chunks;
      if (Array.isArray(persistedChunks) && persistedChunks.length) {
        // 后端返回的是按 Milvus 实际存储值确认过的块，覆盖前端草稿显示。
        setGeneratedChunks(persistedChunks.map((chunk, index) => ({
          block_id: chunk.source_chunk_id || `${documentToIndex.document_id}:chunk:${index}`,
          text: chunk.chunk_text || '',
          page_number: chunk.metadata?.page_number,
          metadata: chunk.metadata || {},
          source_chunk_id: chunk.source_chunk_id,
          proposed_visibility: chunk.visibility || 'public',
          source_locator: chunk.source_locator,
        })));
      }
      const publishedDrafts = (response.draft_previews || draftPreviews).map((draft) => ({
        ...draft, status: 'PUBLISHED', _ui_published: true, _ui_dirty: false,
      }));
      const document = updateRecord(documentToIndex, {
        ...response, supervisor_task_id: taskId, stage: 'index', status: '已建立索引',
        indexed_count: response.indexed_count || 0, parsed_blocks: parsed.blocks || [],
        draft_previews: publishedDrafts, draft_preview: publishedDrafts[0], result_status: 'PUBLISHED',
      });
      const indexedChunks = response.indexed_chunks || response.index_result?.indexed_chunks || [];
      const attachmentCount = indexedChunks.filter((chunk) => chunk.chunk_role === 'admin_doc').length;
      const fieldCount = indexedChunks.filter((chunk) => chunk.chunk_role === 'admin_draft').length;
      const detail = attachmentCount || fieldCount
        ? `（${attachmentCount} 个附件分块${fieldCount ? ` + ${fieldCount} 条字段索引` : ''}）`
        : '';
      setUploaded(document); setSelected(document); setNotice(`完成：已写入 ${response.indexed_count || 0} 条索引${detail}。`);
      setParsedDirty(false);
      if (indexPreview?.document?.document_id === document.document_id) setIndexPreview(null);
    } catch (error) {
      const document = updateRecord(documentToIndex, {
        stage: 'failed', status: '索引失败', error: error.message,
        error_code: error.code || undefined,
      });
      setUploaded(document); setSelected(document); setNotice(`索引失败：${error.message}`);
    } finally { setBusy(''); }
  }

  function updateDocument(document) {
    setUploadTarget(document);
    navigate('/admin', 'upload');
    setScopeId(document.scope_id || scopeId);
    if (document.knowledge_type) setKnowledgeType(document.knowledge_type);
    setSelected(document); setUploaded(null); setParsed(null); setFile(null);
    setNotice(`请重新选择“${document.file_name}”。上传后会创建替换版本，再重新解析并建立索引。`);
    window.requestAnimationFrame(() => fileInputRef.current?.click());
  }

  function updateAdminDraft(index, field, value) {
    const document = uploaded || selected;
    const taskDrafts = document?.supervisor_task || {};
    const existing = document?.draft_previews || (document?.draft_preview ? [document.draft_preview] : (taskDrafts.draft_previews || (taskDrafts.draft_preview ? [taskDrafts.draft_preview] : [])));
    if (!document || !existing[index]) return;
    const drafts = existing.map((draft, draftIndex) => draftIndex === index
      ? { ...draft, fields: { ...draft.fields, [field]: value }, ...(field === 'title' ? { title: value } : {}), _ui_dirty: true, _ui_published: false }
      : draft);
    updateRecord(document, { draft_previews: drafts, draft_preview: drafts[0] });
  }

  function updateParsedBlock(index, text) {
    if (!parsed) return;
    const sourceIndex = previewBlocks[index]?.preview_source_block_index ?? index;
    const blocks = parsed.blocks.map((block, blockIndex) => blockIndex === sourceIndex ? { ...block, text } : block);
    const currentDocument = uploaded || selected;
    setParsed({ ...parsed, blocks });
    setParsedDirty(true);
    if (currentDocument) updateRecord(currentDocument, {
      parsed_blocks: blocks,
      parsed_metadata: parsed.metadata || {},
    });
  }

  function updateParsedRawText(text) {
    if (!parsed) return;
    parsedDraftRef.current = text;
    setParsed((currentParsed) => currentParsed ? { ...currentParsed, raw_text: text, text, edited: true } : currentParsed);
    setParsedDirty(true);
    const currentDocument = uploaded || selected;
    if (currentDocument) updateRecord(currentDocument, {
      parsed_raw_text: text,
      parsed_blocks: parsed.blocks || [],
      parsed_metadata: parsed.metadata || {},
    });
  }

  function updateParsedDraft(text) {
    parsedDraftRef.current = text;
    setParsedDirty(true);
  }

  async function saveParsedRawText() {
    const documentId = parsed?.document_id || (uploaded || selected)?.document_id;
    const rawText = removeBlankLines(parsedDraftRef.current ?? parsed?.raw_text ?? '');
    if (!documentId || busy) return null;
    setBusy('save-parse');
    try {
      const saved = await api.saveParseResult(documentId, rawText, { clear_saved_chunks: true });
      const savedText = removeBlankLines(saved.raw_text ?? rawText);
      parsedDraftRef.current = savedText;
      setParsed((currentParsed) => currentParsed ? { ...currentParsed, ...saved, raw_text: savedText, text: savedText } : currentParsed);
      const currentDocument = uploaded || selected;
      if (currentDocument) updateRecord(currentDocument, { parsed_raw_text: savedText, parsed_edited: true });
      setParsedDirty(false);
      setNotice('解析原文已保存，刷新页面后仍会保留。');
      return savedText;
    } catch (error) {
      setNotice(`解析原文保存失败：${error.message}`);
      return null;
    } finally {
      setBusy('');
    }
  }

  async function saveGeneratedChunks() {
    const documentId = parsed?.document_id || (uploaded || selected)?.document_id;
    const { rawText, options } = buildSavedChunksParseUpdate(
      { ...parsed, raw_text: parsedDraftRef.current ?? parsed?.raw_text },
      generatedChunks,
    );
    if (!documentId || !rawText || busy) return;
    setBusy('save-parse');
    try {
      const saved = await api.saveParseResult(documentId, rawText, options);
      const savedText = removeBlankLines(saved.raw_text || rawText);
      parsedDraftRef.current = savedText;
      setParsed((currentParsed) => currentParsed ? { ...currentParsed, ...saved, raw_text: savedText, text: savedText, saved_chunks: generatedChunks } : currentParsed);
      const currentDocument = uploaded || selected;
      if (currentDocument) updateRecord(currentDocument, { parsed_raw_text: savedText, parsed_edited: true, saved_chunks: generatedChunks });
      setParsedDirty(false);
      setNotice('分块已保存，并已同步更新解析原文；后续字段提取使用已保存内容。');
    } catch (error) {
      setNotice(`分块内容保存失败：${error.message}`);
    } finally {
      setBusy('');
    }
  }

  function updateParsedSegment(blockIndex, segments, segmentIndex, text) {
    updateParsedBlock(blockIndex, segments.map((segment, index) => index === segmentIndex ? text : segment).join('\n\n'));
  }

  function updateParsedVisibility(index, visibility) {
    if (!parsed) return;
    const sourceIndex = previewBlocks[index]?.preview_source_block_index ?? index;
    const blocks = parsed.blocks.map((block, blockIndex) => (
      blockIndex === sourceIndex
        ? { ...block, proposed_visibility: visibility }
        : block
    ));
    const next = { ...parsed, blocks };
    const currentDocument = uploaded || selected;
    setParsed(next);
    setParsedDirty(true);
    if (currentDocument) updateRecord(currentDocument, { parsed_blocks: blocks, parsed_metadata: next.metadata || {} });
  }

  async function generateChunks() {
    if (!parsed) return setNotice('当前没有可生成的解析内容。');
    if (parsedDirty) return setNotice('请先点击“保存原文”后再分块，分块将使用已保存的最新原文。');
    const rawText = removeBlankLines(parsed.raw_text ?? parsed.text ?? '');
    if (!rawText.trim()) return setNotice('当前没有可生成的解析内容。');
    const knowledgeTypeValue = current?.knowledge_type || parsed.metadata?.knowledge_type || knowledgeType;
    if (!['enterprise', 'demand', 'achievement', 'expert', 'solution'].includes(knowledgeTypeValue)) {
      return setNotice('请先选择有效的知识类型后再分块。');
    }
    if (['demand', 'solution', 'achievement'].includes(knowledgeTypeValue)) {
      // 保存后的原文是唯一正文来源；旧块仅保留来源和页码等元数据。
      // 即使原文已经保存过，也要按原始 PPT 块重新映射；不能把第一块的
      // image_ids 复制到整份原文，否则后端分块时每个 chunk 都会带全部图片。
      const editedBlocks = applyEditedTextToSolutionBlocks(
        parsed.blocks || [], rawText, parsed.page_texts, parsed.metadata?.images,
      );
      const chunks = editedBlocks.filter((block) => String(block.text || '').trim());
      if (!chunks.length) return setNotice('当前解析内容没有可生成的分块。');
      const document = current || {};
      setBusy('chunk');
      try {
        const persisted = await api.retrievalChunkPreview({
          document_id: parsed.document_id || document.document_id || 'solution',
          scope_id: document.scope_id || scopeId,
          knowledge_type: knowledgeTypeValue,
          blocks: chunks,
          identity,
        });
        const visibleChunks = (persisted || []).filter((chunk) => chunk.visibility !== 'internal_supplement').map((chunk, index) => ({
          block_id: chunk.source_chunk_id || `${document.document_id || 'solution'}:chunk:${index}`,
          text: chunk.chunk_text || '',
          page_number: chunk.metadata?.page_start,
          metadata: chunk.metadata || {},
          source_chunk_id: chunk.source_chunk_id,
          proposed_visibility: chunk.visibility || 'public',
          source_locator: chunk.source_locator,
        }));
        setGeneratedChunks(visibleChunks);
        setParsedDirty(visibleChunks.length > 0);
        return setNotice(`已按 Retrieval 正式规则生成 ${(persisted || []).length} 个${knowledgeTypeValue === 'demand' ? '需求' : knowledgeTypeValue === 'achievement' ? '成果' : '技术方案'}分块；确认后将写入 Milvus。`);
      } catch (error) {
        return setNotice(`分块预览失败：${error.message}`);
      } finally { setBusy(''); }
    }
    const sourceBlocks = [{
      ...(previewBlocks[0] || {}),
      block_id: `${parsed.document_id || current?.document_id || 'document'}:edited-source`,
      text: rawText.trim(),
      metadata: {
        ...(previewBlocks[0]?.metadata || {}),
        // ponytail: 保留首块元数据，正文统一以管理员保存后的原文为唯一分块输入。
        field_policy_managed: false,
      },
    }];
    const chunks = sourceBlocks.filter((block) => String(block.text || '').trim());
    if (!chunks.length) return setNotice('当前解析内容没有可生成的分块。');
    const document = current || {};
    setBusy('chunk');
    try {
      const persisted = await api.retrievalChunkPreview({
        document_id: parsed.document_id || document.document_id || 'document',
        scope_id: document.scope_id || scopeId,
        knowledge_type: knowledgeTypeValue,
        blocks: chunks,
        identity,
      });
      const visibleChunks = (persisted || []).filter((chunk) => chunk.visibility !== 'internal_supplement').map((chunk, index) => ({
        block_id: chunk.source_chunk_id || `${document.document_id || 'document'}:chunk:${index}`,
        text: chunk.chunk_text || '',
        page_number: chunk.metadata?.page_start,
        metadata: chunk.metadata || {},
        source_chunk_id: chunk.source_chunk_id,
        proposed_visibility: chunk.visibility || 'public',
        source_locator: chunk.source_locator,
      }));
      setGeneratedChunks(visibleChunks);
      setParsedDirty(visibleChunks.length > 0);
      setNotice(`已按 Retrieval 正式规则生成 ${(persisted || []).length} 个分块；确认后才会写入索引。`);
    } catch (error) {
      setNotice(`分块预览失败：${error.message}`);
    } finally { setBusy(''); }
  }

  function updateGeneratedChunk(index, text) {
    setGeneratedChunks((currentChunks) => currentChunks.map((chunk, chunkIndex) => (
      chunkIndex === index ? { ...chunk, text } : chunk
    )));
    setParsedDirty(true);
    setNotice('分块内容已修改，请确认后更新索引。');
  }

  async function deleteDocument(document) {
    if (!document.storage_key) return setNotice('当前记录缺少 MinIO 对象地址，无法删除原始文件；请重新上传该文件。');
    if (!window.confirm(`确认删除“${document.file_name}”吗？将同时删除 Milvus 索引、MinIO 原始文件和解析产物。`)) return;
    setBusy('delete'); setNotice('正在删除文档索引、MinIO 文件和解析产物…');
    try {
      const response = await api.adminDeleteDocument(document.document_id, {
        storage_key: document.storage_key,
        scope_id: document.scope_id || scopeId,
        knowledge_type: document.knowledge_type || knowledgeType,
        user_id: identity.user_id,
      });
      if (response.status !== 'DELETED') throw new Error(response.error || '删除文档失败');
      const nextRecords = { ...records };
      delete nextRecords[document.document_id];
      persist(nextRecords);
      setSelected(null);
      setVersionsDocument(null);
      setVersions([]);
      setUploaded(null);
      setFiles((currentFiles) => currentFiles.filter((item) => item.document_id !== document.document_id));
      setNotice('文档、Milvus 索引和 MinIO 解析产物已删除。');
    } catch (error) { setNotice(`删除失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function loadVersions(document) {
    if (!document?.document_id) return;
    setVersionsDocument(document); setVersions([]); setVersionsLoading(true);
    setDocumentDetails((current) => ({ ...current, [document.document_id]: null }));
    try {
      const response = await api.adminDocument(document.document_id);
      setVersions(Array.isArray(response.versions) ? response.versions : []);
      setDocumentDetails((current) => ({ ...current, [document.document_id]: {
        associated_drafts: Array.isArray(response.associated_drafts) ? response.associated_drafts : [],
        task_records: Array.isArray(response.task_records) ? response.task_records : [],
      } }));
      const latestTask = response.task_records?.[0];
      if (latestTask) receiveAdminTaskUpdate(latestTask);
      setNotice(`已读取 ${document.file_name} 的 ${response.versions?.length || 0} 个版本。`);
    } catch (error) {
      setNotice(`读取版本失败：${error.message}`);
    } finally { setVersionsLoading(false); }
  }

  async function changeVersionPublication(version, action = 'unpublish') {
    if (!version?.version_id || !versionsDocument) return;
    const republish = action === 'publish';
    const actionLabel = republish ? '重新上架' : '下架';
    if (!window.confirm(`确认${actionLabel}版本 v${version.version_number ?? ''} 吗？`)) return;
    setVersionsBusy(true); setNotice(`正在${actionLabel}版本…`);
    try {
      const payload = { user_id: identity.user_id, note: '管理员工作台下架版本' };
      if (republish) {
        await api.adminPublishVersion(version.version_id, { ...payload, note: '管理员工作台重新上架版本' });
      } else {
        await api.adminUnpublishVersion(version.version_id, payload);
      }
      await loadVersions(versionsDocument);
      await refreshFiles();
      const indexHint = republish && version.index_status !== 'INDEXED' ? ' 当前版本尚未建立索引，检索前请重新建立索引。' : '';
      setNotice(`版本 v${version.version_number ?? ''} 已${actionLabel}。${indexHint}`);
    } catch (error) {
      setNotice(`${actionLabel}版本失败：${error.message}`);
    } finally { setVersionsBusy(false); }
  }

  async function changeVersionStorage(version, action) {
    if (!version?.version_id || !versionsDocument) return;
    const options = {
      recycle: ['移入回收站', api.adminRecycleVersion],
      restore: ['恢复版本', api.adminRestoreVersion],
      purge: ['彻底清理', api.adminPurgeVersion],
    };
    const [label, request] = options[action] || [];
    if (!label || !request) return;
    const warning = action === 'purge' ? '此操作会删除该版本关联的存储与索引，无法恢复。' : '';
    if (!window.confirm(`确认${label} v${version.version_number ?? ''} 吗？${warning}`)) return;
    setVersionsBusy(true); setNotice(`正在${label}…`);
    try {
      await request(version.version_id, { user_id: identity.user_id, note: `管理员工作台${label}` });
      await loadVersions(versionsDocument);
      await refreshFiles();
      setNotice(`版本 v${version.version_number ?? ''} 已${label}。`);
    } catch (error) {
      setNotice(`${label}失败：${error.message}`);
    } finally { setVersionsBusy(false); }
  }

  async function recoverTask(task) {
    if (!task?.task_id) return;
    setBusy('recover');
    setNotice(`正在恢复任务 ${task.task_id}…`);
    try {
      await api.adminRecoverTask(task.task_id, {
        operator_id: identity.user_id,
        decision_note: '材料任务列表确认恢复一次',
      });
      await refreshFiles();
      setNotice(`任务 ${task.task_id} 已提交恢复，请查看最新处理状态。`);
    } catch (error) {
      setNotice(`任务恢复失败：${error.message}`);
    } finally { setBusy(''); }
  }

  function taskStage(task, currentDocument = null) {
    if (task.status === 'UPLOADED' && currentDocument?.stage === 'parse') {
      return { stage: 'parse', status: 'Perception 解析中' };
    }
    if (task.status === 'UPLOADED') return { stage: 'upload', status: '已上传，等待解析' };
    if (['QUEUED', 'RUNNING', 'PARSING'].includes(task.status)) return { stage: 'parse', status: 'Perception 解析中' };
    if (task.status === 'WAITING_CONFIRM') return { stage: 'confirm', status: '解析完成，等待确认' };
    if (task.status === 'WAITING_DRAFT_CONFIRM') return { stage: 'draft_confirm', status: '草稿已生成，等待确认' };
    if (task.status === 'INDEXING') return { stage: 'index', status: 'Retrieval 索引中' };
    if (task.status === 'SUCCEEDED' && task.lifecycle_action === 'recycle') return { stage: 'deleted', status: '已移入回收站' };
    if (task.status === 'SUCCEEDED' && task.lifecycle_action === 'purge') return { stage: 'deleted', status: '已删除' };
    if (task.status === 'SUCCEEDED') return { stage: 'index', status: '已建立索引' };
    if (task.status === 'FAILED') return { stage: 'failed', status: '处理失败', error: task.error };
    return {};
  }

  const receiveAdminTaskUpdate = useCallback((task) => {
    if (!task?.task_id) return;
    setSupervisorTasks((previous) => {
      const exists = previous.some((item) => item.task_id === task.task_id);
      return exists
        ? previous.map((item) => item.task_id === task.task_id ? { ...item, ...task } : item)
        : [task, ...previous];
    });
    if (!task.document_id) return;
    const patch = { supervisor_task: task, updated_at: task.updated_at };
    setFiles((previous) => previous.map((item) => item.document_id === task.document_id
      ? { ...item, ...patch, ...taskStage(task, item) }
      : item));
    setSelected((previous) => previous?.document_id === task.document_id
      ? { ...previous, ...patch, ...taskStage(task, previous) }
      : previous);
    setUploaded((previous) => previous?.document_id === task.document_id
      ? { ...previous, ...patch, ...taskStage(task, previous) }
      : previous);
  }, []);

  async function waitForSupervisorTask(taskId, terminalStatuses, initialDocument = null) {
    // 后端文件任务默认超时 900 秒；前端不能在 180 秒时提前判定失败。
    for (let attempt = 0; attempt < 900; attempt += 1) {
      const task = route === '/admin'
        ? await api.adminTask(taskId)
        : await api.adminTask(taskId);
      const document = uploaded?.document_id === task.document_id
        ? uploaded
        : selected?.document_id === task.document_id
          ? selected
          : initialDocument?.document_id === task.document_id ? initialDocument : null;
      if (document) updateRecord(document, { ...task, supervisor_task_id: task.task_id, ...taskStage(task, document) });
      if (terminalStatuses.includes(task.status)) {
        await refreshFiles();
        return task;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    throw new Error('Supervisor 任务等待超时，请在任务列表中查看状态。');
  }

  async function parseSupervisorTask(document) {
    const taskId = document.supervisor_task_id || document.supervisor_task?.task_id;
    if (!taskId) throw new Error('当前文件没有 Supervisor 任务');
    if (route === '/admin') await api.adminParseTask(taskId);
    else await api.adminParseTask(taskId);
    const task = await waitForSupervisorTask(taskId, ['WAITING_CONFIRM', 'WAITING_DRAFT_CONFIRM', 'WAITING_INTENT_CONFIRM', 'FAILED'], document);
    if (!['WAITING_CONFIRM', 'WAITING_DRAFT_CONFIRM', 'WAITING_INTENT_CONFIRM'].includes(task.status)) throw new Error(displayError(task.error, '解析失败'));

    // Supervisor 任务状态只保存轻量字段，完整解析结果保存在 MinIO。
    // 因此不能要求任务响应直接携带 parsed_result；确认阶段从受控接口补取正文。
    let parsedResponse = task.parsed_result;
    if (!parsedResponse) {
      try {
        parsedResponse = route === '/admin'
          ? await api.adminParseResult(document.document_id)
          : await api.parseResult(document.document_id);
      } catch (error) {
        throw new Error(`解析已完成，但读取解析结果失败：${error.message}`);
      }
    }
    if (!parsedResponse?.blocks) throw new Error('解析已完成，但没有可用的解析内容');
    setParsed(parsedResponse); setParsedDirty(false);
    const parsedDocument = updateRecord(document, {
      ...task, supervisor_task_id: taskId,
      stage: task.status === 'WAITING_CONFIRM' ? 'confirm' : 'draft_confirm',
      status: task.status === 'WAITING_CONFIRM' ? '解析完成，等待确认' : task.status === 'WAITING_DRAFT_CONFIRM' ? '草稿已生成，等待确认' : '等待确认资料意图',
      block_count: parsedResponse.blocks?.length || 0, parsed_blocks: parsedResponse.blocks || [],
      parsed_raw_text: parsedResponse.raw_text || '', parsed_metadata: parsedResponse.metadata || {},
      parsed_edited: Boolean(parsedResponse.edited),
    });
    setUploaded(parsedDocument); setSelected(parsedDocument);
    return {
      ...parsedResponse,
      supervisor_task: task,
      draft: task.draft_preview || task.draft_previews?.[0] || null,
      draft_previews: task.draft_previews || (task.draft_preview ? [task.draft_preview] : []),
      draft_id: task.draft_id || null,
      intent_knowledge_type: task.intent_knowledge_type || null,
    };
  }

  async function pollAdminDraft(taskId, document) {
    let lastError = null;
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        const task = await api.adminTask(taskId);
        if (task?.draft_status === 'READY' || task?.draft_status === 'FAILED' || task?.draft_status === 'EMPTY') {
          updateRecord(document, { ...task, supervisor_task_id: taskId });
          return task;
        }
        lastError = null;
      } catch (error) {
        // 管理后台任务仍在运行时，单次网络抖动不能丢弃已生成的服务端草稿。
        lastError = error;
      }
    }
    throw lastError || new Error('字段提取任务等待超时，请稍后重试。');
  }

  async function search(event) {
    event.preventDefault();
    if (!query.trim()) return;
    const started = performance.now();
    setSearchResult(null);
    setBusy('search'); setNotice('Retrieval 正在检索证据…');
    try {
      const payload = { query: query.trim(), scope_id: searchScopeId, knowledge_type: searchKnowledgeType || undefined, identity };
      const result = await (isAdminPage ? api.adminRetrieve(payload) : api.search(payload));
      setSearchResult({ ...result, client_elapsed_ms: performance.now() - started });
      setNotice(result.error || result.status === 'FAILED' ? '检索失败，请查看结果区域。' : '检索完成。');
    }
    catch (error) {
      setNotice(`检索失败：${error.message}`);
      setSearchResult({ status: 'FAILED', error: error.message, error_code: error.code,
        trace_id: error.payload?.trace_id, request_id: error.payload?.request_id,
        client_elapsed_ms: performance.now() - started });
    }
    finally { setBusy(''); }
  }

  async function openTaskDocument(task) {
    const document = files.find((item) => item.document_id === task.document_id);
    if (!document) {
      setNotice(`任务 ${task.task_id} 关联的文件不在当前 MinIO 清单中。`);
      return;
    }
    await selectStoredFile({ ...document, supervisor_task: task });
      navigate('/admin', 'documents');
  }

  return <main className={`shell${isAdminPage ? ' admin-shell' : ' user-shell'}`}>
    {isAdminPage ? <>
      <header className="admin-topbar">
        <button className="admin-brand" type="button" onClick={() => setPage('overview')} aria-label="返回管理总览">
          <span className="admin-brand-avatar assistant-avatar" aria-hidden="true"><i /></span>
          <span><strong>科创服务助手</strong><small>管理控制台</small></span>
        </button>
        <div className="admin-topbar-actions">
          <HealthStatus health={health} />
        </div>
      </header>
    </> : <header className="user-topbar">
      <button className="user-brand" type="button" onClick={() => navigate('/', 'supervisor')} aria-label="返回智能问答">
        <span className="user-brand-avatar assistant-avatar" aria-hidden="true"><i /></span><strong>科创服务助手</strong>
      </button>
      <nav className="user-topnav" aria-label="用户功能导航">
        <button className={page === 'supervisor' ? 'active' : ''} type="button" onClick={() => navigate('/', 'supervisor')}>智能问答</button>
        <button className={page === 'personal' ? 'active' : ''} type="button" onClick={() => navigate('/', 'personal')}>个人中心</button>
      </nav>
      <div className="user-topbar-actions">
        <button type="button" className="user-management-link" onClick={() => navigate('/admin', 'overview')}>管理中心</button>
        <button className="user-profile-trigger" type="button" onClick={() => navigate('/', 'personal')} title="打开个人中心">
          <span className="user-profile-avatar">{(selectedTestUser?.name || identity.name || selectedTestUser?.user_id || identity.user_id || 'U').slice(0, 1).toUpperCase()}</span>
          <span className="user-profile-copy"><b>{selectedTestUser?.name || identity.name || selectedTestUser?.user_id || identity.user_id || '当前用户'}</b><small>{selectedTestUser?.company || identity.company || '企业人员'}</small></span>
        </button>
      </div>
    </header>}

    {isAdminPage && <nav className="document-nav admin-nav admin-tabbar" aria-label="管理员功能导航">
      <button type="button" className={page === 'overview' ? 'active' : ''} onClick={() => setPage('overview')}>总览</button><button type="button" className={page === 'retrieval' ? 'active' : ''} onClick={() => setPage('retrieval')}>检索验证</button><button type="button" className={page === 'documents' ? 'active' : ''} onClick={() => setPage('documents')}>数据管理</button><button type="button" className={page === 'upload' ? 'active' : ''} onClick={() => setPage('upload')}>上传文档</button><button type="button" className={page === 'tasks' ? 'active' : ''} onClick={() => setPage('tasks')}>会话管理</button><button type="button" className={page === 'governance' ? 'active' : ''} onClick={() => setPage('governance')}>审核治理</button><button type="button" className={page === 'permissions' ? 'active' : ''} onClick={() => setPage('permissions')}>字段权限</button>
    </nav>}

    <Suspense fallback={<div role="status">页面加载中…</div>}>
    {isAdminPage && page === 'retrieval' && <RetrievalPage query={query} setQuery={setQuery} search={search} busy={busy} searchResult={searchResult} searchScopeId={searchScopeId} setSearchScopeId={setSearchScopeId} searchScopeOptions={searchScopeOptions} searchKnowledgeType={searchKnowledgeType} setSearchKnowledgeType={setSearchKnowledgeType} identity={identity} />}

    {isAdminPage && page === 'upload' && <AdminUploadPage
      intakeProps={{ fileInputRef, file, chooseFile, scopeId, setScopeId, scopeOptions, knowledgeType, setKnowledgeType, knowledgeTypes, uploadTarget, upload, busy, selected, uploaded, parse, extractFields, notice, current, currentStage, fieldPolicies: adminFieldPolicies || [], parsed, previewBlocks, generatedChunks, saveParsedRawText, saveGeneratedChunks, parsedDirty, generateChunks, updateParsedRawText, updateParsedDraft, parsedDraftRef, updateGeneratedChunk, setGeneratedChunks, setNotice, blockLocationLabel, confirmIndex, draftPreviews: current?.draft_previews || (current?.draft_preview ? [current.draft_preview] : (current?.supervisor_task?.draft_previews || (current?.supervisor_task?.draft_preview ? [current.supervisor_task.draft_preview] : []))), updateDraft: updateAdminDraft }}
      catalogProps={{ knowledgeTypes, catalogKnowledgeType, getKnowledgeType: displayKnowledgeType, switchCatalog, fieldPolicies: adminFieldPolicies || [], files, documentSearch, onDocumentSearchChange: searchAdminDocuments, documentPage, documentTotal, documentTotalPages, onDocumentPageChange: setDocumentPage, versionsDocument, toggleDocumentDetails, fileLoading, versionsProps: { versions, loading: versionsLoading, busy: versionsBusy, identity, loadVersions, associatedDrafts: documentDetails[versionsDocument?.document_id]?.associated_drafts || [], taskRecords: documentDetails[versionsDocument?.document_id]?.task_records || [], openPreview: openIndexPreview, updateDocument, changeVersionPublication, changeVersionStorage }, supervisorTasks, recoverTask, busy, refreshFiles, onSyncMysql: syncMysqlTables, onStopMysql: stopMysqlSync, mysqlSync, mysqlCounts, knowledgeUpdates: draftCatalog, onOpenKnowledgeUpdate: openKnowledgeUpdate, knowledgeUpdateDetail, knowledgeUpdateDetailLoading, onCloseKnowledgeUpdate: () => setKnowledgeUpdateDetail(null), showDraftCatalog: false, showUpdateDetail: false, showDocumentCatalog: true }}
    />}

    {isAdminPage && page === 'overview' && <AdminOverview files={files} updates={knowledgeUpdates} updatePage={overviewUpdatePage} updateTotal={overviewUpdateTotal} updateTotalPages={overviewUpdateTotalPages} onUpdatePageChange={setOverviewUpdatePage} tasks={supervisorTasks} summary={adminOverview} loading={fileLoading} onRefresh={() => refreshFiles({ refreshCounts: true })} mysqlCounts={mysqlCounts} getKnowledgeType={displayKnowledgeType} onNavigate={(nextPage, type, update) => { if (type) setSearchKnowledgeType(type); if (update?.record_id) void openKnowledgeUpdate(update); else setPage(nextPage); }} />}

    {isAdminPage && page === 'permissions' && <AdminFieldPolicy policies={adminFieldPolicies || undefined} />}

    {isAdminPage && page === 'governance' && <AdminGovernance operatorId={identity.user_id} />}

    {route === '/' && page === 'supervisor' && <SupervisorPage identity={identity} testUsers={testUsers} testUsersLoading={testUsersLoading} testUsersError={testUsersError} selectedTestUser={selectedTestUser} allowedScopes={allowedScopes} onIdentityChange={async (userId) => { const next = (await api.authLogin({ user_id: userId })).user; if (!next) throw new Error('用户不存在'); setIdentity((currentIdentity) => ({ ...currentIdentity, user_id: next.user_id, name: next.name || next.user_id, user_role: next.user_role || (next.is_inner_user ? 'internal' : 'external'), user_type: next.user_type || null, company: next.company || null, phone: next.phone || null })); }} knowledgeType={knowledgeType} onKnowledgeTypeChange={setKnowledgeType} onUploadAndParse={uploadAndParseFromChat} uploadBusy={busy === 'chat-upload'} />}
    {route === '/' && page === 'personal' && <PersonalPage identity={identity} profile={myProfile} documents={myDocuments} conversations={myConversations} drafts={myDrafts} visitRecords={myVisitRecords} matchResults={myMatchResults} notice={notice} loading={personalLoading} busy={busy === 'unpublish' || busy === 'publish' || busy === 'draft-attachment' || busy === 'draft-delete' || busy === 'visit-delete'} conversationMessages={personalConversationMessages} conversationLoading={personalConversationLoading} selectedConversationId={personalConversationId} documentDetail={personalDocumentDetail} documentDetailLoading={personalDocumentLoading} onRefresh={refreshPersonal} onUnpublish={unpublishMyDocument} onRepublish={republishMyDocument} onOpenDocument={openPersonalDocument} onCloseDocument={closePersonalDocument} onOpenConversation={openPersonalConversation} draftDetail={personalDraftDetail} onOpenDraft={openPersonalDraft} onCloseDraft={() => setPersonalDraftDetail(null)} onDeleteDraft={deletePersonalDraft} onUnpublishDraft={unpublishMyDraft} onRepublishDraft={republishMyDraft} onDeleteDraftAttachment={deletePersonalDraftAttachment} onDeleteVisitRecord={deletePersonalVisitRecord} />}

    {isAdminPage && page === 'tasks' && <TaskCenter />}

    {isAdminPage && page === 'documents' && <AdminDocumentsPage
      knowledgeTypes={knowledgeTypes}
      catalogKnowledgeType={catalogKnowledgeType}
      getKnowledgeType={displayKnowledgeType}
      switchCatalog={switchCatalog}
      fieldPolicies={adminFieldPolicies || []}
      intakeProps={{ fileInputRef, file, chooseFile, scopeId, setScopeId, scopeOptions, knowledgeType, setKnowledgeType, knowledgeTypes, uploadTarget, upload, busy, selected, uploaded, parse, extractFields, notice, current, currentStage, fieldPolicies: adminFieldPolicies || [], parsed, previewBlocks, generatedChunks, saveParsedRawText, saveGeneratedChunks, parsedDirty, generateChunks, updateParsedRawText, updateParsedDraft, parsedDraftRef, updateGeneratedChunk, setGeneratedChunks, setNotice, blockLocationLabel, confirmIndex, draftPreviews: current?.draft_previews || (current?.draft_preview ? [current.draft_preview] : (current?.supervisor_task?.draft_previews || (current?.supervisor_task?.draft_preview ? [current.supervisor_task.draft_preview] : []))), updateDraft: updateAdminDraft }}
      files={files}
      documentPage={documentPage}
      documentTotal={documentTotal}
      documentTotalPages={documentTotalPages}
      onDocumentPageChange={setDocumentPage}
      versionsDocument={versionsDocument}
      toggleDocumentDetails={toggleDocumentDetails}
      fileLoading={fileLoading}
      versionsProps={{ versions, loading: versionsLoading, busy: versionsBusy, identity, loadVersions, associatedDrafts: documentDetails[versionsDocument?.document_id]?.associated_drafts || [], taskRecords: documentDetails[versionsDocument?.document_id]?.task_records || [], openPreview: openIndexPreview, updateDocument, changeVersionPublication, changeVersionStorage }}
      supervisorTasks={supervisorTasks}
      recoverTask={recoverTask}
      busy={busy}
      refreshFiles={refreshFiles}
      onSyncMysql={syncMysqlTables}
      onStopMysql={stopMysqlSync}
      mysqlSync={mysqlSync}
      mysqlCounts={mysqlCounts}
      onRebuildMysqlData={rebuildMysqlData}
      knowledgeUpdates={draftCatalog}
      knowledgeUpdatePage={draftCatalogPage}
      knowledgeUpdateTotal={draftCatalogTotal}
      knowledgeUpdateTotalPages={draftCatalogTotalPages}
      onKnowledgeUpdatePageChange={setDraftCatalogPage}
      onOpenKnowledgeUpdate={openKnowledgeUpdate}
      knowledgeUpdateDetail={knowledgeUpdateDetail}
      knowledgeUpdateDetailLoading={knowledgeUpdateDetailLoading}
      onCloseKnowledgeUpdate={() => setKnowledgeUpdateDetail(null)}
      onUnpublishDraft={unpublishAdminDraft}
      onRepublishDraft={republishAdminDraft}
      onDeleteDraft={deleteAdminDraft}
      onPurgeDraft={purgeAdminDraft}
      showDocumentCatalog={false}
    />}
    </Suspense>

    {indexPreview && <Suspense fallback={<div role="status">预览加载中…</div>}><IndexPreviewModal
      document={indexPreview.document}
      parsed={indexPreview.parsed}
      loading={indexPreview.loading}
      busy={busy === 'index'}
      onClose={() => setIndexPreview(null)}
      onConfirm={() => void confirmIndex(indexPreview.document, true)}
    /></Suspense>}

  </main>;
}
