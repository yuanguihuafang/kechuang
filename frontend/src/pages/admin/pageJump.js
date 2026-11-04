export function normalizePageJump(value, totalPages) {
  const maxPage = Math.trunc(Number(totalPages));
  if (String(value).trim() === '') return null;
  const requestedPage = Number(value);
  if (!Number.isFinite(maxPage) || maxPage < 1 || !Number.isFinite(requestedPage)) return null;
  return Math.min(maxPage, Math.max(1, Math.trunc(requestedPage)));
}
