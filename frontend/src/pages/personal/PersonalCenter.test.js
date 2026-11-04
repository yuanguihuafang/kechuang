import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

test('personal center keeps independent same-title drafts and scopes attachments to each match', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({ configFile: './vite.config.js', optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, ws: false } });
  try {
    const { PersonalCenter, matchAttachments } = await vite.ssrLoadModule('/src/pages/personal/PersonalCenter.jsx');
    const drafts = ['draft-A', 'draft-B'].map(draft_id => ({
      draft_id, thread_id: 'session-1', draft_type: 'solution', draft_title: '同名方案', draft: { fields: {} },
    }));
    const markup = renderToStaticMarkup(React.createElement(PersonalCenter, {
      identity: { user_id: 'user-1' }, documents: [], conversations: [], drafts: [...drafts, drafts[0]],
    }));
    assert.equal((markup.match(/class="personal-draft-open"/g) || []).length, 2);
    const own = { document_id: 'doc-1', source_id: 'source-1' };
    const other = { document_id: 'doc-2', source_id: 'source-2' };
    assert.deepEqual(matchAttachments({ source_id: 'source-1' }, { documents: [own, other] }), [own]);
    assert.deepEqual(matchAttachments({ document_id: 'doc-1' }, { documents: [own, other] }), [own]);
    assert.deepEqual(matchAttachments({}, { documents: [own, other] }), []);
    const activity = renderToStaticMarkup(React.createElement(PersonalCenter, {
      identity: { user_id: 'user-1' }, documents: [], drafts: [],
      conversations: [{ thread_id: 's-1', state: { attachments: [{ document_id: 'doc-1', file_name: '资料.pdf', file_url: '/private-file' }] } }],
    }));
    assert.match(activity, /<button[^>]*>查看附件<\/button>/);
    assert.doesNotMatch(activity, /href="\/private-file"/);
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

function documentLoader(api) {
  const source = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
  const start = source.indexOf('  async function openPersonalDocument(document)');
  const end = source.indexOf('  async function unpublishMyDocument', start);
  const state = { detail: null, loading: false, notice: '' };
  const handlers = new Function('api', 'identity', 'personalDocumentRequestRef', 'setPersonalDocumentLoading', 'setPersonalDocumentDetail', 'setNotice',
    source.slice(start, end) + '\nreturn { openPersonalDocument, closePersonalDocument };')(
    api, { user_id: 'user-1' }, { current: 0 }, value => { state.loading = value; }, value => { state.detail = value; }, value => { state.notice = value; },
  );
  return { ...handlers, state };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('closing a pending document does not reopen it when the response arrives', async () => {
  const pending = deferred();
  const loader = documentLoader({ myDocumentDetail: () => pending.promise, myDocumentParseResult: async () => ({ raw_text: '原文' }) });
  const request = loader.openPersonalDocument({ document_id: 'doc-1' });
  loader.closePersonalDocument();
  pending.resolve({ document: { document_id: 'doc-1' } });
  await request;
  assert.equal(loader.state.detail, null);
  assert.equal(loader.state.loading, false);
});

test('the last selected document wins when responses arrive out of order', async () => {
  const first = deferred();
  const loader = documentLoader({
    myDocumentDetail: async (_user, id) => id === 'doc-1' ? first.promise : { document: { document_id: id } },
    myDocumentParseResult: async () => ({ raw_text: '原文' }),
  });
  const request = loader.openPersonalDocument({ document_id: 'doc-1' });
  await loader.openPersonalDocument({ document_id: 'doc-2' });
  first.resolve({ document: { document_id: 'doc-1' } });
  await request;
  assert.equal(loader.state.detail.document.document_id, 'doc-2');
});

test('parse failures remain visible while the document detail stays available', async () => {
  const loader = documentLoader({
    myDocumentDetail: async () => ({ document: { document_id: 'doc-1' } }),
    myDocumentParseResult: async () => { throw Object.assign(new Error('解析服务不可用'), { status: 503 }); },
  });
  await loader.openPersonalDocument({ document_id: 'doc-1' });
  assert.equal(loader.state.detail.document.document_id, 'doc-1');
  assert.match(loader.state.detail.parseError, /解析服务不可用/);
});

function personalLoader(api) {
  const source = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
  const start = source.indexOf('  async function refreshPersonal()');
  const end = source.indexOf('  async function syncMysqlTables', start);
  const state = { documents: [{ document_id: 'old' }] };
  const ref = { current: 0 };
  const names = ['profile', 'documents', 'conversations', 'drafts', 'visits', 'matches', 'conversationId', 'messages', 'loading', 'notice'];
  const setters = names.map(name => value => { state[name] = typeof value === 'function' ? value(state[name]) : value; });
  const refresh = new Function('api', 'identity', 'isAdminPage', 'personalRefreshRequestRef',
    'setMyProfile', 'setMyDocuments', 'setMyConversations', 'setMyDrafts', 'setMyVisitRecords', 'setMyMatchResults',
    'setPersonalConversationId', 'setPersonalConversationMessages', 'setPersonalLoading', 'setNotice',
    source.slice(start, end) + '\nreturn refreshPersonal;')(
    api, { user_id: 'user-1', user_role: 'internal' }, false, ref, ...setters,
  );
  return { refresh, state, ref };
}

const personalApi = {
  myProfile: async () => ({ user_id: 'user-1' }), myDocuments: async () => ({ documents: [] }),
  myConversations: async () => ({ sessions: [] }), myDrafts: async () => [],
  myVisitRecords: async () => [], myMatchResults: async () => [],
};

test('a failed list does not prevent successful personal sections from refreshing', async () => {
  const loader = personalLoader({ ...personalApi, myDocuments: async () => { throw new Error('文档暂不可用'); } });
  await loader.refresh();
  assert.equal(loader.state.profile.user_id, 'user-1');
  assert.deepEqual(loader.state.documents, [{ document_id: 'old' }]);
  assert.deepEqual(loader.state.matches, []);
  assert.match(loader.state.notice, /文档暂不可用/);
  assert.equal(loader.state.loading, false);
});

test('an invalidated personal refresh cannot restore the previous user data', async () => {
  const pending = deferred();
  const loader = personalLoader({ ...personalApi, myProfile: () => pending.promise });
  const request = loader.refresh();
  loader.ref.current += 1;
  loader.state.profile = null;
  pending.resolve({ user_id: 'old-user' });
  await request;
  assert.equal(loader.state.profile, null);
});

test('logical draft deletion hides its entries and does not promise timed purging', () => {
  const source = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
  const start = source.indexOf('  async function deletePersonalDraft(record)');
  const end = source.indexOf('\n  async function deletePersonalVisitRecord', start);
  const handler = source.slice(start, end);

  assert.match(handler, /setPersonalDraftDetail\(null\)/);
  assert.match(handler, /草稿和附件入口立即隐藏/);
  assert.match(handler, /管理员可在管理台查看并按需彻底删除/);
  assert.doesNotMatch(handler, /30天/);
});
