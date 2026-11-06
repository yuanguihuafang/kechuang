import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { currentDocumentTask, currentDocumentVersion, catalogStage } from './documentStatus.js';
import { reparseResetState } from './reparseState.js';

const source = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
const document = { document_id: 'A', version_id: 'A:v2', storage_key: 'uploads/A-v2.pdf', file_name: 'A.pdf' };
const task = { ...document, task_id: 'current', operation: 'file_ingestion', task_type: 'document_ingestion', status: 'UPLOADED' };
const parsed = { document_id: 'A', version_id: 'A:v2', raw_text: 'A正文', blocks: [{ text: 'A正文' }] };
const other = { document_id: 'B', version_id: 'B:v1', storage_key: 'uploads/B.pdf' };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Execute the actual App functions; fake only React setters and the API boundary.
function harness(functions, overrides = {}) {
  functions = [...new Set([...functions, 'updateRecord'])];
  const state = { selected: document, uploaded: null, parsed, generatedChunks: [], versionsDocument: document, versions: [], documentDetails: {}, notice: '', files: [] };
  const env = {
    api: {}, adminDocumentRequestRef: { current: 0 }, supervisorTasks: [], records: {},
    currentDocumentTask, currentDocumentVersion, catalogStage, reparseResetState,
    route: '/admin', parsedDirty: false, parsedDraftRef: { current: '' }, identity: { user_id: 'test' },
    parsed, selected: document, uploaded: null, versionsDocument: document, generatedChunks: [],
    currentStage: 'confirm', indexPreview: null, taskStage: () => ({}), displayError: error => error.message,
    refreshFiles: async () => {}, receiveAdminTaskUpdate: () => {},
    window: { confirm: () => true }, persist: () => {},
    page: 'upload', documentPage: 2, documentSearch: 'B', catalogKnowledgeType: '',
    overviewUpdatePage: 1, draftCatalogPage: 1, DOCUMENT_PAGE_SIZE: 20, ADMIN_PAGE_SIZE: 20,
    displayKnowledgeType: item => item.knowledge_type,
    ...overrides,
  };
  for (const name of ['selected', 'uploaded', 'parsed', 'generatedChunks', 'versionsDocument', 'versions', 'versionsLoading', 'documentDetails', 'notice', 'parseReadState', 'parsedDirty', 'file', 'busy', 'versionsBusy', 'indexPreview', 'uploadTarget', 'knowledgeType', 'files', 'fileLoading', 'documentTotal', 'documentTotalPages', 'documentPage', 'adminOverview', 'knowledgeUpdates', 'draftCatalog', 'mysqlSync', 'supervisorTasks', 'adminFieldPolicies']) {
    env[`set${name[0].toUpperCase()}${name.slice(1)}`] = value => { state[name] = typeof value === 'function' ? value(state[name]) : value; };
  }
  const bodies = functions.map(name => {
    const start = source.search(new RegExp(`^  (?:async )?function ${name}\\(`, 'm'));
    assert.notEqual(start, -1, `missing real function ${name}`);
    const rest = source.slice(start + 1);
    const end = rest.search(/^  (?:async )?function |^  const receiveAdminTaskUpdate/m);
    return source.slice(start, end < 0 ? undefined : start + 1 + end);
  });
  const actions = new Function(...Object.keys(env), bodies.join('\n') + `\nreturn {${functions.join(',')}};`)(...Object.values(env));
  return { ...actions, state, env, switchToB() { env.adminDocumentRequestRef.current += 1; state.selected = other; state.uploaded = null; state.parsed = { ...parsed, document_id: 'B', raw_text: 'B正文' }; state.versionsDocument = other; } };
}

for (const invalid of [
  { version_id: 'A:v1' }, { storage_key: 'uploads/old.pdf' }, { document_id: 'B' },
  { operation: 'document_lifecycle', task_type: 'document_lifecycle', lifecycle_action: 'purge' },
  { operation: 'mysql_sync' }, { task_type: 'unexpected' },
]) {
  test(`task identity rejects ${JSON.stringify(invalid)}`, () => {
    assert.equal(currentDocumentTask(document, [{ ...task, ...invalid }, task]), task);
    assert.equal(currentDocumentTask(document, [{ ...task, ...invalid }]), null);
  });
}

test('resolver skips a stale cached task and finds the exact current file task without replacing version/storage', async () => {
  const stale = { ...task, task_id: 'old', version_id: 'A:v1', storage_key: 'uploads/old.pdf' };
  const lifecycle = { ...task, task_id: 'purge', operation: 'document_lifecycle', task_type: 'document_lifecycle' };
  const app = harness(['resolveSupervisorDocument'], { api: { adminTask: async () => stale, adminTasks: async () => ({ items: [stale, lifecycle, task] }) } });
  const resolved = await app.resolveSupervisorDocument({ ...document, supervisor_task_id: 'old' });
  assert.equal(resolved.supervisor_task_id, 'current');
  assert.equal(resolved.version_id, document.version_id);
  assert.equal(resolved.storage_key, document.storage_key);
});

for (const action of ['parse', 'extractFields', 'confirmIndex']) {
  test(`${action} cannot submit an invalid cached task when no current task exists`, async () => {
    let submissions = 0;
    const stale = { ...document, supervisor_task_id: 'old', draft_previews: [{ title: '草稿' }] };
    const app = harness(['resolveSupervisorDocument', action], {
      selected: stale, api: { adminTask: async () => ({ ...task, version_id: 'A:v1' }), adminTasks: async () => ({ items: [] }), adminExtractFields: async () => { submissions += 1; }, adminConfirmTask: async () => { submissions += 1; return {}; } },
      parseSupervisorTask: async () => { submissions += 1; return parsed; },
      waitForSupervisorTask: async () => ({ ...task, status: 'SUCCEEDED' }),
      pollAdminDraft: async () => ({ ...task, draft_status: 'READY' }),
    });
    await app[action]();
    assert.equal(submissions, 0);
    assert.match(app.state.notice, /任务|重新上传|更新/);
  });
}

test('a detail failure propagates through the real loader instead of accepting a stale parsed version', async () => {
  const app = harness(['loadVersions', 'selectStoredFile'], { api: { adminDocument: async () => { throw new Error('版本服务不可用'); }, adminParseResult: async () => parsed } });
  const success = await app.selectStoredFile(document);
  assert.notEqual(success, true);
  assert.equal(app.state.parsed, null);
  assert.match(app.state.parseReadState.error, /版本服务不可用/);
});

test('pagination/search keeps the independent selection, body and details together', async () => {
  const app = harness(['refreshFilesNow'], { api: { adminDocuments: async () => ({ documents: [other], total: 30, total_pages: 2 }), adminOverview: async () => ({}), adminFieldPolicies: async () => ({}) } });
  await app.refreshFilesNow();
  assert.equal(app.state.selected.document_id, 'A');
  assert.equal(app.state.parsed.document_id, 'A');
  assert.equal(app.state.versionsDocument.document_id, 'A');
});

test('explicit close clears selection, body, chunks and pending reads together', () => {
  const app = harness(['clearDocumentSelection']);
  app.state.generatedChunks = [{ text: 'old' }];
  app.clearDocumentSelection();
  for (const key of ['selected', 'uploaded', 'parsed', 'versionsDocument', 'indexPreview']) assert.equal(app.state[key], null);
  assert.deepEqual(app.state.generatedChunks, []);
  assert.equal(app.env.adminDocumentRequestRef.current, 1);
});

for (const finish of ['resolve', 'reject']) {
  test(`parse A ${finish} after selecting B cannot replace B body or selection`, async () => {
    const pending = deferred();
    const app = harness(['parse', 'parseSupervisorTask', 'updateRecord'], {
      resolveSupervisorDocument: async () => ({ ...document, supervisor_task_id: task.task_id }),
      api: { adminParseTask: async () => {}, adminParseResult: async () => parsed },
      waitForSupervisorTask: () => pending.promise,
    });
    const parsing = app.parse();
    await new Promise(resolve => setImmediate(resolve));
    app.switchToB();
    pending[finish](finish === 'resolve' ? { ...task, status: 'WAITING_CONFIRM' } : new Error('解析失败'));
    await parsing;
    assert.equal(app.state.selected.document_id, 'B');
    assert.equal(app.state.uploaded, null);
    assert.equal(app.state.parsed.raw_text, 'B正文');
  });
}

for (const name of ['changeVersionPublication', 'changeVersionStorage']) {
  test(`${name} finishing A cannot replace B details`, async () => {
    const pending = deferred();
    const reads = [];
    const app = harness([name, 'loadVersions'], { api: { adminUnpublishVersion: () => pending.promise, adminRecycleVersion: () => pending.promise, adminDocument: async id => { reads.push(id); return { document, versions: [document] }; } } });
    const changing = app[name](document, name === 'changeVersionStorage' ? 'recycle' : 'unpublish');
    app.switchToB();
    pending.resolve({});
    await changing;
    assert.equal(app.state.versionsDocument.document_id, 'B');
    assert.deepEqual(reads, []);
  });
}

test('closing while parsing prevents its completion from reopening the selected document', async () => {
  const pending = deferred();
  const app = harness(['parse', 'parseSupervisorTask', 'clearDocumentSelection'], {
    resolveSupervisorDocument: async () => ({ ...document, supervisor_task_id: task.task_id }),
    api: { adminParseTask: async () => {}, adminParseResult: async () => parsed }, waitForSupervisorTask: () => pending.promise,
  });
  const parsing = app.parse();
  await new Promise(resolve => setImmediate(resolve));
  app.clearDocumentSelection();
  pending.resolve({ ...task, status: 'WAITING_CONFIRM' });
  await parsing;
  assert.equal(app.state.selected, null);
  assert.equal(app.state.parsed, null);
});

test('switching selection during task lookup prevents a parse submission', async () => {
  const lookup = deferred();
  let submissions = 0;
  const app = harness(['parse', 'resolveSupervisorDocument'], {
    api: { adminTasks: () => lookup.promise }, parseSupervisorTask: async () => { submissions += 1; return parsed; },
  });
  const parsing = app.parse();
  app.switchToB();
  lookup.resolve({ items: [task] });
  await parsing;
  assert.equal(submissions, 0);
  assert.equal(app.state.selected.document_id, 'B');
});

for (const action of ['extractFields', 'confirmIndex']) {
  test(`${action} completion after switching selection preserves B`, async () => {
    const pending = deferred();
    const app = harness([action], {
      selected: { ...document, draft_previews: [{ title: '草稿' }] },
      resolveSupervisorDocument: async value => ({ ...value, supervisor_task_id: task.task_id }),
      api: { adminExtractFields: async () => {}, adminConfirmTask: async () => ({}) },
      waitForSupervisorTask: () => pending.promise, pollAdminDraft: async () => ({ ...task, draft_status: 'READY' }),
    });
    const running = app[action]();
    await new Promise(resolve => setImmediate(resolve));
    app.switchToB();
    pending.resolve({ ...task, status: 'SUCCEEDED' });
    await running;
    assert.equal(app.state.selected.document_id, 'B');
    assert.equal(app.state.uploaded, null);
    assert.equal(app.state.parsed.raw_text, 'B正文');
  });
}

test('task lookup errors remain explicit rather than silently switching to a catalog task', async () => {
  const app = harness(['resolveSupervisorDocument'], { api: { adminTask: async () => { throw new Error('任务服务不可用'); }, adminTasks: async () => ({ items: [task] }) } });
  await assert.rejects(app.resolveSupervisorDocument({ ...document, supervisor_task_id: 'current' }), /任务服务不可用/);
});

test('task lookup 404 may resolve another exact current task from the catalog', async () => {
  const app = harness(['resolveSupervisorDocument'], { api: { adminTask: async () => { throw Object.assign(new Error('已清理'), { status: 404 }); }, adminTasks: async () => ({ items: [task] }) } });
  assert.equal((await app.resolveSupervisorDocument({ ...document, supervisor_task_id: 'missing' })).supervisor_task_id, 'current');
});

test('index preview uses the validated selection result once and cannot bypass detail failure', async () => {
  for (const failDetails of [false, true]) {
    let reads = 0;
    const app = harness(['openIndexPreview', 'selectStoredFile', 'loadVersions'], { api: {
      adminDocument: async () => { if (failDetails) throw new Error('版本读取失败'); return { document }; },
      adminParseResult: async () => { reads += 1; return parsed; },
    } });
    await app.openIndexPreview(document);
    assert.equal(reads, 1);
    if (failDetails) {
      assert.equal(app.state.indexPreview, null);
      assert.equal(app.state.parsed, null);
      assert.match(app.state.notice, /版本读取失败/);
    } else assert.equal(app.state.indexPreview.parsed, parsed);
  }
});

test('index preview cannot restore A after selecting B', async () => {
  const pending = deferred();
  const app = harness(['openIndexPreview', 'selectStoredFile', 'loadVersions'], { api: { adminDocument: async () => ({ document }), adminParseResult: () => pending.promise } });
  const reading = app.openIndexPreview(document);
  app.switchToB();
  pending.resolve(parsed);
  await reading;
  assert.equal(app.state.parsed.raw_text, 'B正文');
  assert.notEqual(app.state.indexPreview?.loading, false);
});

test('choosing a replacement file clears old body and chunks together', () => {
  const app = harness(['chooseFile'], { uploadTarget: document });
  app.state.generatedChunks = [{ text: 'old' }];
  app.chooseFile({ target: { files: [{ name: 'replacement.pdf' }] } });
  assert.equal(app.state.parsed, null);
  assert.deepEqual(app.state.generatedChunks, []);
});

test('request sequence also protects a document closed and selected again with the same id', async () => {
  const pending = deferred();
  const app = harness(['parse', 'parseSupervisorTask', 'clearDocumentSelection'], {
    resolveSupervisorDocument: async () => ({ ...document, supervisor_task_id: task.task_id }),
    api: { adminParseTask: async () => {}, adminParseResult: async () => parsed }, waitForSupervisorTask: () => pending.promise,
  });
  const parsing = app.parse();
  await new Promise(resolve => setImmediate(resolve));
  app.clearDocumentSelection();
  app.state.selected = document;
  app.state.parsed = { ...parsed, raw_text: '重新选择后的正文' };
  pending.resolve({ ...task, status: 'WAITING_CONFIRM' });
  await parsing;
  assert.equal(app.state.parsed.raw_text, '重新选择后的正文');
});

test('fresh upload attaches its real task before enabling workflow actions', async () => {
  const app = harness(['upload'], { file: { name: 'A.pdf' }, uploadTarget: null, scopeId: 'public', knowledgeType: 'solution', createUuid: () => 'upload-key', api: { adminUpload: async () => task } });
  await app.upload();
  assert.equal(app.state.uploaded.supervisor_task, task);
  assert.equal(currentDocumentTask(app.state.uploaded, [app.state.uploaded.supervisor_task]), task);
  assert.doesNotMatch(app.state.notice, /失败/);
});

test('replacement upload replaces cached active version before resolving its new task', async () => {
  const oldVersion = { ...document, version_id: 'A:v1', storage_key: 'uploads/A-v1.pdf' };
  const oldDocument = { ...oldVersion, active_version_id: oldVersion.version_id, active_version: oldVersion };
  const submissions = [];
  const app = harness(['upload', 'parse', 'resolveSupervisorDocument'], {
    records: { A: oldDocument }, uploadTarget: oldDocument, file: { name: 'replacement.pdf' },
    scopeId: 'public', knowledgeType: 'solution', createUuid: () => 'upload-key',
    api: { adminUpload: async () => task, adminTask: async () => task, adminTasks: async () => ({ items: [task] }) },
    parseSupervisorTask: async value => { submissions.push(value); return parsed; },
  });
  await app.upload();
  assert.equal(submissions.length, 1);
  assert.equal(currentDocumentVersion(submissions[0]).version_id, 'A:v2');
  assert.equal(submissions[0].storage_key, task.storage_key);
  assert.equal(currentDocumentTask(app.state.uploaded, [task]), task);
  assert.doesNotMatch(app.state.notice, /缺少.*任务/);
});

for (const detailsFirst of [true, false]) {
  test(`current version and task remain selectable when parsing is unavailable (details first: ${detailsFirst})`, async () => {
    const oldVersion = { ...document, version_id: 'A:v1', storage_key: 'uploads/A-v1.pdf' };
    const oldTask = { ...task, ...oldVersion, task_id: 'old' };
    const oldDocument = { ...oldVersion, active_version_id: oldVersion.version_id, active_version: oldVersion, supervisor_task: oldTask };
    const details = deferred();
    const body = deferred();
    const app = harness(['selectStoredFile', 'loadVersions'], { api: { adminDocument: () => details.promise, adminParseResult: () => body.promise } });
    const detailResponse = { document: { ...document, active_version_id: document.version_id, active_version: document }, task_records: [oldTask, { ...task, task_type: 'document_lifecycle', lifecycle_action: 'purge' }, task], versions: [oldVersion, document] };
    const reading = app.selectStoredFile(oldDocument);
    if (detailsFirst) {
      details.resolve(detailResponse);
      await new Promise(resolve => setImmediate(resolve));
      body.reject(Object.assign(new Error('当前版本尚无正文'), { status: 404 }));
    } else {
      body.reject(Object.assign(new Error('当前版本尚无正文'), { status: 404 }));
      await reading;
      details.resolve(detailResponse);
    }
    assert.equal(await reading, undefined);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(currentDocumentVersion(app.state.selected).version_id, 'A:v2');
    assert.equal(app.state.selected.storage_key, task.storage_key);
    assert.equal(app.state.selected.supervisor_task_id, task.task_id);
    assert.equal(currentDocumentTask(app.state.selected, [app.state.selected.supervisor_task]), task);
    assert.equal(app.state.versionsDocument.active_version_id, 'A:v2');
    assert.equal(app.state.parsed, null);
    assert.match(app.state.parseReadState.error, /没有保存的解析结果/);
    assert.doesNotMatch(app.state.notice, /已读取/);
  });
}

test('late current-version details after a failed body read cannot change selection B', async () => {
  const details = deferred();
  const app = harness(['selectStoredFile', 'loadVersions'], { api: { adminDocument: () => details.promise, adminParseResult: async () => { throw Object.assign(new Error('没有正文'), { status: 404 }); } } });
  await app.selectStoredFile(document);
  app.switchToB();
  details.resolve({ document: { ...document, active_version_id: 'A:v3', active_version: { ...document, version_id: 'A:v3', storage_key: 'uploads/A-v3.pdf' } } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.state.selected.document_id, 'B');
  assert.equal(app.state.versionsDocument.document_id, 'B');
  assert.equal(app.state.parsed.raw_text, 'B正文');
});

for (const name of ['waitForSupervisorTask', 'pollAdminDraft']) {
  test(`${name} rejects a changed task identity immediately, without retrying a permanent mismatch`, async () => {
    let reads = 0;
    const app = harness([name], { setTimeout: done => done(), api: { adminTask: async () => { reads += 1; return { ...task, version_id: 'A:v1', status: 'WAITING_CONFIRM', draft_status: 'READY' }; } } });
    const running = name === 'waitForSupervisorTask'
      ? app[name](task.task_id, ['WAITING_CONFIRM'], document)
      : app[name](task.task_id, document);
    await assert.rejects(running, /任务.*(?:文档|版本).*不一致/);
    assert.equal(reads, 1);
    assert.equal(app.state.selected.version_id, 'A:v2');
  });
}
