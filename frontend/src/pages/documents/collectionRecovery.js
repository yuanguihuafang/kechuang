import { useEffect, useRef, useState } from 'react';

export function collectionRecoveryView(task, mysqlTask) {
  const status = task?.status;
  const running = ['QUEUED', 'RUNNING', 'SWITCHING'].includes(status);
  const legacyRecovery = status === 'WAITING_RECOVERY';
  const showRestore = status === 'PREFLIGHT_COMPLETE' && Boolean(task?.task_id);
  const showActivate = status === 'READY_TO_SWITCH' && Boolean(task?.task_id);
  const labels = {
    PREFLIGHT_COMPLETE: '可恢复来源检查完成（未修改活动 Collection）',
    WAITING_RECOVERY: '检测到旧版恢复任务；为保护活动库，已禁用继续恢复',
    QUEUED: '影子 Collection 恢复排队中', RUNNING: '影子 Collection 恢复中', READY_TO_SWITCH: '影子 Collection 已校验，等待管理员确认切换', SWITCHING: '正在切换活动 Alias', FAILED: '恢复失败',
    INTERRUPTED: '恢复已中断', SUCCESS: '索引恢复完成',
  };
  let message = task ? labels[status] || status : '';
  if (task?.stage) message += ` · ${task.stage}`;
  if (task?.stage === 'published_content') message += ` · 草稿与原文 ${task.source_processed ?? task.processed ?? 0} / ${task.source_total ?? task.total ?? 0} 份`;
  if (task?.stage === 'mysql') message += ` · MySQL 已处理至 ID ${task.last_id ?? 0}，写入 ${task.indexed_count ?? 0} 个分块`;
  return {
    showRestore,
    showActivate,
    running,
    blockWrites: running || legacyRecovery,
    message,
    report: task?.recovery_result || null,
  };
}

export function groupSkippedSources(sources = []) {
  const groups = new Map();
  for (const source of sources) {
    const documentId = source.document_id || '';
    const key = documentId ? `document:${documentId}` : `source:${source.source_type}:${source.source_id}`;
    let group = groups.get(key);
    if (!group) {
      group = { key, document_id: documentId, attachment_name: source.attachment_name || '', related_sources: [], issues: [] };
      groups.set(key, group);
    }
    group.issues.push(source);
    for (const related of source.related_sources || []) {
      const relatedKey = `${related.draft_id || ''}:${related.document_id || ''}`;
      if (!group.related_sources.some((item) => `${item.draft_id || ''}:${item.document_id || ''}` === relatedKey)) {
        group.related_sources.push(related);
      }
    }
    if (!group.attachment_name && source.attachment_name) group.attachment_name = source.attachment_name;
  }
  return [...groups.values()];
}

export async function loadLatestCollectionRecovery(client, knowledgeType) {
  let latest = null;
  // ponytail: 维护页面分页遍历后比较创建时间；任务量很大时再增加后端按类型查最新任务接口。
  for (let page = 1; ; page += 1) {
    const result = await client.adminTasks({ operation: 'milvus_recovery', page, page_size: 100 });
    for (const task of result.items) {
      if (task.knowledge_type !== knowledgeType) continue;
      if (!latest || Date.parse(task.created_at) > Date.parse(latest.created_at)) latest = task;
    }
    if (page >= result.total_pages) break;
  }
  return latest;
}

export function useCollectionRecovery(client, knowledgeType, enabled, onComplete) {
  const [state, setState] = useState({});
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const inFlight = useRef(false);
  const completedTask = useRef(null);
  const onCompleteRef = useRef(onComplete);
  onCompleteRef.current = onComplete;

  useEffect(() => {
    const current = ++generation.current;
    let timer;
    if (!enabled || !knowledgeType) return;
    setState({ knowledgeType, loading: true });
    async function read(taskId) {
      try {
        const task = taskId ? await client.adminTask(taskId) : await loadLatestCollectionRecovery(client, knowledgeType);
        const mysqlTaskId = task?.compensation?.mysql_task_id;
        const mysqlTask = mysqlTaskId ? await client.adminMysqlSyncStatus(mysqlTaskId) : null;
        if (generation.current !== current) return;
        setState({ knowledgeType, task, mysqlTask, loading: false });
        if (collectionRecoveryView(task).running) timer = setTimeout(() => read(task.task_id), 1500);
        else if (task?.status === 'SUCCESS' && completedTask.current !== task.task_id) {
          completedTask.current = task.task_id;
          onCompleteRef.current?.({ refreshCounts: true });
        }
      } catch (error) {
        if (generation.current === current) setState((previous) => ({ ...previous, loading: false, readError: error.message }));
      }
    }
    read();
    return () => { ++generation.current; clearTimeout(timer); };
  }, [client, knowledgeType, enabled, revision]);

  const visible = state.knowledgeType === knowledgeType ? state : { loading: true };
  const view = collectionRecoveryView(visible.task, visible.mysqlTask);
  async function submit() {
    if (inFlight.current || visible.loading || visible.readError || view.running) return;
    if (view.blockWrites) return;
    const confirmation = `检查 ${knowledgeType} Collection 的可恢复来源？这会读取已发布草稿、附件来源和 MySQL 数量，并保存预检任务报告；不会重建或修改 Collection，也不会更改业务数据。`;
    if (!window.confirm(confirmation)) return;
    inFlight.current = true;
    const current = generation.current;
    setState((previous) => ({ ...previous, pending: 'preflight', error: null }));
    try {
      const task = await client.adminRebuildCollection(knowledgeType);
      if (generation.current !== current) return;
      setState({ knowledgeType, task, loading: false });
      setRevision((value) => value + 1);
      onCompleteRef.current?.({ refreshCounts: true });
    } catch (error) {
      if (generation.current === current) {
        setState((previous) => ({ ...previous, pending: null, readError: error.message }));
      }
    } finally { inFlight.current = false; }
  }
  async function restore() {
    if (inFlight.current || !view.showRestore || !visible.task || !view.report) return;
    const fingerprint = view.report.missing_fingerprint;
    const skipped = view.report.skipped_count || 0;
    const confirmation = skipped
      ? `将按预检报告恢复 ${knowledgeType} 知识库。当前有 ${skipped} 项来源无法恢复；确认接受这份缺失清单（指纹：${fingerprint}）并继续？恢复只写影子 Collection，验证通过后等待管理员再次确认切换。`
      : `将按预检报告恢复 ${knowledgeType} 知识库。恢复只写影子 Collection；验证通过后等待管理员再次确认切换，旧 Collection 会保留。继续？`;
    if (!window.confirm(confirmation)) return;
    inFlight.current = true;
    const current = generation.current;
    setState((previous) => ({ ...previous, pending: 'restore', readError: null }));
    try {
      const task = await client.adminRestoreCollection(knowledgeType, {
        confirm: true,
        preflight_id: visible.task.task_id,
        accepted_missing_fingerprint: fingerprint,
      });
      if (generation.current !== current) return;
      setState({ knowledgeType, task: { ...task, status: 'QUEUED', stage: 'queued', recovery_result: view.report }, loading: false });
      setRevision((value) => value + 1);
    } catch (error) {
      if (generation.current === current) {
        setState((previous) => ({ ...previous, pending: null, readError: error.message }));
      }
    } finally { inFlight.current = false; }
  }
  async function activate() {
    if (inFlight.current || !view.showActivate || !visible.task) return;
    if (!window.confirm(`确认将 ${knowledgeType} 知识库的活动 Alias 切换到已验证的影子 Collection？切换前会再次核对来源、旧 Alias 和影子库状态。`)) return;
    inFlight.current = true;
    const current = generation.current;
    setState((previous) => ({ ...previous, pending: 'activate', readError: null }));
    try {
      const task = await client.adminActivateCollection(knowledgeType, { confirm: true, task_id: visible.task.task_id });
      if (generation.current !== current) return;
      setState({ knowledgeType, task: { ...visible.task, ...task }, loading: false });
      setRevision((value) => value + 1);
    } catch (error) {
      if (generation.current === current) setState((previous) => ({ ...previous, pending: null, readError: error.message }));
    } finally { inFlight.current = false; }
  }
  return { ...visible, ...view, rebuild: submit, restore, activate, refresh: () => setRevision((value) => value + 1) };
}
