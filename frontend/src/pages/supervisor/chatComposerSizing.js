export function getChatComposerSize({
  scrollHeight,
  lineHeight,
  paddingTop = 0,
  paddingBottom = 0,
  borderTopWidth = 0,
  borderBottomWidth = 0,
}, maxLines = 5) {
  const verticalChrome = paddingTop + paddingBottom + borderTopWidth + borderBottomWidth;
  const minHeight = lineHeight + verticalChrome;
  const maxHeight = lineHeight * maxLines + verticalChrome;
  const contentHeight = scrollHeight + borderTopWidth + borderBottomWidth;
  const height = Math.min(Math.max(contentHeight, minHeight), maxHeight);

  return {
    height,
    minHeight,
    maxHeight,
    overflowY: contentHeight > maxHeight ? 'auto' : 'hidden',
  };
}
