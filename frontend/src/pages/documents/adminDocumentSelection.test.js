import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { currentDocumentTask, currentDocumentVersion, catalogStage } from './documentStatus.js';

function loader(api, tasks = [], loadVersions = async () => null) {
  const source = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
  const start = source.indexOf('  async function selectStoredFile(document)');
  const end = source.indexOf('  async function toggleDocumentDetails', start);
  const state = { selected: null, parsed: null, notice: '', read: {}, chunks: [] };
  const requestRef = { current: 0 };
  const names = ['api', 'supervisorTasks', 'loadVersions', 'adminDocumentRequestRef', 'currentDocumentTask', 'currentDocumentVersion', 'catalogStage', 'taskStage', 'displayError', 'updateRecord', 'setKnowledgeType', 'setSelected', 'setUploaded', 'setParsed', 'setParsedDirty', 'setFile', 'setNotice', 'setParseReadState', 'setGeneratedChunks', 'setIndexPreview'];
  const select = new Function(...names, source.slice(start, end) + '\nreturn selectStoredFile;')(
    api, tasks, loadVersions, requestRef, currentDocumentTask, currentDocumentVersion, catalogStage, () => ({}), String,
    (document, patch) => ({ ...document, ...patch }), () => {}, value => { state.selected = value; }, () => {},
    value => { state.parsed = value; }, () => {}, () => {}, value => { state.notice = value; },
    value => { state.read = value; }, value => { state.chunks = value; }, () => {},
  );
  return { select, state, close: () => { requestRef.current += 1; state.parsed = null; state.selected = null; } };
}

const document = { document_id: 'doc', version_id: 'doc:v2', storage_key: 'uploads/current.pdf', file_name: '先德.pdf', status: 'PUBLISHED' };
const result = { document_id: 'doc', version_id: 'doc:v2', raw_text: '完整解析原文', blocks: [{ text: '正文' }], saved_chunks: [] };
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('first click reads stored parsing without any workflow task', async () => {
  const reads = [];
  const details = deferred();
  const { select, state } = loader({ adminParseResult: async id => { reads.push(id); return result; } }, [], () => details.promise);
  const pending = select(document);
  assert.deepEqual(reads, ['doc']);
  details.resolve(null);
  await pending;
  assert.equal(state.parsed, result);
  assert.equal(state.read.loading, false);
});

test('failed or expired old tasks do not prevent reading a repaired current document', async () => {
  for (const status of ['FAILED', 'EXPIRED']) {
    const old = { task_id: 'old', document_id: 'doc', version_id: 'doc:v1', status, storage_key: 'uploads/old.pdf' };
    const { select, state } = loader({ adminParseResult: async () => result }, [old]);
    await select({ ...document, supervisor_task: old });
    assert.equal(state.parsed, result);
    assert.equal(state.selected.version_id, 'doc:v2');
    assert.equal(state.selected.storage_key, document.storage_key);
  }
});

test('a slow older document response cannot overwrite the newest selection', async () => {
  const old = deferred();
  const { select, state } = loader({ adminParseResult: id => id === 'doc' ? old.promise : Promise.resolve({ ...result, document_id: id, version_id: 'other:v1' }) });
  const first = select(document);
  await select({ ...document, document_id: 'other', version_id: 'other:v1' });
  old.resolve(result);
  await first;
  assert.equal(state.selected.document_id, 'other');
  assert.equal(state.parsed.document_id, 'other');
});

test('closing selection prevents a pending read from restoring the preview', async () => {
  const pending = deferred();
  const { select, state, close } = loader({ adminParseResult: () => pending.promise });
  const reading = select({ ...document, supervisor_task: { task_id: 'task', document_id: 'doc', version_id: 'doc:v2', status: 'SUCCEEDED' } });
  close();
  pending.resolve(result);
  await reading;
  assert.equal(state.parsed, null);
});

test('read errors and mismatched versions are explicit instead of showing the upload placeholder', async () => {
  for (const read of [async () => { throw new Error('MinIO 读取失败'); }, async () => ({ ...result, version_id: 'doc:v1' })]) {
    const { select, state } = loader({ adminParseResult: read });
    await select(document);
    assert.equal(state.parsed, null);
    assert.ok(state.read.error);
    assert.equal(state.read.loading, false);
  }
});

test('parsed results without version ids must match the current version storage key', async () => {
  for (const version_id of [null, undefined]) {
    const staleResult = { ...result, version_id, storage_key: 'uploads/previous.pdf' };
    const stale = loader({ adminParseResult: async () => staleResult });
    assert.equal(await stale.select({ ...document, status: 'UPLOADED' }), undefined);
    assert.equal(stale.state.parsed, null);
    assert.equal(stale.state.read.loading, false);
    assert.match(stale.state.read.error, /当前版本.*旧版本/);
    assert.doesNotMatch(stale.state.notice, /已读取/);

    const currentResult = { ...staleResult, storage_key: document.storage_key };
    const current = loader({ adminParseResult: async () => currentResult });
    await current.select(document);
    assert.equal(current.state.parsed, currentResult);
    assert.equal(current.state.read.error, '');
  }
});
