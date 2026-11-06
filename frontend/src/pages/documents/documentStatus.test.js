import test from 'node:test';
import assert from 'node:assert/strict';
import { currentDocumentVersion, currentDocumentTask, catalogStage, documentProgressLabel } from './documentStatus.js';

const current = { document_id: 'doc', version_id: 'doc:v2', storage_key: 'uploads/current.pdf', version_number: 2, status: 'INDEXED', publication_status: 'PUBLISHED', indexed_count: 14 };
const old = { document_id: 'doc', version_id: 'doc:v1', version_number: 1, status: 'PURGED', publication_status: 'UNPUBLISHED', indexed_count: 39 };
const document = { document_id: 'doc', active_version_id: current.version_id, active_version: current };
const purge = { task_id: 'purge', document_id: 'doc', version_id: old.version_id, lifecycle_action: 'purge', status: 'SUCCEEDED', stage: 'completed' };
const index = { task_id: 'index', document_id: 'doc', version_id: current.version_id, storage_key: current.storage_key, operation: 'file_ingestion', task_type: 'document_ingestion', status: 'SUCCEEDED', stage: 'completed' };

test('persisted file operations without the redundant task_type retain exact source validation', () => {
  const persisted = { ...index, task_type: undefined };
  assert.equal(currentDocumentTask(document, [persisted]), persisted);
  assert.equal(currentDocumentTask(document, [{ ...persisted, storage_key: 'uploads/old.pdf' }]), null);
  assert.equal(currentDocumentTask(document, [{ ...persisted, version_id: old.version_id }]), null);
  assert.equal(currentDocumentTask(document, [{ ...persisted, operation: 'document_purge' }]), null);
  assert.equal(currentDocumentTask(document, [{ ...index, task_type: 'document_lifecycle' }]), null);
});

test('document selection follows the active version, never the order of historical cards', () => {
  assert.equal(currentDocumentVersion(document, [old, current]), current);
  assert.equal(currentDocumentVersion(document, [current, old]), current);
  assert.equal(currentDocumentVersion(document, [old]), current);
  assert.equal(currentDocumentVersion({ document_id: 'doc', active_version_id: 'missing' }, [old]), null);
});

test('old purge tasks and cached tasks cannot overwrite a published document', () => {
  assert.equal(currentDocumentTask(document, [purge, index]), index);
  assert.equal(currentDocumentTask(document, [purge]), null);
  assert.equal(currentDocumentTask(document, [{ ...purge, version_id: undefined }]), null);
  assert.equal(currentDocumentTask(document, [{ ...index, document_id: 'other' }]), null);
  assert.equal(catalogStage(document, purge), 'index');
  assert.equal(documentProgressLabel({ ...document, supervisor_task: purge }), '已建立索引');
});

test('current task progress remains visible, completed tasks defer to persisted lifecycle', () => {
  const parsing = { ...current, status: 'PARSING', publication_status: 'UNPUBLISHED' };
  const task = { ...index, status: 'PARSING', stage: 'parse' };
  const pending = { ...document, active_version: parsing, supervisor_task: task };
  assert.equal(catalogStage(pending, task), 'parse');
  assert.equal(documentProgressLabel(pending), '解析中');
  for (const [status, label] of [['UNPUBLISHED', '已下架'], ['RECYCLED', '已移入回收站'], ['PURGED', '已删除']]) {
    const item = { ...document, active_version: { ...current, status, publication_status: 'UNPUBLISHED' }, supervisor_task: index };
    assert.equal(documentProgressLabel(item), label);
    assert.equal(catalogStage(item, index), status === 'UNPUBLISHED' ? 'index' : 'deleted');
  }
});
