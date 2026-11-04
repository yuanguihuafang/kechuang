import { knowledgeTypeLabels } from '../config/fieldPolicy.js';

const statusLabels = { INDEXED: '已建索引', WAITING_REVIEW: '待确认', FAILED: '失败', RECYCLED: '回收站', UNPUBLISHED: '已下架' };

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false }).replaceAll('/', '-');
}

export function AdminOverview({ files, tasks, summary, loading, onRefresh, onNavigate, getKnowledgeType }) {
  const activeFiles = files.filter((file) => file.status !== 'PURGED');
  const indexed = summary?.indexed ?? activeFiles.filter((file) => file.index_status === 'INDEXED' || file.status === 'INDEXED' || file.status === 'PUBLISHED').length;
  const waiting = summary?.waiting ?? activeFiles.filter((file) => ['WAITING_REVIEW', 'WAITING_CONFIRM', 'UPLOADED'].includes(file.status) || ['confirm', 'upload'].includes(file.stage)).length;
  const failed = summary?.task_failed ?? summary?.failed ?? activeFiles.filter((file) => file.status === 'FAILED' || file.supervisor_task?.status === 'FAILED').length;
  const total = summary?.total ?? activeFiles.length;
  const processing = summary?.task_active ?? tasks.filter((task) => ['QUEUED', 'RUNNING', 'PARSING', 'INDEXING', 'RECOVERING'].includes(task.status)).length;
  const recent = [...activeFiles].sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0)).slice(0, 6);
  const typeCounts = Object.entries(knowledgeTypeLabels).map(([type, label]) => ({
    label,
    count: activeFiles.filter((file) => (getKnowledgeType ? getKnowledgeType(file) : file.knowledge_type) === type).length,
  }));
  const metrics = [
    { icon: '▤', label: '知识总量', value: `${total} 份`, note: `较昨日 ${total ? '+ 1' : '—'}`, tone: 'blue' },
    { icon: '◎', label: '已建索引', value: `${indexed} 份`, note: '可进入检索链路', tone: 'orange' },
    { icon: '◌', label: '处理任务', value: `${processing} 项`, note: `${waiting} 项等待处理`, tone: 'purple' },
    { icon: '☆', label: '异常任务', value: `${failed} 项`, note: failed ? '需要人工处理' : '当前运行正常', tone: failed ? 'red' : 'orange' },
  ];
  const shortcuts = [
    { icon: '▤', label: '新建文档', page: 'documents' }, { icon: '▧', label: '知识审核', page: 'documents' },
    { icon: '➤', label: '检索验证', page: 'retrieval' }, { icon: '▥', label: '数据报表', page: 'tasks' },
  ];

  return <section className="admin-console admin-dashboard" aria-labelledby="admin-overview-title">
    <div className="admin-dashboard-head"><div><h1 id="admin-overview-title">管理总览</h1><p className="admin-kicker">DASHBOARD / KNOWLEDGE OPERATIONS</p></div><button type="button" className="admin-refresh" onClick={onRefresh} disabled={loading}>{loading ? '同步中…' : '刷新数据'}</button></div>
    <div className="dashboard-layout"><div className="dashboard-main"><div className="dashboard-metric-grid">{metrics.map((metric) => <article className={`dashboard-metric metric-${metric.tone}`} key={metric.label}><span className="metric-icon">{metric.icon}</span><div><small>{metric.label}</small><strong>{metric.value}</strong><em>{metric.note}</em></div></article>)}</div>
      <section className="dashboard-storage"><header><h2>知识资产分类</h2><span>按业务资料类型统计</span></header><div>{typeCounts.map((item) => <article key={item.label}><span>{item.label}</span><strong>{item.count}</strong><small>份资料</small></article>)}</div></section>
      <section className="dashboard-documents"><header><h2>最新知识文档</h2><button type="button" onClick={() => onNavigate('documents')}>查看全部</button></header>{recent.length ? <div className="dashboard-table-wrap"><table><thead><tr><th>文档名称</th><th>知识类型</th><th>范围</th><th>更新时间</th><th>状态</th><th>操作</th></tr></thead><tbody>{recent.map((file) => <tr key={file.document_id}><td><b>{file.file_name || file.source_name || '未命名资料'}</b></td><td>{knowledgeTypeLabels[file.knowledge_type] || '未分类'}</td><td>{file.scope_id || 'public'}</td><td>{formatTime(file.updated_at)}</td><td><span className={`dashboard-status status-${String(file.status || '').toLowerCase()}`}>{statusLabels[file.status] || file.supervisor_task?.status || file.status || '待处理'}</span></td><td><button type="button" onClick={() => onNavigate('documents', file.knowledge_type, file)}>查看</button></td></tr>)}</tbody></table></div> : <p className="dashboard-empty">暂无文档。请从“新建文档”开始导入资料。</p>}</section>
    </div><aside className="dashboard-shortcuts" aria-label="快捷入口"><h2>快捷入口</h2><div>{shortcuts.map((item) => <button type="button" key={item.label} onClick={() => onNavigate(item.page)}><span>{item.icon}</span><small>{item.label}</small></button>)}</div></aside></div>
  </section>;
}
