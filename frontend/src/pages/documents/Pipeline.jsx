const STEPS = [
  ['upload', '上传至 MinIO'],
  ['parse', 'Perception 文档解析'],
  ['draft', 'LLM 提取草稿'],
  ['confirm', '确认结果'],
  ['index', 'Retrieval 建立索引'],
];

const ORDER = { idle: -1, upload: 0, parse: 1, draft: 2, confirm: 3, index: 4, failed: -1 };

export function Pipeline({ stage, error, draftStatus }) {
  const steps = STEPS;
  const effectiveDraftStatus = draftStatus || (stage === 'parse' ? 'PENDING' : stage === 'failed' ? 'NOT_STARTED' : 'RUNNING');
  const displayStage = stage === 'confirm' && ['RUNNING', 'FAILED'].includes(effectiveDraftStatus)
    ? 'draft'
    : stage;
  const activeOrder = ORDER[displayStage] ?? -1;
  return <ol className="pipeline" aria-label="材料处理进度">
    {steps.map(([key, label], index) => {
      const done = activeOrder > index || (displayStage === 'index' && index === steps.length - 1);
      const active = displayStage === key;
      return <li className={`pipeline-step ${done ? 'done' : ''} ${active ? 'active' : ''}`} key={key}>
        <span className="step-dot">{done ? '✓' : String(index + 1).padStart(2, '0')}</span>
        <span>{label}</span>
      </li>;
    })}
    {error && <li className="pipeline-error">{error}</li>}
  </ol>;
}

export const stageLabel = (stage) => ({
  idle: '待处理', upload: '已上传', legacy: '需重新上传', parse: '解析中', confirm: '解析完成，待提取字段', index: '已建立索引', deleted: '已删除索引', failed: '处理失败',
}[stage] || '已上传');
