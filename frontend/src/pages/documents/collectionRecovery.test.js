import assert from 'node:assert/strict';
import test from 'node:test';
import { collectionRecoveryView, groupSkippedSources, loadLatestCollectionRecovery } from './collectionRecovery.js';

test('skipped issues for one document are grouped under one data entry', () => {
  const related = [{ draft_id: 'draft-1', draft_title: '方案 A', document_id: 'doc-1', attachment_name: '附件.pdf' }];
  const groups = groupSkippedSources([
    { source_type: 'image', source_id: 'image-1', document_id: 'doc-1', reason: '图片对象缺失', related_sources: related },
    { source_type: 'artifact', source_id: 'derived/doc-1/manifest.json', document_id: 'doc-1', reason: '解析清单缺失', related_sources: related },
    { source_type: 'document', source_id: 'doc-2', document_id: 'doc-2', reason: '原文件缺失', attachment_name: '另一个.pdf' },
  ]);

  assert.equal(groups.length, 2);
  assert.equal(groups[0].document_id, 'doc-1');
  assert.equal(groups[0].issues.length, 2);
  assert.equal(groups[0].related_sources.length, 1);
  assert.equal(groups[1].document_id, 'doc-2');
  assert.equal(groups[1].issues.length, 1);
});

test('restore is offered only for a persisted preflight report', () => {
  const report = { recoverable_count: 2, skipped_count: 1, missing_fingerprint: 'a'.repeat(64) };
  const preflight = collectionRecoveryView({ task_id: 'preflight-1', status: 'PREFLIGHT_COMPLETE', recovery_result: report });
  assert.equal(preflight.showRestore, true);
  assert.equal(preflight.blockWrites, false);
  assert.equal(preflight.report, report);
  assert.equal(collectionRecoveryView({ status: 'PREFLIGHT_COMPLETE', recovery_result: report }).showRestore, false);
  for (const status of ['WAITING_RECOVERY', 'FAILED', 'INTERRUPTED']) {
    const view = collectionRecoveryView({ status, stage: 'restore' });
    assert.equal(view.showRestore, false);
    assert.equal(view.blockWrites, status === 'WAITING_RECOVERY');
    assert.equal(view.running, false);
  }
  assert.equal(collectionRecoveryView({ status: 'QUEUED', stage: 'restore' }).showRestore, false);
  assert.equal(collectionRecoveryView({ status: 'RUNNING', stage: 'published_content' }).showRestore, false);
  assert.equal(collectionRecoveryView({ status: 'FAILED', stage: 'rebuild' }).showRestore, false);
  assert.equal(collectionRecoveryView({ status: 'SUCCESS', stage: 'completed' }).blockWrites, false);
  assert.equal(collectionRecoveryView(null).showRestore, false);
});

test('queued and running restoration blocks duplicates and shows stage-specific progress', () => {
  const content = collectionRecoveryView({ status: 'RUNNING', stage: 'published_content', source_processed: 2, source_total: 5 });
  assert.equal(content.running, true);
  assert.equal(content.showRestore, false);
  assert.match(content.message, /2 \/ 5/);
  const mysql = collectionRecoveryView({ status: 'RUNNING', stage: 'mysql', last_id: 64, indexed_count: 92 });
  assert.match(mysql.message, /ID 64/);
  assert.match(mysql.message, /92/);
  assert.equal(collectionRecoveryView({ status: 'QUEUED', stage: 'restore' }).running, true);
  assert.equal(collectionRecoveryView({ status: 'SWITCHING', stage: 'alias_switch' }).blockWrites, true);
});

test('a validated shadow waits for a separate administrator switch confirmation', () => {
  const ready = collectionRecoveryView({ task_id: 'recovery-1', status: 'READY_TO_SWITCH', stage: 'ready_to_switch' });
  assert.equal(ready.showActivate, true);
  assert.equal(ready.running, false);
  assert.equal(ready.blockWrites, false);
  assert.match(ready.message, /管理员确认/);
  assert.equal(collectionRecoveryView({ status: 'READY_TO_SWITCH' }).showActivate, false);
  assert.equal(collectionRecoveryView({ task_id: 'recovery-1', status: 'SUCCESS' }).showActivate, false);
  assert.match(ready.message, /等待管理员确认切换/);
});

test('refresh finds the selected collection beyond the first page, not another collection', async () => {
  const pages = [
    { items: [{ knowledge_type: 'solution', status: 'SUCCESS' }], total_pages: 2 },
    { items: [{ task_id: 'latest', knowledge_type: 'demand', status: 'WAITING_RECOVERY', created_at: '2026-10-07T08:00:00Z' }, { task_id: 'older', knowledge_type: 'demand', status: 'SUCCESS', created_at: '2026-10-06T08:00:00Z' }], total_pages: 2 },
  ];
  const task = await loadLatestCollectionRecovery({ adminTasks: async ({ page }) => pages[page - 1] }, 'demand');
  assert.equal(task.task_id, 'latest');
});

test('task reading failure is propagated instead of pretending there is no recovery', async () => {
  await assert.rejects(loadLatestCollectionRecovery({ adminTasks: async () => { throw new Error('offline'); } }, 'demand'), /offline/);
  assert.equal(await loadLatestCollectionRecovery({ adminTasks: async () => ({ items: [], total_pages: 0 }) }, 'demand'), null);
});

test('an old task updated recently does not hide a newer completed restoration', async () => {
  const pages = [
    { items: [{ task_id: 'old', knowledge_type: 'demand', status: 'WAITING_RECOVERY', created_at: '2026-10-06T08:00:00Z', updated_at: '2026-10-07T12:00:00Z' }], total_pages: 2 },
    { items: [{ task_id: 'new', knowledge_type: 'demand', status: 'SUCCESS', created_at: '2026-10-07T08:00:00Z', updated_at: '2026-10-07T09:00:00Z' }], total_pages: 2 },
  ];
  const task = await loadLatestCollectionRecovery({ adminTasks: async ({ page }) => pages[page - 1] }, 'demand');
  assert.equal(task.task_id, 'new');
  assert.equal(collectionRecoveryView(task).showRestore, false);
});
