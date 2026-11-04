import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';

const draftTypeLabels = {
  demand: '采购需求', solution: '技术方案', achievement: '科研成果',
  enterprise: '企业资料', expert: '专家资料', customer_visit: '客户拜访记录',
};

function time(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
}

function metricRows(snapshot) {
  return Object.entries(snapshot?.persistent?.metrics || {}).map(([name, value]) => ({ name, ...value }));
}

function draftLabel(type) {
  return draftTypeLabels[type] || '业务资料';
}

export function AdminGovernance({ operatorId }) {
  const [state, setState] = useState({ drafts: [], feedback: [], events: [], eventsTotal: 0, eventsPage: 1, eventsTotalPages: 1, metrics: null, operations: null });
  const [eventsPage, setEventsPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const results = await Promise.allSettled([
        api.adminDrafts({ limit: 100 }), api.adminFeedback(100), api.adminAuditEvents(eventsPage, 8), api.metrics(),
        api.adminOperations(),
      ]);
      const [drafts, feedback, events, metrics, operations] = results;
      setState({
        drafts: drafts.status === 'fulfilled' ? drafts.value.drafts || [] : [],
        feedback: feedback.status === 'fulfilled' ? feedback.value.feedback || [] : [],
        events: events.status === 'fulfilled' ? events.value.events || [] : [],
        eventsTotal: events.status === 'fulfilled' ? events.value.total || 0 : 0,
        eventsPage: events.status === 'fulfilled' ? events.value.page || eventsPage : eventsPage,
        eventsTotalPages: events.status === 'fulfilled' ? events.value.total_pages || 1 : 1,
        metrics: metrics.status === 'fulfilled' ? metrics.value : null,
        operations: operations.status === 'fulfilled' ? operations.value : null,
      });
      const failed = results.filter((item) => item.status === 'rejected');
      setNotice(failed.length ? `治理数据已部分同步，${failed.length} 个指标接口暂不可用。` : '已同步审核、反馈、审计和指标数据。');
    } catch (error) {
      setNotice(`读取治理数据失败：${error.message}`);
    } finally { setLoading(false); }
  }, [eventsPage]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function review(draft, action) {
    const label = action === 'PUBLISH' ? '通过并发布' : '退回修改';
    if (!window.confirm(`确认${label}草稿“${draft.draft?.title || draft.draft_id}”吗？`)) return;
    setBusy(draft.draft_id);
    try {
      await api.adminReviewDraft(draft.draft_id, {
        action, operator_id: operatorId || 'admin', scope_id: 'public',
        note: action === 'PUBLISH' ? '管理员审核通过' : '管理员退回修改',
      });
      setNotice(`草稿已${label}。`);
      await refresh();
    } catch (error) {
      setNotice(`${label}失败：${error.message}`);
    } finally { setBusy(''); }
  }

  async function closeFeedback(item, action) {
    const label = action === 'RESOLVE' ? '标记已解决' : '忽略反馈';
    if (!window.confirm(`确认${label}该检索反馈吗？`)) return;
    setBusy(item.feedback_id);
    try {
      await api.adminResolveFeedback(item.feedback_id, {
        action, operator_id: operatorId || 'admin',
        note: action === 'RESOLVE' ? '管理员已处理' : '管理员判定无需处理',
      });
      setNotice(`反馈已${label}，处置记录已写入审计。`);
      await refresh();
    } catch (error) {
      setNotice(`${label}失败：${error.message}`);
    } finally { setBusy(''); }
  }

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

  const pending = state.drafts.filter((item) => item.status === 'PENDING_REVIEW');
  const metrics = metricRows(state.metrics);
  const operations = state.operations || {};
  const alerts = operations.alerts || [];
  return <section className="admin-console admin-governance" aria-labelledby="governance-title">
    <div className="admin-console-hero"><div><h1 id="governance-title">审核与运行治理</h1><p className="admin-kicker">GOVERNANCE / OPERATIONS</p><p>集中处理异常草稿、检索反馈、审计事件与服务指标。</p></div><button type="button" className="admin-refresh" onClick={() => void refresh()} disabled={loading}>{loading ? '同步中…' : '同步数据'}</button></div>
    {notice && <p className="governance-notice" role="status">{notice}</p>}

    <section className="admin-card governance-card">
      <div className="admin-card-head"><div><p className="admin-kicker">OPERATIONS / CLOSED LOOP</p><h2>运行保障</h2></div><button type="button" className="secondary" onClick={() => void runRetention()} disabled={busy === 'retention'}>{busy === 'retention' ? '清理中…' : '清理过期运行状态'}</button></div>
      <div className="governance-list">
        <article className="governance-row"><div><b>{operations.health?.status === 'ok' ? '服务运行正常' : '服务需要关注'}</b><small>处理中 {operations.tasks?.active || 0} 项 · 失败 {operations.tasks?.failed || 0} 项 · 待人工接管 {operations.tasks?.manual_review || 0} 项</small></div></article>
        {operations.last_retention && <article className="governance-row"><div><b>最近保留期任务：{operations.last_retention.event_name === 'retention_cleanup_completed' ? '完成' : '失败'}</b><small>{time(operations.last_retention.created_at)}</small></div></article>}
        {!alerts.length ? <article className="governance-row"><div><b>当前无运行告警</b><small>依赖健康且持久化指标未超过错误率阈值。</small></div></article> : alerts.map((alert, index) => <article className="governance-row" key={`${alert.type}-${index}`}><div><b className="danger-text">{alert.severity} · {alert.type}</b><small>{alert.service || alert.metric_name || '平台'} · {alert.runbook}</small></div></article>)}
      </div>
    </section>

    <div className="governance-grid">
      <section className="admin-card governance-card"><div className="admin-card-head"><div><p className="admin-kicker">EXCEPTION REVIEW</p><h2>异常/历史待审核草稿</h2></div><span className="policy-count">{pending.length} 条</span></div>
        {!pending.length ? <p className="admin-empty">暂无异常或历史待审核草稿。</p> : <div className="governance-list">{pending.map((draft) => <article key={draft.draft_id} className="governance-row"><div><b>{draft.draft?.title || '未命名草稿'}</b><small>{draftLabel(draft.draft_type)} · 提交人：{draft.user_id} · {time(draft.updated_at)}</small></div><div className="governance-actions"><button type="button" className="secondary" onClick={() => void review(draft, 'RETURN')} disabled={busy === draft.draft_id}>退回修改</button><button type="button" onClick={() => void review(draft, 'PUBLISH')} disabled={busy === draft.draft_id}>{busy === draft.draft_id ? '处理中…' : '确认并发布'}</button></div></article>)}</div>}
      </section>

      <section className="admin-card governance-card"><div className="admin-card-head"><div><p className="admin-kicker">RETRIEVAL FEEDBACK</p><h2>检索反馈</h2></div><span className="policy-count">{state.feedback.filter((item) => item.governance_status === 'OPEN').length} 条待处理</span></div>
        {!state.feedback.length ? <p className="admin-empty">暂无用户反馈。</p> : <div className="governance-list">{state.feedback.slice(0, 8).map((item) => <article key={item.feedback_id} className="governance-row"><div><b>{item.feedback === 'ACCURATE' ? '有帮助' : item.feedback === 'LOW_VALUE' ? '价值低' : '不相关'}</b><small>{item.user_id || '匿名'} · 证据 {item.evidence_count} 条 · {time(item.created_at)} · {item.governance_status === 'RESOLVED' ? '已解决' : item.governance_status === 'IGNORED' ? '已忽略' : '待处理'}</small>{item.resolution_note && <small>处置：{item.resolved_by || '管理员'} · {item.resolution_note}</small>}</div>{item.governance_status === 'OPEN' && <div className="governance-actions"><button type="button" className="secondary" onClick={() => void closeFeedback(item, 'IGNORE')} disabled={busy === item.feedback_id}>忽略</button><button type="button" onClick={() => void closeFeedback(item, 'RESOLVE')} disabled={busy === item.feedback_id}>{busy === item.feedback_id ? '处理中…' : '标记已解决'}</button></div>}</article>)}</div>}
      </section>
    </div>

    <div className="governance-grid">
      <section className="admin-card governance-card"><div className="admin-card-head"><div><p className="admin-kicker">OBSERVABILITY</p><h2>阶段指标</h2></div><span className="policy-count">{metrics.length} 项</span></div>
        {!metrics.length ? <p className="admin-empty">暂无持久化指标。</p> : <div className="metric-table">{metrics.map((item) => <div key={item.name}><b>{item.name}</b><span>调用 {item.count}</span><span>P50 {item.p50_ms} ms</span><span>P95 {item.p95_ms} ms</span><span className={item.error_count ? 'danger-text' : ''}>失败 {item.error_count}</span></div>)}</div>}
      </section>

      <section className="admin-card governance-card"><div className="admin-card-head"><div><p className="admin-kicker">AUDIT TRAIL</p><h2>最近审计事件</h2></div><span className="policy-count">{state.eventsTotal} 条</span></div>
        {!state.events.length ? <p className="admin-empty">暂无审计事件。</p> : <><div className="governance-list">{state.events.map((item, index) => <article key={`${item.created_at}-${item.event_name}-${index}`} className="governance-row"><div><b>{item.event_name}</b><small>{item.service || 'platform'} · {item.task_id || item.trace_id || '—'} · {time(item.created_at)}</small></div></article>)}</div><div className="audit-pagination"><button type="button" className="secondary" onClick={() => setEventsPage((value) => Math.max(1, value - 1))} disabled={eventsPage <= 1 || loading}>上一页</button><span>第 {state.eventsPage} / {state.eventsTotalPages} 页</span><button type="button" className="secondary" onClick={() => setEventsPage((value) => Math.min(state.eventsTotalPages, value + 1))} disabled={eventsPage >= state.eventsTotalPages || loading}>下一页</button></div></>}
      </section>
    </div>
  </section>;
}
