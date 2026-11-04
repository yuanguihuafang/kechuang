# 本地知识数据生命周期统一标准 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一本地管理端、文档/图片、发布索引和问答检索流程，确保跨存储操作可核对、可重试且读操作无隐式写入。

**Architecture:** 复用现有 `DocumentLifecycleApplicationService`、`DraftPublishingApplicationService`、`document_images`、检索服务和 Outbox/任务补偿，不增加全局工作流引擎。先修图片读取边界，再补生命周期/来源组和问答的失败用例，只修复验证中实际发现的缺口。

**Tech Stack:** Python 3.11、FastAPI、pytest、PostgreSQL/MySQL/MinIO/Milvus 现有适配层；前端仅在后端契约或图片展示需要时改动。

**Spec:** `docs/superpowers/specs/2026-10-20-local-knowledge-lifecycle-standard-design.md`

## Global Constraints

- 仅修改 `D:\Work\kechuang` 本地代码、测试和文档；不 SSH、不访问服务器、不部署。
- PostgreSQL 管文档/版本/草稿/确认分块；MySQL 管业务行；MinIO 管原件/解析产物/图片；Milvus 仅为派生索引。
- 重解析不得重新提取或覆盖人工确认的草稿字段、`mysql_id` 或 `source_id`。
- 管理图片（包含封面图和解析原文图片）只经 `(document_id, image_id)` 清单映射到统一受控 URL；外部图片 URL 保持原样。
- 管理员重解析进入待发布状态后，前端隐藏旧解析原文和旧字段提取结果；重新发布通过后再整体显示新结果，不混合新旧版本。
- 每次发布先完整校验 PostgreSQL 关系/版本、MySQL 稳定业务身份、MinIO 原件/解析产物/引用图片对象和 Milvus 来源组目标；任一校验失败即阻断写入并说明原因。
- 跨存储失败必须显式可见并可安全重试；不宣称分布式原子事务，不静默切换数据源。
- 用户逻辑删除后立即隐藏并暂存关联资源；满 30 天后统一在北京时间每日 02:00 清理，未发布用户/管理员草稿 30 天未更新后清附件但保留字段，已发布数据及附件受保护；审计记录保留 180 天。
- 保留现有未提交改动；不提交代码、不安装依赖、不启动服务。

## Review Focus

- MinIO 产物清单记录对象已丢失：解析结果读取和问答不能把该图片当作可用对象（Task 1、Task 3）。
- 读取解析结果意外写回图片清单：GET 不应改变 MinIO 或业务状态（Task 1）。
- 重解析后的旧原文/旧字段仍可见或与新结果混合：重新发布前隐藏旧结果，成功后整体切换（Task 2）。
- 发布前置依赖缺失或目标索引冲突：必须预检阻断，不得部分显示成功（Task 2）。
- 同一草稿重复发布或索引失败后重试：MySQL ID/source_id 不变且 Milvus 不产生重复业务记录（Task 2）。
- 共享附件或旧版本被删除：不得级联误删仍被发布草稿引用的数据（Task 2）。
- 下架记录、无权限记录或过期图片进入问答证据：不得返回为有效命中（Task 3）。

---

### Task 1: 解析结果读取与图片对象一致性

**Files:**
- Modify: `backend/services/application/document_images.py`
- Modify only if required: `backend/services/storage/minio/minio_storage.py`
- Test: `backend/tests/test_document_image_normalization.py`
- Test: `backend/tests/test_minio_object_inspection.py`
- Test: existing parse-result/admin route tests discovered with `rg --files backend/tests`

**Interfaces:**
- Consumes: `hydrate_parse_result_images(document_id, result, storage)` and existing image/artifact manifest readers.
- Produces: same response shape; hydration is read-only, and managed image references are exposed only when their mapped MinIO objects are confirmed available. Cover and parse-body images use the same canonical URL helper.

- [ ] **Step 1: Add failing tests** asserting hydration never calls `save_image_manifest`, and absent/ambiguous object references do not return a usable managed image URL while external URLs remain unchanged.
- [ ] **Step 2: Run the targeted tests** with `D:\Anaconda\envs\py311\python.exe -m pytest backend/tests/test_document_image_normalization.py -q`; confirm the new assertions fail for the current implicit write.
- [ ] **Step 3: Implement the smallest shared fix** in image hydration/storage; keep explicit `normalize-images` write operation as the repair path and preserve response contract.
- [ ] **Step 4: Run image plus parse-result route tests** and verify the targeted tests pass.

### Task 2: CRUD, publishing, and cross-store retry invariants

**Files:**
- Modify only where tests expose a gap: `backend/services/application/document_lifecycle.py`, `backend/services/application/draft_publishing.py`, `backend/services/application/draft_cleanup.py`, `backend/services/application/business_records.py`, `backend/admin/documents.py`, `backend/admin/service.py`, `backend/services/vector/milvus/milvus_service.py`
- Tests: `backend/tests/test_draft_publishing.py`, `backend/tests/test_business_records_source_reuse.py`, `backend/tests/test_document_lifecycle_safety.py`, `backend/tests/test_lifecycle_cleanup_replay.py`, `backend/tests/test_draft_cleanup_safety.py`, `backend/tests/test_admin_upload_catalog.py`, `backend/tests/test_mcp_document_tools.py`, `backend/tests/test_field_extraction.py`, `backend/tests/test_task_runner_lifecycle.py`

**Interfaces:**
- Consumes: existing lifecycle/draft application services and stable IDs.
- Produces: no public API change unless an existing contract is demonstrably unsafe; retries preserve IDs, owner/version checks and shared-attachment behavior.

- [ ] **Step 1: Add missing failure/replay tests** for publish retry after partial index failure, unchanged MySQL/source identity, shared-version purge, user/admin ownership boundaries, complete publish preflight, and hiding stale parse/field results until a successful replacement publish.
- [ ] **Step 2: Run those tests** and identify whether each contract already passes; retain passing existing behavior without rewriting it.
- [ ] **Step 3: Fix only failing shared application-service paths**, not individual route workarounds; preserve task/Outbox compensation and explicit MySQL field-clear semantics.
- [ ] **Step 4: Run the focused CRUD/publish/lifecycle test set** and confirm failed preflight makes no publish writes, stale results stay hidden while pending, and successful replacement switches the whole visible result set.

### Task 3: Q&A publication, authorization, and image evidence

**Files:**
- Modify only where tests expose a gap: `backend/agents/retrieval_agent/application/retrieval_service.py`, `backend/agents/retrieval_agent/retrieval/search_service.py`, `backend/services/application/chat_response.py`, and the existing frontend evidence/image component if response behavior requires it.
- Tests: `backend/tests/test_retrieval_relevance.py`, `backend/tests/test_retrieval_details.py`, `backend/tests/test_retrieval_helpers.py`, `backend/tests/test_public_chat_result.py`, `backend/tests/test_recommendation_card_images.py`; corresponding frontend tests if UI changes.

**Interfaces:**
- Consumes: current retrieval contracts, identity policy, `ImageManifestService`, canonical URL helper.
- Produces: existing response shape unless a tested contract defect requires a compatible correction; only published, authorized evidence and readable managed images are shown.

- [ ] **Step 1: Add regression tests** for unpublished/unauthorized candidates, same business record grouping by `mysql_id`, missing MinIO image objects and external image URL preservation.
- [ ] **Step 2: Run the focused retrieval/chat tests** to establish failing cases and preserve already-correct filtering/fallback behavior.
- [ ] **Step 3: Correct the shared retrieval/response path**; do not add a second permission or URL policy in the UI.
- [ ] **Step 4: Run backend retrieval/chat tests and any affected frontend tests.**

### Task 4: Publish the local standard workflow and verify

**Files:**
- Modify: `docs/方案/方案库完整修复与重建执行方案.md` only if its rules conflict with the approved cross-flow contract.
- Modify: `docs/Agent.md` and `docs/README.md` to index the local standard workflow.
- Create/update: `docs/方案/本地知识数据标准操作流程.md` with a concise local CRUD/upload/reparse/publish/retry/QA checklist; index it in `docs/Agent.md` and `docs/README.md`; do not duplicate the detailed recovery log.

- [ ] **Step 1: Add documentation checks/content** linking each operation to its authoritative store, preflight, retry, and post-write verification.
- [ ] **Step 2: Run `git diff --check` and review only this task's files.**
- [ ] **Step 3: Run the complete backend test suite** with the project `py311` interpreter; if it stalls, record the last observed progress and leave it unclaimed as passing. Run frontend tests only for changed frontend files.
- [ ] **Step 4: Report modified paths, test results, unverified live-service behavior, and confirm no server/commit actions occurred.**

## Execution Notes

- Implement in this session (native execution); do not delegate overlapping files.
- Before every edit, inspect the current diff for that exact file and preserve unrelated existing changes.
- If a planned invariant already passes, keep the code and test evidence; do not refactor it for uniformity alone.
- Do not run live database/MinIO/Milvus mutations during unit tests; local integration requires explicit local service configuration and must not be inferred from mocked tests.
- No commit is part of this plan; the user explicitly requires local-only work and project instructions prohibit commits unless requested.
