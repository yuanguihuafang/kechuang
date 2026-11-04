import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { updateDraftPublicationState } from './draftPresentation.js';

test('card image lookup and detail lookup preserve the query-selected draft', () => {
  const source = readFileSync(new URL('./BusinessBlocks.jsx', import.meta.url), 'utf8');
  const requests = source.match(/api\.retrievalChunkDetail\(\{[\s\S]*?\}\)/g) || [];
  assert.equal(requests.length, 2);
  for (const request of requests) {
    assert.match(request, /matched_draft_record_id: item\.matched_draft_record_id \|\| null/);
  }
});

test('manifest outages are visible on cards and details without replacing text content', () => {
  const source = readFileSync(new URL('./BusinessBlocks.jsx', import.meta.url), 'utf8');
  assert.match(source, /recommendationImageDetails\[recommendationCardKey\(item\)\]\?\.image_error && <small role="alert">/);
  assert.match(source, /selectedRecommendationDetail\?\.image_error && <p[^>]+role="alert">/);
  assert.match(source, /detailDraftText && <section>/);
});

test('failed card and detail image requests hide the image element', () => {
  const source = readFileSync(new URL('./BusinessBlocks.jsx', import.meta.url), 'utf8');
  assert.match(source, /function hideBrokenImage\(event\)\s*\{\s*event\.currentTarget\.hidden = true;\s*\}/);
  assert.equal((source.match(/onError=\{hideBrokenImage\}/g) || []).length, 2);
});

test('limits recommendation-card images to three while detail markdown keeps all images', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({
    configFile: './vite.config.js',
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  });
  try {
    const { BusinessBlocks, markdownContent } = await vite.ssrLoadModule('/src/pages/supervisor/BusinessBlocks.jsx');
    const imageUrls = Array.from({ length: 5 }, (_, index) => `/api/v1/perception/images/doc-1/image-${index + 1}`);
    const markup = renderToStaticMarkup(React.createElement(BusinessBlocks, {
      response: { match_results: [{ id: 'match-1', summary: '匹配摘要', image_urls: imageUrls }] },
    }));
    const detailMarkup = renderToStaticMarkup(React.createElement(React.Fragment, null,
      markdownContent(imageUrls.map((url) => `![图](${url})`).join('\n')),
    ));

    assert.equal((markup.match(/alt="匹配结果图片"/g) || []).length, 3);
    assert.equal((detailMarkup.match(/class="business-evidence-image"/g) || []).length, 5);
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('updates only the published draft by id even if its page index is stale', () => {
  const drafts = [
    { draft_id: 'draft-1', status: 'DRAFT_SAVED' },
    { draft_id: 'draft-2', status: 'DRAFT_SAVED' },
  ];
  const statuses = ['DRAFT_SAVED', 'DRAFT_SAVED'];

  const updated = updateDraftPublicationState(
    drafts,
    statuses,
    'draft-2',
    0,
    'PUBLISHED',
  );

  assert.deepEqual(updated.drafts.map((draft) => draft.status), ['DRAFT_SAVED', 'PUBLISHED']);
  assert.deepEqual(updated.statuses, ['DRAFT_SAVED', 'PUBLISHED']);
});

test('renders a backend-provided contact action even when the test identity has no normalized role', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({
    configFile: './vite.config.js',
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  });
  try {
    const { BusinessBlocks } = await vite.ssrLoadModule('/src/pages/supervisor/BusinessBlocks.jsx');
    const markup = renderToStaticMarkup(React.createElement(BusinessBlocks, {
      response: {
        actions: [{ id: 'contact_wecom', type: 'CONTACT_WECOM', label: '联系客服', enabled: true }],
      },
      identity: { user_id: 'external-test-user', is_inner_user: false },
    }));

    assert.match(markup, /联系客服/);
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('hides the bottom confirm-and-save action for a customer visit draft', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({
    configFile: './vite.config.js',
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  });
  try {
    const { BusinessBlocks } = await vite.ssrLoadModule('/src/pages/supervisor/BusinessBlocks.jsx');
    const markup = renderToStaticMarkup(React.createElement(BusinessBlocks, {
      response: {
        draft: { draft_type: 'customer_visit', title: '客户拜访草稿', fields: {} },
        ui_blocks: [{
          type: 'draft_card',
          data: { draft: { draft_type: 'customer_visit', title: '客户拜访草稿', fields: {} } },
        }],
        actions: [{ id: 'confirm_visit', type: 'CONFIRM_DRAFT', label: '确认并保存', enabled: true }],
      },
    }));

    assert.doesNotMatch(markup, /确认并保存/);
    assert.match(markup, /<div class="draft-card-actions"><button[^>]*>编辑草稿<\/button><button[^>]*aria-label="复制草稿内容"[^>]*>复制<\/button><\/div>/);
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('shows the stable session draft label separately from the newest-first page number', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({
    configFile: './vite.config.js',
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  });
  try {
    const { BusinessBlocks } = await vite.ssrLoadModule('/src/pages/supervisor/BusinessBlocks.jsx');
    const markup = renderToStaticMarkup(React.createElement(BusinessBlocks, {
      response: {
        draft: { draft_id: 'draft-2', ordinal: 2, draft_type: 'solution', title: '工艺方案', fields: {} },
        drafts: [
          { draft_id: 'draft-2', ordinal: 2, draft_type: 'solution', title: '工艺方案', fields: {} },
          { draft_id: 'draft-1', ordinal: 1, draft_type: 'solution', title: '业务方案', fields: {} },
        ],
        ui_blocks: [{
          type: 'draft_card',
          data: { draft: { draft_id: 'draft-2', ordinal: 2, draft_type: 'solution', title: '工艺方案', fields: {} } },
        }],
      },
    }));

    assert.match(markup, /草稿预览 · 技术方案 · 草稿 2/);
    assert.match(markup, /第 1 \/ 2 页/);
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('renders one paginated draft card when the response contains one card block per draft', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({
    configFile: './vite.config.js',
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  });
  try {
    const { BusinessBlocks } = await vite.ssrLoadModule('/src/pages/supervisor/BusinessBlocks.jsx');
    const drafts = Array.from({ length: 8 }, (_, index) => ({
      draft_id: `draft-${index + 1}`,
      ordinal: index + 1,
      draft_type: 'demand',
      title: `需求 ${index + 1}`,
      fields: {},
    }));
    const markup = renderToStaticMarkup(React.createElement(BusinessBlocks, {
      response: {
        draft: drafts[0],
        drafts,
        ui_blocks: drafts.map((draft) => ({ type: 'draft_card', data: { draft } })),
      },
    }));

    assert.equal((markup.match(/class="business-block business-block-draft"/g) || []).length, 1);
    assert.match(markup, /第 1 \/ 8 页/);
    assert.match(markup, /需求 1/);
    assert.doesNotMatch(markup, /需求 2/);
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('normalizes API and perception image path slashes', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({
    configFile: './vite.config.js',
    define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('http://127.0.0.1:8793/') },
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  });
  try {
    const { api } = await vite.ssrLoadModule('/src/api.js');
    assert.equal(
      api.perceptionAssetUrl('/api/v1/perception/images/doc-1/image-1'),
      'http://127.0.0.1:8793/api/v1/perception/images/doc-1/image-1',
    );
    assert.equal(
      api.perceptionAssetUrl('//api/v1/perception/images/doc-1/image-1'),
      'http://127.0.0.1:8793/api/v1/perception/images/doc-1/image-1',
    );
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('renders detail images without leaking orphaned markdown image markers', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const vite = await createServer({
    configFile: './vite.config.js',
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  });
  try {
    const { markdownContent } = await vite.ssrLoadModule('/src/pages/supervisor/BusinessBlocks.jsx');
    const markup = renderToStaticMarkup(React.createElement(React.Fragment, null,
      markdownContent('设备示意图：\n)\n![\n![设备](/api/v1/perception/images/doc-1/image-1)'),
    ));

    assert.equal((markup.match(/<img\b/g) || []).length, 1);
    assert.doesNotMatch(markup, /(?:^|>)\s*(?:\)|!\[)\s*(?:<|$)/);
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});
