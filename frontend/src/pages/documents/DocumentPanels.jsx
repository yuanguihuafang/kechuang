import { api } from '../../api.js';
import { publicationStatusLabel } from './documentStatus.js';
import { AuthenticatedDownload } from './AuthenticatedDownload.jsx';

const versionStatusLabel = (value) => ({
  PUBLISHED: '已发布', UNPUBLISHED: '已下架', EXPIRED: '已过期', RESTORED: '已恢复', RECYCLED: '回收站',
  INDEXED: '已索引', FAILED: '失败', RUNNING: '处理中',
}[value] || value || '未记录');

export function VersionPanel({ document, versions, loading, busy, identity, associatedDrafts = [], taskRecords = [], onUpdate, onUnpublish, onRepublish, onRecycle, onRestore, onPurge }) {
  if (!document) return null;
  return <section className="document-detail version-panel">
    <div className="section-head document-detail-head">
      <div><p className="overline">DOCUMENT / DETAIL</p><h2>文档详情</h2></div>
      <div className="document-detail-head-meta">
        <span>{document.file_name || '—'}</span>
        <span>文档 ID：{document.document_id || '—'}</span>
      </div>
    </div>
    {loading ? <div className="table-empty">正在读取版本记录…</div> : !versions.length ? <div className="table-empty">暂无可管理的文档版本。</div> : <div className="version-list">
      {versions.map((version) => {
        const published = version.publication_status === 'PUBLISHED' || version.status === 'PUBLISHED';
        const unpublished = version.publication_status === 'UNPUBLISHED' || version.status === 'UNPUBLISHED';
        const recycled = version.publication_status === 'RECYCLED' || version.status === 'RECYCLED';
        return <article className="version-card" key={version.version_id}>
          <div className="version-card-main">
            <small>索引：{versionStatusLabel(version.index_status)} · 可检索：{publicationStatusLabel(version)}</small>
            <small>分块：{version.indexed_count ?? version.chunk_count ?? '—'} · 更新时间：{version.updated_at ? new Date(version.updated_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</small>
            {version.unpublish_reason_code && <small className="version-unpublish-reason">下架原因：{version.unpublish_reason_text || version.unpublish_reason_code}{version.unpublished_at ? ` · ${new Date(version.unpublished_at).toLocaleString('zh-CN', { hour12: false })}` : ''}</small>}
            {version.error && <em>{version.error}</em>}
          </div>
          <div className="version-actions">
            {!recycled && <button className="text-button" type="button" onClick={() => onUpdate?.(document)} disabled={busy}>更新</button>}
            <AuthenticatedDownload url={api.documentFileUrl(document.document_id, identity)} filename={document.file_name} label="下载原文件" />
            {published && <button className="text-button danger-text" type="button" onClick={() => onUnpublish(version)} disabled={busy}>下架</button>}
            {unpublished && !recycled && <button className="text-button" type="button" onClick={() => onRepublish(version)} disabled={busy}>重新上架</button>}
            {unpublished && !recycled && <button className="text-button danger-text" type="button" onClick={() => onRecycle(version)} disabled={busy}>移入回收站</button>}
            {recycled && <><button className="text-button" type="button" onClick={() => onRestore(version)} disabled={busy}>恢复版本</button><button className="text-button danger-text" type="button" onClick={() => onPurge(version)} disabled={busy}>彻底清理</button></>}
          </div>
        </article>;
      })}
    </div>}
    {!loading && <>
      <h3>关联草稿（{associatedDrafts.length}）</h3>
      {associatedDrafts.length ? <ul>{associatedDrafts.map((item) => <li key={item.draft_id}>{item.draft_title || item.draft?.title || item.draft_id} · {item.status || '未知状态'}</li>)}</ul> : <p>暂无关联草稿。</p>}
      <h3>处理任务（{taskRecords.length}）</h3>
      {taskRecords.length ? <ul>{taskRecords.map((item) => <li key={item.task_id}>{item.operation || '任务'} · {item.status || '未知状态'}{item.updated_at ? ` · ${new Date(item.updated_at).toLocaleString('zh-CN', { hour12: false })}` : ''}</li>)}</ul> : <p>暂无关联任务。</p>}
    </>}
  </section>;
}
