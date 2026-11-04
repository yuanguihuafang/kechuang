# DeerFlow Parallel Migration Implementation Plan

> **状态（2026-09-24）：历史迁移计划。** 本文保留早期多 AI 拆分与执行记录；其中 `orchestrator` 主 Skill 等方案已被当前 `kechuang` Agent + SOUL + `kc-*` Skill 布局取代。当前运行、接入和验收状态以 `docs/DeerFlow迁移与Skill接入方案.md` 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改动现有前端接口和参数的前提下，将科创业务逐步迁移到 OPIEos/DeerFlow 运行时。

**Architecture:** 保留现有 `/api/v1/*` 作为唯一前端业务入口，在后端增加 DeerFlow 适配层，将外部请求转换为 DeerFlow Thread/Agent 输入，再将 DeerFlow 内部事件转换为现有 SSE 契约。Skill 只负责编排，业务 Tool/MCP 负责调用现有服务，PostgreSQL、MySQL、Milvus 和 MinIO 继续由原业务后端管理。

**Tech Stack:** OPIEos/DeerFlow, LangGraph, FastAPI, Python, PostgreSQL, MySQL, Milvus, MinIO, SSE, Tool/MCP。

**Spec:** `D:/Work/kechuang/docs/DeerFlow迁移与Skill接入方案.md`；前端契约：`D:/Work/kechuang/docs/前端API/问答界面功能说明.md`。

## 当前执行状态（2026-09-22）

- 当前唯一工作区是 `D:/Work/kechuang`；`D:/Work/kc-rag-agent` 仅可只读参考，禁止写入。
- Skill 根目录是 `D:/Work/kechuang/OPIEos-main/skills`，业务 Skill 通过 DeerFlow 配置映射到 `skills/custom`，不得把宿主机路径硬编码进 `SKILL.md`。
- Tool/MCP 与知识检索可以在 Task 0 契约冻结后并行，但必须使用独立工作树或完全不重叠的文件边界；最终接线和回归必须串行完成。
- 本轮并行执行曾误删 `kechuang/backend` 部分源码，现已还原。PaddleOCR 注入参数问题已修复；历史阶段后端全量回归为 `343 passed`，Docker backend、MCP endpoint 和 Gateway 网络连通已验证。已用本地 MySQL 业务用户完成真实 Skill → MCP 调用验证，当前测试查询暂无命中数据；本轮最新全量结果记录在 Task 6。
- Task 2、Task 3、Task 4 已完成静态/定向验证；Task 5A-5D 的检索/匹配、草稿、文档、画像和客户拜访首批 MCP 均已接线，合并后定向回归 `57 passed`。新增工具尚未全部在真实 DeerFlow 会话和真实业务数据上联调；Task 6 的旧链路对比仍未完成。
- 当前 Docker 构建中的 `ghcr.io/astral-sh/uv:0.11.1`、`python:3.12-slim-bookworm` 和 `node:22-alpine` 均为基础镜像；它们被合并到最终服务镜像，不作为独立容器运行。实际运行服务为 `Gateway`、`业务 Backend` 和 `前端`。
- 最小运行验证已通过：三个 Compose 配置 `config -q` 通过，OPIEos 页面和业务 backend 健康接口均返回 `200`，业务 backend、PostgreSQL、Milvus、MinIO、etcd、Redis 均为 healthy。

## Global Constraints

- 只修改 `D:/Work/kechuang`；不得修改或写入 `D:/Work/kc-rag-agent`。
- 前端继续使用现有 `/api/v1/*` 接口，不直接调用 DeerFlow 原生 `/api/langgraph/*`。
- 现有请求字段、Token-only 身份、`thread_id` 语义、草稿/附件参数保持兼容。
- `/api/v1/chat/stream` 和材料解析流继续输出 `start`、`progress`、`token`、`done`、`error`。
- DeerFlow 事件只在后端适配层转换，不能把 `values`、`messages-tuple`、`custom`、`end` 暴露给前端。
- Skill 根目录为 `D:/Work/kechuang/OPIEos-main/skills`；业务 Skill 位于 `skills/custom`，公共 Skill 位于 `skills/public`。
- Skill 不直接连接数据库、不保存密钥、不接收模型自带的用户身份和权限字段。
- 不提前删除旧 `SupervisorAgent`、旧业务服务或现有 `/api/v1` 路由。
- 每个 AI 使用独立分支或工作树；不同工作流不得修改同一文件。
- AI 不得执行清理、批量删除、旧目录恢复、覆盖同步或无关重构；发现目标文件缺失时立即停止并报告。

## Review Focus

- 历史 `thread_id` 超过 DeerFlow 内部限制时，后端必须映射而不是要求前端修改。
- DeerFlow 流式事件结束或异常时，前端仍必须收到兼容的 `done` 或 `error` 事件。
- Token 解析出的用户身份必须覆盖请求体中伪造的 `user_id`、角色和企业字段。
- 同一 `thread_id` 的草稿、附件、检索条件和匹配排除集合必须保持连续；新 Thread 不得串状态。
- Tool/MCP 部分失败必须保留业务阶段、`trace_id`、可重试性和已产生的部分结果。

---

## 协作顺序

### Wave 0：契约冻结，顺序执行

先由一个负责人完成 Task 0。其他 AI 不得在契约未冻结前修改 Gateway、Schema 或 Skill 路由。

### Wave 1：可并行实现

Task 1、Task 2、Task 3、Task 4、Task 5 可以并行，但必须遵守文件边界。Task 2 和 Task 3 依赖 Task 0 定义的契约，Task 4 和 Task 5 使用 Task 0 定义的 Tool 输入/输出。

### Wave 2：集成与回归，顺序执行

先合并 Task 1-5，再执行 Task 6。Task 6 负责端到端适配、回归测试、兼容检查和回滚验证，不允许在这一阶段重新设计前端接口。

## Task 0: Freeze the public frontend contract

**Files:**
- Modify: `D:/Work/kechuang/docs/前端API/问答界面功能说明.md`
- Modify: `D:/Work/kechuang/docs/DeerFlow迁移与Skill接入方案.md`
- Baseline test: `D:/Work/kechuang/backend/tests/test_stream_protocol.py`
- Baseline test: `D:/Work/kechuang/backend/tests/test_stream_auth_contract.py`

**Interfaces:**
- Produces: 固定的路径、请求字段、SSE 事件、错误语义、`thread_id` 和 Token-only 规则。

- [x] 记录 `/api/v1/chat/stream`、材料解析、会话、草稿、文档和人工联系接口为兼容入口。
- [x] 记录现有五类 SSE 事件和 `done.result` 字段。
- [x] 明确 DeerFlow 原生事件只允许在后端内部使用。
- [x] 运行 `pytest backend/tests/test_stream_protocol.py backend/tests/test_stream_auth_contract.py -q` 作为基线。

## Task 1: Align DeerFlow runtime and custom Skill loading

**Files:**
- Modify: `D:/Work/kechuang/OPIEos-main/config.yaml`
- Modify: `D:/Work/kechuang/OPIEos-main/skills/custom/**`
- Test: `D:/Work/kechuang/OPIEos-main/backend/tests/test_runtime_paths.py`
- Test: `D:/Work/kechuang/OPIEos-main/backend/tests/test_skills_loader.py`

**Interfaces:**
- Consumes: Task 0 的 Skill 名称和前端业务意图。
- Produces: `skills.path: skills` 相对 OPIEos 仓库根目录解析到 Skill 根目录，业务 Skill 从 `skills/custom` 发现，公共 Skill 从 `skills/public` 发现。

- [x] 确认 `skills.path: skills`（相对 OPIEos 仓库根目录）和 `skills.container_path: /mnt/skills`，不在 Skill 正文硬编码宿主机路径。
- [x] 确认主 Skill 名称为 `orchestrator`，不再使用旧的主 Skill 名称。
- [x] 检查 `SKILL.md` 不包含宿主机路径、密钥或可伪造身份字段。
- [x] 运行 `pytest backend/tests/test_runtime_paths.py backend/tests/test_skills_loader.py -q`。

## Task 2: Build the external API and SSE compatibility adapter

**Files:**
- Create: `D:/Work/kechuang/backend/api/v1/execution/deerflow_adapter.py`
- Create: `D:/Work/kechuang/backend/runtime/deerflow_gateway.py`
- Modify: `D:/Work/kechuang/backend/api/v1/execution/supervisor.py`
- Modify: `D:/Work/kechuang/backend/runtime/config.py`
- Modify: `D:/Work/kechuang/backend/runtime/container.py`
- Modify: `D:/Work/kechuang/backend/runtime/bootstrap.py`
- Test: `D:/Work/kechuang/backend/tests/test_stream_protocol.py`
- Test: `D:/Work/kechuang/backend/tests/test_public_chat_result.py`
- Test: `D:/Work/kechuang/backend/tests/test_deerflow_adapter.py`
- Test: `D:/Work/kechuang/backend/tests/test_deerflow_gateway.py`

**Interfaces:**
- Consumes: 现有 ChatRequest、DeerFlow StreamEvent。
- Produces: 现有 `start/progress/token/done/error` SSE 输出，不改变前端请求字段。

- [x] 将外部请求转换为 DeerFlow Agent 输入，保留 `message`、`intent_hint`、`intent_confirmation`、`flow_action`、`draft_context` 和 `thread_id`。
- [x] 将 DeerFlow `values`、`messages-tuple`、`custom`、`end` 转成当前五类 SSE 事件；同时兼容 Gateway 原生 `messages` 事件名。
- [x] `done.result` 保留 `answer`、`intent`、`match_results`、`ui_blocks`、`actions`、`thread_id` 和 `status`。
- [x] 异常在流已建立时通过 `error` 事件返回，不把内部堆栈发送给前端。
- [x] 源码恢复后重新运行适配器/Gateway 测试和既有流协议测试；后端全量回归为 `343 passed`，本地 focused DeerFlow 回归为 `19 passed`。PaddleOCR 注入参数问题已修复。

> 当前实现是可回退的可选通道：只有同时配置 `DEERFLOW_ENABLED=true` 和 `DEERFLOW_BASE_URL` 才会走 OPIEos Gateway；本机 `backend/.env.local` 已开启 DeerFlow/MCP 供测试，缺少这两个变量时仍回退旧 Supervisor。

## Task 3: Preserve identity, Thread ownership, and ID compatibility

**Files:**
- Modify: `D:/Work/kechuang/backend/api/v1/deps.py`
- Create: `D:/Work/kechuang/backend/runtime/thread_identity.py`
- Modify: `D:/Work/kechuang/backend/api/v1/chat/threads.py`
- Test: `D:/Work/kechuang/backend/tests/test_stream_auth_contract.py`
- Test: `D:/Work/kechuang/backend/tests/test_chat_material_session.py`

**Interfaces:**
- Consumes: Bearer Token、外部 `thread_id`、当前会话归属校验。
- Produces: DeerFlow 可接受的内部身份和 Thread 上下文；外部仍使用原 `thread_id`。

- [x] 从 Token 解析身份，忽略请求体、查询参数和表单中的 `user_id`。
- [x] 复用当前 `thread_id`；长度或字符不符合 DeerFlow 内部规则时建立稳定映射。
- [x] 继续校验当前用户是否拥有目标会话、草稿和附件；DeerFlow 分支也登记会话、持久化用户/助手消息并拒绝跨用户 Thread。
- [x] 运行 `pytest backend/tests/test_stream_auth_contract.py backend/tests/test_chat_material_session.py -q`，并补充 DeerFlow 跨用户 Thread 回归测试。

## Task 4: Define controlled Tool/MCP contracts

**Files:**
- Modify: `D:/Work/kechuang/backend/core/contracts/tools.py`
- Modify: `D:/Work/kechuang/backend/runtime/tool_adapters/adapters.py`
- Test: `D:/Work/kechuang/backend/tests/test_runtime_evidence.py`

**Interfaces:**
- Produces: `kc_retrieval`、`kc_match`、`kc_document`、`kc_draft`、`kc_customer`、`kc_profile` 的稳定输入/输出契约。通用上下文直接使用 DeerFlow Thread/消息历史，不新增 `kc_context` MCP；客服由小程序原生动作处理，不新增 `kc_contact` MCP。

Tool/MCP 合同任务与 Task 5A 的知识检索内部实现可以并行；并行 AI 不得同时修改同一合同、适配器、Gateway 或 Skill 文件。当前第一批 MCP 边界已由集成负责人接线，剩余能力仍由 Task 6 统一接线。

- [x] 所有当前 InProcess Tool 接收后端注入的用户和 Thread 上下文，不接受模型自带的身份权限作为可信来源。
- [x] 统一返回 `status`、`data`、`error`、`trace_id`；写操作保留业务版本、任务或同步状态字段。
- [x] Tool 内部复用现有业务服务，不在 Skill 或 Agent 中直接访问数据库、Milvus、MinIO。
- [x] 运行 `pytest backend/tests/test_runtime_evidence.py -q`；第一批可选 MCP 边界已接线，真实外部 OPIEos → MCP → 业务服务闭环尚未验证。

## Task 5: Migrate independent business capabilities

以下三个子任务可以由三个不同 AI 并行完成，分别只修改自己的业务目录和测试。

### Task 5A: Knowledge retrieval and matching

**Files:** `backend/runtime/mcp_server.py`, `backend/agents/retrieval_agent/**`, `backend/services/vector/**`, `backend/services/mysql/**`, `backend/tests/test_mcp_server.py`, `backend/tests/test_retrieval_*.py`, `backend/tests/test_next_match_batch.py`, `OPIEos-main/extensions_config.example.json`, `OPIEos-main/skills/custom/knowledge-search/SKILL.md`, `OPIEos-main/skills/custom/supply-demand-match/SKILL.md`

- [x] 通过 DeerFlow `kc_platform` 配置映射接入 `knowledge-search` 和 `supply-demand-match` Skill；Skill 不包含宿主机路径。
- [x] MCP 输入拒绝模型身份字段，身份/角色从已认证请求上下文解析；检索与匹配复用既有 Tool Adapter 和对象身份去重逻辑。
- [x] `kc_match` 保留两个方向、`top_k`、有限超量召回和 `exclude_result_ids` 换一批语义；`full` 模式的完整业务编排仍由后续业务闭环接入，不在 MCP 边界复制。
- [x] 运行 `pytest backend/tests/test_retrieval_defaults.py backend/tests/test_retrieval_quality_test.py backend/tests/test_next_match_batch.py -q`，结果 `23 passed`。
- [x] 启用真实 MCP 配置并完成 DeerFlow Gateway → Skill → MCP → Retrieval/Milvus/MySQL 端到端验证；本地 MySQL 业务用户令牌已通过 `kc_platform_kc_retrieval` 调用链。
- [x] 已完成 Kechuang Docker backend、独立业务 PostgreSQL 数据库、MinIO bucket、MCP `/mcp/` 生命周期和 Gateway → business-backend 网络连通验证；当前测试查询返回 `NO_EVIDENCE`，后续需要导入有效业务检索数据。

### Task 5B: Draft and lifecycle operations

**Files:** `backend/agents/supervisor_agent/**`, `backend/services/persistence/**`, `backend/api/v1/drafts/**`, `backend/runtime/mcp_tools/draft.py`, `backend/tests/test_draft_*.py`, `backend/tests/test_mcp_draft_tools.py`

- [x] 接入需求、方案、成果草稿 Skill。
- [x] 在 `kc_platform` 注册草稿创建、读取、版本化保存、发布、下架和逻辑删除工具。
- [x] 所有草稿更新保持字段增量、版本校验、发布/下架/删除权限和审计。
- [x] 运行 `pytest backend/tests/test_draft_type_selection.py backend/tests/test_draft_session_sync.py backend/tests/test_draft_publishing.py -q`。
- [x] 新增草稿 MCP 定向测试；真实 DeerFlow 会话中的写入/生命周期联调仍属于 Task 6。

### Task 5C: Document and attachment processing

**Files:** `backend/services/document_parsing/**`, `backend/services/storage/**`, `backend/api/v1/chat/routes.py`, `backend/runtime/mcp_tools/document.py`, `backend/tests/test_chat_material_*.py`, `backend/tests/test_document_*.py`, `backend/tests/test_mcp_document_tools.py`

- [x] 接入 `document-parse` 和 `field-extraction` Skill。
- [x] 通过 `kc_platform` 接入本人文档列表、详情、触发解析和有界解析结果读取；上传、原件及图片二进制继续使用既有 HTTP API。
- [x] 保持单文件 FormData、`attachment_mode`、MinIO 文件权限和五类 SSE 事件，并保留附件 `version_id`。
- [x] 运行 `pytest backend/tests/test_chat_material_session.py backend/tests/test_chat_material_limits.py backend/tests/test_document_access.py -q`。

### Task 5D: Enterprise/expert profiles and customer visits

**Files:** `backend/runtime/mcp_tools/customer_profile.py`, `backend/tests/test_mcp_customer_profile_tools.py`, `backend/runtime/mcp_server.py`, `backend/tests/test_mcp_server.py`, related `OPIEos-main/skills/custom/**`.

- [x] `kc_profile` 按内部 MySQL 查询/外部权限过滤检索企业和专家。
- [x] `kc_customer` 仅读取当前内部用户拜访记录；`kc_customer_save_visit` 只接受白名单字段，由后端注入身份与会话，并复用现有 MySQL、Milvus 和草稿确认服务。
- [x] Skill 映射工具名；验证外部用户拒绝、身份字段拒绝、幂等重试和 MCP 注册。
- [x] 合并定向回归：`57 passed`；真实 DeerFlow → MCP → 数据库写入联调未做。

### Task 5E: Contact action (no MCP)

- [x] Return `CONTACT_WECOM` for external contact requests and eligible match/no-result responses; internal users receive no customer-service prompt/action.
- [x] `/api/v1/me/contact` returns the action only, rejects internal users with `403`, and creates no pending handoff task.
- [x] Do not register `kc_contact`; the mini-program's native `open-type="contact"` button owns the actual WeChat customer-service navigation.

## Task 6: Integrate and verify without changing the frontend contract

**Files:**
- Baseline tests: `D:/Work/kechuang/backend/tests/test_stream_protocol.py`
- Baseline tests: `D:/Work/kechuang/backend/tests/test_stream_auth_contract.py`
- Baseline tests: `D:/Work/kechuang/backend/tests/test_parse_error_contract.py`
- Baseline tests: `D:/Work/kechuang/backend/tests/test_flow_contracts.py`

**Interfaces:**
- Consumes: Task 1-5 的已通过测试结果和变更清单。
- Produces: 可回退的 DeerFlow 接入链路和前端兼容证据。

- [x] 按顺序合并 Wave 1 工作流，发现共享文件冲突时由集成负责人解决，不让两个 AI 互相覆盖。
- [x] 运行流式、认证、材料解析、会话、草稿和流程契约测试；本次后端全量结果为 `386 passed`。
- [x] 本轮新增工具合并回归：`57 passed`；Ruff 检查通过。
- [x] DeerFlow 事件适配复用现有公共结果清洗与超时语义；请求字段、结果字段和错误语义的代码级回归通过（本轮合计 `44 passed`）。
- [ ] 对比 DeerFlow 链路和旧链路的相同输入，确认路径、请求字段、SSE 事件、响应字段和错误语义一致；本地已先完成最小真实 smoke（`8793 -> 8001 -> kc-main -> start/done`）。
- [x] 记录集成变更、测试结果、未完成依赖和回滚方式；不新增独立进度文件，避免与本计划重复维护状态。
- [x] 保留旧链路回退开关；未完成的能力不得从旧路由删除。

本轮仅运行代码级回归，没有启动 Gateway、业务 backend 或 Docker 服务。当前人工验收项：用本地有效业务数据验证检索/匹配，并以相同输入对比旧链路与 DeerFlow 的完整 SSE/响应；在此之前不切换 `DEERFLOW_ENABLED` 默认流量。服务器部署时再填真实 Gateway/MCP 地址和密钥，并验证双向网络及工具发现。

### Task 6A：本机单独启动与服务器接入准备（2026-09-23）

- [x] 确认当前测试由宿主机 `backend/python main.py` 前台运行，Docker 不启动业务 backend，终端保留业务日志。
- [x] `DEERFLOW_BASE_URL`、`KC_PLATFORM_MCP_URL` 和 `KC_MCP_ALLOWED_HOSTS` 改为运行时配置；本机值只作为测试默认值。
- [x] DeerFlow `kc_platform.url` 改为 `$KC_PLATFORM_MCP_URL`，Skill 不写 backend 主机路径。
- [x] host backend 调用 Gateway 前幂等创建线程，兼容当前 OPIEos 的线程权限约束；真实消息分片/values 事件已恢复为旧公共 SSE `done.result.answer`。
- [x] 本地真实问答 smoke 通过：`/api/v1/chat/stream` 返回 `start` 和 `done`，Gateway 日志确认请求进入 `kc-main`。
- [ ] 服务器迁移时填写真实 DeerFlow Gateway 地址、MCP 回调地址和运行时鉴权密钥，并重新完成双向网络和真实数据验证。

## Handoff Rules for Multiple AIs

每个 AI 接收任务时必须附带：

1. 本计划中对应的 Task 编号和允许修改的文件列表。
2. 禁止修改的共享文件列表。
3. 依赖的契约、Tool 返回结构和测试命令。
4. 完成后的变更文件、测试结果、未解决问题和回滚方式。

推荐分派顺序：先由一个 AI 完成 Task 0；再在独立工作树中并行分派 Task 1-5；最后由一个集成 AI 完成 Task 6。不要让多个 AI 同时修改 Gateway 路由、全局配置或同一个 Skill 文件。每个分派消息必须明确允许修改文件、禁止修改文件、测试命令、不得删除/覆盖文件，以及发现文件缺失时立即停止的规则。
