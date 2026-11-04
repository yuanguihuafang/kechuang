import { knowledgeTypeLabels } from '../../config/fieldPolicy.js';
import { PageJump } from './PageJump.jsx';

const statusLabels = { INDEXED: '已建索引', WAITING_REVIEW: '待确认', FAILED: '失败', RECYCLED: '回收站', UNPUBLISHED: '已下架', WITHDRAWN: '已下架', EXPIRED: '已过期', CONFIRMED: '已确认', PENDING_REVIEW: '待审核', DRAFT_GENERATED: '草稿已生成' };

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false }).replaceAll('/', '-');
}

function formatDelta(value) {
  if (value === null || value === undefined || value === '') return '—';
  const delta = Number(value);
  return Number.isFinite(delta) ? `${delta >= 0 ? '+' : ''}${delta}` : '—';
}

export function AdminOverview({ files, updates = [], updatePage = 1, updateTotal = 0, updateTotalPages = 0, onUpdatePageChange, tasks, summary, loading, onRefresh, mysqlCounts, onNavigate, getKnowledgeType }) {
  const activeFiles = files.filter((file) => file.status !== 'PURGED');
  const waiting = summary?.waiting ?? activeFiles.filter((file) => ['WAITING_REVIEW', 'WAITING_CONFIRM', 'UPLOADED'].includes(file.status) || ['confirm', 'upload'].includes(file.stage)).length;
  const failed = summary?.task_failed ?? summary?.failed ?? activeFiles.filter((file) => file.status === 'FAILED' || file.supervisor_task?.status === 'FAILED').length;
  const indexed = Object.values(mysqlCounts || {}).reduce((sum, item) => sum + Number(item.milvus_count || 0), 0);
  const processing = summary?.task_active ?? tasks.filter((task) => ['QUEUED', 'RUNNING', 'PARSING', 'INDEXING', 'RECOVERING'].includes(task.status)).length;
  const recent = updates.map((update) => ({
    ...update,
    document_names: update.documents?.length
      ? update.documents.map((item) => item.source_name || item.file_name || '—')
      : (update.document_names?.length ? update.document_names : ['—']),
    draft_names: [update.draft_title || '—'],
  }));
  const typeCounts = Object.entries(knowledgeTypeLabels).map(([type, label]) => ({
    type,
    label,
    count: Number(mysqlCounts?.[type]?.milvus_count || 0),
    deltaValue: mysqlCounts?.[type]?.comparison?.delta === null || mysqlCounts?.[type]?.comparison?.delta === undefined
      ? null
      : Number.isFinite(Number(mysqlCounts[type].comparison.delta)) ? Number(mysqlCounts[type].comparison.delta) : null,
    delta: formatDelta(mysqlCounts?.[type]?.comparison?.delta),
  }));
  const comparisonValues = typeCounts.map((item) => item.deltaValue).filter((value) => value !== null);
  const comparisonText = comparisonValues.length ? formatDelta(comparisonValues.reduce((sum, value) => sum + value, 0)) : '—';
  const metrics = [
    { icon: '▤', label: '向量资料总量', value: `${indexed} 条`, delta: comparisonText, note: 'Milvus 已写入草稿主记录', tone: 'blue' },
    { icon: '◎', label: '可检索资料', value: `${indexed} 条`, note: '按草稿主记录去重', tone: 'orange' },
    { icon: '◌', label: '处理任务', value: `${processing} 项`, note: `${waiting} 项等待处理`, tone: 'purple' },
    { icon: '☆', label: '异常任务', value: `${failed} 项`, note: failed ? '需要人工处理' : '当前运行正常', tone: failed ? 'red' : 'orange' },
  ];
  const shortcuts = [
    { icon: '▤', label: '新建文档', page: 'documents' }, { icon: '▧', label: '知识审核', page: 'documents' },
    { icon: '➤', label: '检索验证', page: 'retrieval' }, { icon: '▥', label: '会话管理', page: 'tasks' },
  ];

  return <section className="admin-console admin-dashboard" aria-labelledby="admin-overview-title">
    <div className="admin-dashboard-head"><div><h1 id="admin-overview-title">管理总览</h1><p className="admin-kicker">DASHBOARD / KNOWLEDGE OPERATIONS</p></div><button type="button" className="admin-refresh" onClick={onRefresh} disabled={loading}>刷新数据</button></div>
    <div className="dashboard-layout"><div className="dashboard-main"><div className="dashboard-metric-grid">{metrics.map((metric) => <article className={`dashboard-metric metric-${metric.tone}`} key={metric.label}><span className="metric-icon">{metric.icon}</span><div><small>{metric.label}</small><strong>{metric.value}{metric.delta && <span className="metric-delta"> {metric.delta}</span>}</strong><em>{metric.note}</em></div></article>)}</div>
      <section className="dashboard-storage"><header><h2>知识资产分类</h2><span>按 Milvus 中已发布草稿主记录统计，附件分块不重复计数</span></header><div>{typeCounts.map((item) => <article key={item.label}><span>{item.label}</span><strong>{item.count} <small className="metric-delta">{item.delta}</small></strong><small>Milvus 草稿资料</small></article>)}</div></section>
    </div>
    <aside className="dashboard-shortcuts" aria-label="快捷入口"><h2>快捷入口</h2><div>{shortcuts.map((item) => <button type="button" key={item.label} onClick={() => onNavigate(item.page)}><span>{item.icon}</span><small>{item.label}</small></button>)}</div></aside>
    <section className="dashboard-documents">
      <header><h2>最新知识库更新</h2><button type="button" onClick={() => onNavigate('documents')}>查看全部</button></header>
      {recent.length ? <>
        <div className="dashboard-table-wrap"><table><thead><tr><th>草稿</th><th>文档</th><th>知识类型</th><th>提交人</th><th>企业</th><th>更新时间</th><th>状态</th><th>操作</th></tr></thead><tbody>{recent.map((file) => { const owner = file.user_name || (file.user_id === 'admin' ? '管理员' : file.user_id) || '—'; const knowledgeType = file.draft_type === 'solution' ? '方案' : knowledgeTypeLabels[file.draft_type] || file.draft_type || '—'; return <tr key={file.record_id || file.document_id}><td><b>{file.draft_names.join('、')}</b></td><td><b>{file.document_names.join('、')}</b>{file.document_names.length > 1 && <small className="dashboard-document-source">共 {file.document_names.length} 个附件</small>}</td><td>{knowledgeType}</td><td>{owner}</td><td>{file.company_name || (owner === '管理员' ? '平台管理员' : '未关联企业')}</td><td>{formatTime(file.updated_at)}</td><td><span className={`dashboard-status status-${String(file.status || '').toLowerCase()}`}>{statusLabels[file.status] || file.status || '已发布'}</span></td><td><button type="button" onClick={() => onNavigate('documents', file.draft_type, file)}>查看详情</button></td></tr>; })}</tbody></table></div>
        {updateTotalPages > 1 && <nav className="task-pagination" aria-label="知识库更新分页"><span>第 {updatePage} / {updateTotalPages} 页，共 {updateTotal} 条</span><div><button type="button" className="secondary" onClick={() => onUpdatePageChange(Math.max(1, updatePage - 1))} disabled={updatePage <= 1 || loading}>上一页</button><PageJump page={updatePage} totalPages={updateTotalPages} onJump={onUpdatePageChange} disabled={loading} label="知识库更新" /><button type="button" className="secondary" onClick={() => onUpdatePageChange(Math.min(updateTotalPages, updatePage + 1))} disabled={updatePage >= updateTotalPages || loading}>下一页</button></div></nav>}
      </> : <p className="dashboard-empty">{loading ? '正在读取知识库更新…' : '暂无知识库更新。'}</p>}
    </section>
    </div>
  </section>;
}
