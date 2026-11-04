import assert from 'node:assert/strict';
import test from 'node:test';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

test('non-initial pages are emitted outside the entry chunk', async () => {
  const root = fileURLToPath(new URL('.', import.meta.url));
  const result = await build({
    root,
    configFile: join(root, 'vite.config.js'),
    mode: 'server',
    logLevel: 'silent',
    build: { write: false },
  });
  const chunks = (Array.isArray(result) ? result : [result])
    .flatMap(({ output }) => output)
    .filter((item) => item.type === 'chunk');
  const entry = chunks.find((chunk) => chunk.isEntry);

  assert.ok(entry, 'Vite should emit an entry chunk');
  for (const page of [
    'pages/documents/IndexPreviewModal.jsx',
    'pages/documents/AdminDocumentsPage.jsx',
    'pages/documents/AdminUploadPage.jsx',
    'pages/personal/PersonalPage.jsx',
    'pages/retrieval/RetrievalPage.jsx',
    'pages/admin/AdminOverview.jsx',
    'pages/admin/AdminFieldPolicy.jsx',
    'pages/admin/AdminGovernance.jsx',
    'pages/admin/TaskCenter.jsx',
  ]) {
    const chunk = chunks.find((item) => item.moduleIds.some((id) => (
      id.replaceAll('\\', '/').endsWith(`/src/${page}`)
    )));
    assert.ok(chunk, `${page} should be included in the production build`);
    assert.ok(chunk !== entry, `${page} should be loaded on demand`);
  }
});
