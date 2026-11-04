# 科创后端清理与恢复数据安全 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 修复有限引用扫描、共享来源误删和确认来源过期问题，让仅 Milvus 重建始终保留业务数据库与 MinIO。

**Architecture:** 复用现有仓储、版本 `artifact_refs`、生命周期应用服务与 Task/Outbox。修复共享入口的根因，不给每个调用者添加独立清理实现，不引入新存储或自动重建。

**Tech Stack:** Python 3.11、FastAPI、现有 PostgreSQL/SQLAlchemy、MinIO、Milvus、pytest。

**Spec:** [后端系统审计与管理员/智能问答分块](../../方案/后端系统审计与管理员问答分块.md)。

## Global Constraints

- 2026-10-06 用户确认“保留当前”鉴权：不改登录、管理员身份、MCP 凭据要求或认证方案；这些已知风险仍保留，不宣称完成安全加固。
- 只改科创 `backend` 和相关文档；不改 OPIEos、前端、环境配置，不提交代码。
- 不操作真实 PostgreSQL/MySQL/MinIO/Milvus；测试使用替身。旧数据回填必须单独授权。
- HTTP 路径、请求/响应字段与 SSE 事件名称保持不变；内部导入一次更新，不保留兼容转发。
- 禁止缺来源猜路径、自动重新解析、自动 release/load、自动重建以及查询失败按零继续。
- Collection 重建只删除指定 Milvus Collection；不清空 PostgreSQL/MySQL，不删除 MinIO 正文及附件。
- 保留全部既有未提交修改。每步先失败测试，再最小实现，再相关回归；不执行 git add/commit。

## Review Focus

1. 引用超过 10000 条、历史版本或后半段页读取失败：有效对象不删除，查询失败停止清理（任务 1）。
2. 清理候选生成后又被引用：删除前重查；同实例清理/发布写入串行，跨进程不保证原子性（任务 1）。
3. 成功任务超过 30 天、确认正文缺失或版本来源不匹配：有效版本独立恢复，坏来源明确拒绝（任务 2—3）。
4. 不同类型同数值 MySQL ID、多个草稿共用来源、旧草稿超出列表窗口：删除只影响目标，保留他人来源（任务 4—5）。
5. purge 部分成功后重放、关联草稿仍发布：预检先于外部删除；首次执行和补偿结果一致（任务 6）。

## 执行与验证约定

以下命令均在 `D:\Work\kechuang\backend` 执行；测试文件使用本任务指定的现有或新增文件。

```powershell
rtk proxy D:\Anaconda\envs\py311\python.exe -B -m pytest tests/<文件>.py -q -p no:cacheprovider -o addopts=
```

每个任务的“红/绿”都运行其 Files 中列出的测试；红阶段应在目标行为断言失败，而不是因真实数据库连接失败。禁止为验证启动服务或扫描真实桶。

### Task 0：单文档操作不依赖列表窗口

**Files:** `backend/api/v1/documents/lifecycle.py`；扩展 `backend/tests/test_document_lifecycle_safety.py`。

**Interfaces:** 复用 `get_document(document_id, user_id=user_id)`；用户发布/下架 HTTP 签名与所有权规则不变。

- [x] 红测试 `test_lifecycle_finds_owned_document_outside_list_window`：目标不在最新 200 条中但属于当前用户，仍找到；他人目标返回现有 404。核心断言：

```python
assert owned_response.status_code == 200
assert other_user_response.status_code == 404
assert list_documents_calls == []
```

- [x] 运行 `test_document_lifecycle_safety.py`，记录失败。
- [x] 两个按列表找对象的入口都改为仓储定向查询，不搬权限逻辑，不扫描全表。
- [x] 再运行该测试，确认通过。

### Task 1：完整存储引用与删除前核验

**Files:** `backend/services/persistence/retention.py`、`backend/services/storage/minio/minio_storage.py`、`backend/runtime/bootstrap.py`；扩展 `backend/tests/test_storage_consistency.py`。

**Interfaces:**
- 保留 `StorageReferences(document_ids: set[str], storage_keys: set[str])`。
- `PostgresRetentionService.storage_references() -> StorageReferences`：读取文档及历史版本、草稿附件、任务及确认正文引用；游标 fetchmany 读到结束，不加总条数上限。
- `PostgresRetentionService.is_storage_referenced(storage_key: str, document_id: str | None) -> bool`：删除前按相同引用定义做定向查询。
- `MinioUploadService.cleanup_orphaned_objects` 增加必填 `is_referenced: Callable[[str, str | None], bool]`；完整更新所有调用者，不设默认绕过分支。

- [x] 红测试 `test_cleanup_preserves_reference_10001_and_historical_versions`：模拟 10001 条引用及旧版本，断言 `deleted == 0`。
- [x] 红测试 `test_cleanup_aborts_on_reference_read_failure` 与 `test_cleanup_rechecks_new_reference`：分页失败时没有 delete；候选被新引用时没有 delete。

```python
assert deleted_keys == []
assert references.document_ids == all_referenced_document_ids
```

- [x] 运行 `test_storage_consistency.py`，记录上述失败。
- [x] 在现有 retention 服务实现完整读取和定向重查；同一引用提取规则覆盖 `artifact_refs`、`version_history`、确认请求和附件，不新增万能仓储层。清理与当前实例会写引用的操作共用现有维护互斥点；核验抛错即中止，不吞错继续删除。
- [x] 运行同文件，断言通过。文档记录：多进程/外部写入仍需维护窗口，不能把重查当跨系统事务。

### Task 2：确认正文长期归属于版本

**Files:** `backend/runtime/workflows/file_workflow.py`、`backend/services/persistence/document_lifecycle.py`、`backend/services/persistence/retention.py`；扩展 `backend/tests/test_document_lifecycle_safety.py`。

**Interfaces:**
- 不改变 `save_confirmation_request(...) -> str` 和 `update_version(version_id, **fields)` 的公开签名。
- 有效确认成功后，版本 `artifact_refs.confirmed_index_request` 唯一包含 `storage_key`、`task_id`、`document_id`、`version_id`、`identity`。保留其他 artifact_refs 键，不保存 Bearer。
- 未完成确认不能覆盖有效发布版本的恢复来源。版本写入失败不能把任务标为成功。

- [x] 红测试 `test_successful_confirmation_retains_version_source_after_task_retention`：删除 31 天前任务后，版本仍有正文引用和确认身份。
- [x] 红测试 `test_failed_confirmation_does_not_replace_published_source`：失败/版本不匹配不覆盖原来源。

```python
assert version["artifact_refs"]["confirmed_index_request"] == confirmed_reference
assert failed_version["artifact_refs"] == original_artifact_refs
```

- [x] 运行 `test_document_lifecycle_safety.py`，记录失败。
- [x] 在成功确认提交点合并并保存引用；retention 仅在确认来源已安全落到版本后允许清理相关成功任务。旧有效版本未迁移时保留其唯一任务来源，避免先删证据；恢复读取不因此增加旧任务兜底。
- [x] 运行同文件及 `test_storage_consistency.py`；确认新 artifact_refs 也受任务 1 引用保护。

### Task 3：恢复只读权威版本来源，旧数据显式回填

**Files:** `backend/admin/index_recovery.py`；新增 `backend/services/application/index_recovery_sources.py`、`backend/scripts/backfill_confirmed_index_sources.py`；扩展 `backend/tests/test_collection_index_recovery.py`。

**Interfaces:**
- `load_confirmed_version_request(storage: StorageService, document: dict[str, Any], version: dict[str, Any]) -> IndexRequest` 为 async：核验正文、身份、文档、版本、scope/type；读取既有 MinIO API 所需的真实 task_id，不猜路径。
- `backfill_confirmed_sources(runtime: GatewayRuntime, *, apply: bool = False) -> dict[str, int]` 为 async：只扫描缺少引用的版本，逐个从真实成功确认任务加载并核验；默认只报告，显式 apply 才写 PostgreSQL artifact_refs。
- `prepare_collection_recovery` 签名不变，长期来源仅版本引用及已有有效发布草稿，不再运行时查任务历史兜底。

- [x] 红测试 `test_recovery_works_without_old_task`：版本引用有效、任务已删，仍生成原始确认 chunks。
- [x] 红测试 `test_recovery_rejects_missing_or_mismatched_version_source`：坏正文/身份/版本在 drop Collection 前拒绝。
- [x] 红测试 `test_backfill_dry_run_and_apply_are_explicit`：默认零写；apply 仅更新核验通过且仍未改变的对应版本，不改业务状态或 MinIO。

```python
assert recovery_request.chunks == originally_confirmed_chunks
assert dry_run_version_updates == []
assert destructive_storage_calls == []
```

- [x] 运行 `test_collection_index_recovery.py`，记录失败。
- [x] 实现严格版本读取及回填函数。脚本提供 `--apply` 和明确汇总；本轮不执行。新旧代码切换前，部署说明必须安排显式核验/回填；来源缺失拒绝维护，不伪造可恢复状态。
- [x] 运行该文件及 `test_draft_index_recovery.py`；再次验证只 Milvus 恢复不删除其他存储数据。

### Task 4：草稿关联按业务键查询，不按有限列表猜测

**Files:** `backend/services/persistence/repositories/workflow_repository.py`、`backend/runtime/contracts.py`；新增 `backend/tests/test_draft_reference_queries.py`。

**Interfaces:**
- `list_for_document(document_id: str) -> list[dict[str, Any]]` 完整返回关联草稿，去掉总条数上限；分页 UI 另用已有 `list_page`。
- 复用已有完整 `list_for_thread(thread_id)` 与 `get_admin(draft_id)`。
- 新增 `list_business_references(draft_type: str, mysql_record_id: str, *, user_id: str | None) -> list[dict[str, Any]]`：按类型、业务 ID、私有归属查活动引用；不把不同表的同数值 ID 当同一来源。

- [x] 红测试 `test_document_references_are_complete_beyond_500`；断言所有关联都返回。
- [x] 红测试 `test_business_references_are_typed_and_owner_scoped`；不同类型和不同私有用户不得混入，共享公开活动来源必须返回。

```python
assert len(all_document_references) == 501
assert {row["draft_id"] for row in business_references} == expected_draft_ids
```

- [x] 运行新测试，记录失败。
- [x] 实现参数化仓储查询；更新现有 Protocol 与全部调用者，明确活动状态集合沿用现有发布/删除规则，不引入新业务状态。
- [x] 运行新测试和 `test_draft_attachment_association.py`。

### Task 5：草稿、会话和关联来源安全删除

**Files:** `backend/admin/service.py`、`backend/admin/documents.py`、`backend/services/application/draft_publishing.py`；扩展 `backend/tests/test_deleted_draft_purge.py`、`backend/tests/test_draft_attachment_association.py`。

**Interfaces:**
- 删除入口签名与响应不变；目标按 ID/thread/document 查询，引用判断消费任务 4 接口。
- 在现有应用服务增加 `plan_draft_cleanup(drafts: DraftStore, targets: list[dict[str, Any]]) -> dict[str, Any]`：区分可删除目标与仍被其他活动来源引用的 typed business/vector/file keys；不执行写入。

- [x] 红测试 `test_delete_draft_keeps_shared_mysql_and_vectors`：删目标 A 不删除仍由 B 使用的同类型业务行或向量；不同类型同 ID 不误保护/误删。
- [x] 红测试 `test_delete_old_thread_drafts_uses_complete_targets`：超过全库 10000 窗口的目标仍先完成外部处理，再移除其 PostgreSQL 引用。
- [x] 红测试 `test_cleanup_reference_failure_has_no_external_writes`：仓储失败时业务行、向量、附件均零删除。

```python
assert shared_mysql_key not in deleted_mysql_keys
assert shared_vector_key not in deleted_vector_keys
assert shared_storage_key not in deleted_storage_keys
```

- [x] 运行两份测试，记录失败。
- [x] 三种入口共用清理计划，统一 MySQL、Milvus、附件的共享来源保护。先完整预检所有目标及来源；保留引用直到已有任务/补偿记录能够前向恢复，禁止先删 PostgreSQL 引用再发现外部步骤失败。
- [x] 运行两份测试及 `test_draft_publishing.py`，保持用户草稿删除和管理员删除一致。

### Task 6：purge 首次执行与补偿统一

**Files:** `backend/services/application/document_lifecycle.py`、`backend/admin/documents.py`、`backend/runtime/bootstrap.py`；扩展 `backend/tests/test_document_lifecycle_safety.py`。

**Interfaces:**
- `DocumentLifecycleApplicationService.run_action(...)` 保持现有签名；扩展管理端已存在 recycle/restore 动作的共同编排。
- 新增 `recover(event: dict[str, object]) -> None` async：消费已有 LIFECYCLE_RECOVERY payload 和 completed_steps，调用同一业务步骤，不保留 bootstrap 中另一套清理算法。
- 管理员调用显式传入当前既有身份语义；不在共用服务从内部人员标记推断管理员。

- [x] 红测试 `test_purge_with_published_link_rejects_before_deletion`：仍发布的关联草稿使操作拒绝，外部零删除。
- [x] 红测试 `test_purge_replay_matches_initial_cleanup`：在 MySQL/MinIO/PostgreSQL 各阶段注入失败并重放，最终目标与关联草稿状态一致，共享来源仍保留。

```python
assert published_link_external_deletes == []
assert replay_final_state == successful_first_run_state
assert shared_source_deleted is False
```

- [x] 运行该测试，记录失败。
- [x] 将任务 5 预检与关联清理接入应用服务；首次执行、管理员入口和 Outbox 统一调用。保持已有任务/Outbox 前向恢复，不加入失败自动重建。
- [x] 运行该文件、`test_document_lifecycle_status.py`、`test_storage_consistency.py`；检查未提交 diff，记录实际通过数和未验证真实依赖。

## Checkpoint：数据安全完成后才进行模块迁移

- [x] 以上七步隔离测试全部通过，确认单对象不依赖列表窗口、清理不依赖有限 list_all。
- [x] 持久化确认来源已纳入完整引用保护；坏来源在重建前拒绝。
- [x] 更新本计划进度、审计文档、Milvus 恢复操作文档及 docs/Agent.md。服务器回填/部署仍待另行授权。
- [x] 再执行[管理员与问答模块实施计划](2026-10-06-backend-module-boundaries.md)。

## 执行记录（2026-10-07）

- Task 0—6 本地完成：直接查单文档、完整引用及定向关联、长期版本来源、显式回填、共享来源删除保护、统一 purge/补偿。
- 新上传保留旧版本历史；确认来源提交先于旧版本清理。历史仍有效但当前版本已清理的来源受保护。旧数据回填不将普通未确认上传当成坏来源。
- 终审复现并修复：过期读取直接删除共享附件、文档分页键不匹配、较新共享草稿/历史发布版本保护、单版本 purge 误删其他版本产物、PG 已提交但任务终态未完成、状态变更漏传版本及数据库并发 CAS。
- 保留服务器回填/部署审批门：未连接或修改真实 PostgreSQL/MySQL/MinIO/Milvus。维护锁仅同 ASGI 循环有效，不承诺跨系统原子性。
- 最终全量命令：在 backend 运行 `rtk proxy D:\Anaconda\envs\py311\python.exe -B scripts/test_backend_offline.py` → 693 passed，1 项既有 Starlette 弃用警告。
- 最终语法检查 189 个生产/脚本文件通过，Ruff F821/F822/F823 通过；公开路径/方法快照 120 项保持一致。
- 最终自审已完成；独立审查工具加载工作区失败，真实依赖联调仍待执行。全部裁决/代价见[实现记录](../../方案/后端系统审计与管理员问答分块.md#实施裁决与代价)。

## 补充：Worker 租约与后台线程关停（2026-10-07）

**范围：**仅处理 Worker 关停后任务长时间保持 RUNNING，以及 `asyncio.to_thread` 的后台 MySQL 同步/Collection 恢复在线程仍运行时共享资源先关闭的问题。不接入真实数据库、Milvus、MinIO 或服务器。

- [x] Worker 关停时只中断本 Worker 的活动 claim 并立即清租约；将租约改为 60 秒、心跳续租，并让 `PARSING/INDEXING` 同样参与续租和过期回收。
- [x] MySQL 同步与 Collection 恢复后台线程共享协作停止信号；同步在写批次前/批次后检查，恢复在索引记录和发布状态写入边界检查，保存停止/中断状态与检查点。
- [x] lifespan 关停先发后台停止信号、停止 Worker、取消普通协程；等待后台包装任务和默认线程池中已提交的 `to_thread` 工作退出后才释放共享资源。
- [x] 回归：生命周期、同步安全、索引恢复 48 项通过；全量离线后端 704 项通过（1 条既有 Starlette 弃用警告）；Ruff F821/F822/F823/E9 通过。未连接真实存储、未提交、未部署。
