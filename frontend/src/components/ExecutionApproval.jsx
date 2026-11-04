import { useState } from 'react';
import { api } from '../api.js';

export function ExecutionApproval({ userId, threadId }) {
  const [action, setAction] = useState('archive_draft');
  const [parameters, setParameters] = useState('{}');
  const [approvalId, setApprovalId] = useState('');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event.preventDefault();
    let parsed;
    try {
      parsed = JSON.parse(parameters || '{}');
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('参数必须是 JSON 对象');
    } catch (parseError) {
      setError(`参数格式错误：${parseError.message}`);
      return;
    }
    setBusy(true); setError('');
    try {
      const response = await api.submitExecutionRequest({
        action, parameters: parsed, requested_by: userId, thread_id: threadId || undefined,
        idempotency_key: `ui-${userId}-${action}-${JSON.stringify(parsed)}`,
      });
      setResult(response); setApprovalId(response.approval_id || '');
    } catch (requestError) { setError(requestError.message); }
    finally { setBusy(false); }
  }

  async function decide(decision) {
    if (!approvalId) return setError('请先提交执行请求。');
    setBusy(true); setError('');
    try {
      const response = decision === 'APPROVE'
        ? await api.approveExecutionRequest(approvalId, { decided_by: userId, decision_note: '前端测试确认' })
        : await api.rejectExecutionRequest(approvalId, { decided_by: userId, decision_note: '前端测试拒绝' });
      setResult(response);
    } catch (requestError) { setError(requestError.message); }
    finally { setBusy(false); }
  }

  return <section className="panel execution-approval" aria-label="Execution 人工审批">
    <div className="panel-title"><span>05</span><div><h2>Execution 人工审批</h2><p>仅测试审批流，不连接真实外部业务系统。</p></div></div>
    <form onSubmit={submit} className="execution-form">
      <label>动作<select value={action} onChange={(event) => setAction(event.target.value)}><option value="archive_draft">archive_draft</option><option value="send_notification">send_notification</option></select></label>
      <label>参数 JSON<textarea value={parameters} onChange={(event) => setParameters(event.target.value)} rows="3" aria-label="执行参数 JSON" /></label>
      <button type="submit" disabled={busy}>{busy ? '提交中…' : '提交审批请求'}</button>
    </form>
    <label className="execution-approval-id">审批 ID<input value={approvalId} onChange={(event) => setApprovalId(event.target.value)} placeholder="提交后自动填充，也可粘贴已有 ID" /></label>
    <div className="execution-actions"><button type="button" onClick={() => decide('APPROVE')} disabled={busy || !approvalId}>通过</button><button type="button" className="secondary" onClick={() => decide('REJECT')} disabled={busy || !approvalId}>拒绝</button></div>
    {error && <p className="supervisor-error" role="alert">{error}</p>}
    {result && <pre className="json-viewer execution-result">{JSON.stringify(result, null, 2)}</pre>}
  </section>;
}
