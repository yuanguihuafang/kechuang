import test from 'node:test';
import assert from 'node:assert/strict';
import { copyDraftText, formatDraftForClipboard } from './draftClipboard.js';

test('formats the title, summary, and visible draft fields as readable text', () => {
  const text = formatDraftForClipboard({
    draft_type: 'demand',
    title: '采购需求草稿',
    requirement_summary: '寻找自动化检测方案',
    fields: { category: '技术需求', contect: '需要在线检测设备', remark: '尽快联系', unused: '' },
  }, [['category', '技术需求'], ['contect', '需要在线检测设备'], ['remark', '尽快联系']]);

  assert.equal(text, '采购需求草稿\n\n寻找自动化检测方案\n\n需求类型：技术需求\n需求描述：需要在线检测设备\n备注：尽快联系');
});

test('uses the Clipboard API when the browser provides it', async () => {
  const copied = [];
  await copyDraftText('完整草稿', { clipboard: { writeText: async (text) => copied.push(text) } });
  assert.deepEqual(copied, ['完整草稿']);
});

test('falls back to document copy for browsers without the Clipboard API', async () => {
  let value = '';
  let removed = false;
  const textarea = {
    style: {},
    setAttribute() {},
    select() {},
    remove() { removed = true; },
  };
  const documentRef = {
    body: { appendChild() {} },
    createElement: () => textarea,
    execCommand: (command) => command === 'copy' && value === '完整草稿',
  };
  Object.defineProperty(textarea, 'value', { set(next) { value = next; } });

  await copyDraftText('完整草稿', { clipboard: undefined, documentRef });
  assert.equal(removed, true);
});
