import assert from 'node:assert/strict';
import test from 'node:test';
import { buildSavedChunksParseUpdate, removeBlankLines } from './parseResultSave.js';

test('removes empty and whitespace-only lines without changing content lines', () => {
  assert.equal(removeBlankLines('第一行\r\n\r\n  \r\n 第二行 \n\n第三行'), '第一行\n 第二行 \n第三行');
  assert.equal(removeBlankLines(' \n\t\r\n'), '');
});

test('saving edited chunks writes their joined content as the parsed text', () => {
  const chunks = [{ text: '编辑后的第一个分块' }, { text: '编辑后的第二个分块' }];

  assert.deepEqual(
    buildSavedChunksParseUpdate({ raw_text: '管理员修改后的解析原文' }, chunks),
    {
      rawText: '编辑后的第一个分块\n编辑后的第二个分块',
      options: { saved_chunks: chunks },
    },
  );
});
