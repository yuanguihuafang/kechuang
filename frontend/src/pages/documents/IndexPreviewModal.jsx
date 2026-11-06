import { MineruInlinePreview } from './MineruInlinePreview.jsx';
import { currentDocumentTask } from './documentStatus.js';

function blockLocationLabel(block) {
  const metadata = block.metadata || {};
  const sourceLocator = String(block.source_locator || '');
  const range = sourceLocator.match(/(?:^|\|)(?:pages|slides):(\d+)-(\d+)/);
  if (range) return `第 ${range[1]}-${range[2]} 页`;
  const single = sourceLocator.match(/(?:^|\|)page:(\d+)/);
  if (single && metadata.page_start == null && metadata.slide_start == null) return `第 ${single[1]} 页`;
  if (metadata.page_start != null && metadata.page_end != null) {
    return metadata.page_start === metadata.page_end ? `第 ${metadata.page_start} 页` : `第 ${metadata.page_start}-${metadata.page_end} 页`;
  }
  if (metadata.slide_start != null && metadata.slide_end != null) {
    return metadata.slide_start === metadata.slide_end ? `第 ${metadata.slide_start} 页` : `第 ${metadata.slide_start}-${metadata.slide_end} 页`;
  }
  return `第 ${block.page_number ?? '—'} 页`;
}

export function IndexPreviewModal({ document, parsed, loading, busy, onClose, onConfirm }) {
  if (!document) return null;
  const taskUnavailable = !currentDocumentTask(document, [document.supervisor_task]);
  const blocks = parsed?.blocks || [];
  const images = parsed?.metadata?.images || [];
  return <div className="index-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="index-preview-modal" role="dialog" aria-modal="true" aria-labelledby="index-preview-title">
      <header className="index-preview-header"><div><p className="overline">PERCEPTION / PARSE PREVIEW</p><h2 id="index-preview-title">更新索引前确认</h2><p>{document.file_name} · {blocks.length} 个解析块 · {images.length} 张图片</p></div><button type="button" className="secondary" onClick={onClose}>关闭</button></header>
      {loading ? <div className="table-empty">正在读取当前文档的 Perception 解析结果…</div> : !parsed ? <div className="index-preview-body"><div className="table-empty">当前文档暂无可用解析结果，请先完成解析。</div></div> : <div className="index-preview-body">
        <div className="index-preview-meta"><span>文档 ID：{document.document_id || '—'}</span><span>解析状态：{parsed.status || 'SUCCESS'}</span><span>块数量：{blocks.length}</span></div>
        {parsed.raw_text && <details className="mineru-original" open><summary>解析原文</summary><MineruInlinePreview text={parsed.raw_text} blocks={blocks} pageTexts={parsed.page_texts} images={images} /></details>}
        <div className="index-preview-blocks">{blocks.map((block, index) => <article className="parse-block" key={block.block_id || index}><header><span>{block.block_type || 'text'}</span><span>{blockLocationLabel(block)}</span><span>#{index + 1}</span></header><div className="parse-block-text">{block.text || '（空块）'}</div></article>)}</div>
      </div>}
      <footer className="index-preview-footer"><span>{taskUnavailable ? '当前版本缺少可用的文件处理任务，请通过文档目录的“更新”重新上传。' : '确认后将先替换当前文档旧索引，再写入新的有效解析块。'}</span><button type="button" onClick={onConfirm} disabled={loading || !parsed || busy || taskUnavailable}>{busy ? '索引提交中…' : '确认并更新索引'}</button></footer>
    </section>
  </div>;
}
