# Implementation Plan: Goal-Aware Retrieval Relevance and Web Fallback

## Goal

Across all retrieval flows, show only candidates that an LLM judges useful for the user's complete goal. Keep related results for broad/category requests; require a concrete match for specific entity/fact requests. If none remain, search the public web without asking again unless the user explicitly restricted the source to internal knowledge. Never turn web results into internal match cards.

## Architecture Decisions

- Keep goal interpretation in the existing agent instructions and tool schema; do not add a separate intent-classification model call.
- Batch-judge candidates once per retrieval result set before constructing recommendation cards; no per-candidate LLM calls.
- Apply the same candidate policy to `kc_retrieval`, `kc_profile`, `kc_match`, and next-batch results through the shared result path.
- Reuse the existing Tavily adapter for public fallback. The agent decides from the full user goal and explicit source constraints; draft/attachment origin alone does not prohibit fallback. Build a focused query from the relevant topic, without forwarding full candidate or document bodies.
- Return web fallback as sourced text, not as internal retrieval or supply-demand match cards. Preserve identity, permissions, source scope, and existing response contract.
- No frontend changes, server deployment, service restart, or commit.

## Task List

### Task 1: Candidate judgment and filtering

**Acceptance criteria:**
- Broad queries retain relevant adjacent candidates that are useful as references.
- Specific entity/fact queries drop candidates that are merely keyword-similar.
- Results are limited to actual relevant candidates (0–3); no top-3 padding.
- Judge/provider errors do not silently pass irrelevant candidates as verified.

**Verification:** Add focused tests for broad/specific decisions, empty candidates, malformed judge output, and provider failure; run the new tests plus relevant adapter tests.

**Dependencies:** None.

**Files touched:** shared helper `backend/assistant/application/retrieval_relevance.py`, the three MCP search entry points, response adapter, focused backend tests.

**Estimated scope:** Medium.

### Task 2: Automatic public-web fallback

**Acceptance criteria:**
- When filtering leaves no relevant candidate, call the existing web-search adapter without asking the user.
- Explicit internal-only requests and disallowed identities never fall back to web.
- Draft or attachment origin alone does not disable fallback; the query may use its relevant topic, but does not forward full candidate/document bodies.
- Web results are returned with source URLs as text; no recommendation or match cards are synthesized from them.
- Web failures are stated plainly and do not restore rejected KB cards.

**Verification:** Tests cover fallback, explicit internal-only scope, safe query construction, zero web-derived cards, and external-service failure.

**Dependencies:** Task 1.

**Files touched:** the shared relevance helper and DeerFlow response adapter; reuse existing Tavily adapter and focused backend tests.

**Estimated scope:** Medium.

### Task 3: Agent policy, regression coverage, and documentation

**Acceptance criteria:**
- Agent instructions apply the broad-vs-specific policy to every retrieval flow, not only enterprise visit examples.
- Quick entries remain strong intent hints only when aligned with the user's complete goal.
- Tool descriptions tell the agent to judge fallback from the complete goal and explicit source constraints, without a confirmation question; draft/attachment origin alone is not a prohibition.
- Existing source, card, match, and next-batch behavior remains intact for relevant results.

**Verification:** Run the targeted routing, MCP, adapter, and response tests only; run `git diff --check` and inspect touched paths against the existing dirty worktree.

**Dependencies:** Tasks 1 and 2.

**Files touched:** `OPIEos/agents/kechuang/SOUL.md`, the four retrieval Skills, MCP schemas/descriptions, backend tests, and this spec.

**Estimated scope:** Medium.

## Checkpoint

Before completion, confirm all three retrieval entry points (general retrieval, profile search, supply-demand matching) and next-batch behavior share the same relevance policy, permissions remain unchanged, and web fallback never appears as internal match cards.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Extra LLM judgment adds latency and usage per candidate batch | Medium | Judge all candidates in one call; skip call for empty results. |
| Web query could expose unnecessary internal detail | Medium | Send a focused topic query rather than full candidate or document bodies; respect explicit internal-only requests and permissions. |
| LLM may over-filter broad queries or retain weak exact-entity candidates | Medium | Regression cases assert different broad/specific behavior and exact result/card counts. |
| Existing worktree is heavily modified | High | Preserve all unrelated edits; touch only task-specific lines and run focused tests. |

## Execution

User selected inline execution. Implemented the shared batch judge/fallback for `kc_retrieval`, `kc_match`, and `kc_profile`; broad/exact behavior is in the LLM adjudication prompt, web fallback bypasses the judge only for an empty candidate set, and only focused query text reaches Tavily. Stale KB cards are cleared and web results remain non-card response data. Next-batch searches reuse the original goal and saved fallback judgment. Agent SOUL/Skills pass the full goal; explicit internal-only requests and permissions prohibit fallback, while draft/attachment origin alone does not. Focused verification: `pytest backend/tests/test_chat_stream_terminal_state.py backend/tests/test_next_match_batch.py backend/tests/test_retrieval_relevance.py backend/tests/test_mcp_server.py backend/tests/test_deerflow_adapter.py -q` (184 passed, 8 skipped). No commit, deployment, service restart, or full suite.
