export function recommendationCardImages(item, detail = null, imageMap = new Map()) {
  const documentId = item?.document_id || item?.source?.document_id;
  const detailChunks = [
    ...(Array.isArray(detail?.draft_chunks) ? detail.draft_chunks : []),
    ...(Array.isArray(detail?.source_chunks) ? detail.source_chunks : []),
  ];
  const matchedDetailChunks = Number.isInteger(item?.chunk_index)
    ? detailChunks.filter((chunk) => (
      Number(chunk?.chunk_index) === item.chunk_index
      && (!item.chunk_role || chunk?.chunk_role === item.chunk_role)
      && (!documentId || !chunk?.document_id || chunk.document_id === documentId)
    ))
    : [];
  const detailText = matchedDetailChunks.map((chunk) => chunk?.text).filter(Boolean).join('\n');
  const imageUrls = [
    ...(Array.isArray(item?.image_urls) ? item.image_urls : []),
    ...matchedDetailChunks.flatMap((chunk) => chunk?.image_urls || []),
    ...[...detailText.matchAll(/!\[[^\]]*\]\((\/api\/v1\/perception\/images\/[^)\s]+)\)/gi)].map((match) => match[1]),
  ]
    .map((url) => {
      const value = String(url || '').trim();
      if (!/(?:^|\/)(?:auto\/)?images\//i.test(value) || /\/api\/v1\/perception\/images\//i.test(value)) return value;
      const path = value.replaceAll('\\', '/').replace(/^\/+/, '').split(/[?#]/, 1)[0];
      return imageMap.get(value)
        || imageMap.get(path)
        || imageMap.get(path.split('/').pop())
        || '';
    })
    .filter(Boolean);

  return [...new Set(imageUrls)];
}

export function recommendationCardSummary(value) {
  return String(value || '')
    .replace(/<img\b[^>]*>/gi, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/!\[[^\]]*\]\([^\r\n]*$/gm, ' ')
    .replace(/!\[[^\]]*$/gm, ' ')
    .replace(/(?:https?:\/\/[^/\s<>"']+)?\/+api\/v1\/perception\/images\/[^\s<>"')]+/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}
