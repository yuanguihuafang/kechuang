export function publicationStatusLabel(version = {}) {
  return String(version.publication_label || '').trim() || '未发布';
}
