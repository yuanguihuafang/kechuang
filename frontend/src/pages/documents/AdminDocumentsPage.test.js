import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('all admin document catalogs reload when pagination or filters change', () => {
  const source = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
  const start = source.indexOf('    const previous = adminDocumentContextRef.current;');
  const end = source.indexOf('\n  }, [', start);
  assert.ok(start >= 0 && end > start);
  const effect = new Function(
    'route', 'page', 'identity', 'adminDocumentContextRef', 'refreshFiles', 'refreshPersonal',
    source.slice(start, end),
  );
  const dependencies = source.slice(end, source.indexOf(']);', end));
  for (const key of ['documentPage', 'documentSearch', 'catalogKnowledgeType', 'overviewUpdatePage', 'draftCatalogPage']) {
    assert.ok(dependencies.includes(key));
  }
  for (const page of ['overview', 'documents', 'upload']) {
    let requests = 0;
    const context = { current: null };
    const refresh = () => { requests += 1; };
    // React invokes the same effect again after documentPage changes.
    effect('/admin', page, { user_id: 'admin' }, context, refresh, () => {});
    effect('/admin', page, { user_id: 'admin' }, context, refresh, () => {});
    assert.equal(requests, 2, `${page} must reload on entry and page change`);
  }
});

test('document catalog search is in the header and preserves each page data scope', () => {
  const app = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
  const page = readFileSync(new URL('./AdminDocumentsPage.jsx', import.meta.url), 'utf8');
  const api = readFileSync(new URL('../../api.js', import.meta.url), 'utf8');
  const catalogStart = page.indexOf('{showDocumentCatalog && <section className="panel files">');
  const catalogEnd = page.indexOf('<FileTable', catalogStart);
  const catalog = page.slice(catalogStart, catalogEnd);

  assert.match(app, /search: \['documents', 'upload'\]\.includes\(page\) \? documentSearch \|\| undefined : undefined/);
  assert.match(app, /published_or_admin_only: page === 'upload'/);
  assert.match(catalog, /className="section-actions">[\s\S]*?<form className="document-catalog-search"/);
  assert.doesNotMatch(page, /\{catalogOnly && <form className="document-catalog-search"/);
  assert.match(page, /id="admin-document-search" type="search"/);
  assert.match(page, /onDocumentSearchChange\?\.\(documentSearchInput\.trim\(\)\)/);
  assert.match(api, /query\.set\("search", params\.search\)/);
  assert.match(api, /query\.set\("published_or_admin_only", "true"\)/);
});

test('clears previous field drafts before starting a new extraction request', () => {
  const source = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');
  const start = source.indexOf('  async function extractFields() {');
  const request = source.indexOf('await api.adminExtractFields(taskId, extractionKnowledgeType)', start);
  const clear = source.indexOf("draft_status: 'RUNNING'", start);

  assert.ok(start >= 0 && clear > start && request > clear);
  for (const field of ['draft_id: null', 'draft_ids: []', 'draft_preview: null', 'draft_previews: []']) {
    assert.ok(source.indexOf(field, start) < request, `${field} must be cleared before extraction starts`);
  }
});

test('admin draft details show withdrawal reason and deletion time without enabling edits', () => {
  const page = readFileSync(new URL('./AdminDocumentsPage.jsx', import.meta.url), 'utf8');

  assert.match(page, /status_note/);
  assert.match(page, /deleted_at/);
  assert.match(page, /currentDraftStatus !== 'DELETED'/);
  assert.match(page, /disabled=\{currentDraftStatus === 'DELETED'\}/);
  assert.match(page, /onPurgeDraft/);
  assert.match(page, /管理员可按需立即彻底删除/);
  assert.doesNotMatch(page, /超过\s*30\s*天|30天后由定期清理/);
});
