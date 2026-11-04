import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api.js';
import { PageJump } from './PageJump.jsx';

function time(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
}

function metricRows(snapshot) {
  return Object.entries(snapshot?.persistent?.metrics || {}).map(([name, value]) => ({ name, ...value }));
}

function metricAverageMs(item) {
  const value = item.avg_ms ?? item.average_ms ?? item.avg_latency_ms;
  return value == null || !Number.isFinite(Number(value)) ? null : Number(value).toFixed(1);
}

function metricAverageSeconds(item) {
  const value = metricAverageMs(item);
  return value == null ? '—' : (Number(value) / 1000).toFixed(2);
}

function metricByName(metrics, name) {
  return metrics.find((item) => item.name === name) || {};
}

function metricSummary(snapshot) {
  const metrics = metricRows(snapshot);
  const retrieval = metricByName(metrics, 'retrieval.total');
  const llm = metricByName(metrics, 'llm');
  const fileParsing = metricByName(metrics, 'perception.parse');
  return {
    retrieval,
    llm,
    fileParsing,
  };
}

function metricFailureRows(metrics) {
  return metrics.flatMap((item) => Object.entries(item.error_codes || {}).map(([code, count]) => ({
    stage: item.name, code, count, recent: item.recent_failures || [],
  }))).filter((item) => item.count > 0);
}

function failureReason(code) {
  if (code === 'ConnectError') return '连接模型服务失败，请检查模型服务地址、网络或服务状态。';
  if (code === 'MILVUS_RUNTIMEERROR') return 'Milvus 运行时异常，请检查 Milvus 服务、Collection 和连接配置。';
  if (code === 'SUPERVISOR_FAILED') return '智能编排流程失败，需结合对应 Trace 查看上游失败阶段。';
  return '未记录异常正文，请结合 Trace 和后端日志定位。';
}

function errorText(error) {
  return error?.message || '接口暂不可用';
}

function serviceLabel(value) {
  return ({ supervisor: '智能助手', perception: '文件解析', retrieval: '知识检索', operations: '运行治理', governance: '审核治理', file_workflow: '文件流程' }[value] || '系统');
}

export function AdminGovernance({ operatorId }) {
  const [state, setState] = useState({ events: [], eventsTotal: 0, eventsPage: 1, eventsTotalPages: 1, metrics: null, operations: null, runtimeEvidence: null, errors: {} });
  const [eventsPage, setEventsPage] = useState(1);
  const [auditService, setAuditService] = useState('');
  const [auditFilters, setAuditFilters] = useState({ user_id: '', task_id: '', document_id: '', event_name: '' });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const results = await Promise.allSettled([
        api.adminAuditEvents(eventsPage, 20, auditService, auditFilters), api.adminMetrics(),
        api.adminOperations(), api.adminRuntimeEvidence(),
      ]);
      const [events, metrics, operations, runtimeEvidence] = results;
      const errors = {};
      [events, metrics, operations, runtimeEvidence].forEach((result, index) => {
        if (result.status === 'rejected') errors[['events', 'metrics', 'operations', 'runtimeEvidence'][index]] = errorText(result.reason);
      });
      setState({
        events: events.status === 'fulfilled' ? events.value.events || [] : [],
        eventsTotal: events.status === 'fulfilled' ? events.value.total || 0 : 0,
        eventsPage: events.status === 'fulfilled' ? events.value.page || eventsPage : eventsPage,
        eventsTotalPages: events.status === 'fulfilled' ? events.value.total_pages || 1 : 1,
        metrics: metrics.status === 'fulfilled' ? metrics.value : null,
        operations: operations.status === 'fulfilled' ? operations.value : null,
        runtimeEvidence: runtimeEvidence.status === 'fulfilled' ? runtimeEvidence.value : null,
        errors,
      });
      const failed = results.filter((item) => item.status === 'rejected');
      const failedNames = Object.keys(errors).map((name) => ({ events: '系统审计', metrics: '指标', operations: '运行状态', runtimeEvidence: '运行时长证据' }[name] || name));
      setNotice(failed.length ? `治理数据部分不可用：${failedNames.join('、')}。` : '已同步系统审计、运行指标和运行状态。');
    } catch (error) {
      setNotice(`读取治理数据失败：${error.message}`);
    } finally { setLoading(false); }
  }, [eventsPage, auditService, auditFilters]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function runRetention() {
    if (!window.confirm('确认清理已超过保留期的运行状态吗？不会删除文档、MinIO 文件或 Milvus 向量。')) return;
    setBusy('retention');
    try {
      const result = await api.adminRunRetention({ operator_id: operatorId || 'admin', confirm: true });
      setNotice(`${result.message}`);
      await refresh();
    } catch (error) {
      setNotice(`运行状态清理失败：${error.message}`);
    } finally { setBusy(''); }
  }

  async function setRuntimeStart() {
    if (!window.confirm('确认从现在开始累计正式运行时间吗？之前开发阶段的运行时间不会计入，系统开始时间只能设置一次。')) return;
    setBusy('runtime-start');
    try {
      await api.adminStartRuntimeEvidence({ operator_id: operatorId || 'admin', confirm: true });
      setNotice('已设置系统开始时间，后续按结构化心跳累计有效运行时长。');
      await refresh();
    } catch (error) { setNotice(`设置系统开始时间失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function resetRuntimeStart() {
    if (!window.confirm('重新开始计时会清零当前累计时长，历史数据不会删除但不再计入本次统计。确定继续吗？')) return;
    setBusy('runtime-reset');
    try {
      await api.adminResetRuntimeEvidence({ operator_id: operatorId || 'admin', confirm: true });
      setNotice('已重新开始计时，当前统计周期已从现在开始。');
      await refresh();
    } catch (error) { setNotice(`重新开始计时失败：${error.message}`); }
    finally { setBusy(''); }
  }

  async function exportRuntimeEvidence(format) {
    setBusy(`runtime-export-${format}`);
    try {
      const blob = await api.adminRuntimeEvidenceExport(format);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `runtime-evidence.${format}`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) { setNotice(`导出运行时长证据失败：${error.message}`); }
    finally { setBusy(''); }
  }

  const metrics = metricRows(state.metrics);
  const metricTotals = metricSummary(state.metrics);
  const failureRows = metricFailureRows(metrics);
  const operations = state.operations || {};
  const alerts = operations.alerts || [];
  return <section className="admin-console admin-governance" aria-labelledby="governance-title">
    <div className="admin-console-hero"><div><h1 id="governance-title">审核与运行治理</h1><p className="admin-kicker">治理 / 运行</p><p>集中处理异常草稿、操作记录与服务指标。</p></div><button type="button" className="admin-refresh" onClick={() => void refresh()} disabled={loading}>{loading ? '同步中…' : '同步数据'}</button></div>
    {notice && <p className="governance-notice" role="status">{notice}</p>}

    <section className="admin-card governance-card"><div className="admin-card-head"><div><p className="admin-kicker">运行指标</p><h2>阶段指标</h2></div><span className="policy-count">{metrics.length} 项</span></div>
      {state.errors.metrics ? <p className="governance-error">接口不可用：{state.errors.metrics}</p> : !metrics.length ? <p className="admin-empty">暂无持久化指标记录。</p> : <>
        <div className="admin-stat-grid governance-metric-summary">
          <article className="admin-stat"><span>平均检索耗时</span><strong>{metricAverageSeconds(metricTotals.retrieval)}<em> s</em></strong><small>P95 {metricTotals.retrieval.p95_ms ?? '—'} ms</small></article>
          <article className="admin-stat"><span>平均模型调用耗时</span><strong>{metricAverageSeconds(metricTotals.llm)}<em> s</em></strong><small>P95 {metricTotals.llm.p95_ms ?? '—'} ms</small></article>
          <article className="admin-stat"><span>平均文件解析耗时</span><strong>{metricAverageSeconds(metricTotals.fileParsing)}<em> s</em></strong><small>解析阶段，不含上传和存储下载 · P95 {metricTotals.fileParsing.p95_ms ?? '—'} ms</small></article>
        </div>
        {failureRows.length > 0 && <div className="governance-failure-details"><div className="governance-subhead"><b>失败明细</b><span>按阶段与错误码统计</span></div><div className="governance-list">{failureRows.map((item) => <article className="governance-row" key={`${item.stage}-${item.code}`}><div><b>{item.stage}</b><small>错误码：{item.code} · 发生 {item.count} 次</small><small>定位提示：{failureReason(item.code)}</small>{item.recent.slice(0, 3).map((failure, index) => <small key={`${failure.created_at}-${index}`}>最近：{time(failure.created_at)} · Trace：{failure.trace_id || '—'}</small>)}</div></article>)}</div></div>}
      </>}
    </section>

    <section className="admin-card governance-card">
      <div className="admin-card-head"><div><p className="admin-kicker">运行时长证据</p><h2>项目累计运行时间</h2><p className="governance-help">只统计系统开始时间之后、被结构化心跳覆盖的有效运行时长。</p></div><div className="governance-head-actions"><button type="button" className="secondary" onClick={() => void exportRuntimeEvidence('json')} disabled={busy.startsWith('runtime-export')}>导出 JSON</button><button type="button" className="secondary" onClick={() => void exportRuntimeEvidence('csv')} disabled={busy.startsWith('runtime-export')}>导出 CSV</button></div></div>
      {state.errors.runtimeEvidence ? <p className="governance-error">接口不可用：{state.errors.runtimeEvidence}</p> : <>
        <div className="admin-stat-grid governance-metric-summary">
          <article className="admin-stat admin-stat-primary"><span>累计有效运行</span><strong>{Number(state.runtimeEvidence?.valid_runtime_hours || 0).toFixed(2)}<em> 小时</em></strong><small>按 30 分钟结构化心跳核算</small></article>
          <article className="admin-stat"><span>系统开始时间</span><strong>{time(state.runtimeEvidence?.system_start_at)}</strong><small>之前开发阶段不计入</small></article>
          <article className="admin-stat"><span>最近有效心跳</span><strong>{time(state.runtimeEvidence?.latest_heartbeat)}</strong><small>PostgreSQL 记录</small></article>
        </div>
        <div className="governance-list">
          {!state.runtimeEvidence?.system_start_at && <article className="governance-row"><div><b>尚未设置系统开始时间</b><small>项目正式验收运行前设置一次，开发阶段时间不会计入。</small></div><button type="button" className="secondary" onClick={() => void setRuntimeStart()} disabled={busy === 'runtime-start'}>{busy === 'runtime-start' ? '设置中…' : '设置开始时间'}</button></article>}
          {!!state.runtimeEvidence?.system_start_at && <article className="governance-row"><div><b>计时周期管理</b><small>需要重新开始验收计时，点击后从现在重新累计。</small></div><button type="button" className="secondary" onClick={() => void resetRuntimeStart()} disabled={busy === 'runtime-reset'}>{busy === 'runtime-reset' ? '重置中…' : '重新开始计时'}</button></article>}
          {(state.runtimeEvidence?.abnormal_interruptions || []).slice(0, 5).map((item, index) => <article className="governance-row" key={`${item.session_id}-${item.from}-${index}`}><div><b className="danger-text">异常心跳中断 · {item.reason || 'UNKNOWN'}</b><small>{time(item.from)} 至 {time(item.to)} · 约 {Math.round(Number(item.duration_seconds || 0) / 60)} 分钟</small><small>{item.reason_text || '未提供具体原因，请结合服务日志和 PostgreSQL 心跳记录定位。'}</small></div></article>)}
          {!!state.runtimeEvidence?.system_start_at && !(state.runtimeEvidence?.abnormal_interruptions || []).length && <article className="governance-row"><div><b>暂未发现异常心跳中断</b><small>正常手动关闭不会记录为异常。</small></div></article>}
        </div>
      </>}
    </section>

    <section className="admin-card governance-card">
      <div className="admin-card-head"><div><p className="admin-kicker">运行保障 / 闭环处理</p><h2>运行保障</h2></div><button type="button" className="secondary" onClick={() => void runRetention()} disabled={busy === 'retention'}>{busy === 'retention' ? '清理中…' : '清理过期运行状态'}</button></div>
      <div className="governance-list">
        {state.errors.operations ? <article className="governance-row"><div><b className="danger-text">运行状态接口不可用</b><small>{state.errors.operations}</small></div></article> : <><article className="governance-row"><div><b>{operations.health?.status === 'ok' ? '服务运行正常' : '服务需要关注'}</b><small>处理中 {operations.tasks?.active || 0} 项 · 失败 {operations.tasks?.failed || 0} 项 · 待人工接管 {operations.tasks?.manual_review || 0} 项</small></div></article>
        {operations.last_retention && <article className="governance-row"><div><b>最近保留期任务：{operations.last_retention.event_name === 'retention_cleanup_completed' ? '完成' : '失败'}</b><small>{time(operations.last_retention.created_at)}</small></div></article>}
        {!alerts.length ? <article className="governance-row"><div><b>当前无运行告警</b><small>依赖健康且持久化指标未超过错误率阈值。</small></div></article> : alerts.map((alert, index) => <article className="governance-row" key={`${alert.type}-${index}`}><div><b className="danger-text">{alert.severity} · {alert.type}</b><small>{alert.service || alert.metric_name || '平台'} · {alert.runbook}</small>{alert.count != null && <small>失败 {alert.error_count} / {alert.count} · 错误率 {(alert.error_rate * 100).toFixed(1)}%</small>}{alert.checks && <small>检查项：{Object.entries(alert.checks).map(([name, check]) => `${name}=${check.status}`).join(' · ')}</small>}</div></article>)}</>}
      </div>
    </section>

    <div className="governance-grid">
      <section className="admin-card governance-card"><div className="admin-card-head"><div><p className="admin-kicker">系统操作</p><h2>最近业务与系统运行记录</h2><p className="governance-help">记录用户操作以及智能助手、文件解析和知识检索结果，便于定位异常。</p></div><div className="governance-head-actions"><select aria-label="按服务筛选系统操作记录" value={auditService} onChange={(event) => { setAuditService(event.target.value); setEventsPage(1); }}><option value="">全部服务</option><option value="supervisor">智能助手</option><option value="governance">治理</option><option value="operations">运行</option><option value="file_workflow">文件流程</option><option value="perception">文件解析</option></select><span className="policy-count">{state.eventsTotal} 条</span></div></div>
        <p className="governance-filter-title">按条件筛选（可选）</p><div className="audit-filter-grid">{[["user_id","用户 ID，例如 user_001"],["task_id","任务 ID"],["document_id","文档 ID"],["event_name","操作名称"]].map(([key, label]) => <input key={key} aria-label={`按${label}筛选`} placeholder={label} value={auditFilters[key]} onChange={(event) => { setAuditFilters((current) => ({ ...current, [key]: event.target.value })); setEventsPage(1); }} />)}</div>
        {state.errors.events ? <p className="governance-error">接口不可用：{state.errors.events}</p> : !state.events.length ? <p className="admin-empty">暂无系统操作记录。</p> : <><div className="governance-list">{state.events.map((item, index) => <article key={`${item.created_at}-${item.event_name}-${index}`} className="governance-row"><div><b>{item.event_label || '系统运行'}</b><small>{serviceLabel(item.service)} · 结果：{item.result_label || '已记录'} · 时间：{time(item.created_at)}</small>{(item.error_detail || item.summary) && <small>{item.error_detail || item.summary}</small>}</div></article>)}</div><div className="audit-pagination"><button type="button" className="secondary" onClick={() => setEventsPage((value) => Math.max(1, value - 1))} disabled={eventsPage <= 1 || loading}>上一页</button><span>第 {state.eventsPage} / {state.eventsTotalPages} 页</span><PageJump page={eventsPage} totalPages={state.eventsTotalPages} onJump={setEventsPage} disabled={loading} label="系统治理事件" /><button type="button" className="secondary" onClick={() => setEventsPage((value) => Math.min(state.eventsTotalPages, value + 1))} disabled={eventsPage >= state.eventsTotalPages || loading}>下一页</button></div></>}
      </section>
    </div>
  </section>;
}
