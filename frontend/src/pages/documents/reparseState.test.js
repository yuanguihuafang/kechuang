import test from 'node:test';
import assert from 'node:assert/strict';

import { reparseResetState } from './reparseState.js';

test('starting a reparse clears visible prior parse and extraction results', () => {
  assert.deepEqual(reparseResetState(), {
    parsed_result: null,
    parsed_blocks: [],
    parsed_raw_text: '',
    parsed_metadata: {},
    parsed_edited: false,
    saved_chunks: [],
    draft_status: 'NOT_STARTED',
    draft_id: null,
    draft_ids: [],
    draft_preview: null,
    draft_previews: [],
    draft_error_code: null,
    draft_error: null,
    result_status: null,
  });
});
