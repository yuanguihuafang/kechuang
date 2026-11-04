import { useEffect, useState } from 'react';
import { api } from '../../api.js';
import { displayDraftField } from '../../draftFieldLabels.js';
import { applyDraftOverrides, normalizeSolutionDraftFields, projectDraftFields } from './draftPresentation.js';
import { copyDraftText, formatDraftForClipboard } from './draftClipboard.js';
import { recommendationCardImages, recommendationCardSummary } from './recommendationCardImages.js';
import { AuthenticatedDownload } from '../documents/AuthenticatedDownload.jsx';

function ActionButton({ action, onAction, disabled }) {
  return <button type="button" className={`business-action${disabled ? ' is-selected' : ''}`} onClick={() => onAction(action)} disabled={disabled || action.enabled === false}>
    {action.label}
  </button>;
}

const draftTypeLabels = {
  demand: '采购需求', solution: '技术方案', achievement: '科研成果',
  enterprise: '企业资料', expert: '专家资料', customer_visit: '客户拜访记录',
  supply_draft: '技术方案 / 科研成果',
};

const intentLabels = {
  clarification: '信息补充', knowledge_search: '知识查询', supply_demand_match: '供需匹配',
  enterprise_profile: '企业画像', demand_discovery: '需求发现',
  achievement_discovery: '成果发现', expert_search: '专家匹配', customer_visit: '客户拜访',
  contact_request: '人工对接', web_search: '联网搜索', demand: '整理用户需求',
  solution: '整理技术方案', achievement: '整理科研成果', supply_draft: '技术方案 / 科研成果',
};

const draftStatusLabels = {
  DRAFT_GENERATED: '草稿已生成，待保存',
  DRAFT_SAVED: '草稿已保存，待发布',
  PUBLISHED: '已发布',
};

const analysisDimensionLabels = {
  basic_info: '企业基础信息',
  enterprise_profile: '企业画像',
  technical_capability: '技术能力',
  potential_demand: '潜在需求',
  adapted_solution: '适配方案',
  supplier_match_dimensions: '供应商匹配维度',
};

const demandTypeOptions = ['物料采购', '智能制造', '创新研发', '软件服务', '数字化管理', '其他'];
const visitDraftFieldKeys = ['enterprise_name', 'enterprise_portrait', 'potential_demand_list_text', 'budget_range', 'expect_cycle', 'adapt_solution', 'supplier_match_dim', 'inner_mark_note'];

const fallbackDraftFields = {
  demand: [
    ['title', true], ['category', true], ['company', true], ['contect', true], ['analyse_contect', true],
    ['link_name', true], ['link_tel', true], ['budget_range', false], ['demand_cycle', false],
  ],
  solution: [
    ['title', true], ['pub_dept', false], ['product_type', false], ['contect', true], ['analyse_contect', false],
    ['link_name', false], ['link_tel', false], ['deliver_cycle', false], ['quote_range', false],
  ],
  achievement: [
    ['title', true], ['category', false], ['analyse_contect', true], ['tech_maturity', false], ['cooperation_mode', false], ['company', true],
    ['link_name', false], ['link_tel', false],
  ],
  customer_visit: visitDraftFieldKeys.map((key) => [key, false]),
};

function emptyDraftFieldHint(field, draftType) {
  if (draftType === 'customer_visit' && field === 'adapt_solution') return '首次生成不自动填充，执行方案匹配后生成';
  if (draftType === 'customer_visit' && field === 'inner_mark_note') return '用户可以补充本次拜访记录';
  return `未提取到${displayDraftField(field, draftType)}相关内容`;
}

function draftLabel(type) {
  return draftTypeLabels[type] || '业务资料';
}

function intentLabel(value) {
  const key = String(value || '').trim();
  return intentLabels[key] || (key && !/^[A-Za-z][A-Za-z0-9_-]*$/.test(key) ? key : '当前业务意图');
}

function sourceLabel(locator, knowledgeType, mysqlId) {
  const value = String(locator || '来源已记录');
  const parts = value.split('|').filter(Boolean);
  const source = parts[0] || '';
  const collectionLabels = {
    xp_pub_result: '科研成果库', xp_pub_tech_solution: '技术方案库',
    xp_pub_demand: '企业需求库', xp_enterprise_library: '垂直企业库',
    xp_expert_library: '领域专家库',
    demand: '企业需求库', solution: '技术方案库', achievement: '科研成果库',
    enterprise: '垂直企业库', expert: '领域专家库',
    customer_visit: '拜访记录库',
  };
  const sourceKey = source.replace(/^mysql:/, '');
  const collection = collectionLabels[sourceKey] || ({
    demand: '企业需求库', solution: '技术方案库', achievement: '科研成果库',
    enterprise: '垂直企业库', expert: '领域专家库',
    customer_visit: '拜访记录库',
  }[knowledgeType] || '知识库');
  const documentName = source.startsWith('mysql:') || collectionLabels[sourceKey] ? '' : source;
  if (value.includes('record_type:customer_visit')) return `拜访记录库${mysqlId ? ` · ID：${mysqlId}` : ''}`;
  return [collection, documentName, mysqlId ? `ID：${mysqlId}` : ''].filter(Boolean).join(' · ');
}

function sourceDocumentLabel(locator, knowledgeType, mysqlId) {
  const source = String(locator || '').split('|')[0].trim();
  const name = source && !source.startsWith('mysql:') && !source.startsWith('draft:')
    ? source
    : sourceLabel(locator, knowledgeType, null);
  return [name || '知识库记录', mysqlId ? `ID：${mysqlId}` : ''].filter(Boolean).join(' · ');
}

function localizedRecommendationText(text, knowledgeType) {
  return String(text || '').split('\n').map((line) => {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(.*)$/);
    if (!match) return line;
    return `${displayDraftField(match[1], knowledgeType)}：${match[2]}`;
  }).join('\n');
}

function recommendationTitle(title) {
  const value = String(title || '').trim();
  return /^title\s*[:：]/i.test(value) ? '' : value;
}

function displaySourceContext(text) {
  return String(text || '').replace(/^\s*(?:#{1,6}\s*)?(?:整理原文|解析原文上下文)\s*[:：]?\s*/i, '').replace(/^\s*-{3,}\s*/,'').trim();
}

function visibleDraftText(value) {
  const controlMessages = new Set(['供需匹配', '开始供需匹配', '立即匹配', '匹配', '我有需求', '我有技术方案或科研成果', '我有技术方案', '我有科研成果', '立即进入需求草稿完善流程']);
  return String(value || '').split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !/^\s*(?:user|assistant|human|system|用户|助手)\s*[:：]/i.test(line) && !controlMessages.has(line))
    .join('\n');
}

function hideBrokenImage(event) {
  event.currentTarget.hidden = true;
}

function evidenceContent(text, imageMap = new Map()) {
  const parts = [];
  const pattern = /(<img\b[^>]*\bsrc=["']([^"']+)["'][^>]*>)|!\[[^\]]*\]\(([^)\s]+)[^)]*\)|((?:https?:\/\/|\/)?api\/v1\/perception\/images\/[^\s<>'")]+)|(https?:\/\/[^\s<>'")]+\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s<>'")]+)?)/gi;
  let cursor = 0;
  let match;
  const source = String(text || '');
  while ((match = pattern.exec(source))) {
    if (match.index > cursor) parts.push({ type: 'text', value: source.slice(cursor, match.index) });
    parts.push({ type: 'image', value: match[2] || match[3] || match[4] || match[5] });
    cursor = pattern.lastIndex;
  }
  if (cursor < source.length) parts.push({ type: 'text', value: source.slice(cursor) });
  if (!parts.length) parts.push({ type: 'text', value: source });
  return <>{parts.map((part, index) => part.type === 'image'
    ? <img className="business-evidence-image" key={`${part.value}-${index}`} src={imageMap.get(part.value) || imageMap.get(String(part.value).replace(/^\/+/, '')) || imageMap.get(String(part.value).split('/').pop().replace(/\.[^.]+$/, '')) || api.perceptionAssetUrl(part.value)} alt="证据图片" loading="lazy" onError={hideBrokenImage} />
    : <span key={`${part.value}-${index}`}>{part.value.replace(/<\/?[^>]+>/g, '')}</span>)}</>;
}

export function markdownContent(text, imageMap = new Map()) {
  const lines = String(text || '').split(/\r?\n/);
  return lines.filter((line, index) => {
    const value = line.trim();
    if (!value || /^!\[[^\]]*$/.test(value)) return false;
    if (
      value === ')'
      && [lines[index - 1], lines[index + 1]].some((neighbor) => (
        /!\[|api\/v1\/perception\/images/i.test(neighbor || '')
      ))
    ) return false;
    return true;
  }).map((line, index) => {
    const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*$/);
    if (heading) {
      const Tag = heading[1].length <= 2 ? 'h4' : 'h5';
      return <Tag key={`heading-${index}`} className="business-markdown-heading">{evidenceContent(heading[2], imageMap)}</Tag>;
    }
    return <div key={`line-${index}`} className="business-markdown-line">{evidenceContent(line, imageMap)}</div>;
  });
}

function evidenceImageMap(images) {
  const map = new Map();
  for (const image of Array.isArray(images) ? images : []) {
    const url = image?.url || image?.image_url;
    if (!url) continue;
    const resolved = api.perceptionAssetUrl(url);
    for (const value of [image.url, image.image_url, image.image_id, image.relativePath, image.relative_path, image.path, image.source_locator]) {
      const normalized = String(value || '').replaceAll('\\', '/').replace(/^\/+/, '').split(/[?#]/, 1)[0];
      if (normalized) map.set(normalized, resolved);
      const name = normalized.split('/').pop();
      if (name) map.set(name, resolved);
    }
  }
  return map;
}

function mergeImageMap(baseMap, urls) {
  const map = new Map(baseMap);
  for (const url of Array.isArray(urls) ? urls : []) {
    const normalized = String(url || '').replaceAll('\\', '/').replace(/^\/+/, '').split(/[?#]/, 1)[0];
    if (!normalized) continue;
    map.set(normalized, api.perceptionAssetUrl(url));
    map.set(normalized.split('/').pop(), api.perceptionAssetUrl(url));
  }
  return map;
}

function applyIdentityDefaults(draft, identity) {
  if (!draft) return draft;
  let mysqlFields = { ...(draft.mysql_fields || {}) };
  let fields = { ...mysqlFields, ...(draft.fields || {}) };
  if (draft.draft_type === 'solution') {
    ({ fields, mysqlFields } = normalizeSolutionDraftFields(fields, mysqlFields, identity?.company));
  }
  const allowedKeys = new Set((fallbackDraftFields[draft.draft_type] || []).map(([key]) => key));
  ({ fields, mysqlFields } = projectDraftFields(fields, mysqlFields, allowedKeys));
  const title = draft.title || fields.title || '';
  if (title && allowedKeys.has('title')) fields.title = title;
  const defaults = draft.draft_type === 'customer_visit'
    ? { enterprise_name: identity?.company }
    : draft.draft_type === 'solution'
      ? { pub_dept: identity?.company, link_name: identity?.name, link_tel: identity?.phone }
      : { company: identity?.company, link_name: identity?.name, link_tel: identity?.phone };
  Object.entries(defaults).forEach(([key, value]) => {
    if (String(value || '').trim() && !String(fields[key] || '').trim()) fields[key] = value;
  });
  const profileKeys = new Set(Object.entries(defaults).filter(([, value]) => String(value || '').trim()).map(([key]) => key));
  const profileLabels = new Set(['company', 'enterprise_name', 'link_name', 'link_tel', '企业名称', '拜访企业', '公司名称', '联系人', '联系电话', '电话']);
  return {
    ...draft,
    title: title || draft.title,
    fields,
    mysql_fields: { ...mysqlFields, ...Object.fromEntries([...profileKeys].map((key) => [key, fields[key]])) },
    missing_fields: (draft.missing_fields || []).filter((field) => !profileLabels.has(field)),
    optional_questions: (draft.optional_questions || []).filter((question) => !['企业名称', '公司名称', '联系人', '联系电话', '电话'].some((term) => String(question).includes(term))),
  };
}

function editorFieldsFor(draft) {
  const configured = draft?.flow_definition?.fields;
  const fallback = (fallbackDraftFields[draft?.draft_type] || []).map(([key, required]) => ({ key, required }));
  if (!Array.isArray(configured)) return fallback;
  const configuredByKey = new Map(configured.map((field) => [field.key, field]));
  return fallback.map((field) => ({ ...field, ...(configuredByKey.get(field.key) || {}), key: field.key, required: field.required }));
}

function draftHasExpiredAttachment(draft) {
  return (draft?.attachments || []).some((item) => (
    item?.temporary && item?.expires_at && Date.parse(item.expires_at) <= Date.now()
  ));
}

function draftStatusAt(response, index, draft) {
  return draft?.status || draft?.publication_status || draft?.draft?.status || response?.draft_statuses?.[index] || (
    response?.status === 'PUBLISHED' ? 'PUBLISHED'
      : response?.status === 'DRAFT_SAVED' && response?.draft_index === index ? 'DRAFT_SAVED'
        : 'DRAFT_GENERATED'
  );
}

function attachmentDisplayName(item) {
  return item?.source_type === 'audio_transcript'
    ? '语音转写文本'
    : (item?.file_name || item?.document_id || '未命名附件');
}

function recommendationCardKey(item) {
  return String(item?.id || `${item?.document_id || item?.source?.document_id || ''}:${item?.chunk_role || ''}:${item?.chunk_index ?? ''}`);
}

/** 渲染助手的结构化业务结果：匹配卡片/详情、流程动作和可编辑草稿卡片。 */
export function BusinessBlocks({ response, onAction, onUploadRequest, identity, busy = false }) {
  const [notice, setNotice] = useState('');
  const responseDraftSnapshots = Array.isArray(response?.drafts) && response.drafts.length
    ? response.drafts : (response?.draft ? [response.draft] : []);
  const [draftOverrides, setDraftOverrides] = useState({});
  const responseDrafts = applyDraftOverrides(responseDraftSnapshots, draftOverrides);
  const [draftIndex, setDraftIndex] = useState(0);
  const [draft, setDraft] = useState(responseDrafts[0] || null);
  const currentDraftStatus = draftStatusAt(response, draftIndex, draft);
  const [savedDraftIndexes, setSavedDraftIndexes] = useState(() => new Set());
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorSaved, setEditorSaved] = useState(response?.status === 'DRAFT_SAVED');
  const [editorBusy, setEditorBusy] = useState(false);
  const [editorPublishing, setEditorPublishing] = useState(false);
  const [editorError, setEditorError] = useState('');
  const [selectedActionId, setSelectedActionId] = useState('');
  const [customDemandType, setCustomDemandType] = useState(false);
  const [selectedRecommendation, setSelectedRecommendation] = useState(null);
  const [selectedRecommendationDetail, setSelectedRecommendationDetail] = useState(null);
  const [recommendationImageDetails, setRecommendationImageDetails] = useState({});
  const [recommendationDetailLoading, setRecommendationDetailLoading] = useState(false);
  const [recommendationDetailError, setRecommendationDetailError] = useState('');
  const showSelectedSourceContext = ['agent_doc', 'admin_doc'].includes(selectedRecommendation?.chunk_role);
  const matchResults = Array.isArray(response?.match_results) ? response.match_results : [];
  const imageMap = evidenceImageMap(response?.images);
  const detailImageUrls = [...new Set([
    ...(selectedRecommendationDetail?.image_urls || []),
    ...(selectedRecommendationDetail?.draft_chunks || []).flatMap((chunk) => chunk.image_urls || []),
    ...(selectedRecommendationDetail?.source_chunks || []).flatMap((chunk) => chunk.image_urls || []),
  ])];
  const detailDraftText = String(selectedRecommendationDetail?.draft || (selectedRecommendationDetail?.draft_chunks || []).map((chunk) => chunk.text || '').filter(Boolean).join('\n\n')).trim();
  const detailSourceText = String(selectedRecommendationDetail?.cleaned_source_text || (selectedRecommendationDetail?.source_chunks || []).map((chunk) => chunk.text || '').filter(Boolean).join('\n\n')).trim();
  const detailRawText = String(selectedRecommendationDetail?.text || selectedRecommendation?.details || selectedRecommendation?.summary || '').trim();
  const hasVisibleDetailSource = showSelectedSourceContext && detailSourceText;
  const hasRawDetail = Boolean(detailDraftText || hasVisibleDetailSource || (!detailDraftText && !hasVisibleDetailSource && detailRawText));
  const blocks = Array.isArray(response?.ui_blocks) ? response.ui_blocks : [];
  const recommendationItems = matchResults.length > 0
    ? matchResults
    : blocks.filter((block) => block?.type === 'recommendation_list').flatMap((block) => block.data?.items || []);
  const recommendationBlock = { type: 'recommendation_list', data: { title: '匹配结果', count: recommendationItems.length, items: recommendationItems } };
  const baseBlocks = blocks.some((block) => block?.type === 'recommendation_list')
    ? blocks.reduce((result, block) => {
      if (block?.type !== 'recommendation_list') return [...result, block];
      return result.some((item) => item?.type === 'recommendation_list') ? result : [...result, recommendationBlock];
    }, [])
    : recommendationItems.length > 0 ? [...blocks, recommendationBlock] : blocks;
  const followUps = baseBlocks.filter((block) => block?.type === 'match_follow_up');
  const blocksWithoutFollowUps = baseBlocks.filter((block) => block?.type !== 'match_follow_up');
  const lastRecommendationIndex = blocksWithoutFollowUps.reduce((last, block, index) => block?.type === 'recommendation_list' ? index : last, -1);
  const renderBlocks = followUps.length > 0 && lastRecommendationIndex >= 0
    ? [...blocksWithoutFollowUps.slice(0, lastRecommendationIndex + 1), ...followUps, ...blocksWithoutFollowUps.slice(lastRecommendationIndex + 1)]
    : baseBlocks;
  const firstDraftCardBlockIndex = renderBlocks.findIndex((block) => block?.type === 'draft_card');
  const actions = Array.isArray(response?.actions) ? response.actions : [];
  const recommendationImageRequestKey = recommendationItems.map((item) => [
    recommendationCardKey(item), item?.document_id || item?.source?.document_id || '',
    item?.chunk_role || '', item?.chunk_index ?? '', item?.knowledge_type || '',
    item?.matched_draft_record_id || '',
  ].join(':')).join('|');

  useEffect(() => {
    if (!identity?.user_id) return undefined;
    let cancelled = false;
    setRecommendationImageDetails({});
    const unresolved = recommendationItems.filter((item) => (
      recommendationCardImages(item, null, imageMap).length === 0
      && item?.chunk_role
      && Number.isInteger(item?.chunk_index)
    ));
    if (!unresolved.length) return undefined;

    void Promise.all(unresolved.map(async (item) => {
      const key = recommendationCardKey(item);
      try {
        const detail = await api.retrievalChunkDetail({
          user_id: identity.user_id,
          mysql_id: item.mysql_id || null,
          source_id: item.source_id || null,
          matched_draft_record_id: item.matched_draft_record_id || null,
          document_id: item.document_id || item.source?.document_id || null,
          chunk_role: item.chunk_role,
          chunk_index: item.chunk_index,
          knowledge_type: item.knowledge_type || null,
          include_summary: false,
        });
        return [key, detail];
      } catch (error) {
        console.warn('匹配卡片图片信息读取失败', key, error);
        return null;
      }
    })).then((results) => {
      if (!cancelled) setRecommendationImageDetails(Object.fromEntries(results.filter(Boolean)));
    });

    return () => { cancelled = true; };
  }, [recommendationImageRequestKey, identity?.user_id, response?.images]);

  useEffect(() => {
    const nextDrafts = Array.isArray(response?.drafts) && response.drafts.length
      ? response.drafts : (response?.draft ? [response.draft] : []);
    const normalizedDrafts = nextDrafts.map((item) => applyIdentityDefaults(item, identity));
    const nextIndex = normalizedDrafts.length > 1 ? Math.min(draftIndex, normalizedDrafts.length - 1) : 0;
    setDraftIndex(nextIndex);
    setDraft(normalizedDrafts[nextIndex] || null);
    setSavedDraftIndexes(new Set());
    setSelectedActionId('');
    setSelectedRecommendation(null);
    setSelectedRecommendationDetail(null);
    setRecommendationDetailError('');
    const demandType = normalizedDrafts[0]?.fields?.category || '';
    setCustomDemandType(Boolean(demandType && !demandTypeOptions.includes(demandType)));
    const selectedStatus = draftStatusAt(response, nextIndex, normalizedDrafts[nextIndex]);
    setEditorSaved(selectedStatus === 'PUBLISHED' || selectedStatus === 'DRAFT_SAVED');
    setEditorBusy(false);
    setEditorPublishing(false);
  }, [response, identity]);

  async function openRecommendationDetail(item) {
    setSelectedRecommendation(item);
    setSelectedRecommendationDetail(null);
    setRecommendationDetailError('');
    if (!item?.chunk_role || !Number.isInteger(item.chunk_index) || !identity?.user_id) return;
    setRecommendationDetailLoading(true);
    try {
      const detail = await api.retrievalChunkDetail({
        user_id: identity.user_id,
        mysql_id: item.mysql_id || null,
        source_id: item.source_id || null,
        matched_draft_record_id: item.matched_draft_record_id || null,
        document_id: item.document_id || item.source?.document_id || null,
        chunk_role: item.chunk_role,
        chunk_index: item.chunk_index,
        knowledge_type: item.knowledge_type || null,
      });
      setSelectedRecommendationDetail(detail);
    } catch (error) {
      setRecommendationDetailError(error.message || '详情加载失败');
    } finally {
      setRecommendationDetailLoading(false);
    }
  }
  useEffect(() => {
    setDraftOverrides({});
  }, [response]);

  useEffect(() => {
    if (!editorOpen) return;
    document.querySelectorAll('.draft-long-field').forEach((node) => {
      node.style.height = 'auto';
      node.style.height = `${Math.min(node.scrollHeight, 142)}px`;
      node.style.overflowY = node.scrollHeight > 142 ? 'auto' : 'hidden';
    });
  }, [draft, editorOpen]);

  async function openDraftEditor(snapshot, actionId = '') {
    if (!snapshot) return;
    const draftId = String(snapshot.draft_id || '').trim();
    const index = responseDrafts.findIndex((item) => item?.draft_id === draftId);
    setEditorBusy(true);
    setNotice(draftId ? '正在读取草稿最新版本…' : '正在打开草稿…');
    try {
      const detail = draftId ? await api.myDraftDetail(draftId) : null;
      const source = detail?.draft ? {
        ...detail.draft,
        draft_id: detail.draft.draft_id || detail.draft_id || draftId,
        version: detail.version ?? detail.draft.version,
        status: detail.draft.status || detail.status,
      } : snapshot;
      if (draftId && !detail?.draft) throw new Error('草稿详情响应缺少内容');
      if (draftHasExpiredAttachment(source)) {
        setNotice('文档已过期，请重新上传文档');
        return;
      }
      const editable = applyIdentityDefaults(source, identity);
      setDraft(editable);
      if (index >= 0) setDraftIndex(index);
      setEditorSaved(draftStatusAt(response, index >= 0 ? index : draftIndex, editable) !== 'DRAFT_GENERATED');
      setEditorError('');
      setSelectedActionId(actionId);
      setEditorOpen(true);
      setNotice('');
    } catch (error) {
      setNotice(`草稿最新版本读取失败，未打开旧内容：${error?.message || String(error)}`);
    } finally {
      setEditorBusy(false);
    }
  }

  async function copyDraft(snapshot, fields) {
    try {
      await copyDraftText(formatDraftForClipboard(snapshot, fields));
      setNotice('草稿内容已复制。');
    } catch (error) {
      setNotice(`复制失败：${error?.message || String(error)}`);
    }
  }

  if (!renderBlocks.length && !actions.length) return null;

  function handleAction(action) {
    const repeatableAction = action.type === 'NEXT_MATCH_BATCH';
    if (selectedActionId && action.type !== 'EDIT_DRAFT' && !repeatableAction) return;
    if (action.type === 'UPLOAD_MATERIAL') {
      onUploadRequest?.();
      setNotice('请选择要上传的资料。');
      return;
    }
    if (action.type === 'MATCH_RESULT_CONFIRM') {
      const startDraft = action.value === 'start_draft';
      onAction?.({ ...action, message: startDraft ? (action.message || '立即进入需求草稿完善流程') : '否' });
      setSelectedActionId(action.id);
      setNotice(startDraft ? (action.message || '立即进入需求草稿完善流程') : '已结束当前匹配。');
      return;
    }
    if (action.type === 'EDIT_DRAFT') {
      void openDraftEditor(action.draft || draft, action.id);
      return;
    }
    if (action.type === 'START_DEMAND_DRAFT') {
      onAction?.({
        ...action,
        intent_hint: 'demand',
        intent_confirmation: 'confirmed',
        draft_context: { draft_type: 'demand', match_follow_up: true },
        message: '补充需求并重新匹配',
      });
      setSelectedActionId(action.id);
      setNotice('已进入需求补充流程，请继续描述完整需求。');
      return;
    }
    const draftActionTypes = new Set(['CONFIRM_DRAFT', 'EDIT_DRAFT', 'SAVE_DRAFT', 'PUBLISH_DRAFT', 'MATCH_DRAFT', 'REMATCH_DRAFT', 'SAVE_MATCHED_DEMAND']);
    if (!repeatableAction) setSelectedActionId(action.id);
    const pending = onAction?.(draftActionTypes.has(action.type) ? { ...action, draft: draft || action.draft } : action);
    setNotice(`已选择“${action.label}”。`);
    return pending;
  }

  function selectDraft(index) {
    const nextDraft = applyIdentityDefaults(responseDrafts[index], identity);
    if (!nextDraft) return;
    setDraftIndex(index);
    setDraft(nextDraft);
    setEditorSaved(draftStatusAt(response, index, nextDraft) !== 'DRAFT_GENERATED' || savedDraftIndexes.has(index));
    setEditorError('');
    setSelectedActionId('');
    const demandType = nextDraft.fields?.category || '';
    setCustomDemandType(Boolean(demandType && !demandTypeOptions.includes(demandType)));
  }

  async function completeEdit() {
    if (editorBusy || editorSaved) return;
    const missing = missingRequiredFields(draft);
    if (missing.length) {
      setEditorError(`请填写必填字段：${missing.join('、')}`);
      return;
    }
    setEditorError('');
    setEditorSaved(false);
    setEditorBusy(true);
    try {
      const saved = await onAction?.({ id: 'editor-save', type: 'SAVE_DRAFT', label: '保存', draft, draft_index: draftIndex });
      if (saved?.version != null) setDraft((current) => ({ ...current, version: saved.version }));
      setEditorSaved(true);
      setSavedDraftIndexes((indexes) => new Set(indexes).add(draftIndex));
      setNotice('草稿已保存，可继续发布。');
    } catch (error) {
      setEditorError(`保存失败：${error?.message || String(error)}`);
    } finally {
      setEditorBusy(false);
    }
  }

  function closeEditor() {
    if (editorSaved && draftStatusAt(response, draftIndex, draft) !== 'PUBLISHED' && !window.confirm('当前草稿尚未发布，确定关闭吗？')) return;
    setEditorOpen(false);
  }

  async function removeDraftAttachment(index) {
    const attachment = draft?.attachments?.[index];
    if (!attachment) return;
    if (!window.confirm(`确认从当前草稿移除附件“${attachment.file_name || attachment.name || '未命名附件'}”吗？原始文件会保留，其他草稿不受影响。`)) return;
    setEditorBusy(true);
    setEditorError('');
    try {
      if (draft?.draft_id) {
        const saved = await api.removeDraftAttachment(draft.draft_id, attachment);
        const savedDraft = {
          ...(saved?.draft || {}),
          draft_id: saved?.draft?.draft_id || saved?.draft_id || draft.draft_id,
          version: saved?.version ?? draft.version,
          status: saved?.status || saved?.draft?.status || draft.status,
        };
        setDraftOverrides((overrides) => ({ ...overrides, [draft.draft_id]: savedDraft }));
        setDraft(editorSaved ? savedDraft : {
          ...draft,
          attachments: savedDraft.attachments || [],
          source_blocks: savedDraft.source_blocks || [],
          version: savedDraft.version,
          status: savedDraft.status,
        });
        setEditorError('');
        setNotice('附件已从当前草稿移除，原文件保留；其他草稿不受影响。');
        return;
      }

      const nextDraft = draft ? {
        ...draft,
        attachments: (draft.attachments || []).filter((_, attachmentIndex) => attachmentIndex !== index),
        source_blocks: [],
      } : null;
      if (nextDraft) setDraft(nextDraft);
      setEditorSaved(false);
      setSavedDraftIndexes((indexes) => {
        const next = new Set(indexes);
        next.delete(draftIndex);
        return next;
      });
    } catch (error) {
      setEditorError(`附件删除失败：${error.message}`);
      return;
    } finally {
      setEditorBusy(false);
    }
    setEditorError('');
  }

  function matchDraftSolutions() {
    if (editorBusy) return;
    setEditorBusy(true);
    setSelectedActionId('match_visit_solutions');
    const pending = onAction?.({ id: 'match_visit_solutions', type: 'MATCH_VISIT_SOLUTIONS', label: '方案匹配', draft, draft_index: draftIndex });
    pending?.finally?.(() => setEditorBusy(false));
    if (!pending?.finally) setEditorBusy(false);
    setEditorOpen(false);
  }

  async function publishEditedDraft() {
    if (editorBusy) return;
    const missing = missingRequiredFields(draft);
    if (missing.length) {
      setEditorError(`请填写必填字段：${missing.join('、')}`);
      return;
    }
    setEditorError('');
    setEditorBusy(true);
    setEditorPublishing(true);
    try {
      await onAction?.({ id: 'editor-publish', type: 'PUBLISH_DRAFT', label: '发布', draft, draft_index: draftIndex });
      if (responseDrafts.length <= 1) setEditorOpen(false);
    } catch (error) {
      setEditorError(`发布失败：${error?.message || String(error)}`);
    } finally {
      setEditorBusy(false);
      setEditorPublishing(false);
    }
  }

  function missingRequiredFields(value) {
    return editorFieldsFor(value).filter((field) => field.required === true).filter((field) => {
      const fieldValue = field.key === 'title' ? value?.title : value?.fields?.[field.key];
      return !String(fieldValue || '').trim();
    }).map((field) => displayDraftField(field, value?.draft_type));
  }

  function requestRecommendationHandoff(item) {
    onAction?.({
      id: `contact-${item.id}`,
      type: 'CONTACT_WECOM',
      label: '申请人工对接',
      enabled: true,
      query: item.title || item.summary || '当前匹配方案',
      document_ids: (response?.documents || []).filter((document) => (
        item.source?.document_id && document.document_id === item.source.document_id
      )).map((document) => document.document_id).filter(Boolean),
    });
    setSelectedRecommendation(null);
  }

  function draftEditor() {
    if (!editorOpen || !draft) return null;
    return <div className="draft-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeEditor(); }}>
      <section className="draft-modal" role="dialog" aria-modal="true" aria-labelledby="draft-modal-title">
        <header className="draft-modal-head"><div><span>资料草稿</span><h2 id="draft-modal-title">编辑{draft.draft_type === 'demand' ? '需求' : draftLabel(draft.draft_type)}</h2></div><button type="button" className="draft-modal-close" onClick={closeEditor} aria-label="关闭草稿编辑">×</button></header>
        <div className="draft-modal-body">
          {editorFieldsFor(draft).map((field) => {
            const value = field.key === 'title' ? (draft.title || '') : (draft.fields?.[field.key] || '');
            const isDemandType = draft.draft_type === 'demand' && field.key === 'category';
            const isCustomDemandType = isDemandType && (customDemandType || (value && !demandTypeOptions.includes(value)));
            const longText = ['contect', 'analyse_contect', 'enterprise_portrait', 'potential_demand_list_text', 'adapt_solution', 'supplier_match_dim'].includes(field.key);
            const unit = field.key === 'budget_range' ? '元' : field.key === 'demand_cycle' ? '天' : '';
            const placeholder = String(value || '').trim() ? undefined : emptyDraftFieldHint(field.key, draft.draft_type);
            const update = (event) => { setEditorError(''); setEditorSaved(false); setSavedDraftIndexes((indexes) => { const next = new Set(indexes); next.delete(draftIndex); return next; }); setEditorBusy(false); setSelectedActionId(''); setDraft(field.key === 'title' ? { ...draft, title: event.target.value, fields: { ...draft.fields, title: event.target.value } } : { ...draft, fields: { ...draft.fields, [field.key]: event.target.value } }); };
            return <label key={field.key}><span>{displayDraftField(field, draft.draft_type)}{field.required === true && <em className="required-mark" aria-label="必填">*</em>}</span><div className={unit ? 'draft-input-with-unit' : ''}>{isDemandType ? <><select value={isCustomDemandType ? '其他' : value} onChange={(event) => { const isOther = event.target.value === '其他'; setCustomDemandType(isOther); update({ target: { value: isOther ? '' : event.target.value } }); }}><option value="" disabled hidden>请选择需求类型</option>{demandTypeOptions.map((option) => <option key={option} value={option}>{option}</option>)}</select>{isCustomDemandType && <input className="draft-custom-type-input" placeholder="请输入其他需求类型" value={value} onChange={update} />}</> : longText ? <textarea className="draft-long-field" rows="1" value={value} placeholder={placeholder} onChange={update} onInput={(event) => { event.currentTarget.style.height = 'auto'; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 142)}px`; event.currentTarget.style.overflowY = event.currentTarget.scrollHeight > 142 ? 'auto' : 'hidden'; }} /> : <input value={value} placeholder={placeholder} onChange={update} />}{unit && <em className="draft-field-unit">{unit}</em>}</div></label>;
          })}
          {Array.isArray(draft.attachments) && draft.attachments.length > 0 && <div className="draft-attachments"><strong>附件</strong>{draft.attachments.map((item, index) => {
            const downloadUrl = item.file_url
              ? api.perceptionAssetUrl(item.file_url)
              : (item.document_id ? api.documentFileUrl(item.document_id, identity) : '');
            return <div className="draft-attachment-item" key={item.document_id || item.file_name || index}>
              <span title={item.source_name || item.file_name || item.document_id}>{attachmentDisplayName(item)}</span>
              <span className="draft-attachment-actions">
                {downloadUrl && <AuthenticatedDownload url={downloadUrl} filename={item.file_name} label="下载" className="text-button" />}
                <button type="button" className="draft-attachment-remove" onClick={() => void removeDraftAttachment(index)} disabled={editorBusy} aria-label={`删除附件 ${item.file_name || item.document_id}`} title="删除附件">删除</button>
              </span>
            </div>;
          })}</div>}
          <small><span className="required-mark">*</span> 标记为必填；待补充：{(draft.missing_fields || []).map((field) => displayDraftField(field)).join('、') || '无'}；企业/联系人信息已按登记身份带入，可直接修改。</small>
          {editorError && <small className="draft-editor-error" role="alert">{editorError}</small>}
        </div>
        <footer className="draft-modal-foot">{responseDrafts.length > 1 && <nav className="draft-pagination draft-modal-pagination" aria-label="编辑草稿翻页"><button type="button" className="business-action" onClick={() => selectDraft(draftIndex - 1)} disabled={draftIndex === 0}>上一页</button><span>第 {draftIndex + 1} / {responseDrafts.length} 页</span><button type="button" className="business-action" onClick={() => selectDraft(draftIndex + 1)} disabled={draftIndex === responseDrafts.length - 1}>下一页</button></nav>}<div><button type="button" className={`business-action draft-save-button${editorSaved ? ' is-saved' : ''}`} onClick={completeEdit} disabled={editorBusy || editorSaved}>{editorSaved ? '已保存' : '保存'}</button>{draft.draft_type === 'customer_visit' && !String(draft.fields?.adapt_solution || '').trim() ? <button type="button" className="business-action draft-publish-button" onClick={matchDraftSolutions} disabled={editorBusy}>方案匹配</button> : <button type="button" className="business-action draft-publish-button is-ready" onClick={publishEditedDraft} disabled={draftStatusAt(response, draftIndex, draft) === 'PUBLISHED' || editorBusy}>{editorPublishing ? '发布中…' : draftStatusAt(response, draftIndex, draft) === 'PUBLISHED' ? '已发布' : '发布'}</button>}</div></footer>
      </section>
    </div>;
  }

  return <section className="business-blocks" aria-label="业务响应内容">
    {renderBlocks.map((block, index) => {
      const data = block.data || {};
      if (block.type === 'notice') {
        return <div className="business-block business-block-notice" role="status" key={`${block.type}-${index}`}>
          <strong>{data.title || '提示'}</strong><small>{data.message || ''}</small>
        </div>;
      }
      if (block.type === 'human_input') {
        const fields = Array.isArray(data.fields) ? data.fields : [];
        const options = Array.isArray(data.options) ? data.options : [];
        const submit = (event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          const answers = fields.map((field) => {
            const values = field.type === 'multi_select' ? form.getAll(field.name) : [form.get(field.name)];
            const value = values.filter(Boolean).join('、') || (field.type === 'checkbox' ? '否' : '');
            return value ? `${field.label || field.name}：${value}` : '';
          }).filter(Boolean);
          const other = fields.length === 0 && options.length > 0 ? String(form.get('other') || '').trim() : '';
          if (other) answers.push(`补充说明：${other}`);
          if (!answers.length) return;
          setSelectedActionId('human-input-submit');
          onAction?.({ type: 'SUBMIT_HUMAN_INPUT', message: `补充信息：\n${answers.join('\n')}` });
        };
        return <div className="business-block business-block-human-input" key={`${block.type}-${index}`}>
          <strong>请补充信息</strong>
          {data.context && <small>{data.context}</small>}
          {fields.length > 0 ? <form className="human-input-form" onSubmit={submit}>
            {fields.map((field) => <label className="human-input-field" key={field.name}>
              <span>{field.label || field.name}{field.required ? ' *' : ''}</span>
              {field.type === 'textarea' ? <textarea name={field.name} required={field.required} placeholder={field.placeholder || ''} />
                : field.type === 'select' || field.type === 'multi_select' ? <select name={field.name} required={field.required} multiple={field.type === 'multi_select'} defaultValue={field.type === 'multi_select' ? [] : ''}>
                  {field.type === 'select' && <option value="">请选择</option>}
                  {(field.options || []).map((option) => <option value={option} key={option}>{option}</option>)}
                </select>
                  : field.type === 'checkbox' ? <input type="checkbox" name={field.name} required={field.required} />
                    : <input type={['number', 'date'].includes(field.type) ? field.type : 'text'} name={field.name} required={field.required} placeholder={field.placeholder || ''} />}
            </label>)}
            {fields.some((field) => field.required) && <div className="human-input-required-hint">带 * 为必填项</div>}
            <button type="submit" className="business-action" disabled={Boolean(selectedActionId)}>提交信息</button>
          </form> : options.length > 0 ? <>
            <div className="draft-type-options"><div>{options.map((option, optionIndex) => {
              const value = option.value || option.label;
              const label = option.label || value;
              return <button type="button" className="business-action" key={`${value}-${optionIndex}`} disabled={Boolean(selectedActionId)} onClick={() => {
                setSelectedActionId(`human-input-${optionIndex}`);
                onAction?.({ type: 'SUBMIT_HUMAN_INPUT', message: `我选择：${value}` });
              }}>{label}</button>;
            })}</div></div>
            <form className="human-input-form" onSubmit={submit}>
              <label className="human-input-field"><span>其他补充</span><input name="other" placeholder="也可以填写其他内容" /></label>
              <button type="submit" className="business-action" disabled={Boolean(selectedActionId)}>提交补充</button>
            </form>
          </> : <small>请在下方输入框回复。</small>}
        </div>;
      }
      if (block.type === 'evidence_list') {
        return null;
      }
      if (block.type === 'recommendation_list') {
        const items = recommendationItems;
        return <div className="business-block business-block-recommendations" key={`${block.type}-${index}`}>
          <div className="business-recommendation-heading">
            <strong>{data.title || '匹配结果'}</strong><span>· {items.length || data.count || 0} 条</span>
          </div>
          <div className="business-recommendation-list">{items.map((item, itemIndex) => {
            const imageUrls = recommendationCardImages(item, recommendationImageDetails[recommendationCardKey(item)], imageMap);
            return <article key={item.id} className="business-recommendation-card">
            <strong>匹配结果{itemIndex + 1}{recommendationTitle(item.title) ? `：${recommendationTitle(item.title)}` : ''}</strong>
            <small>{item.summary ? evidenceContent(recommendationCardSummary(localizedRecommendationText(item.summary, item.knowledge_type)), mergeImageMap(imageMap, item.image_urls)) : '暂无摘要'}</small>
            {imageUrls.length > 0 && <div className="business-recommendation-images">{imageUrls.slice(0, 3).map((url, imageIndex) => <img className="business-evidence-image" key={`${url}-${imageIndex}`} src={api.perceptionAssetUrl(url)} alt="匹配结果图片" loading="lazy" onError={hideBrokenImage} />)}</div>}
            {recommendationImageDetails[recommendationCardKey(item)]?.image_error && <small role="alert">{recommendationImageDetails[recommendationCardKey(item)].image_error}</small>}
            <em>检索来源：{sourceLabel(item.source?.source_locator, item.knowledge_type || item.source?.knowledge_type, item.mysql_id || item.source?.mysql_id)}</em>
            <button type="button" className="recommendation-detail-trigger" onClick={() => openRecommendationDetail(item)}>查看详情</button>
          </article>;
          })}</div>
        </div>;
      }
      if (block.type === 'match_follow_up') {
        return <div className="business-block business-block-match-follow-up" role="status" key={`${block.type}-${index}`}>
          <div>{data.text || ''}</div>
        </div>;
      }
      if (block.type === 'match_progress') {
        return null;
      }
      if (block.type === 'business_sections') {
        return <div className="business-block business-block-intent" key={`${block.type}-${index}`}>
          <strong>{data.title || '内部市场分析维度'}</strong>
          <div className="business-section-list">{(data.items || []).map((item) => <span key={item.key}>{item.label || analysisDimensionLabels[item.key] || '分析维度'}：{item.evidence_count || 0} 条证据</span>)}</div>
        </div>;
      }
      if (block.type === 'image_references') {
        return null;
      }
      if (block.type === 'document_references') {
        return null;
      }
      if (block.type === 'empty_state') {
        return <div className="business-block business-block-empty" key={`${block.type}-${index}`}>
          <strong>{data.title}</strong><small>{data.message}</small>
        </div>;
      }
      if (block.type === 'contact_prompt') {
        if (!['internal', 'admin'].includes(identity?.user_role)) return null;
        return <div className="business-block business-block-contact" key={`${block.type}-${index}`}>
          <strong>{data.title || '资料需要人工对接'}</strong><small>{data.message}</small>
        </div>;
      }
      if (block.type === 'intent_confirmation') {
        const intent = data.intent || {};
        return <div className="business-block business-block-intent" key={`${block.type}-${index}`}>
          <strong>意图识别</strong>
          <span>{intentLabel(intent.name)}</span>
          <small>{intent.confirmed ? '处理方向已确认，您可以继续描述或上传资料。' : '请您确认处理方向，确认后即可继续。'}</small>
        </div>;
      }
      if (block.type === 'draft_information_required') {
        const missing = data.missing_fields || [];
        const displayMissing = (field) => displayDraftField(
          field === '标题' ? '需求名称' : field === '需求类型（技术类别）' ? '需求类型' : field,
        );
        const isSupplyChoice = data.intent?.name === 'supply_draft'
          || (Array.isArray(data.options) && data.options.includes('技术方案'));
        const options = Array.isArray(data.options) && data.options.length > 0
          ? data.options
          : (isSupplyChoice ? ['技术方案', '科研成果'] : []);
        return <div className="business-block business-block-draft-info" key={`${block.type}-${index}`}>
          <strong>{data.title || '生成草稿前需要补充信息'}</strong>
          <small>{isSupplyChoice ? '请选择资料类型，之后可以继续描述内容或上传资料。' : '以下是少量必要信息；其他内容会先生成草稿，之后可以直接编辑补充。'}</small>
          {options.length > 0 && <div className="draft-type-options" aria-label={isSupplyChoice ? '选择资料类型' : '选择需求类型'}>
            <span>{isSupplyChoice ? '请选择资料类型' : '请选择需求类型'}</span>
            <div>{options.map((option) => {
              const optionId = `${isSupplyChoice ? 'supply' : 'demand'}-type-${option}`;
              return <button type="button" className={`business-action${selectedActionId === optionId ? ' is-selected' : ''}`} key={option} onClick={() => handleAction({ id: optionId, type: isSupplyChoice ? 'SELECT_SUPPLY_TYPE' : 'SELECT_DEMAND_TYPE', value: option, label: option })} disabled={Boolean(selectedActionId)}> {option}</button>;
            })}</div>
          </div>}
          <div className="draft-required-fields">{missing.map((field) => <span key={field}>{displayMissing(field)}</span>)}</div>
          {Array.isArray(data.questions) && data.questions.length > 0 && <ul>{data.questions.map((question) => <li key={question}>{question}</li>)}</ul>}
        </div>;
      }
      if (block.type === 'internal_flow') {
        return <div className="business-block business-block-intent" key={`${block.type}-${index}`}>
          <strong>{data.title || '内部市场处理方向'}</strong>
          <span>{(data.output_blocks || []).map((item) => analysisDimensionLabels[item] || '分析维度').join(' · ') || '待确认处理方向'}</span>
          <small>确认后将按该方向检索公开库和当前内部人员可访问的私有库。</small>
        </div>;
      }
      if (block.type === 'draft_card' && draft) {
        if (index !== firstDraftCardBlockIndex) return null;
        const isVisit = draft.draft_type === 'customer_visit';
        const previewFields = (isVisit
          ? visitDraftFieldKeys.map((key) => ({ key }))
          : (draft.flow_definition?.fields || Object.keys(draft.fields || {}).map((key) => ({ key })))).map((field) => [field.key, visibleDraftText(draft.fields?.[field.key])]);
        const visibleTitle = visibleDraftText(draft.title);
        const visibleSummary = visibleDraftText(draft.requirement_summary || draft.technical_summary || draft.achievement_summary);
        const sessionDraftLabel = Number.isInteger(draft.ordinal) && draft.ordinal > 0
          ? ` · 草稿 ${draft.ordinal}` : '';
        return <div className="business-block business-block-draft" key={`${block.type}-${index}`}>
          <strong>{isVisit ? `本次完整拜访记录${sessionDraftLabel}` : `草稿预览 · ${draftLabel(draft.draft_type)}${sessionDraftLabel}`}</strong>
          <small className="draft-current-status" role="status">当前状态：{draftStatusLabels[draftStatusAt(response, draftIndex, draft)] || '草稿已生成，待保存'}</small>
          {responseDrafts.length > 1 && <nav className="draft-pagination" aria-label="草稿翻页"><button type="button" className="business-action" onClick={() => selectDraft(draftIndex - 1)} disabled={draftIndex === 0}>上一页</button><span>第 {draftIndex + 1} / {responseDrafts.length} 页</span><button type="button" className="business-action" onClick={() => selectDraft(draftIndex + 1)} disabled={draftIndex === responseDrafts.length - 1}>下一页</button></nav>}
          <h3>{visibleTitle || `待完善${draftLabel(draft.draft_type)}`}</h3>
          <p>{visibleSummary || '暂无内容摘要，请点击编辑补充。'}</p>
          <div className="personal-draft-fields" aria-label={`${isVisit ? '本次拜访记录' : draftLabel(draft.draft_type)}字段`}>
            {previewFields.map(([key, value]) => <div key={key}><span>{displayDraftField(key, draft.draft_type)}</span><p className={!String(value || '').trim() ? 'personal-draft-field-empty' : undefined}>{String(value || '').trim() || emptyDraftFieldHint(key, draft.draft_type)}</p></div>)}
          </div>
          {isVisit && Array.isArray(draft.solution_evidences) && draft.solution_evidences.length > 0 && <div className="draft-attachments">
            <strong>适配方案来源</strong>
            {draft.solution_evidences.map((item, evidenceIndex) => <span key={item.record_id || item.source_locator || evidenceIndex}>{sourceLabel(item.source_locator, item.source_knowledge_type || item.knowledge_type || item.source?.knowledge_type || item.source?.source_collection, item.mysql_id || item.source?.mysql_id)}</span>)}
          </div>}
          <div className="draft-card-actions">
            <button type="button" className="business-action draft-edit-trigger" onClick={() => void openDraftEditor(draft)} disabled={editorBusy}>
              {editorBusy ? '正在读取…' : '编辑草稿'}
            </button>
            <button type="button" className="business-action" aria-label="复制草稿内容" onClick={() => void copyDraft(draft, previewFields)}>复制</button>
          </div>
          {Array.isArray(draft.attachments) && draft.attachments.length > 0 && <div className="draft-attachments"><strong>附件</strong>{draft.attachments.map((item) => <span title={item.source_name || item.file_name || item.document_id} key={item.document_id || item.file_name}>{attachmentDisplayName(item)}</span>)}</div>}
          <small>待补充：{(draft.missing_fields || []).map((field) => displayDraftField(field)).join('、') || '无'}；企业/联系人信息已按登记身份带入，可直接修改。</small>
          {(draft.optional_questions || []).length > 0 && <div className="draft-optional-questions">
            <small>可选补充：</small>
            <ul>{draft.optional_questions.map((question) => <li key={question}>{question}</li>)}</ul>
          </div>}
          {draft.draft_review?.status && <small>AI整理状态：{draft.draft_review.status}</small>}
        </div>;
      }
      if (block.type === 'error_state') {
        return <div className="business-block business-block-error" key={`${block.type}-${index}`}>
          <strong>{data.title}</strong><small>{data.message}</small>
        </div>;
      }
      return null;
    })}
    {actions.length > 0 && <div className="business-actions">
      {actions.filter((action) => !['SAVE_DRAFT', 'PUBLISH_DRAFT'].includes(action.type)
        && !(draft?.draft_type === 'customer_visit' && action.type === 'CONFIRM_DRAFT'))
        .map((action) => <ActionButton key={action.id} action={action} onAction={handleAction} disabled={busy || Boolean(action.type !== 'NEXT_MATCH_BATCH' && selectedActionId && action.type !== 'EDIT_DRAFT' && selectedActionId === action.id)} />)}
    </div>}
    {notice && <small className="business-action-notice" role="status">{notice}</small>}
    {selectedRecommendation && <div className="draft-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelectedRecommendation(null); }}>
      <section className="draft-modal recommendation-detail-modal" role="dialog" aria-modal="true" aria-labelledby="recommendation-detail-title">
        <header className="draft-modal-head"><div><span>匹配详情</span><h2 className="recommendation-detail-title" id="recommendation-detail-title">{selectedRecommendation.title || '匹配结果'}</h2></div><button type="button" className="draft-modal-close" onClick={() => setSelectedRecommendation(null)} aria-label="关闭匹配详情">×</button></header>
        <div className="draft-modal-body">
          <dl className="personal-detail-meta"><div><dt>知识类型</dt><dd>{draftLabel(selectedRecommendation.knowledge_type)}</dd></div><div><dt>来源</dt><dd>{sourceDocumentLabel(selectedRecommendation.source?.source_locator, selectedRecommendation.knowledge_type, selectedRecommendation.mysql_id || selectedRecommendation.source?.mysql_id)}</dd></div></dl>
          <h3>{draftLabel(selectedRecommendation.knowledge_type)}详情</h3>
          {recommendationDetailLoading && <p className="recommendation-detail-text">正在整理完整内容…</p>}
          {recommendationDetailError && <p className="recommendation-detail-text">{recommendationDetailError}</p>}
          {selectedRecommendationDetail?.image_error && <p className="recommendation-detail-text" role="alert">{selectedRecommendationDetail.image_error}</p>}
          {!recommendationDetailLoading && !recommendationDetailError && <>
            {selectedRecommendationDetail?.formatted_text && <div className="recommendation-detail-text">{markdownContent(localizedRecommendationText(selectedRecommendationDetail.formatted_text, selectedRecommendation.knowledge_type), mergeImageMap(imageMap, detailImageUrls))}</div>}
            {!selectedRecommendationDetail?.formatted_text && <p className="recommendation-detail-text">暂未生成简短总结，可展开查看完整内容和文档原文。</p>}
            {hasRawDetail && <details className="recommendation-detail-source">
              <summary>查看完整内容与文档原文</summary>
              {detailDraftText && <section><h4>完整内容</h4><div className="recommendation-detail-text">{markdownContent(localizedRecommendationText(detailDraftText, selectedRecommendation.knowledge_type), mergeImageMap(imageMap, detailImageUrls))}</div></section>}
              {hasVisibleDetailSource && <section><h4>文档原文</h4><div className="recommendation-detail-text">{markdownContent(displaySourceContext(detailSourceText), mergeImageMap(imageMap, detailImageUrls))}</div></section>}
              {!detailDraftText && !hasVisibleDetailSource && detailRawText && <section><h4>检索原文</h4><div className="recommendation-detail-text">{markdownContent(localizedRecommendationText(detailRawText, selectedRecommendation.knowledge_type), mergeImageMap(imageMap, detailImageUrls))}</div></section>}
            </details>}
          </>}
          <h3>相关附件</h3>
          {(() => {
            const availableDocuments = selectedRecommendationDetail?.documents?.length
              ? selectedRecommendationDetail.documents
              : selectedRecommendation.documents?.length
                ? selectedRecommendation.documents
                : (response?.documents || []);
            const documents = availableDocuments.filter((document) => (
              selectedRecommendationDetail?.documents?.length || selectedRecommendation.source_id
                ? true
                : selectedRecommendation.source?.document_id && document.document_id === selectedRecommendation.source.document_id
            ));
            return documents.length > 0
              ? <ul className="personal-draft-attachments">{documents.map((document) => {
                const downloadUrl = document.document_id
                  ? api.documentFileUrl(document.document_id, identity)
                  : '';
                return <li key={`${document.document_id}-${document.version_id || 'latest'}`}><span>{document.source_name || '相关文件'}</span>{selectedRecommendation.knowledge_type !== 'demand' && document.can_download && downloadUrl && <AuthenticatedDownload url={downloadUrl} filename={document.source_name} label="下载附件" className="recommendation-attachment-download" />}</li>;
              })}</ul>
              : <p className="table-empty">暂无可下载的相关附件。</p>;
          })()}
        </div>
        <footer className="draft-modal-foot"><button type="button" className="secondary" onClick={() => setSelectedRecommendation(null)}>关闭</button>{identity?.user_role === 'external' && <button type="button" onClick={() => requestRecommendationHandoff(selectedRecommendation)}>申请人工对接</button>}</footer>
      </section>
    </div>}
    {draftEditor()}
  </section>;
}
