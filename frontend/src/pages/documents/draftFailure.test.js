import assert from 'node:assert/strict';
import test from 'node:test';
import { draftFailureMessage } from './draftFailure.js';

test('shows the specific backend failure category and readable message', () => {
  assert.equal(
    draftFailureMessage({ draft_error_code: 'LLM_TIMEOUT', draft_error: '模型服务响应超时，请稍后重试。' }),
    '[LLM_TIMEOUT] 模型服务响应超时，请稍后重试。',
  );
});

test('falls back to older task error fields and a safe generic message', () => {
  assert.equal(draftFailureMessage({ error_code: 'DRAFT_NOT_CREATED', error: '未生成草稿' }), '[DRAFT_NOT_CREATED] 未生成草稿');
  assert.equal(draftFailureMessage({}), '字段提取处理失败，请查看任务审计记录后重试。');
});
