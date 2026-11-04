import { EvidenceList } from './EvidenceList.jsx';

const knowledgeTypes = [
  ['demand', '需求库'], ['solution', '方案库'], ['achievement', '成果库'],
  ['enterprise', '企业库'], ['expert', '专家库'],
];

const ragMetricLabels = [
  ['precision', 'Precision'], ['recall', 'Recall'], ['f1', 'F1'], ['auc_roc', 'AUC'],
];

function formatRagMetric(key, value) {
  if (value === undefined || value === null || value === '') return '—';
  if (['precision', 'recall', 'f1', 'auc_roc'].includes(key)) return `${(Number(value) * 100).toFixed(1)}%`;
  return String(value);
}

function RetrievalEvaluation({ result }) {
  const metrics = result?.rag_metrics || result?.evaluation_metrics || result?.evaluation?.rag_metrics;
  return <section className="rag-evaluation-panel" aria-label="RAG评估指标">
    <div className="rag-evaluation-head"><div><p className="admin-kicker">RAG / EVALUATION</p><h2>RAG评估指标</h2></div><small>{metrics ? '评测集已加载' : '评测集结果待加载'}</small></div>
    <div className="rag-metric-grid">{ragMetricLabels.map(([key, label]) => <div className="rag-metric-card" key={key}><span>{label}</span><strong>{formatRagMetric(key, metrics?.[key])}</strong></div>)}</div>
    {!metrics && <p className="rag-evaluation-note">运行已确认的评测集后显示 Precision、Recall、F1 和 AUC。</p>}
  </section>;
}

function RetrievalObservation({ result }) {
  const trace = result?.trace || {};
  const failed = result?.status === 'FAILED' || Boolean(result?.error);
  const elapsed = trace.elapsed_ms ?? trace.latency_ms;
  const formatTime = (value) => Number.isFinite(value) ? `${value.toFixed(1)} ms` : '未获取';
  const observation = [
    ['本次证据数', result && !failed ? (result.evidences || []).length : null],
    ['服务端检索耗时', result ? formatTime(elapsed) : null],
    ['请求总耗时', result ? formatTime(result.client_elapsed_ms) : null],
    ['重排状态', !result ? null : failed ? '未获取' : (trace.rerank_applied === true ? '已执行' : trace.rerank_applied === false ? '未执行' : '未获取')],
    ['无答案判断', !result ? null : failed ? '请求失败，无法判断' : (result.evidences?.length || result.records?.length) ? '有候选证据' : '无候选证据'],
  ];
  return <div className="rag-observation" aria-label="本次检索指标"><b>本次检索指标</b><div>{observation.map(([label, value]) => <span key={label}>{label}：{value === undefined || value === null ? '—' : value}</span>)}</div></div>;
}

export function RetrievalPage({ query, setQuery, search, busy, searchResult, searchScopeId, setSearchScopeId, searchScopeOptions, searchKnowledgeType, setSearchKnowledgeType, identity }) {
  return <section className="retrieval-page" aria-labelledby="retrieval-title">
    <div className="document-heading"><h1 id="retrieval-title">检索验证</h1><p className="overline">RETRIEVAL / VALIDATION</p></div>
    <RetrievalEvaluation result={searchResult} />
    <form className="document-search-form" onSubmit={search}>
      <label htmlFor="retrieval-query">检索问题</label>
      <textarea id="retrieval-query" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="例如：哪些项目具备高端装备研发能力？" rows="4" />
      <div className="document-search-toolbar"><div className="search-filters">
        <label htmlFor="retrieval-scope">检索范围<select id="retrieval-scope" value={searchScopeId} onChange={(e) => setSearchScopeId(e.target.value)}>{searchScopeOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select></label>
        <label htmlFor="retrieval-knowledge-type">知识类型<select id="retrieval-knowledge-type" value={searchKnowledgeType} onChange={(e) => setSearchKnowledgeType(e.target.value)}><option value="">全部五类知识</option>{knowledgeTypes.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      </div><button type="submit" disabled={busy === 'search' || !query.trim()}>{busy === 'search' ? '检索中…' : '检索文档'}</button></div>
    </form>
    <div className="document-results"><div className="document-results-head"><span>检索结果</span><small>{busy === 'search' ? '检索中…' : !searchResult ? '待检索' : searchResult.status === 'FAILED' || searchResult.error ? '请求失败' : `${searchResult.records?.length || searchResult.evidences?.length || 0} 份资料`}</small></div>{busy === 'search' ? <div className="empty-evidence" role="status">正在检索，请稍候…</div> : <EvidenceList result={searchResult} query={query} identity={identity} />}<RetrievalObservation result={searchResult} /></div>
  </section>;
}
