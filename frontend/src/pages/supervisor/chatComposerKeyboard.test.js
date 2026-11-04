import test from 'node:test';
import assert from 'node:assert/strict';
import { submitChatOnEnter } from './chatComposerKeyboard.js';

function keyboardEvent(overrides = {}) {
  const calls = { prevented: false, submitted: false };
  return {
    calls,
    event: {
      key: 'Enter',
      shiftKey: false,
      isComposing: false,
      keyCode: 13,
      preventDefault: () => { calls.prevented = true; },
      currentTarget: { form: { requestSubmit: () => { calls.submitted = true; } } },
      ...overrides,
    },
  };
}

test('Enter submits the chat form', () => {
  const { event, calls } = keyboardEvent();

  submitChatOnEnter(event);

  assert.deepEqual(calls, { prevented: true, submitted: true });
});

test('Shift+Enter keeps the textarea newline', () => {
  const { event, calls } = keyboardEvent({ shiftKey: true });

  submitChatOnEnter(event);

  assert.deepEqual(calls, { prevented: false, submitted: false });
});

test('Enter during IME composition does not submit', () => {
  for (const overrides of [{ isComposing: true }, { keyCode: 229 }]) {
    const { event, calls } = keyboardEvent(overrides);

    submitChatOnEnter(event);

    assert.deepEqual(calls, { prevented: false, submitted: false });
  }
});
