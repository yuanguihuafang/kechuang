# 科创管理员与智能问答模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在同一后端内分开管理员和问答入口，修复草稿上下文与失败终态，消除 HTTP/MCP 互调路由。

**Architecture:** 模块化单体；`main/runtime → admin 或 assistant → 现有共用 services/core/agents → 存储适配器`。只迁移现有职责，不新增微服务、泛化容器或单实现接口框架。

**Tech Stack:** Python 3.11、FastAPI/Pydantic、现有 DeerFlow HTTP/SSE、FastMCP、pytest。

**Spec:** [后端系统审计与管理员/智能问答分块](../../方案/后端系统审计与管理员问答分块.md)。依赖[数据安全计划](2026-10-06-backend-data-safety.md)完成。

## Global Constraints

- 用户确认保留当前鉴权：登录、管理员角色、MCP 凭据与线程头认证方式不改；相关已知风险不列为“已修复”。
- 外部用户脱敏本轮仅初步卡片/详情的需求联系人及联系方式；不扩大至 MCP 模型证据、答案/debug，不影响供给侧或本人编辑草稿，不新增 LLM 调用。
- 不动 OPIEos、前端、端口、环境配置和真实数据，不提交代码；保留既有未提交改动。
- 公开路径、请求/响应字段、MCP 工具名及 SSE 名称不变；Python 旧路径不保留转发，不加兜底模型/自动重试。
- 测试全部隔离外部依赖。不是按行数拆文件，不建立万能 shared 目录。

## Review Focus

1. 管理员路由重复注册、草稿管理员下架漏挂：路径和方法唯一，原路径仍存在（任务 3）。
2. HTTP/MCP 草稿入参、状态、版本冲突语义分叉：两入口调用同一应用服务，响应契约一致（任务 4）。
3. 已保存未发布草稿只有 fields、客户端回传旧值或他人草稿：模型使用当前用户最新权威字段，不泄露其他用户（任务 1）。
4. 上游 error、超时、结束但无 done、客户端取消：只记录一次正确终态，失败不留旧成功（任务 2）。
5. 嵌套需求 fields 没写类型、供给侧同名联系方式、启动依赖未装配：继承脱敏语义且不误删；依赖缺失显式报不可用（任务 1、6—7）。

## 验证命令

工作目录 `D:\Work\kechuang\backend`。每步先红再绿，执行该任务 Tests；不因框架拆分运行线上工具。

```powershell
rtk proxy D:\Anaconda\envs\py311\python.exe -B -m pytest tests/<文件>.py -q -p no:cacheprovider -o addopts=
```

### Task 1：最新草稿输入和嵌套卡片脱敏

**Files:** `backend/services/application/draft_session.py`、`backend/api/v1/execution/supervisor.py`、`backend/api/v1/execution/deerflow_adapter.py`、`backend/core/governance/redaction.py`；扩展 `backend/tests/test_draft_session_sync.py`、`backend/tests/test_public_chat_result.py`。

**Interfaces:**
- `load_session_draft_context(runtime: Any, *, thread_id: str, user_id: str) -> dict[str, Any] | None`：按当前会话 active_draft_id 和 owner 读持久化最新版本；不存在活动草稿返回 None，读取失败不当作无草稿。
- `build_deerflow_input(...)` 签名保持不变；由字段合同和既有 context_window 限制构造不可信数据块，保留当前确定入口/方向，不依赖 summary 才能传 fields。
- `sanitize_demand_payload` 保持外部签名；递归继承父级需求语义，子项明确为 supply 时切换，不扩大遮蔽字段集合。

- [x] 红测试 `test_next_turn_uses_latest_unpublished_fields`：保存后下一轮 messages 中有最新字段，无旧客户端值。
- [x] 红测试 `test_other_user_active_draft_is_not_injected`、`test_nested_demand_contact_is_hidden_but_supply_unchanged`：他人草稿不进入输入，需求嵌套联系方式隐藏而供给不变。

```python
assert latest_value in model_message and stale_value not in model_message
assert other_owner_draft_text not in model_message
assert demand_contact not in str(public_card)
assert supply_contact in str(public_supply_card)
```

- [x] 运行两份测试记录失败。
- [x] 实现权威恢复和明确数据边界；规则脱敏只接原有卡片/详情调用者。保持没有草稿的普通问答行为。
- [x] 运行两份测试及 `test_draft_access.py`。

### Task 2：每轮流式结果唯一终态

**Files:** `backend/api/v1/execution/supervisor.py`、`backend/api/v1/execution/deerflow_adapter.py`；新增 `backend/tests/test_chat_stream_terminal_state.py`。

**Interfaces:**
- 提取现有终态保存为 `persist_stream_terminal(runtime: GatewayRuntime, *, thread_id: str, user_id: str, trace_id: str, result: dict[str, Any], status: str) -> None`，置于 supervisor 所属应用编排，不建通用事件框架。
- 复用本轮既有 request/trace 标识；status 与现有状态契约一致，失败保存 error/stage 和部分文本；done/error/异常最终只保存一次。

- [x] 红测试 `test_terminal_state_saved_once` 参数覆盖 done、上游 error、ReadTimeout、无 done 的正常 EOF、客户端取消；断言一个本轮终态且非成功轮没有 SUCCESS。
- [x] 红测试 `test_failed_turn_replaces_previous_success_state`：会话历史能看到本轮失败和 trace，不沿用上一轮成功结果。

```python
assert len(current_turn_terminal_records) == 1
assert current_turn_terminal_records[0]["trace_id"] == current_trace_id
assert failed_turn_status != "SUCCESS"
```

- [x] 运行新测试记录失败。
- [x] 所有退出分支走同一个本轮终态提交点；CancelledError 记录后继续抛出，不发送伪造 done，不重试上游。
- [x] 运行新测试及 `test_deerflow_material_flow.py`，保持草稿/匹配卡片事件契约。

### Task 3：管理员契约与路由归属独立

**Files:** `backend/api/v1/schemas.py`、`backend/admin/api/v1/admin.py`、`backend/admin/documents.py`、`backend/admin/governance.py`、`backend/api/v1/drafts/routes.py`；新增 `backend/admin/api/v1/schemas.py`；扩展 `backend/tests/test_runtime_architecture.py`。

**Interfaces:**
- 管理专属 HTTP DTO 原名迁到 `admin.api.v1.schemas`；真正共用 DTO 留/迁现有 `core/contracts`，不是复制两份。
- `/api/v1/admin/drafts/{draft_id}/unpublish` 只由管理员 router 注册；当前业务实现于任务 4 共用应用服务。
- 删除专属 DTO 的旧定义并完整更新所有引用，其他用户 API DTO 保持不变。

- [x] 红测试 `test_admin_dtos_and_routes_do_not_depend_on_assistant_http`：AST 检查管理模块不导入用户 HTTP schema/router；同路径方法不重复。

```python
assert admin_to_user_http_imports == []
assert route_inventory.count(("POST", "/api/v1/admin/drafts/{draft_id}/unpublish")) == 1
```

- [x] 运行架构测试记录失败。
- [x] 单次迁移上述专属 DTO 与全部生产/测试导入；先抽下架业务至现有 `services/application/draft_publishing.py`，两个 HTTP 入口直接调用，不能从 admin 导入用户 router。
- [x] 运行架构测试、`test_draft_publishing.py`、`test_draft_access.py`；比较路由清单，确保下架路径不丢。

### Task 4：草稿 HTTP 与 MCP 共用应用服务

**Files:** `backend/api/v1/drafts/routes.py`、`backend/runtime/mcp_tools/draft.py`、`backend/services/application/draft_publishing.py`；新增 `backend/services/application/drafts.py`；扩展 `backend/tests/test_mcp_draft_tools.py`。

**Interfaces:**
- `DraftApplicationService(runtime)` 提供 `create(content, *, context: ToolContext)`、`get(draft_id, *, context)`、`list(*, context, limit)`、`save(draft_id, content, *, context, expected_version)`、`transition(draft_id, action, *, context, expected_version)`、`delete(draft_id, *, context)`；异步写入方法返回现有 dict 响应，读取方法返回现有 dict/list。不接收 HTTP Request。
- publish/unpublish 继续复用既有发布应用服务；HTTP DTO 和 MCP schema 在边界各自校验，业务版本/owner/type 规则由共同服务执行。

- [x] 红测试 `test_mcp_drafts_do_not_build_http_router_or_fake_request`：替身 HTTP router 构建器抛错，MCP 仍可创建、保存、读取。
- [x] 红测试 `test_http_mcp_share_owner_type_and_version_rules`：同身份输入业务结果相同；他人 ID、改类型、旧版本都按同一规则拒绝。

```python
assert fake_request_calls == [] and http_router_build_calls == []
assert http_business_result == mcp_business_result
assert unauthorized_write_calls == []
```

- [x] 运行 MCP 测试记录失败。
- [x] 从现有 endpoint 移动编排到应用服务，去掉 endpoint(name)、route_request、伪 Request；所有调用者直接传 ToolContext，不调整 MCP 认证要求。
- [x] 运行 MCP 测试和 `test_draft_access.py`、`test_draft_session_sync.py`。

### Task 5：迁移智能问答模块，保持公开接口

**Files:** 移动 `backend/api/v1/{chat,documents,drafts,execution,identity,memory}` 到 `backend/assistant/api/v1/`；移动用户 schema/router 至同处；移动 `backend/runtime/{mcp_server.py,mcp_tools}` 与 DeerFlow gateway/adapter 到 `backend/assistant/integrations/`。更新全部真实调用与测试导入；不留旧路径。

这是机械导入迁移，文件数多于普通业务修复，禁止夹带行为改动或并行编辑。按三片依次搬迁并各自测试：identity/memory/documents；chat/drafts/execution；MCP/DeerFlow。未迁移的旧模块仍是真实现，迁移过的旧路径删除，绝不加转发别名；全部完成才交接。

**Interfaces:**
- `assistant.api.v1.router.build_router(runtime: GatewayRuntime) -> APIRouter` 不带 include_admin，永远只装配用户模块。
- `main.create_app` 分别装配 admin 与 assistant；MCP 单独 mount，现有路径及工具名保持不变。
- supervisor 的流编排归 `assistant.application.chat_stream`，材料长流程归 `assistant.application.materials`；HTTP request/response 留 router，DeerFlow 协议转换归 integrations。

- [x] 红测试 `test_module_import_direction_and_route_inventory`：admin/assistant 互导、services 导 HTTP router 均失败；原公开路径和方法完整且唯一。

```python
assert forbidden_import_edges == []
assert actual_public_routes == baseline_public_routes
assert actual_mcp_schemas == baseline_mcp_schemas
```

- [x] 运行架构测试，记录当前交叉依赖失败。
- [x] 先迁移既有文件，再抽材料/流编排；抽取保留原函数入参与返回语义，不新建与 GatewayRuntime 并列的万能容器。同步更新 main、agents HTTP 装配引用及测试，不做旧 import fallback。
- [x] 运行 `test_runtime_architecture.py`、`test_mcp_server.py`、`test_mcp_draft_tools.py`、`test_chat_material_session.py`、`test_deerflow_material_flow.py`、任务 2 新测试；核验公开路由快照和现有 MCP 工具字段合同无公开变更。

### Task 6：管理同步职责与共用解析能力解耦

**Files:** `backend/admin/service.py`、`backend/admin/documents.py`、`backend/admin/governance.py`、`backend/agents/perception_agent/routes.py`；新增 `backend/admin/mysql_sync.py`；图片补全迁入现有 `backend/services/application/document_lifecycle.py`；扩展 `backend/tests/test_runtime_architecture.py`。

**Interfaces:**
- `AdminMysqlSyncMixin` 承接现有 start/status/stop/counts/resume/_run_mysql_sync 方法，原签名与状态不变；沿用现有 Mixin 结构，不新增第二套同步 service。
- AdminService 显式获得共享 session_store 和 metrics callable，不从 conversation_service.session_store 或问答 HTTP 获取。
- `hydrate_parse_result_images` 保持现有签名但唯一归共用应用服务；Perception 路由和管理模块直接导入它。

- [x] 红测试 `test_admin_uses_shared_dependencies_not_assistant_or_agent_routes`：AST 不得导入问答/Perception router；AdminService 不需要构造问答 router 即可查询会话/运行同步。

```python
assert admin_route_imports == []
assert admin.session_store is injected_session_store
assert sync_task["operation"] == baseline_sync_operation
```

- [x] 运行架构测试记录失败。
- [x] 一次迁移同步职责及图片补全，复用当前缓存/维护锁/恢复串行逻辑，不改变之前 Milvus 修复行为。
- [x] 运行架构测试、`test_mysql_sync_safety.py`、`test_mysql_sync_preflight.py`、`test_collection_index_recovery.py`、`test_material_timing.py`。

### Task 7：运行时只装配，不创建业务兜底

**Files:** `backend/runtime/bootstrap.py`、`backend/runtime/container.py`、`backend/runtime/contracts.py`；已有清理业务迁至 `backend/services/application/document_lifecycle.py` 和 `backend/services/persistence/retention.py`；扩展 `backend/tests/test_task_runner_lifecycle.py`、`backend/tests/test_runtime_architecture.py`。

**Interfaces:**
- `GatewayRuntime.require_admin()` 仅返回已装配服务，缺失 503，不临时 new AdminService。
- bootstrap 注册上述共用业务函数与 Outbox recover，不保留第二套生命周期步骤；resources/start/stop 沿用既有资源生命周期。

- [x] 红测试 `test_missing_admin_is_explicitly_unavailable` 与 `test_partial_startup_closes_created_resources_once`：缺装配明确拒绝，启动失败/关闭/任务取消不泄漏已有资源。

```python
assert missing_admin_error.status_code == 503
assert constructed_admin_services == []
assert close_calls_per_created_resource == {name: 1 for name in created_resource_names}
```

- [x] 运行两份测试记录失败。
- [x] 完成唯一装配和窄依赖传递；不改配置来源，不把 runtime.config 引用机械迁移为另一套配置。
- [x] 运行两份测试、`test_postgres_pool_lifecycle.py`、`test_retrieval_route_lifecycle.py`、`test_storage_consistency.py`。

## Checkpoint：最终审查和文档

- [x] 检查公开 HTTP/MCP/SSE 契约未变化，数据安全计划回归仍通过。
- [x] 对本次所有修改生产文件做 AST/未定义名称检查；测试命令明确隔离真实网络和数据写入，不以测试数量替代真实依赖验收。
- [ ] 独立审查员只读检查本次 diff 的数据丢失、模块反向导入、失败终态和公开接口变化；主协调者合并修复。
- [x] 更新 docs/Agent.md、审计状态、API 文档和模块职责图；标注鉴权及模型证据脱敏未改，真实服务与服务器未验证。

## 协作边界与执行门

先审核两份计划，再执行。可使用子 agent，但任务 1/2/3/4 修改草稿/流相关文件以及数据安全计划的 service/bootstrap 修改必须串行由一个协调者合并。并行只用于不共享文件的只读审查、独立测试或已定义接口的任务；不得对同一文件同时改动，不自动创建新聊天任务或提交代码。

## 执行记录（2026-10-07）

- Task 1—7 本地完成。身份/记忆/文档、材料/草稿/执行、MCP/DeerFlow 完成一次性迁移；旧路径无转发。HTTP/MCP 共用草稿应用服务，管理员不引用问答 HTTP 契约。
- 材料、流式编排从 HTTP 抽出；公开结果组装与既有业务身份解析归共用应用服务，应用/integration 不反向导 HTTP 模块。签发/验证/角色映射保持原算法。
- 同步沿用现有 Mixin、维护串行和断点规则；共用图片补全归 document_images.py。运行时显式注入会话仓储/指标、缺管理员装配返回 503。
- schema 检查/Checkpointer 启动失败关闭已建资源；Runner 停止失败仍等待后台任务与清理状态，不丢弃异常。
- 契约核验采用 120 项公开路径/方法基线、现有 MCP 工具名/字段与 SSE 测试；未额外引入完整 HTTP/MCP JSON schema 快照系统。
- 最终全量命令：在 backend 运行 `rtk proxy D:\Anaconda\envs\py311\python.exe -B scripts/test_backend_offline.py` → 693 passed，1 项既有 Starlette 弃用警告。189 文件语法和 Ruff F821/F822/F823 通过。
- 独立审查工具未能加载工作区；改为主协调者自审，未伪称独立验收。真实依赖/服务器未验证；鉴权、模型证据与答案脱敏保留原有边界。
- 全部裁决/代价及延后小项见[实现记录](../../方案/后端系统审计与管理员问答分块.md#实施裁决与代价)。
