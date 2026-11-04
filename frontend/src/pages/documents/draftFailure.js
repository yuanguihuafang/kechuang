export function draftFailureMessage(task) {
  const state = task?.supervisor_task || task || {};
  const code = state.draft_error_code || state.error_code || '';
  const message = state.draft_error || state.error_message || state.error || '';
  return [code && `[${code}]`, message || '字段提取处理失败，请查看任务审计记录后重试。']
    .filter(Boolean)
    .join(' ');
}
