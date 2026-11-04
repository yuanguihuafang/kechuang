import { ExecutionApproval } from './ExecutionApproval.jsx';
import { DraftList } from './DraftList.jsx';

const formatJson = (value) => JSON.stringify(value, null, 2);

function responseError(response) {
  return response?.error || response?.result?.error || response?.result?.message || response?.result?.detail || '本次请求未完成，请检查服务状态和依赖配置。';
}

function ResponseSummary({ response }) {
  if (!response) return <div className="response-empty">暂无响应</div>;
  if (response.status !== 'FAILED') return <pre className="json-viewer">{formatJson(response)}</pre>;
  return <div className="response-failure" role="alert"><div className="response-failure-mark">!</div><div className="response-failure-body"><strong>本次请求未完成</strong><p>{responseError(response)}</p>{response.error_code && <small>错误码：{response.error_code}</small>}{response.selected_tool && <small>调用工具：{response.selected_tool}</small>}<span>请先检查右上角服务状态，再重新提交问题。</span></div></div>;
}

export function SupervisorInspector({ response, recoveryNote, setRecoveryNote, recoverTask, recoveryBusy, recoveryMessage, loadMemoryCandidates, memoryBusy, memoryError, memoryCandidates, decideMemory, userId, userRole, sessionId, draftRefreshKey, requestPreview, requestId, setRequestId, setSessionId, taskId, setTaskId, traceId, setTraceId }) {
  return <aside className="supervisor-inspector">
    <section className="panel"><div className="panel-title"><span>01</span><div><h2>执行状态</h2><p>当前阶段先展示意图路由和工具响应。</p></div></div><div className={`route-status ${response?.status === 'FAILED' ? 'route-failed' : ''}`}><strong>{response ? response.status : '等待提问'}</strong><span>{response?.selected_tool || 'Supervisor 尚未调用工具'}</span></div>{response?.error_code && <p className="supervisor-error">{response.error_code}：{response.error}</p>}</section>
    <section className="panel"><div className="panel-title"><span>02</span><div><h2>任务计划</h2><p>展示 Supervisor 当前识别出的意图和执行步骤。</p></div></div><div className="plan-summary"><strong>{response?.intent_operation || response?.intent?.operation || '等待识别意图'}</strong>{response?.plan?.steps?.length ? <ol>{response.plan.steps.map((step, index) => <li key={step.id || index}>{step.description || step.tool || '待执行步骤'}</li>)}</ol> : <span>提交问题后显示任务步骤。</span>}</div></section>
    {response?.compensation?.status === 'MANUAL_REVIEW' && response.task_id && <section className="panel compensation-panel"><div className="panel-title"><span>07</span><div><h2>人工接管</h2><p>系统未自动执行补偿，请确认后恢复一次。</p></div></div><dl className="compensation-details"><div><dt>建议动作</dt><dd>{response.compensation.action}</dd></div><div><dt>任务 ID</dt><dd>{response.task_id}</dd></div></dl><label htmlFor="recovery-note">确认说明<textarea id="recovery-note" value={recoveryNote} onChange={(event) => setRecoveryNote(event.target.value)} placeholder="可填写人工确认依据" rows="3" /></label><div className="compensation-actions"><button type="button" onClick={recoverTask} disabled={recoveryBusy}>{recoveryBusy ? '恢复中…' : '确认并恢复一次'}</button></div>{recoveryMessage && <p className="compensation-message" role="status">{recoveryMessage}</p>}</section>}
    <section className="panel"><div className="panel-title"><span>03</span><div><h2>证据 / 响应</h2><p>{response?.status === 'FAILED' ? '请求未完成，以下显示可读错误信息。' : response ? '结构化结果，便于审计和后续生成答案。' : '提交问题后显示结果。'}</p></div></div><ResponseSummary response={response} /></section>
    <section className="panel memory-candidates" aria-label="待确认记忆"><div className="panel-title"><span>04</span><div><h2>待确认记忆</h2><p>仅显示当前用户候选，不会自动生效。</p></div><button type="button" className="secondary" onClick={loadMemoryCandidates} disabled={memoryBusy}>刷新</button></div>{memoryError && <p className="supervisor-error" role="alert">{memoryError}</p>}{memoryCandidates.length === 0 ? <p className="empty-state">暂无待确认记忆</p> : <div className="memory-candidate-list">{memoryCandidates.map((item) => <article className="memory-candidate" key={item.memory_id}><div><strong>{item.memory_type}</strong><p>{item.content}</p></div><div className="memory-candidate-actions"><button type="button" onClick={() => decideMemory(item.memory_id, 'confirm')} disabled={memoryBusy}>确认</button><button type="button" className="secondary" onClick={() => decideMemory(item.memory_id, 'reject')} disabled={memoryBusy}>拒绝</button></div></article>)}</div>}</section>
    <ExecutionApproval userId={userId} threadId={sessionId} />
    <DraftList userId={userId} userRole={userRole} refreshKey={draftRefreshKey} />
    <details className="panel inspector-details"><summary>查看请求上下文</summary><pre className="json-viewer">{requestPreview ? formatJson(requestPreview) : '暂无请求'}</pre></details>
    <details className="panel inspector-details"><summary>调试标识</summary><div className="supervisor-fields"><label>Request ID<input value={requestId} onChange={(event) => setRequestId(event.target.value)} /></label><label>Session ID<input value={sessionId} onChange={(event) => setSessionId(event.target.value)} /></label><label>Task ID<input value={taskId} onChange={(event) => setTaskId(event.target.value)} /></label><label>Trace ID<input value={traceId} onChange={(event) => setTraceId(event.target.value)} placeholder="留空自动生成" /></label></div></details>
  </aside>;
}
