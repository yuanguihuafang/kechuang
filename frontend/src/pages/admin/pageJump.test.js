import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { normalizePageJump } from './pageJump.js';

test('page jump truncates decimal input and clamps to available pages', () => {
  assert.equal(normalizePageJump('3', 5), 3);
  assert.equal(normalizePageJump('0', 5), 1);
  assert.equal(normalizePageJump('9', 5), 5);
  assert.equal(normalizePageJump('3.8', 5), 3);
});

test('page jump rejects empty, non-numeric, and invalid ranges', () => {
  assert.equal(normalizePageJump('', 5), null);
  assert.equal(normalizePageJump('abc', 5), null);
  assert.equal(normalizePageJump('2', 0), null);
});

test('all six management pagination surfaces are wired to the shared page jump', () => {
  const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
  const overview = source('./AdminOverview.jsx');
  const documents = source('../documents/AdminDocumentsPage.jsx');
  const fileTable = source('./FileTable.jsx');
  const governance = source('./AdminGovernance.jsx');
  const sessions = source('./TaskCenter.jsx');

  assert.match(overview, /label="知识库更新"/);
  assert.match(fileTable, /label="文档目录"/);
  assert.match(documents, /aria-label="知识库草稿目录分页"/);
  assert.match(documents, /label="知识库草稿目录"/);
  assert.match(documents, /label="草稿详情"/);
  assert.match(governance, /label="系统治理事件"/);
  assert.match(sessions, /label="会话列表"/);
  assert.ok(overview.includes('disabled={loading}'));
  assert.ok(fileTable.includes('disabled={loading}'));
  assert.ok(documents.includes('disabled={fileLoading}'));
  assert.ok(documents.includes('disabled={confirmingUpdate || knowledgeUpdateDetailLoading}'));
  assert.ok(governance.includes('disabled={loading}'));
  assert.ok(sessions.includes('disabled={loading}'));
  assert.match(source('./PageJump.jsx'), /<form[^>]*onSubmit=\{submit\}/);
});

test('document page changes made during an in-flight refresh trigger a refresh for the latest page', () => {
  const app = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');

  assert.match(app, /filesRefreshQueuedRef/);
  assert.match(app, /if \(filesRefreshInFlightRef\.current\) \{[\s\S]*?filesRefreshQueuedRef\.current/);
  assert.match(app, /const queuedRefresh = filesRefreshQueuedRef\.current;[\s\S]*?if \(queuedRefresh\) \{[\s\S]*?void queuedRefresh\(\)/);
});

test('governance parse-time card uses the persisted perception parse metric', () => {
  const governance = readFileSync(new URL('./AdminGovernance.jsx', import.meta.url), 'utf8');

  assert.match(governance, /metricByName\(metrics, 'perception\.parse'\)/);
  assert.match(governance, /平均文件解析耗时/);
  assert.match(governance, /不含上传和存储下载/);
});

test('governance omits request, token, and failure-rate summary metrics', () => {
  const governance = readFileSync(new URL('./AdminGovernance.jsx', import.meta.url), 'utf8');
  const summary = governance.match(/function metricSummary\(snapshot\) \{([\s\S]*?)\n\}/)?.[1] || '';

  assert.doesNotMatch(governance, /<span>请求量<\/span>|<span>实际 Token<\/span>|<span>失败率<\/span>/);
  assert.doesNotMatch(summary, /\b(requests|tokens|failures|failureRate|comparison)\s*:/);
});
