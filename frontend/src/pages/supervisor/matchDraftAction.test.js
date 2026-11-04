import test from "node:test";
import assert from "node:assert/strict";

import { matchDraftActionPayload } from "./matchDraftAction.js";

test("immediate supplement keeps the match query and the selected draft type", () => {
  const payload = matchDraftActionPayload(
    {
      draft_type: "demand",
      query: "智能客服系统研发",
      draft_context: { match_direction: "demand_to_supply" },
    },
    { fields: { title: "已有标题" } },
  );

  assert.equal(payload.intent_hint, "demand");
  assert.equal(payload.intent_confirmation, "confirmed");
  assert.equal(payload.draft_context.text, "智能客服系统研发");
  assert.equal(payload.draft_context.match_direction, "demand_to_supply");
  assert.equal(payload.draft_context.fields.title, "已有标题");
  assert.equal(payload.draft_context.match_follow_up, true);
});
