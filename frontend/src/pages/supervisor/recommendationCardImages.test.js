import test from 'node:test';
import assert from 'node:assert/strict';
import { recommendationCardImages, recommendationCardSummary } from './recommendationCardImages.js';

test('keeps card image URLs even when the 240-character summary cuts off Markdown', () => {
  const summary = `${'技术方案说明'.repeat(20)}![images/chart.png](images/chart.png`;
  const item = { summary, image_urls: ['/api/v1/perception/images/doc-1/image-1'] };

  assert.equal(recommendationCardSummary(item.summary), '技术方案说明'.repeat(20));
  assert.equal(recommendationCardSummary('<img src="/images/old.jpg">图片标题'), '图片标题');
  assert.deepEqual(recommendationCardImages(item), ['/api/v1/perception/images/doc-1/image-1']);
});

test('deduplicates and ignores empty card image URLs', () => {
  assert.deepEqual(
    recommendationCardImages({ image_urls: ['', '/image-1', '/image-1', null] }),
    ['/image-1'],
  );
});

test('does not render unverified bare image addresses from a card summary', () => {
  assert.equal(recommendationCardSummary('封面：/api/v1/perception/images/doc-1/removed-image\n说明'), '封面： 说明');
});

test('does not create an image URL from an unverified image id', () => {
  const item = {
    document_id: 'doc_a2c0633c_a63cd0038cb7e76d',
    image_ids: ['04c9a25fee18e53b2a7684881ac1ca5c1724a546'],
    image_urls: ['/images/d923282f6462eb60b0900436df6f0980298fcf995b070a8f7efcc350e9e35f35.jpg'],
  };

  assert.deepEqual(recommendationCardImages(item), []);
});

test('shows only images from the matched chunk, not neighboring detail chunks', () => {
  const item = {
    id: 'chunk-1',
    document_id: 'doc-1',
    chunk_role: 'admin_doc',
    chunk_index: 1,
    image_urls: [],
  };
  const detail = {
    image_urls: [
      '/api/v1/perception/images/doc-1/neighbor-before',
      '/api/v1/perception/images/doc-1/matched',
      '/api/v1/perception/images/doc-1/neighbor-after',
    ],
    source_chunks: [
      { chunk_role: 'admin_doc', chunk_index: 0, document_id: 'doc-1', image_urls: ['/api/v1/perception/images/doc-1/neighbor-before'] },
      { chunk_role: 'admin_doc', chunk_index: 1, document_id: 'doc-1', image_urls: ['/api/v1/perception/images/doc-1/matched'] },
      { chunk_role: 'admin_doc', chunk_index: 2, document_id: 'doc-1', image_urls: ['/api/v1/perception/images/doc-1/neighbor-after'] },
    ],
  };

  assert.deepEqual(recommendationCardImages(item, detail), ['/api/v1/perception/images/doc-1/matched']);
});

test('extracts canonical image URLs from Markdown in the matched chunk only', () => {
  const item = { chunk_role: 'admin_doc', chunk_index: 1, document_id: 'doc-1' };
  const detail = {
    image_urls: [],
    source_chunks: [{ chunk_role: 'admin_doc', chunk_index: 0, document_id: 'doc-1',
      image_urls: [], text: '![邻接图](/api/v1/perception/images/doc-1/neighbor)' }, {
      chunk_role: 'admin_doc', chunk_index: 1, document_id: 'doc-1',
      image_urls: [],
      text: '说明 ![图](/api/v1/perception/images/doc-1/image-1) 另一张 ![图](/api/v1/perception/images/doc-1/image-2)',
    }],
  };

  assert.deepEqual(recommendationCardImages(item, detail), [
    '/api/v1/perception/images/doc-1/image-1',
    '/api/v1/perception/images/doc-1/image-2',
  ]);
});

test('resolves legacy image paths through the same image map used by the detail view', () => {
  const item = { image_urls: ['/images/old-name.jpg'] };
  const imageMap = new Map([
    ['images/old-name.jpg', '/api/v1/perception/images/doc-1/image-1'],
    ['old-name.jpg', '/api/v1/perception/images/doc-1/image-1'],
  ]);

  assert.deepEqual(recommendationCardImages(item, null, imageMap), [
    '/api/v1/perception/images/doc-1/image-1',
  ]);
});
