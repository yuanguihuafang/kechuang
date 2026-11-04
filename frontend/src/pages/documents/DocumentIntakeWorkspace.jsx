import { useState } from 'react';
import { MineruInlinePreview } from './MineruInlinePreview.jsx';
import { Pipeline, stageLabel } from './Pipeline.jsx';
import { api } from '../../api.js';
import { errorMessage } from '../../api/request.js';
import { displayDraftField } from '../../draftFieldLabels.js';
import { draftFailureMessage } from './draftFailure.js';

const LONG_DRAFT_FIELDS = new Set(['description', 'business_scope', 'introduction', 'achievements', 'brief_intr', 'research_field', 'application', 'main_product', 'technological_achievements', 'contect', 'analyse_contect', 'enterprise_portrait', 'potential_demand_list_text', 'adapt_solution', 'supplier_match_dim', 'logo', 'avatar', 'image', 'first_img_url']);

function draftFieldsForDisplay(draft, fieldPolicies = []) {
  const values = { ...(draft?.mysql_fields || {}), ...(draft?.fields || {}) };
  const fields = fieldPolicies.find((policy) => policy.type === draft?.draft_type)?.fields;
  if (Array.isArray(fields) && fields.length) {
    return fields.map(({ key, label }) => [key, values[key] ?? '', label]);
  }
  const definition = draft?.flow_definition?.fields;
  if (Array.isArray(definition)) {
    return definition.map(({ key, label }) => [key, draft?.fields?.[key] ?? draft?.mysql_fields?.[key] ?? '', label]);
  }
  return [];
}

function imageCandidates(parsed) {
  const images = Array.isArray(parsed?.metadata?.images) ? parsed.metadata.images : [];
  return images
    .map((image, index) => ({ image, index }))
    .sort((left, right) => {
      const leftPage = imagePageNumber(left.image);
      const rightPage = imagePageNumber(right.image);
      if (leftPage == null && rightPage != null) return 1;
      if (leftPage != null && rightPage == null) return -1;
      if (leftPage != null && rightPage != null && leftPage !== rightPage) return leftPage - rightPage;
      return left.index - right.index;
    })
    .map(({ image }) => image);
}

function imageValue(image) {
  return image?.url || image?.relativePath || image?.relative_path || image?.path || '';
}

function imagePageNumber(image) {
  const page = image?.pageNumber ?? image?.page_number ?? image?.page;
  const number = Number(page);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function imageOptionLabel(image, index) {
  const page = imagePageNumber(image);
  return page ? `第 ${index + 1} 张图片（第 ${page} 页）` : `第 ${index + 1} 张图片`;
}

function imageMatches(value, image) {
  const left = String(value || '').replaceAll('\\', '/').replace(/^\/+/, '');
  const right = String(imageValue(image)).replaceAll('\\', '/').replace(/^\/+/, '');
  return Boolean(left && right && (left === right || left.endsWith(`/${right.split('/').pop()}`) || right.endsWith(`/${left.split('/').pop()}`)));
}

export function DocumentIntakeWorkspace({
  fileInputRef, file, chooseFile, scopeId, setScopeId, scopeOptions, knowledgeType, setKnowledgeType,
  knowledgeTypes, uploadTarget, upload, busy, selected, uploaded, parse, notice, current, currentStage, fieldPolicies = [],
  parsed, previewBlocks, generatedChunks, saveParsedRawText, saveGeneratedChunks,
  parsedDirty, generateChunks, updateParsedRawText, updateParsedDraft, parsedDraftRef, updateGeneratedChunk, setGeneratedChunks,
  extractFields,
  setNotice, blockLocationLabel, confirmIndex,
  draftPreviews = [], updateDraft,
}) {
  const [expandedDraftIndex, setExpandedDraftIndex] = useState(null);
  const pageCount = parsed ? (parsed.metadata?.pages?.length || Object.keys(parsed.page_texts || {}).length || parsed.metadata?.quality?.pageCount || 0) : 0;
  const recordCount = parsed?.metadata?.quality?.recordCount || 0;
  const blockCount = parsed?.blocks?.length || 0;
  const supportsChunking = ['demand', 'solution', 'achievement'].includes(current?.knowledge_type || knowledgeType);
  const hasResult = Boolean(parsed);
  const draftStatus = current?.draft_status || current?.supervisor_task?.draft_status;
  const draftReady = draftStatus === 'READY'
    || current?.result_status === 'PUBLISHED'
    || current?.supervisor_task?.result_status === 'PUBLISHED';
  const visibleDraftPreviews = draftReady ? draftPreviews : [];
  const parsedImages = imageCandidates(parsed);
  const draftPublished = visibleDraftPreviews.length > 0 && (
    visibleDraftPreviews.every((draft) => draft.status === 'PUBLISHED' || draft._ui_published)
    || current?.result_status === 'PUBLISHED'
    || current?.supervisor_task?.result_status === 'PUBLISHED'
  );
  const draftDirty = visibleDraftPreviews.some((draft) => draft._ui_dirty);
  const indexedChunks = current?.indexed_chunks || current?.index_result?.indexed_chunks
    || current?.supervisor_task?.index_result?.indexed_chunks || [];
  const attachmentIndexCount = indexedChunks.filter((chunk) => chunk.chunk_role === 'admin_doc').length
    || (current?.indexed_count && generatedChunks.length ? generatedChunks.length : 0);
  const fieldIndexCount = indexedChunks.filter((chunk) => chunk.chunk_role === 'admin_draft').length
    || Math.max((current?.indexed_count || 0) - attachmentIndexCount, 0);
  const indexDetail = attachmentIndexCount || fieldIndexCount
    ? `（${attachmentIndexCount} 个附件分块${fieldIndexCount ? ` + ${fieldIndexCount} 条字段索引` : ''}）`
    : '';
  const parseStats = parsed
    ? `解析完成：${pageCount ? `${pageCount} 页` : ''}${pageCount && recordCount ? ' · ' : ''}${recordCount ? `${recordCount} 条记录` : ''}${(pageCount || recordCount) ? ' · ' : ''}${blockCount} 个内容块${current?.parse_duration_ms != null ? ` · 耗时 ${current.parse_duration_ms >= 1000 ? `${(current.parse_duration_ms / 1000).toFixed(2)} 秒` : `${current.parse_duration_ms} 毫秒`}` : ''}`
    : currentStage === 'parse' ? 'Perception 正在解析，等待服务端返回真实解析结果…' : '';

  return <>
    <section className="work-grid">
      <div className="work-side">
        <section className="panel intake"><div className="panel-title"><span>01</span><div><h2>处理一份材料</h2><p>上传后先生成解析原文，编辑并确认后再提取字段和写入知识库。</p></div></div>
          <label className="file-picker"><input ref={fileInputRef} type="file" onChange={chooseFile} /><b>{file ? file.name : '选择文件'}</b><span>{file ? `${Math.ceil(file.size / 1024)} KB · 已选择` : 'PDF / Office / 图片 / Markdown / TXT'}</span></label>
          <label>知识范围<select value={scopeId} onChange={(e) => setScopeId(e.target.value)}>{scopeOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select></label>
          <label>知识类型<select value={knowledgeType} onChange={(e) => setKnowledgeType(e.target.value)}>{knowledgeTypes.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <div className="actions"><button type="button" onClick={upload} disabled={!file || busy}>{uploadTarget ? '上传替换版本' : '上传文件'}</button><button type="button" className="secondary" onClick={() => parse()} disabled={Boolean(busy)}>{selected && !uploaded ? '解析已选文件' : '开始解析'}</button></div>
          <div className="notice" role="status">{notice}</div>
        </section>
        <section className="panel progress"><div className="panel-title"><div><h2>当前进度</h2><p>{current ? `${current.file_name} · ${stageLabel(currentStage)}` : '尚未选择处理对象'}</p></div></div><Pipeline stage={currentStage} draftStatus={draftStatus} error={current?.error} />{parseStats && <p className="parse-progress-note" role="status">{parseStats}</p>}</section>
      </div>
<div className="preview-stack">
        <section className="panel preview-panel">
        {<section className={`standalone-parse-preview${!generatedChunks.length ? ' is-raw-view' : ''}`} aria-label="Perception 解析原文">
          <div className="mineru-original-head"><strong>解析原文（可直接编辑）</strong><span>编辑内容需点击保存；保存原文后再分块，将按最新原文生成。</span></div>
          {parsed && !generatedChunks.length && <><MineruInlinePreview text={parsed.raw_text || ''} blocks={parsed.edited ? [] : previewBlocks} pageTexts={parsed.edited ? {} : parsed.page_texts} pageManifest={parsed.edited ? [] : (parsed.metadata?.pages || [])} images={parsed.metadata?.images || parsed.images || []} editable onChange={updateParsedRawText} onDraftChange={updateParsedDraft} /><div className="chunk-actions chunk-actions-generate"><button type="button" onClick={saveParsedRawText} disabled={!parsedDirty || Boolean(busy)}>{busy === 'save-parse' ? '保存中…' : parsedDirty ? '保存原文' : '已保存'}</button>{supportsChunking && <button type="button" onClick={generateChunks} disabled={Boolean(busy) || parsedDirty || !String(parsed.raw_text || '').trim()} title={parsedDirty ? '请先保存修改后的解析原文' : '根据已保存的解析原文分块'}>分块</button>}<button type="button" className="secondary" onClick={() => extractFields?.()} disabled={Boolean(busy) || parsedDirty || !String(parsed.raw_text || '').trim()} title={parsedDirty ? '请先保存修改后的解析原文' : '使用已保存的解析原文提取字段'}>字段提取</button></div></>}
          {!parsed && !generatedChunks.length && <div className="preview-empty"><strong>等待解析结果</strong><span>选择并上传文件后，点击左侧“开始解析”。</span></div>}
          {generatedChunks.length > 0 && supportsChunking && <section className="generated-chunk-preview" aria-label="已生成的文档分块"><div className="generated-chunk-head"><div><strong>分块结果</strong><span>{generatedChunks.length} 个；编辑后点击保存，分块内容将同步写回解析原文。</span></div><button type="button" className="text-button" onClick={() => { setGeneratedChunks([]); setParsedDirty(false); setNotice('已返回已保存的解析原文，可继续编辑；修改后需先保存再重新分块。'); }}>重新编辑原文</button></div><div className="generated-chunk-list">{generatedChunks.map((chunk, index) => { const visibility = chunk.metadata?.field_visibility || chunk.proposed_visibility || 'public'; return <article key={`${chunk.block_id || index}-${index}`}><header><span>#{index + 1}</span><span>{visibility === 'public' ? '公共库 chunk' : '不进入向量库'}</span><span>{blockLocationLabel(chunk)}</span></header><textarea value={chunk.text || ''} onChange={(event) => updateGeneratedChunk(index, event.target.value)} aria-label={`编辑第 ${index + 1} 个分块`} rows={6} /></article>; })}</div><div className="chunk-actions chunk-actions-bottom"><button type="button" onClick={saveGeneratedChunks} disabled={Boolean(busy)}>{busy === 'save-parse' ? '保存中…' : '保存'}</button><button type="button" className="secondary" onClick={() => extractFields?.()} disabled={Boolean(busy) || parsedDirty || !parsed?.raw_text?.trim()} title={parsedDirty ? '请先保存分块修改' : '使用已保存的解析原文提取字段'}>字段提取</button></div></section>}
          {currentStage === 'index' && current?.status === '已建立索引' && <div className="workflow-success" role="status"><strong>索引已完成</strong><span>Retrieval 已将 {current?.indexed_count ?? 0} 条索引写入 Milvus{indexDetail}，可以进行文档检索。</span></div>}
          {current?.error && currentStage === 'failed' && <div className="workflow-error" role="alert"><strong>上次处理失败</strong><span>{current.error_code ? `[${current.error_code}] ` : ''}{errorMessage(current.error)}</span><small>解析结果仍保留，点击下方按钮可以重新提交索引。</small></div>}
        </section>}
      </section>


      </div>
    </section>
          {hasResult && <section className="llm-draft-panel" aria-label="LLM 字段草稿"><div className="panel-title"><span>LLM</span><div><h2>LLM 字段草稿</h2><p>根据已保存的解析原文提取字段，可编辑后确认。</p>{current?.draft_duration_ms != null && <small>字段提取耗时：{current.draft_duration_ms >= 1000 ? `${(current.draft_duration_ms / 1000).toFixed(2)} 秒` : `${current.draft_duration_ms} 毫秒`}</small>}</div></div>{visibleDraftPreviews.length > 0 && <div className="admin-draft-preview"><div className="generated-chunk-head"><div><strong>草稿候选（{visibleDraftPreviews.length} 条）</strong><span>LLM 判断为多条资料时分别编辑、确认。</span></div></div><div className="admin-draft-list">{visibleDraftPreviews.map((draft, draftIndex) => { const isExpanded = expandedDraftIndex === draftIndex; const title = draft.title || draftFieldsForDisplay(draft, fieldPolicies).find(([, value]) => String(value || '').trim())?.[1] || `未命名${draft.draft_type || '资料'}`; return <article className={`admin-draft-item${isExpanded ? ' is-expanded' : ''}`} key={draft.draft_id || draftIndex}><button type="button" className="admin-draft-item-head" aria-expanded={isExpanded} onClick={() => setExpandedDraftIndex(isExpanded ? null : draftIndex)}><span>{draftIndex + 1}. {title}</span><span>{isExpanded ? '收起' : '展开'}</span></button>{isExpanded && <div className="admin-draft-fields">{draftFieldsForDisplay(draft, fieldPolicies).map(([key, value, label]) => { const selectedImage = key === 'first_img_url' ? parsedImages.find((image) => imageMatches(value, image)) : null; return <label className={LONG_DRAFT_FIELDS.has(key) ? 'draft-field-long' : ''} key={key}><span>{label || displayDraftField(key, draft.draft_type)}</span>{key === 'first_img_url' && parsedImages.length > 0 ? <><select value={String(value || '')} aria-label={label || '封面图片'} onChange={(event) => updateDraft?.(draftIndex, key, event.target.value)}><option value="">不设置封面图</option>{parsedImages.map((image, imageIndex) => { const option = imageValue(image); return option ? <option value={option} key={`${option}-${imageIndex}`}>{imageOptionLabel(image, imageIndex)}</option> : null; })}</select>{selectedImage && imageValue(selectedImage) && <img className="draft-cover-preview" src={api.perceptionAssetUrl(selectedImage.url || imageValue(selectedImage))} alt="当前封面图预览" loading="lazy" />}</> : <textarea value={String(value || '')} rows={LONG_DRAFT_FIELDS.has(key) ? 4 : 2} onChange={(event) => updateDraft?.(draftIndex, key, event.target.value)} />}</label>; })}</div>}</article>; })}</div></div>}{!visibleDraftPreviews.length && draftStatus === 'RUNNING' && <div className="draft-generating-notice" role="status">LLM 正在提取字段，完成后会自动显示在这里。</div>}{!visibleDraftPreviews.length && draftStatus === 'FAILED' && <div className="workflow-error" role="alert"><strong>字段提取失败</strong><span>{draftFailureMessage(current)}</span><small>可根据错误类别修正后重试；详细异常见任务审计记录。</small></div>}{!visibleDraftPreviews.length && !['RUNNING', 'FAILED'].includes(draftStatus) && <div className="draft-generating-notice" role="status">暂无字段草稿，尚未提取。</div>}{visibleDraftPreviews.length > 0 && <div className="chunk-actions draft-confirm-actions"><button type="button" onClick={() => confirmIndex(null, false, 'draft')} disabled={Boolean(busy) || (draftPublished && !draftDirty)} aria-busy={busy === 'index'}>{busy === 'index' ? '写入中…' : draftPublished && !draftDirty ? '已发布' : '发布'}</button></div>}</section>}
  </>;
}
