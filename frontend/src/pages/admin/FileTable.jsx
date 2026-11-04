import { Fragment } from 'react';
import { stageLabel } from '../documents/Pipeline.jsx';
import { publicationStatusLabel } from '../documents/documentStatus.js';
import { PageJump } from './PageJump.jsx';

function formatBytes(size) {
  if (!size) return '—';
  return size < 1024 * 1024 ? `${Math.ceil(size / 1024)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
}

const taskStatusLabel = (task) => {
  if (task?.status === 'SUCCEEDED' && task.lifecycle_action === 'recycle') return '已移入回收站';
  if (task?.status === 'SUCCEEDED' && task.lifecycle_action === 'purge') return '已删除';
  return ({
    WAITING_CONFIRM: '解析完成，待确认', WAITING_DRAFT_CONFIRM: '草稿待确认', WAITING_INTENT_CONFIRM: '意图待确认', MANUAL_REVIEW: '待人工接管', RECOVERING: '恢复中', SUCCEEDED: '已建立索引', FAILED: '任务失败',
  }[task?.status] || task?.status);
};

export function FileTable({ files, selectedId, onSelect, renderDetail, loading, page = 1, total = 0, totalPages = 0, onPageChange }) {
  if (loading) return <div className="table-empty">正在读取 PostgreSQL 文档目录…</div>;
  if (!files.length) return <><div className="table-empty">文档目录中还没有材料。</div>{totalPages > 1 && <nav className="task-pagination" aria-label="文档目录分页"><span>第 {page} / {totalPages} 页，共 {total} 条</span><div><button type="button" className="secondary" onClick={() => onPageChange(Math.max(1, page - 1))} disabled={page <= 1 || loading}>上一页</button><PageJump page={page} totalPages={totalPages} onJump={onPageChange} disabled={loading} label="文档目录" /><button type="button" className="secondary" onClick={() => onPageChange(Math.min(totalPages, page + 1))} disabled={page >= totalPages || loading}>下一页</button></div></nav>}</>;
  const showBusinessMeta = files.some((file) => ['demand', 'solution', 'achievement'].includes(file.knowledge_type));
  return <>
  <div className="file-table-wrap">
    <table className="file-table">
      <colgroup>
        <col className="file-col-name" />
        {showBusinessMeta && <col className="file-col-role" />}
        <col className="file-col-scope" /><col className="file-col-progress" /><col className="file-col-updated" /><col className="file-col-actions" />
      </colgroup>
      <thead><tr><th>文件</th>{showBusinessMeta && <th>提交人</th>}<th>范围</th><th>进度</th><th>更新时间</th><th /></tr></thead>
      <tbody>{files.map((file, index) => { const rowKey = file.storage_key || file.document_id || `${file.file_name || 'document'}-${index}`; return <Fragment key={rowKey}>
        <tr key={rowKey} className={selectedId === file.document_id ? 'selected' : ''}>
          <td><strong title={file.file_name}>{file.file_name}</strong><small>{formatBytes(file.file_size)}</small></td>
          {showBusinessMeta && <td className="file-role-cell">{file.user_name || file.role_label || file.user_id || '未知角色'}</td>}
          <td className="file-scope-cell">{file.scope_id || '未记录'}</td>
          <td className="file-progress-cell"><span className={`stage stage-${file.storage_key ? (file.stage || 'upload') : 'legacy'}`}>{file.supervisor_task ? taskStatusLabel(file.supervisor_task) : stageLabel(file.storage_key ? (file.stage || 'upload') : 'legacy')}</span>{file.active_version?.publication_status && <small>{publicationStatusLabel(file.active_version)}</small>}{file.active_version?.unpublish_reason_code && <small className="table-error">下架原因：{file.active_version.unpublish_reason_text || file.active_version.unpublish_reason_code}</small>}{file.supervisor_task?.stage && <small>{file.supervisor_task.stage}</small>}{file.indexed_count ? <small>{file.indexed_count} chunks</small> : null}{file.supervisor_task?.status === 'FAILED' && <small className="table-error" title={file.supervisor_task.error || '任务失败'}>{file.supervisor_task.error_code || 'FAILED'}：{file.supervisor_task.error || '未提供错误详情'}</small>}</td>
          <td className="file-updated-cell">{file.updated_at ? new Date(file.updated_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</td>
          <td className="file-actions">{file.status === 'PURGED' || file.active_version?.publication_status === 'PURGED' ? <span className="task-status task-status-purged">已删除</span> : <button className="text-button" type="button" onClick={() => onSelect(file)} aria-expanded={selectedId === file.document_id}>{selectedId === file.document_id ? '收起详情' : '查看详情'}</button>}</td>
        </tr>
        {selectedId === file.document_id && <tr className="file-detail-row"><td colSpan={showBusinessMeta ? 6 : 5}>{renderDetail?.(file)}</td></tr>}
      </Fragment>; })}</tbody>
    </table>
  </div>
  {totalPages > 1 && <nav className="task-pagination" aria-label="文档目录分页"><span>第 {page} / {totalPages} 页，共 {total} 条</span><div><button type="button" className="secondary" onClick={() => onPageChange(Math.max(1, page - 1))} disabled={page <= 1 || loading}>上一页</button><PageJump page={page} totalPages={totalPages} onJump={onPageChange} disabled={loading} label="文档目录" /><button type="button" className="secondary" onClick={() => onPageChange(Math.min(totalPages, page + 1))} disabled={page >= totalPages || loading}>下一页</button></div></nav>}
  </>;
}
