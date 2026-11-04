import { useEffect, useState } from 'react';
import { VersionPanel } from './DocumentPanels.jsx';
import { FileTable } from '../admin/FileTable.jsx';
import { api } from '../../api.js';
import { displayDraftField } from '../../draftFieldLabels.js';
import { totalSyncCount } from './mysqlSyncCounts.js';
import { groupSkippedSources, useCollectionRecovery } from './collectionRecovery.js';
import { PageJump } from '../admin/PageJump.jsx';
import { AuthenticatedDownload } from './AuthenticatedDownload.jsx';

const LONG_TEXT_FIELDS = new Set([
  'contect', 'analyse_contect', 'description', 'introduction', 'brief_intr',
  'achievements', 'research_field', 'technical_achievements', 'technological_achievements',
  'main_function', 'main_advantage', 'application', 'business_scope', 'enterprise_portrait',
]);
const draftStatusLabels = { PUBLISHED: '已发布', WITHDRAWN: '已下架', DELETED: '已删除', CONFIRMED: '已确认', PENDING_REVIEW: '待审核', DRAFT_GENERATED: '草稿已生成', FAILED: '失败' };

function DraftLifecycleActions({ draft, busy, onUnpublishDraft, onRepublishDraft, onDeleteDraft, onPurgeDraft }) {
  if (!draft) return null;
  if (draft.status === 'PUBLISHED') return <button type="button" className="text-button danger-text" onClick={() => onUnpublishDraft?.(draft)} disabled={Boolean(busy)}>下架</button>;
  if (draft.status === 'WITHDRAWN') return <><button type="button" className="text-button" onClick={() => onRepublishDraft?.(draft)} disabled={Boolean(busy)}>重新上架</button><button type="button" className="text-button danger-text" onClick={() => onDeleteDraft?.(draft)} disabled={Boolean(busy)}>彻底删除</button></>;
  if (draft.status === 'CONFIRMED') return <button type="button" className="text-button danger-text" onClick={() => onDeleteDraft?.(draft)} disabled={Boolean(busy)}>删除记录</button>;
  if (draft.status === 'DELETED') return <button type="button" className="text-button danger-text" onClick={() => onPurgeDraft?.(draft)} disabled={Boolean(busy)}>立即彻底删除</button>;
  return null;
}

export function AdminDocumentsPage({ knowledgeTypes, catalogKnowledgeType, getKnowledgeType, switchCatalog, fieldPolicies = [], files, documentSearch = '', onDocumentSearchChange, documentPage = 1, documentTotal = 0, documentTotalPages = 0, onDocumentPageChange, versionsDocument, toggleDocumentDetails, fileLoading, versionsProps, supervisorTasks, recoverTask, busy, refreshFiles, onSyncMysql, onStopMysql, mysqlSync, mysqlCounts, onRebuildMysqlData, knowledgeUpdates = [], knowledgeUpdatePage = 1, knowledgeUpdateTotal = 0, knowledgeUpdateTotalPages = 0, onKnowledgeUpdatePageChange, onOpenKnowledgeUpdate, knowledgeUpdateDetail, knowledgeUpdateDetailLoading, onCloseKnowledgeUpdate, onUnpublishDraft, onRepublishDraft, onDeleteDraft, onPurgeDraft, catalogOnly = false, showCatalog = true, showCatalogTabs = true, showDraftCatalog = true, showUpdateDetail = true, showDocumentCatalog = true }) {
  const [draftTexts, setDraftTexts] = useState({});
  const [documentSearchInput, setDocumentSearchInput] = useState(documentSearch);
  const [detailDraftPage, setDetailDraftPage] = useState(1);
  const [confirmingUpdate, setConfirmingUpdate] = useState(false);
  useEffect(() => setDraftTexts(Object.fromEntries((knowledgeUpdateDetail?.drafts || []).map((item) => [item.draft_id, JSON.stringify(item.draft || item, null, 2)]))), [knowledgeUpdateDetail]);
  useEffect(() => setDocumentSearchInput(documentSearch), [documentSearch]);
  useEffect(() => setDetailDraftPage(1), [knowledgeUpdateDetail]);
  const draftItems = knowledgeUpdates;
  const lifecycleDrafts = (knowledgeUpdateDetail?.drafts || []).map((item) => ({
    ...item,
    status: item.status || knowledgeUpdateDetail?.status,
  }));
  const detailDraftCount = lifecycleDrafts.length;
  const currentDetailDraft = lifecycleDrafts[detailDraftPage - 1];
  const currentDraftStatus = currentDetailDraft?.status || knowledgeUpdateDetail?.status;
  const currentStatusNote = currentDetailDraft?.draft?.status_note || currentDetailDraft?.status_note;
  const currentDeletedAt = currentDetailDraft?.draft?.deleted_at || currentDetailDraft?.deleted_at;
  function draftValue(item) {
    const fallback = item.draft || item;
    const text = draftTexts[item.draft_id];
    if (!text) return fallback;
    try { return JSON.parse(text); } catch { return fallback; }
  }
  function draftEntries(item) {
    const draft = draftValue(item);
    const values = draft.fields || {};
    const policy = fieldPolicies.find((entry) => entry.type === (item.draft_type || draft.draft_type));
    if (Array.isArray(policy?.fields) && policy.fields.length) {
      const knownKeys = new Set(policy.fields.map(({ key }) => key));
      const ordered = policy.fields
        .filter(({ key }) => Object.prototype.hasOwnProperty.call(values, key))
        .map(({ key, label }) => [key, values[key], label]);
      const unconfigured = Object.entries(values)
        .filter(([key]) => !knownKeys.has(key))
        .map(([key, value]) => [key, value, '']);
      return [...ordered, ...unconfigured];
    }
    return Object.entries(values).map(([key, value]) => [key, value, '']);
  }
  function updateDraftField(item, key, value) {
    const current = draftValue(item);
    setDraftTexts((previous) => ({ ...previous, [item.draft_id]: JSON.stringify({ ...current, fields: { ...(current.fields || {}), [key]: value } }) }));
  }
  async function confirmUpdate() {
    if (!knowledgeUpdateDetail) return;
    setConfirmingUpdate(true);
    try {
      const drafts = Object.entries(draftTexts).map(([draft_id, value]) => ({ draft_id, draft: JSON.parse(value) }));
      await api.adminConfirmKnowledgeUpdate(knowledgeUpdateDetail.record_id, { operator_id: 'admin', drafts });
      onCloseKnowledgeUpdate();
      await refreshFiles();
    } catch (error) {
      window.alert(`确认更新失败：${error.message}`);
    } finally { setConfirmingUpdate(false); }
  }
  const allCatalogTypes = !catalogKnowledgeType;
  const selectedSync = mysqlSync?.[catalogKnowledgeType];
  const recovery = useCollectionRecovery(api, catalogKnowledgeType, !catalogOnly, refreshFiles);
  const skippedGroups = groupSkippedSources(recovery.report?.skipped_sources);
  const maintenanceBusy = Boolean(busy) || recovery.loading || Boolean(recovery.pending) || Boolean(recovery.readError);
  const maintenanceBlocked = maintenanceBusy || recovery.blockWrites;
  const selectedCounts = allCatalogTypes
    ? {
      mysql_count: totalSyncCount(mysqlCounts, 'mysql_count'),
      milvus_count: totalSyncCount(mysqlCounts, 'milvus_count'),
    }
    : mysqlCounts?.[catalogKnowledgeType] || {};
  const hasCountErrors = Object.values(mysqlCounts || {}).some((item) => item?.error);
  const mysqlCountsError = mysqlCounts?.__error || selectedCounts.error
    || (allCatalogTypes && hasCountErrors ? '部分知识库统计失败，汇总可能不完整。' : null);
  const syncRunning = ['RUNNING', 'STOPPING'].includes(selectedSync?.status);
  const syncActive = busy === 'mysql-sync' || syncRunning;
  const syncStatusLabels = { QUEUED: '排队中', RUNNING: '同步中', STOPPING: '停止中', STOPPED: '已停止', INTERRUPTED: '已中断', SUCCESS: '已完成', FAILED: '同步失败' };
  const syncProgress = selectedSync
    ? `${syncStatusLabels[selectedSync.status] || selectedSync.status || '同步中'}：已处理 ${selectedSync.processed ?? 0} / ${selectedSync.total ?? 0} 条，已写入 ${selectedSync.indexed_count ?? 0} 条`
    : '';
  return <section className="admin-page-content">
    {!catalogOnly && <header className="admin-page-intro"><div><h1>数据管理</h1><p className="admin-kicker">KNOWLEDGE / DATA MANAGEMENT</p><p>以知识库更新和同步为主。</p></div></header>}
    {!catalogOnly && <section className="panel mysql-sync-panel">
      <div className="section-head">
        <div><p className="overline">MYSQL / MILVUS SYNC</p><h2>MySQL 数据同步</h2><p>每批同步 64 条；服务重启后不会自动继续，请管理员手动同步。</p></div>
        <div className="section-actions">
          <div className="mysql-sync-summary"><span><small>MySQL 数据</small><strong>{selectedCounts.mysql_count ?? '—'}</strong><em>条</em></span><span><small>Milvus 向量</small><strong>{selectedCounts.milvus_count ?? '—'}</strong><em>条</em></span></div>
          {syncActive && <span className="mysql-sync-progress" role="status">{syncProgress}</span>}
          {!allCatalogTypes && <>
            <button type="button" className="secondary compact" onClick={() => syncRunning ? onStopMysql(catalogKnowledgeType) : onSyncMysql(catalogKnowledgeType)} disabled={selectedSync?.status === 'QUEUED' || selectedSync?.status === 'STOPPING' || (!syncRunning && maintenanceBlocked)}>{syncRunning ? '停止同步' : selectedSync?.status === 'STOPPED' ? '继续同步' : `同步${knowledgeTypes.find(([value]) => value === catalogKnowledgeType)?.[1] || '此表'}`}</button>
            <button type="button" className="text-button" onClick={() => onRebuildMysqlData?.(catalogKnowledgeType)} disabled={maintenanceBlocked || syncRunning || selectedSync?.status === 'QUEUED'}>清理同步数据</button>
            <button type="button" className="text-button" onClick={recovery.rebuild} disabled={maintenanceBlocked || syncRunning || selectedSync?.status === 'QUEUED'}>{recovery.pending === 'preflight' ? '检查中…' : '检查可恢复来源'}</button>
          </>}
        </div>
      </div>
      {!allCatalogTypes && <>
        <p role="status" aria-live="polite">{recovery.loading ? '正在读取索引维护状态…' : recovery.message}</p>
        {recovery.task?.error && <p role="alert" className="inline-error">{recovery.task.error}</p>}
        {recovery.readError && <p role="alert" className="inline-error">维护状态读取或提交失败：{recovery.readError} <button type="button" className="text-button" onClick={recovery.refresh}>重新读取</button></p>}
        {recovery.report && <div className="collection-recovery-report" aria-label="Collection 可恢复来源预检报告">
          <p>可恢复来源：{recovery.report.recoverable_count ?? 0} 项；MySQL 记录：{recovery.report.mysql_record_count ?? 0} 条；不可恢复数据：{skippedGroups.length} 条（共 {recovery.report.skipped_count ?? 0} 个问题）。</p>
          {recovery.report.skipped_sources?.length > 0 ? <details open>
            <summary>查看不可恢复项与原因</summary>
            <ul className="collection-recovery-groups">{skippedGroups.map((group) => <li className="collection-recovery-group" key={group.key}>
              <div className="collection-recovery-record">
                {group.related_sources.length ? group.related_sources.map((related) => <div key={`${related.draft_id}:${related.document_id}`}>
                  草稿：{related.draft_title || related.draft_id || '未知'}（ID：{related.draft_id || '—'}）；附件：{related.attachment_name || group.attachment_name || '未知文件'}（文档 ID：{related.document_id || group.document_id || '—'}）
                </div>) : <>文档 ID：{group.document_id || '—'}{group.attachment_name ? `；附件：${group.attachment_name}` : ''}</>}
              </div>
              <ul className="collection-recovery-issues">{group.issues.map((source) => <li key={`${source.source_type}:${source.source_id}`}>
                {source.source_type} · {source.source_id}：{source.reason}
              </li>)}</ul>
            </li>)}</ul>
          </details> : <p>已发布草稿及附件来源均通过预检。</p>}
          {recovery.showRestore && <button type="button" className="secondary compact" onClick={recovery.restore} disabled={maintenanceBlocked || syncRunning || recovery.pending === 'restore'}>{recovery.pending === 'restore' ? '提交恢复中…' : '确认并恢复影子 Collection'}</button>}
          {recovery.showActivate && <button type="button" className="compact" onClick={recovery.activate} disabled={maintenanceBlocked || syncRunning || recovery.pending === 'activate'}>{recovery.pending === 'activate' ? '提交切换中…' : '确认切换活动 Collection'}</button>}
          {recovery.task?.status === 'SUCCESS' && <p>校验通过，活动 Alias 已切换；旧 Collection 保留为恢复前版本。</p>}
        </div>}
      </>}
      {mysqlCountsError && <p role="alert" className="inline-error">统计读取失败：{mysqlCountsError}</p>}
    </section>}
    {showCatalog && <>
    {showCatalogTabs && <nav className="knowledge-catalog-tabs" aria-label="知识库分类"><button type="button" className={allCatalogTypes ? 'active' : ''} onClick={() => switchCatalog('')}>全部</button>{knowledgeTypes.map(([value, label]) => <button type="button" key={value} className={catalogKnowledgeType === value ? 'active' : ''} onClick={() => switchCatalog(value)}>{label}</button>)}</nav>}
    {showDraftCatalog && <section className="panel files">
      <div className="section-head"><div><p className="overline">POSTGRESQL / DRAFT CATALOG</p><h2>{knowledgeTypes.find(([value]) => value === catalogKnowledgeType)?.[1] || '知识库'}草稿目录</h2><p>已发布和已下架草稿都会保留显示；附件原文在详情中查看或下载。</p></div><button type="button" className="secondary compact" onClick={refreshFiles} disabled={fileLoading}>刷新目录</button></div>
      <div className="draft-catalog-table-wrap"><table className="draft-catalog-table"><thead><tr><th>草稿</th><th>文档</th><th>提交人</th><th>企业</th><th>更新时间</th><th>状态</th><th>操作</th></tr></thead><tbody>{draftItems.map((item) => <tr key={item.draft_id || item.record_id}><td><strong>{item.draft_title || '—'}</strong></td><td><span className="draft-catalog-document-names" title={(item.document_names || ['—']).join('、')}>{(item.document_names || ['—']).join('、')}</span></td><td>{item.user_name || item.user_id || '—'}</td><td>{item.company_name || '未关联企业'}</td><td>{item.updated_at ? new Date(item.updated_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</td><td>{draftStatusLabels[item.status] || item.status || '—'}</td><td><button type="button" className="text-button" onClick={() => onOpenKnowledgeUpdate?.(item)}>查看详情</button></td></tr>)}</tbody></table>{!draftItems.length && <p className="table-empty">暂无草稿记录。</p>}</div>
      {knowledgeUpdateTotalPages > 1 && <nav className="task-pagination" aria-label="知识库草稿目录分页"><span>第 {knowledgeUpdatePage} / {knowledgeUpdateTotalPages} 页，共 {knowledgeUpdateTotal} 条</span><div><button type="button" className="secondary" onClick={() => onKnowledgeUpdatePageChange(Math.max(1, knowledgeUpdatePage - 1))} disabled={knowledgeUpdatePage <= 1 || fileLoading}>上一页</button><PageJump page={knowledgeUpdatePage} totalPages={knowledgeUpdateTotalPages} onJump={onKnowledgeUpdatePageChange} disabled={fileLoading} label="知识库草稿目录" /><button type="button" className="secondary" onClick={() => onKnowledgeUpdatePageChange(Math.min(knowledgeUpdateTotalPages, knowledgeUpdatePage + 1))} disabled={knowledgeUpdatePage >= knowledgeUpdateTotalPages || fileLoading}>下一页</button></div></nav>}
    </section>}
    {showUpdateDetail && knowledgeUpdateDetail && <section className="panel knowledge-update-detail"><div className="section-head"><div><p className="overline">KNOWLEDGE UPDATE</p><h2>知识库更新详情</h2></div><div className="section-actions"><div className="draft-lifecycle-actions"><DraftLifecycleActions draft={currentDetailDraft} busy={busy} onUnpublishDraft={onUnpublishDraft} onRepublishDraft={onRepublishDraft} onDeleteDraft={onDeleteDraft} onPurgeDraft={onPurgeDraft} /></div><button type="button" className="secondary compact" onClick={onCloseKnowledgeUpdate}>关闭</button>{knowledgeUpdateDetail.drafts?.length > 0 && currentDraftStatus !== 'DELETED' && <button type="button" className="compact" onClick={confirmUpdate} disabled={confirmingUpdate}>{confirmingUpdate ? '确认更新中…' : '确认修改并更新'}</button>}</div></div>{knowledgeUpdateDetailLoading ? <p>正在读取详情…</p> : <><div className="dashboard-table-wrap"><table><thead><tr><th>草稿</th><th>文档</th><th>提交人</th><th>企业</th><th>更新时间</th><th>状态</th><th>操作</th></tr></thead><tbody><tr><td>{knowledgeUpdateDetail.draft_title || '—'}</td><td>{knowledgeUpdateDetail.documents?.length ? knowledgeUpdateDetail.documents.map((item) => item.source_name || item.file_name).join('、') : '—'}</td><td>{knowledgeUpdateDetail.user_name || knowledgeUpdateDetail.user_id || '—'}</td><td>{knowledgeUpdateDetail.company_name || '未关联企业'}</td><td>{knowledgeUpdateDetail.updated_at ? new Date(knowledgeUpdateDetail.updated_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</td><td>{draftStatusLabels[knowledgeUpdateDetail.status] || knowledgeUpdateDetail.status || '—'}</td><td>{knowledgeUpdateDetail.attachments?.length ? `${knowledgeUpdateDetail.attachments.length} 个附件` : '无附件'}</td></tr></tbody></table></div>{detailDraftCount > 1 && <nav className="task-pagination" aria-label="草稿详情分页"><span>第 {detailDraftPage} / {detailDraftCount} 页</span><div><button type="button" className="secondary" onClick={() => setDetailDraftPage((page) => page - 1)} disabled={detailDraftPage === 1}>上一页</button><PageJump page={detailDraftPage} totalPages={detailDraftCount} onJump={setDetailDraftPage} disabled={confirmingUpdate || knowledgeUpdateDetailLoading} label="草稿详情" /><button type="button" className="secondary" onClick={() => setDetailDraftPage((page) => page + 1)} disabled={detailDraftPage === detailDraftCount}>下一页</button></div></nav>}<h3>草稿内容</h3>{currentStatusNote && currentDraftStatus === 'WITHDRAWN' && <p role="status" className="inline-notice">下架原因：{currentStatusNote}</p>}{currentDeletedAt && currentDraftStatus === 'DELETED' && <p role="status" className="inline-notice">删除时间：{new Date(currentDeletedAt).toLocaleString('zh-CN', { hour12: false })}；关联资源暂存，管理员可按需立即彻底删除。</p>}{currentDetailDraft ? <details open key={currentDetailDraft.draft_id || detailDraftPage}><summary>{currentDetailDraft.draft?.title || currentDetailDraft.title || currentDetailDraft.draft_id || `草稿 ${detailDraftPage}`}</summary><div className="knowledge-update-fields">{draftEntries(currentDetailDraft).map(([key, value, label]) => { const text = String(value ?? ''); const long = LONG_TEXT_FIELDS.has(key) || text.length > 120; return <label key={key}><span>{label || displayDraftField(key, currentDetailDraft.draft_type)}</span>{long ? <textarea value={text} onChange={(event) => updateDraftField(currentDetailDraft, key, event.target.value)} rows={4} disabled={currentDraftStatus === 'DELETED'} /> : <input value={text} onChange={(event) => updateDraftField(currentDetailDraft, key, event.target.value)} disabled={currentDraftStatus === 'DELETED'} />}</label>; })}</div></details> : <p>本条记录没有草稿。</p>}<h3>附件（{knowledgeUpdateDetail.attachments?.length || 0}）</h3>{knowledgeUpdateDetail.attachments?.length ? <ul>{knowledgeUpdateDetail.attachments.map((item) => <li key={item.document_id}><span>{item.file_name}</span> <AuthenticatedDownload url={api.documentFileUrl(item.document_id, versionsProps?.identity)} filename={item.file_name} label="查看原文 / 下载" /></li>)}</ul> : <p>本条记录没有附件。</p>}</>}</section>}
    {showDocumentCatalog && <section className="panel files"><div className="section-head"><div><p className="overline">POSTGRESQL / DOCUMENT CATALOG</p><h2>{knowledgeTypes.find(([value]) => value === catalogKnowledgeType)?.[1] || '全部知识库'}文档目录</h2><p>{catalogOnly ? '显示全部管理员文档和用户已发布文档；可按文件名或文档 ID 搜索。' : '管理员与用户上传资料统一展示；按需筛选知识类型。'}版本、解析结果和索引生命周期由 PostgreSQL 管理，原文件及图片存放在 MinIO。</p></div><div className="section-actions">
      <form className="document-catalog-search" onSubmit={(event) => { event.preventDefault(); onDocumentSearchChange?.(documentSearchInput.trim()); }}>
        <label className="sr-only" htmlFor="admin-document-search">搜索文件名或文档 ID</label><input id="admin-document-search" type="search" maxLength={200} value={documentSearchInput} onChange={(event) => setDocumentSearchInput(event.target.value)} placeholder="搜索文件名或文档 ID" />
        <button type="submit" className="secondary compact" disabled={fileLoading}>搜索</button>
        {documentSearchInput && <button type="button" className="text-button" onClick={() => { setDocumentSearchInput(''); onDocumentSearchChange?.(''); }} disabled={fileLoading}>清除</button>}
      </form>
      <button type="button" className="secondary compact" onClick={() => refreshFiles({ refreshTasks: true })} disabled={fileLoading}>刷新目录</button></div></div>
      <FileTable files={allCatalogTypes ? files : files.filter((document) => getKnowledgeType(document) === catalogKnowledgeType)} selectedId={versionsDocument?.document_id} onSelect={toggleDocumentDetails} loading={fileLoading} page={documentPage} total={documentTotal} totalPages={documentTotalPages} onPageChange={onDocumentPageChange} renderDetail={(document) => <VersionPanel {...versionsProps} document={document} identity={versionsProps.identity} onUpdate={versionsProps.updateDocument} onUnpublish={(version) => versionsProps.changeVersionPublication(version, 'unpublish')} onRepublish={(version) => versionsProps.changeVersionPublication(version, 'publish')} onRecycle={(version) => versionsProps.changeVersionStorage(version, 'recycle')} onRestore={(version) => versionsProps.changeVersionStorage(version, 'restore')} onPurge={(version) => versionsProps.changeVersionStorage(version, 'purge')} />} />
      {supervisorTasks.some((task) => task.status === 'MANUAL_REVIEW' && task.operation === 'supervisor_compensation' && task.recovery_message) && <div className="task-review-list"><strong>待人工接管任务</strong>{supervisorTasks.filter((task) => task.status === 'MANUAL_REVIEW' && task.operation === 'supervisor_compensation' && task.recovery_message).map((task) => <div className="task-review-row" key={task.task_id}><span>{task.task_id} · {task.selected_tool || task.operation}</span><button className="text-button" type="button" onClick={() => recoverTask(task)} disabled={busy === 'recover'}>确认恢复一次</button></div>)}</div>}
    </section>}
    </>}
  </section>;
}
