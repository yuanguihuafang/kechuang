# Document Multi-Draft Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When one uploaded document contains multiple independent business items, create one draft per item, preserve their shared source-document association, and make editing, publishing, paging, and deletion safe for the entire lifecycle.

**Architecture:** Keep field extraction in Perception, persist all extracted drafts through one atomic repository batch operation, and retain the existing per-draft `draft_id`/`mysql_id` identity with a shared batch `source_id`. Session card responses remain newest-first and item responses retain source order; deletion first resolves and validates the complete source group, then removes its business rows, vectors, and only unshared attachment objects while preserving legacy records without a batch identifier.

**Tech Stack:** Python 3.11, FastAPI application services, SQLAlchemy/PostgreSQL, existing Milvus service, pytest.

**Spec:** `docs/方案/草稿确认后直接创建卡片.md` — sections “草稿与附件的 Milvus 关联标识” and “同一会话的多草稿与附件索引”.

## Global Constraints

- Do not rebuild or migrate existing Milvus indexes; existing records without a batch `source_id` keep the legacy single-draft/document lifecycle.
- A source batch has one stable `source_id`; each draft has its own `draft_id` and, after publication, `mysql_id`.
- Draft vectors are identified by `mysql_id + source_id + chunk_role`; source-document vectors by `document_id + source_id + chunk_role`; MySQL sync vectors remain `mysql_id + chunk_role=mysql` with empty `source_id`.
- Updating one draft replaces only that draft’s draft-role vectors and preserves all shared source-document vectors and sibling drafts.
- Deleting any draft in a batch targets every draft and published business record sharing that batch `source_id`; attachment/document storage is removed only when no outside reference exists.
- Extraction order is preserved in `draft_ids` and `draft_previews`; the first extracted item is the active item for that response, while session paging remains newest-first.
- Do not commit changes. Preserve unrelated pre-existing worktree changes.
- Run Python tests with `D:\Anaconda\envs\py311\python.exe` from `backend`.

## Review Focus

- A document with multiple items of the same `draft_type` must create distinct drafts and never update/reuse an existing thread draft; test in Tasks 1–2.
- Shared document-wide fields/attachments must not overwrite item-specific fields, and ambiguous item ownership must not be guessed; test in Task 1.
- A failed database insert in the middle of a batch must leave no partial draft rows; test in Task 2.
- Updating or publishing one sibling draft must not replace another sibling’s Milvus chunks or shared document chunks; test in Task 3.
- Deleting a batch with a published sibling or an attachment referenced outside the batch must preflight the complete target set, avoid an incorrect success response, and preserve externally referenced storage; test in Task 4.

## File Map

- Modify `backend/agents/perception_agent/application/field_extraction.py`: preserve item boundaries and apply only explicitly shared data to each extracted item.
- Modify `backend/runtime/workflows/knowledge_document_draft.py`: assign/persist one stable batch `source_id`, call batch persistence once, and return ordered draft metadata.
- Modify `backend/services/persistence/repositories/workflow_repository.py`: provide atomic multi-row draft creation and source-group lookup without implicit same-thread reuse.
- Modify `backend/services/application/draft_cleanup.py`, `backend/services/application/draft_publishing.py`, and `backend/admin/service.py`: resolve, preflight, and clean a complete source group safely through the actual admin deletion entry point and document lifecycle cleanup.
- Modify `backend/services/application/drafts.py` only if its existing delete entry point must delegate to the shared group-cleanup service; preserve current ownership and status rules except where the spec explicitly requires whole-batch deletion.
- Modify existing tests in `backend/tests/test_field_extraction.py`, `backend/tests/test_draft_create_and_publish_flow.py`, `backend/tests/test_draft_cleanup_safety.py`, `backend/tests/test_document_lifecycle_safety.py`, `backend/tests/test_draft_publishing.py`, `backend/tests/test_deleted_draft_purge.py`, and add focused repository tests if there is no suitable existing repository test module.
- Modify `docs/方案/草稿确认后直接创建卡片.md` only to record final implementation details or deviations. Inspect the current card response contract before changing frontend code; the session pager already consumes `drafts` and orders newest-first.

---

### Task 1: Preserve independent extraction items and shared source metadata

**Files:**
- Modify: `backend/agents/perception_agent/application/field_extraction.py`
- Modify: `backend/runtime/workflows/knowledge_document_draft.py`
- Test: `backend/tests/test_field_extraction.py`
- Test: `backend/tests/test_draft_create_and_publish_flow.py`

**Interfaces:**
- Consumes: `PerceptionAgent.extract_fields(..., allow_multiple=True, draft_factory=...)` and the existing document task metadata (`document_id`, `version_id`, `storage_key`, `blocks`).
- Produces: `enriched["drafts"]` in source order, with item-specific fields intact and a stable shared `source_id` persisted on every draft independently of whether a document ID exists.

- [x] **Step 1: Add failing extraction tests**
  - Add `test_multiple_extracted_items_keep_item_specific_fields_and_order` with two same-type items and conflicting item titles/fields; assert two ordered drafts and no cross-item field overwrite.
  - Add `test_multiple_extracted_items_share_source_id_without_document_id` with an attachment-free/source-only task; assert all generated drafts have the same non-empty source ID.
  - Add `test_ambiguous_common_fields_are_not_copied_to_each_item` for a field that cannot be assigned to an item; assert it is not guessed into either item.
- [x] **Step 2: Run the focused tests and verify they fail**

Run from `backend`: `& 'D:\Anaconda\envs\py311\python.exe' -m pytest tests/test_field_extraction.py tests/test_draft_create_and_publish_flow.py -q`

Expected: the new assertions fail against current item merge/metadata behavior.
- [x] **Step 3: Implement the smallest extraction change**
  - Preserve each parsed item’s field map and source order; keep document-wide metadata separate from item-specific fields.
  - Derive the batch `source_id` once for the upload/source batch and copy it into each extracted draft before persistence. Do not derive it from `draft_id` or `mysql_id`; retain the existing stable source-generation convention where present.
- [x] **Step 4: Re-run the focused tests**

Expected: all new extraction tests and existing field-extraction tests pass.

### Task 2: Atomically persist a draft batch without thread-draft reuse

**Files:**
- Modify: `backend/services/persistence/repositories/workflow_repository.py`
- Modify: `backend/runtime/workflows/knowledge_document_draft.py`
- Test: `backend/tests/test_draft_create_and_publish_flow.py`
- Test: repository tests for `PostgresDraftStore` (create a focused test file only if no current module covers its SQL transaction behavior).

**Interfaces:**
- Consumes: ordered, source-tagged draft dictionaries from Task 1.
- Produces: `PostgresDraftStore.create_many(*, user_id: str, user_role: str, drafts: list[dict[str, Any]], thread_id: str | None = None) -> list[dict[str, Any]]`; it returns rows in input order and commits all inserts together or none.

- [x] **Step 1: Add failing persistence tests**
  - Replace `test_document_extraction_keeps_its_existing_thread_reuse_behavior` with `test_document_extraction_creates_distinct_drafts_without_thread_reuse`; assert one batch call for N items and N unique `draft_id`s.
  - Add a repository test asserting all inserts use one transaction and preserve input order in the returned records.
  - Add a failure-injection test asserting a database error during an insert rolls back every row in the batch.
- [x] **Step 2: Run focused tests and verify they fail**

Run: `& 'D:\Anaconda\envs\py311\python.exe' -m pytest tests/test_draft_create_and_publish_flow.py <repository-test-file>::test_create_many_is_atomic -q`

Expected: current storage API lacks batch creation and partial rollback coverage.
- [x] **Step 3: Implement `PostgresDraftStore.create_many`**
  - Prepare each row with a unique `draft_<uuid>` ID, normalized merged JSON, version 1, title, and first attachment’s `document_id` as `source_document_id`. Assign deterministic increasing `updated_at` values in reverse input order (microsecond offsets from one batch timestamp) so source item 0 sorts first in the newest-first pager.
  - Use one `_pooled_connection(self.dsn)` transaction for the complete insert set; do not call `create()` in a loop because it opens independent transactions and can reuse thread drafts.
  - Make `persist_drafts()` call `create_many()` once and retain `draft_ids`/`draft_previews` in source order; first item is `draft_id`/`draft_preview`.
- [x] **Step 4: Run focused persistence and workflow tests**

Expected: all rows appear on success; injected failure leaves no batch rows; existing single-draft creation remains unchanged.

### Task 3: Keep response paging, editing, publication, and vector identities isolated

**Files:**
- Modify as needed: `backend/services/application/draft_session.py`
- Modify as needed: `backend/services/application/draft_publishing.py`
- Modify as needed: `backend/services/vector/milvus/milvus_service.py`
- Test: `backend/tests/test_draft_session_sync.py`
- Test: `backend/tests/test_draft_publishing.py`
- Test: `backend/tests/test_draft_index_recovery.py`
- Test: `backend/tests/test_milvus_mysql_replace.py`

**Interfaces:**
- Consumes: batch-created drafts, each with its own `draft_id`, shared `source_id`, and common source-document association.
- Produces: session cards remain owner-scoped and newest-first; publishing/replacing one draft’s vectors is constrained to its `mysql_id + source_id + chunk_role`, leaving sibling and document-role chunks untouched.

- [x] **Step 1: Add failing isolation tests**
  - Add `test_session_cards_include_all_batch_drafts_newest_first` and assert all non-deleted owner-visible batch drafts are returned in descending update order.
  - Add a publication/update regression test with two drafts sharing `source_id` and distinct `mysql_id`s; assert replacing one draft deletes only its own role chunks and keeps the sibling plus document chunks.
  - Add a response contract assertion that ordered `draft_ids` remains source order even though the session pager is newest-first.
- [x] **Step 2: Run focused tests and verify the new assertions fail where the contract is missing**

Run: `& 'D:\Anaconda\envs\py311\python.exe' -m pytest tests/test_draft_session_sync.py tests/test_draft_publishing.py tests/test_draft_index_recovery.py tests/test_milvus_mysql_replace.py -q`
- [x] **Step 3: Implement only missing identity/paging behavior**
  - Keep the first extracted draft as the active draft in the current response/session state without mutating siblings; verify the repository timestamp ordering keeps that same draft at the top of the newest-first pager.
  - Persist/use the same `source_id` on each batch draft and source document chunk; scope draft vector replacement by the draft’s own `mysql_id` and draft role.
  - Do not modify frontend paging unless a failing response-contract test proves the existing `drafts` payload is insufficient.
- [x] **Step 4: Re-run focused lifecycle tests**

Expected: the two orderings remain intentional (source order in creation response, update-time order in pager); sibling vectors and shared document vectors remain intact.

### Task 4: Delete an entire source batch safely

**Files:**
- Modify: `backend/services/persistence/repositories/workflow_repository.py`
- Modify: `backend/services/application/draft_cleanup.py`
- Modify: `backend/services/application/draft_publishing.py`
- Modify: `backend/admin/service.py`
- Modify as needed: `backend/services/application/drafts.py`
- Test: `backend/tests/test_draft_cleanup_safety.py`
- Test: `backend/tests/test_document_lifecycle_safety.py`
- Test: `backend/tests/test_draft_application_rules.py`
- Test: `backend/tests/test_deleted_draft_purge.py`

**Interfaces:**
- Consumes: any draft ID in a source batch and the persisted `source_id` on that draft; batch lookup returns every active sibling in the same batch.
- Produces: preflighted cleanup target set for the whole batch; deletion removes each eligible published MySQL business record, all vectors for the batch source, and only unshared documents/storage objects; legacy no-source drafts retain current behavior.

- [x] **Step 1: Add failing batch deletion tests**
  - Add `test_delete_any_batch_draft_targets_all_siblings_and_source_vectors` to the admin delete contract with two draft IDs sharing one source; assert all batch records, siblings, and source vectors are removed.
  - Add `test_admin_batch_delete_purges_published_sibling_and_shared_source`; assert admin hard-delete removes published and unpublished siblings together.
  - Add `test_batch_delete_preserves_document_and_storage_with_outside_reference`; assert outside-batch references retain document vectors and object storage.
  - Add `test_legacy_draft_without_source_id_uses_existing_single_draft_cleanup`.
- [x] **Step 2: Run focused deletion tests and verify they fail**

Run: `& 'D:\Anaconda\envs\py311\python.exe' -m pytest tests/test_draft_cleanup_safety.py tests/test_document_lifecycle_safety.py tests/test_draft_application_rules.py tests/test_deleted_draft_purge.py -q`
- [x] **Step 3: Implement group resolution and complete preflight**
  - Add a store query for active drafts by exact persisted `source_id`; never group by thread, document filename, or draft type.
  - Resolve business-record references, document references, and storage-key references for the complete target set before the first destructive side effect.
  - For the privileged admin-console hard-delete path, publication status must not block deletion: delete all records and vectors for the exact source batch. Leave ordinary-user publish/unpublish authorization unchanged.
- [x] **Step 4: Implement idempotent batch cleanup**
  - Delete each eligible business record once, delete Milvus vectors once by shared `source_id` (which includes all draft/document roles for this batch), then delete only batch-owned unreferenced document/storage records and mark all target draft rows deleted.
  - Surface failures explicitly; do not return a success count if only part of cross-store cleanup completed. Keep the cleanup safe to retry after a reported partial external-store failure.
- [x] **Step 5: Re-run focused deletion tests**

Expected: all target siblings, including published members, are handled as one source group; outside-reference guards run before deletion; retries do not duplicate or mis-scope effects.

### Task 5: Documentation and regression verification

**Files:**
- Modify: `docs/方案/草稿确认后直接创建卡片.md`
- Test: focused workflow/repository/publishing/deletion test modules from Tasks 1–4.

**Interfaces:**
- Consumes: implemented batch creation and deletion contracts from Tasks 1–4.
- Produces: design/implementation notes that match actual behavior, plus verified focused regression results.

- [x] **Step 1: Update the existing spec’s implementation/verification notes**
  - Record atomic batch creation, source-group cleanup, and explicit cross-store partial-failure behavior; document any requirement not implemented and why.
- [x] **Step 2: Run the full focused regression set**

Run from `backend`: `& 'D:\Anaconda\envs\py311\python.exe' -m pytest tests/test_field_extraction.py tests/test_draft_create_and_publish_flow.py tests/test_draft_session_sync.py tests/test_draft_publishing.py tests/test_draft_index_recovery.py tests/test_milvus_mysql_replace.py tests/test_draft_cleanup_safety.py tests/test_document_lifecycle_safety.py tests/test_draft_application_rules.py tests/test_deleted_draft_purge.py -q`

Expected: all selected tests pass. Also run `git diff --check` from the repository root. Do not claim live model/MCP, production PostgreSQL, or Milvus end-to-end validation unless those environments are actually exercised.

## Self-Review

- Spec coverage: extraction boundaries/common fields are in Task 1; batch atomicity and non-reuse in Task 2; ordering, active draft, and vector isolation in Task 3; group deletion/legacy compatibility/reference checks in Task 4; docs and tests in Task 5.
- Step clarity: tests name the expected inputs, order, rollback, sibling isolation, publication preflight, and outside-reference retention; implementation tasks define exact public method contracts.
- Type consistency: `create_many` returns `list[dict[str, Any]]` in input order; `persist_drafts` maps those rows to ordered previews; cleanup looks up by exact `source_id`.
- Review focus: all five listed risks have matching tests in their owning tasks.
- Scope: no index rebuild, new frontend paging UI, deployment, dependency, or unrelated refactor is planned.
