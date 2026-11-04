export function matchDraftActionPayload(action, currentDraft = {}) {
  const actionContext =
    action?.draft_context && typeof action.draft_context === "object"
      ? action.draft_context
      : {};
  const draftType = action?.draft_type || "demand";

  return {
    intent_hint: draftType,
    intent_confirmation: "confirmed",
    draft_context: {
      ...actionContext,
      ...(currentDraft && typeof currentDraft === "object" ? currentDraft : {}),
      text: String(
        action?.query || actionContext.text || currentDraft?.text || "",
      ).trim(),
      draft_type: draftType,
      match_follow_up: true,
    },
  };
}
