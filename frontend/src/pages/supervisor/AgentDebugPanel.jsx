function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

function intentName(value) {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return '';
  const name = value.name || value.business_intent || value.intent || '';
  return name;
}

function debugQuery(response) {
  return response?.debug?.context?.query
    || response?.query
    || response?.intent?.query
    || response?.internal_flow?.query
    || response?.result?.query
    || '';
}

function debugContext(response, messages, messageIndex) {
  const explicit = response?.debug?.context
    || response?.context
    || response?.draft_context;
  if (explicit) return explicit;
  const previous = messages
    .slice(0, messageIndex)
    .filter((item) => item.role === 'user' && String(item.text || '').trim())
    .slice(-8)
    .map((item) => String(item.text).trim());
  return previous.length ? { previous_user_messages: previous } : null;
}

function debugDraft(response) {
  if (response?.debug?.context && Object.prototype.hasOwnProperty.call(response.debug.context, 'draft')) {
    return response.debug.context.draft;
  }
  if (response?.draft) return response.draft;
  if (Array.isArray(response?.drafts) && response.drafts.length) return response.drafts;
  return null;
}

function debugLlmExchanges(response) {
  return response?.debug?.llm_exchanges || response?.debug?.llm || [];
}

function DebugValue({ value }) {
  if (value === null || value === undefined || value === '' || (Array.isArray(value) && value.length === 0)) {
    return <span className="turn-debug-empty">暂无</span>;
  }
  return <pre className="turn-debug-value">{typeof value === 'string' ? value : formatJson(value)}</pre>;
}

function LlmExchangeList({ exchanges }) {
  if (!Array.isArray(exchanges) || exchanges.length === 0) return <DebugValue value={exchanges} />;
  return <div className="turn-debug-llm-list">{exchanges.map((exchange, index) => <article className="turn-debug-llm-card" key={`${exchange.role || 'llm'}-${index}`}>
    <header><strong>第 {index + 1} 次调用</strong><span>{exchange.role || 'default'} · {exchange.model || '未标注模型'}</span>{exchange.latency_ms != null && <small>{exchange.latency_ms} ms</small>}</header>
    <div className="turn-debug-llm-columns"><div><h4>输入 messages</h4><DebugValue value={exchange.messages} /></div><div><h4>输出</h4><DebugValue value={exchange.output} /></div></div>
  </article>)}</div>;
}

function AgentTurnDebug({ item, turnIndex, messages }) {
  const response = item.response || {};
  const intent = response.intent || response.internal_flow || {};
  const intentLabel = intentName(intent) || '未识别';
  const context = debugContext(response, messages, messages.indexOf(item));
  const query = debugQuery(response);
  const draft = debugDraft(response);
  const llmExchanges = debugLlmExchanges(response);
  const meta = {
    status: response.status || '未知',
    operation: intent.operation || response.operation || undefined,
    tool: intent.tool || response.selected_tool || undefined,
    source: intent.source || undefined,
    knowledge_type: intent.knowledge_type || undefined,
    retrieval_status: response.retrieval_status || undefined,
    reasoning_source: response.reasoning_source || undefined,
  };
  return <details className="turn-debug-item" defaultOpen={turnIndex === 0}>
    <summary><span>第 {turnIndex + 1} 轮</span><strong>{intentLabel}</strong><small>{response.status || '已完成'}</small></summary>
    <div className="turn-debug-grid">
      <section><h3>意图</h3><DebugValue value={intent} /></section>
      <section><h3>上下文</h3><DebugValue value={context} /></section>
      <section><h3>Query</h3><DebugValue value={query} /></section>
      <section><h3>草稿</h3><DebugValue value={draft} /></section>
      <section className="turn-debug-wide"><h3>LLM 输入 / 输出</h3><LlmExchangeList exchanges={llmExchanges} /></section>
      <section className="turn-debug-meta"><h3>执行信息</h3><DebugValue value={meta} /></section>
    </div>
  </details>;
}

export function AgentDebugPanel({ messages, streamEvents = [] }) {
  const turns = messages.filter((item) => item.role === 'assistant' && item.response && !item.pending);
  return <section className="supervisor-turn-debug" aria-label="智能体调试信息">
    <header className="turn-debug-heading"><div><span>DEBUG</span><h2>智能体调试信息</h2></div><p>测试专用：按轮次查看后台识别的意图、上下文、Query、草稿和执行状态。</p></header>
    {turns.length === 0
      ? <p className="turn-debug-empty-state">发送问题后，这里会显示每一轮智能体实际处理的数据。</p>
      : <div className="turn-debug-list">{turns.map((item, index) => <AgentTurnDebug key={item.id} item={item} turnIndex={index} messages={messages} />)}</div>}
    {streamEvents.length > 0 && <details className="turn-debug-events" open><summary>本次流水线步骤（{streamEvents.length}）</summary><ol>{streamEvents.map((event, index) => <li key={`${event.at || 'event'}-${index}`}><strong>{event.event}</strong><span>{event.data?.stage || event.data?.status || 'completed'}</span><DebugValue value={event.data} /></li>)}</ol></details>}
  </section>;
}
