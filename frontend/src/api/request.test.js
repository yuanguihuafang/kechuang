import test from 'node:test';
import assert from 'node:assert/strict';
import { download } from './request.js';

test('download sends the saved login token as a bearer credential', async () => {
  const previousSessionStorage = globalThis.sessionStorage;
  const previousFetch = globalThis.fetch;
  let authorization;
  globalThis.sessionStorage = { getItem: () => 'access-token' };
  globalThis.fetch = async (_url, options) => {
    authorization = options.headers.get('Authorization');
    return { ok: true, blob: async () => new Blob(['file']) };
  };

  try {
    const blob = await download('http://localhost:8793/api/v1/knowledge/documents/doc-1/file');
    assert.equal(authorization, 'Bearer access-token');
    assert.equal(await blob.text(), 'file');
  } finally {
    if (previousSessionStorage === undefined) delete globalThis.sessionStorage;
    else globalThis.sessionStorage = previousSessionStorage;
    globalThis.fetch = previousFetch;
  }
});
