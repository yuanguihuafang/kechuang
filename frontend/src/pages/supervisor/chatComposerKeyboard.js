export function submitChatOnEnter(event) {
  if (
    event.key !== 'Enter' ||
    event.shiftKey ||
    event.isComposing ||
    event.nativeEvent?.isComposing ||
    event.keyCode === 229
  ) return;

  event.preventDefault();
  event.currentTarget.form?.requestSubmit();
}
