import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

test('document panel renders one current card and old purge cannot mark the catalog deleted', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'http:', hostname: 'localhost' } };
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { VersionPanel } = await server.ssrLoadModule('/src/pages/documents/DocumentPanels.jsx');
    const { FileTable } = await server.ssrLoadModule('/src/pages/admin/FileTable.jsx');
    const { DocumentIntakeWorkspace } = await server.ssrLoadModule('/src/pages/documents/DocumentIntakeWorkspace.jsx');
    const { IndexPreviewModal } = await server.ssrLoadModule('/src/pages/documents/IndexPreviewModal.jsx');
    const { api } = await server.ssrLoadModule('/src/api.js');
    assert.match(api.adminDocumentFileUrl('doc with space'), /\/api\/v1\/admin\/documents\/doc%20with%20space\/file$/);
    const current = { document_id: 'doc', version_id: 'doc:v2', status: 'INDEXED', publication_status: 'PUBLISHED', publication_label: '可检索', indexed_count: 14 };
    const old = { ...current, version_id: 'doc:v1', status: 'PURGED', publication_status: 'UNPUBLISHED', publication_label: '已删除', indexed_count: 39 };
    const document = { document_id: 'doc', active_version_id: current.version_id, active_version: current, file_name: '先德.pdf' };
    const panel = renderToStaticMarkup(createElement(VersionPanel, { document, versions: [old, current], identity: {} }));
    assert.equal((panel.match(/class="version-card"/g) || []).length, 1);
    assert.match(panel, /分块：14/);
    assert.doesNotMatch(panel, /分块：39|重新上架|移入回收站/);
    const recycled = renderToStaticMarkup(createElement(VersionPanel, {
      document: { ...document, active_version: { ...current, status: 'RECYCLED' } },
      versions: [{ ...current, status: 'RECYCLED' }],
    }));
    assert.doesNotMatch(recycled, /下载原文件/);

    const table = renderToStaticMarkup(createElement(FileTable, { files: [{ ...document, ...current, storage_key: 'uploads/current.pdf', supervisor_task: { document_id: 'doc', version_id: old.version_id, status: 'SUCCEEDED', lifecycle_action: 'purge', stage: 'completed' } }] }));
    assert.match(table, /已建立索引/);
    assert.doesNotMatch(table, /已删除|completed/);

    const deleted = renderToStaticMarkup(createElement(VersionPanel, { document: { ...document, active_version_id: old.version_id, active_version: old }, versions: [old, current], identity: {} }));
    assert.match(deleted, /已删除/);
    assert.doesNotMatch(deleted, /<button|下载原文件/);

    const intakeProps = { knowledgeTypes: [], scopeOptions: [], generatedChunks: [], previewBlocks: [] };
    for (const [readState, message] of [[{ loading: true }, '正在读取解析结果'], [{ error: 'MinIO 读取失败' }, 'MinIO 读取失败']]) {
      const preview = renderToStaticMarkup(createElement(DocumentIntakeWorkspace, { ...intakeProps, parseReadState: readState }));
      assert.ok(preview.includes(message));
      assert.doesNotMatch(preview, /选择并上传文件后/);
    }

    const restored = { ...document, ...current, storage_key: 'uploads/current.pdf', draft_status: 'READY' };
    for (const supervisor_task of [null, { ...restored, task_id: 'old', version_id: old.version_id }, { ...restored, task_id: 'purge', operation: 'document_lifecycle', task_type: 'document_lifecycle' }]) {
      const preview = renderToStaticMarkup(createElement(DocumentIntakeWorkspace, {
        ...intakeProps, current: { ...restored, supervisor_task }, selected: restored,
        parsed: { raw_text: '保留的原文', blocks: [] }, draftPreviews: [{ title: '草稿' }],
      }));
      assert.match(preview, /缺少.*任务/);
      assert.match(preview, /更新|重新上传/);
      for (const label of ['解析已选文件', '字段提取', '发布']) {
        assert.match(preview, new RegExp(`<button[^>]*disabled=""[^>]*>${label}</button>`));
      }
      const modal = renderToStaticMarkup(createElement(IndexPreviewModal, { document: { ...restored, supervisor_task }, parsed: { raw_text: '原文' } }));
      assert.match(modal, /<button[^>]*disabled=""[^>]*>确认并更新索引<\/button>/);
      assert.match(modal, /缺少.*任务/);
    }
    const freshUpload = { document_id: 'new', version_id: 'new:v1', storage_key: 'uploads/new.pdf', task_id: 'new-task', supervisor_task_id: 'new-task', operation: 'file_ingestion', task_type: 'document_ingestion' };
    for (const current of [{ ...freshUpload, supervisor_task: freshUpload }]) {
      const preview = renderToStaticMarkup(createElement(DocumentIntakeWorkspace, { ...intakeProps, current, uploaded: current }));
      assert.match(preview, /<button[^>]*class="secondary">开始解析<\/button>/);
      assert.doesNotMatch(preview, /缺少.*任务/);
    }
    const staleCache = renderToStaticMarkup(createElement(DocumentIntakeWorkspace, { ...intakeProps, current: freshUpload, selected: freshUpload }));
    assert.match(staleCache, /<button[^>]*disabled=""[^>]*>解析已选文件<\/button>/);
    const readOnlyDocument = renderToStaticMarkup(createElement(DocumentIntakeWorkspace, {
      ...intakeProps, current: { ...restored, knowledge_type: 'solution' }, parsed: { raw_text: '已保存正文' },
    }));
    assert.match(readOnlyDocument, /<button type="button" title="根据已保存的解析原文分块">分块<\/button>/);
  } finally {
    await server.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});
