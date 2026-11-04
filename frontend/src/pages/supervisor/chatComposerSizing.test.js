import test from 'node:test';
import assert from 'node:assert/strict';
import { getChatComposerSize } from './chatComposerSizing.js';

const baseMetrics = {
  lineHeight: 22,
  paddingTop: 13,
  paddingBottom: 13,
  borderTopWidth: 1,
  borderBottomWidth: 1,
};

test('single-line text keeps a single-line composer height', () => {
  assert.deepEqual(getChatComposerSize({ ...baseMetrics, scrollHeight: 48 }), {
    height: 50,
    minHeight: 50,
    maxHeight: 138,
    overflowY: 'hidden',
  });
});

test('multiline text expands until the five-line limit', () => {
  assert.deepEqual(getChatComposerSize({ ...baseMetrics, scrollHeight: 92 }), {
    height: 94,
    minHeight: 50,
    maxHeight: 138,
    overflowY: 'hidden',
  });
});

test('content beyond five lines is capped and scrolls inside the textarea', () => {
  assert.deepEqual(getChatComposerSize({ ...baseMetrics, scrollHeight: 180 }), {
    height: 138,
    minHeight: 50,
    maxHeight: 138,
    overflowY: 'auto',
  });
});
