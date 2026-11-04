export function removeBlankLines(text) {
  return String(text ?? '').split(/\r\n?|\n/).filter((line) => line.trim()).join('\n');
}

export function buildSavedChunksParseUpdate(parsed, chunks) {
  return {
    rawText: removeBlankLines(chunks.map((chunk) => String(chunk?.text || '')).join('\n')),
    options: { saved_chunks: chunks },
  };
}
