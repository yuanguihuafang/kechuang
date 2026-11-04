import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { downloadAuthenticatedFile } from './authenticatedDownload.js';

test('download button resolves the API base through the exported asset URL helper', () => {
  const component = readFileSync(new URL('./AuthenticatedDownload.jsx', import.meta.url), 'utf8');
  assert.match(component, /downloadAuthenticatedFile\(url, filename, api\.perceptionAssetUrl\('\/'\)\)/);
});

test('downloads an API-relative file and clicks a named download link', async () => {
  const previous = {
    fetch: globalThis.fetch,
    sessionStorage: globalThis.sessionStorage,
    document: globalThis.document,
    window: globalThis.window,
    urlCreateObjectURL: URL.createObjectURL,
    urlRevokeObjectURL: URL.revokeObjectURL,
  };
  let requestedUrl;
  let clickedLink;
  globalThis.sessionStorage = { getItem: () => 'test-token' };
  globalThis.fetch = async (url, options) => {
    requestedUrl = url;
    assert.equal(options.headers.get('Authorization'), 'Bearer test-token');
    return { ok: true, blob: async () => new Blob(['file']) };
  };
  URL.createObjectURL = () => 'blob:download';
  URL.revokeObjectURL = () => {};
  globalThis.window = { setTimeout: (callback) => callback() };
  globalThis.document = {
    body: { appendChild: () => {} },
    createElement: (tagName) => {
      assert.equal(tagName, 'a');
      return {
        click() { clickedLink = { href: this.href, filename: this.download }; },
        remove() {},
      };
    },
  };

  try {
    await downloadAuthenticatedFile(
      '/api/v1/knowledge/documents/doc-1/file',
      'source.pdf',
      'http://localhost:8793/',
    );
    assert.equal(requestedUrl, 'http://localhost:8793/api/v1/knowledge/documents/doc-1/file');
    assert.deepEqual(clickedLink, { href: 'blob:download', filename: 'source.pdf' });
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (key.startsWith('url')) {
        const method = key === 'urlCreateObjectURL' ? 'createObjectURL' : 'revokeObjectURL';
        if (value === undefined) delete URL[method];
        else URL[method] = value;
      } else if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});
