export function totalSyncCount(counts, field) {
  const knowledgeCounts = Object.entries(counts || {})
    .filter(([knowledgeType]) => !knowledgeType.startsWith('__'))
    .map(([, item]) => item?.[field]);

  if (!knowledgeCounts.length || knowledgeCounts.some((count) => !Number.isFinite(count))) return null;
  return knowledgeCounts.reduce((total, count) => total + count, 0);
}
