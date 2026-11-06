export function publicationStatusLabel(version = {}) {
  return String(version.publication_label || '').trim() || '未发布';
}

export function currentDocumentVersion(document = {}, versions = []) {
  const activeId = document.active_version_id || document.active_version?.version_id || document.version_id;
  if (activeId) {
    return versions.find((version) => version.version_id === activeId)
      || (document.active_version?.version_id === activeId ? document.active_version : null)
      || (document.version_id === activeId ? document : null);
  }
  return null;
}

export function currentDocumentTask(document, tasks = []) {
  if (!document?.document_id) return null;
  const version = currentDocumentVersion(document);
  return tasks.find((task) => task?.document_id === document.document_id
    && task.task_id && task.operation === 'file_ingestion'
    && (!task.task_type || task.task_type === 'document_ingestion')
    && !task.lifecycle_action
    && version?.version_id && task.version_id === version.version_id
    && version.storage_key && task.storage_key === version.storage_key) || null;
}

export function catalogStage(document, task) {
  const status = currentDocumentVersion(document)?.status || document.status;
  if (['RECYCLED', 'PURGED'].includes(status)) return 'deleted';
  if (currentDocumentTask(document, [task]) && task.status !== 'SUCCEEDED' && task.stage) return task.stage;
  if (status === 'FAILED') return 'failed';
  if (status === 'PARSING') return 'parse';
  if (['WAITING_REVIEW', 'READY_TO_PUBLISH'].includes(status)) return 'confirm';
  if (['INDEXING', 'INDEXED', 'PUBLISHED', 'UNPUBLISHED'].includes(status)) return 'index';
  return 'upload';
}

export function documentProgressLabel(document) {
  const version = currentDocumentVersion(document) || document;
  const task = currentDocumentTask(document, [document.supervisor_task]);
  if (!['RECYCLED', 'PURGED'].includes(version.status) && task && task.status !== 'SUCCEEDED') {
    return ({
      UPLOADED: '已上传', QUEUED: '排队中', RUNNING: '处理中', PARSING: '解析中', INDEXING: '索引中',
      WAITING_CONFIRM: '解析完成，待确认', WAITING_DRAFT_CONFIRM: '草稿待确认',
      WAITING_INTENT_CONFIRM: '意图待确认', MANUAL_REVIEW: '待人工接管', RECOVERING: '恢复中', FAILED: '任务失败',
    })[task.status] || task.status;
  }
  return ({
    UPLOADED: '已上传', PARSING: '解析中', WAITING_REVIEW: '解析完成，待确认', READY_TO_PUBLISH: '待发布',
    INDEXING: '索引中', INDEXED: '已建立索引', PUBLISHED: '已建立索引', UNPUBLISHED: '已下架',
    RECYCLED: '已移入回收站', PURGED: '已删除', FAILED: '处理失败', EXPIRED: '已过期',
  })[version.status] || '未发布';
}
