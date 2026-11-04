# DeerFlow Stream Gap Recovery

## Problem

OPIEos Gateway can emit an SSE `gap` event when a subscriber falls behind its retained event buffer, then close that subscription. The business adapter currently ignores `gap`; when the stream ends without `end`, it reports a generic failure even if the run later completes successfully.

## Decision

The Kechuang HTTP runner will resume the **same run** through Gateway's supported `GET /api/threads/{thread_id}/runs/{run_id}/join` endpoint, setting `Last-Event-ID` to `gap.data.latest_available_event_id`. It will continue until the normal terminal `end` event. It must not create another run or repeat MCP side effects. A gap without a usable run ID or cursor is an explicit integration error, never a fabricated success.

This is an integration-layer change in `D:\Work\kechuang\backend`; do not modify DeerFlow/OPIEos core or change the public Kechuang SSE contract.

## Verification

- Regression test proves an initial `gap` resumes the same run using `Last-Event-ID`, then forwards its final event.
- `py311` targeted tests: 66 passed across Gateway runner, DeerFlow adapter/material flow, chat material session, and completion persistence.
- Ruff passed for the changed runner and Gateway runner tests.
- Live replay against the running local Gateway remains unverified; services were not restarted or interrupted.
