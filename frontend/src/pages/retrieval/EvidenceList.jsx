import { api } from '../../api.js';
import { AuthenticatedDownload } from '../documents/AuthenticatedDownload.jsx';

function sourceLabel(locator) {
  return String(locator || '未知来源').split('|', 1)[0];
}

function sourceDetail(locator) {
  return String(locator || '').split('|').slice(1).join(' · ');
}

export function EvidenceList({ result, query, identity }) {
  if (!result) return <div className="empty-evidence">输入问题后，这里将显示可追溯的知识证据。</div>;
  if (result.status === 'FAILED' || result.error) return <div className="empty-evidence" role="alert">
    <strong>检索请求失败</strong>
    <p>{typeof result.error === 'string' ? result.error : result.error?.message || '检索服务返回失败，请重试。'}</p>
    {result.error_code && <p>错误代码：{result.error_code}</p>}
    {(result.trace_id || result.request_id) && <p>追踪 ID：{result.trace_id || result.request_id}</p>}
  </div>;
  if (!result.records?.length && !result.evidences?.length) return <div className="empty-evidence">没有命中证据。请检查知识范围、索引状态或查询描述。</div>;

  const records = result.records?.length ? result.records : result.evidences.map((item) => ({
    record_id: item.mysql_id || item.document_id || item.record_id,
    mysql_id: item.mysql_id,
    document_id: item.document_id,
    title: sourceLabel(item.source_locator),
    summary: item.text,
    score: item.score,
    evidences: [item],
    documents: [],
  }));

  return (
    <div className="evidence-list">
      {records.map((record) => (
        <article className="evidence-card" key={record.record_id}>
          <div className="evidence-meta"><strong>{record.title}</strong><span>资料：{record.mysql_id || record.document_id || '—'}</span><span>最高得分 {record.score?.toFixed?.(3) ?? record.score}</span></div>
          <p>{record.summary}</p>
          <details className="evidence-detail"><summary>查看详情（{record.evidences?.length || 0} 个相关证据块）</summary>
            {record.documents?.length > 0 && <div className="evidence-downloads"><b>相关附件</b>{record.documents.map((document) => <AuthenticatedDownload key={`${document.document_id}-${document.file_url}`} url={document.file_url ? api.perceptionAssetUrl(document.file_url) : ''} filename={document.source_name} label={`下载 ${document.source_name}`} className="text-button" />)}</div>}
            {record.evidences?.map((item) => <div className="evidence-detail-block" key={item.record_id}><div className="evidence-meta"><span>来源：{sourceLabel(item.source_locator)}</span>{sourceDetail(item.source_locator) && <span>{sourceDetail(item.source_locator)}</span>}<span>块 #{item.chunk_index ?? '—'}</span></div><p>{item.text}</p><footer><span>{item.scope_id || 'public'}</span></footer></div>)}
          </details>
        </article>
      ))}
      {result.images?.length > 0 && (
        <section className="related-images">
          <h3>关联图片</h3>
          <p>以下图片与命中的文本证据位于同页或同一幻灯片范围，不是图片语义检索结果。</p>
          <div>
            {result.images.map((image) => (
              <figure key={image.image_id}>
                <img
                  src={api.perceptionAssetUrl(image.url)}
                  alt={image.caption || image.source_locator || '关联解析图片'}
                />
                {image.page_number != null && <figcaption>第 {image.page_number} 页</figcaption>}
              </figure>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
