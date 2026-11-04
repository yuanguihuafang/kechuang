# 科创后端迁移链路与基础能力收口实施计划

> **状态（2026-09-24）：** 本文是历史执行计划，复选框仅记录当时的执行过程，不代表相关 OPIEos 改动仍在当前源码中。Task 3 的用户 Skill/仓库 Skill 共存与管理列表运行时改动，以及 Task 4 的代码注册/工具隔离改动，均不属于当前底座状态；OPIEos 标准存储对“用户已有个人 Skill 时仓库 `kc-*` 可能不可见”的限制仍未解决。业务 Backend 的链路修复与测试记录仍可参考。当前接入边界见 `docs/DeerFlow迁移与Skill接入方案.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已发现的 DeerFlow 迁移链路断点，并验证/补齐当前链路依赖的 trace、耗时、并发隔离、审计、回退、适配解耦和幂等行为，同时保持前端 API 与流程兼容。

**Architecture:** 科创后端继续通过现有 DeerFlow adapter 调用 `assistant_id=kechuang`；本机 OPIEos 使用标准文件式 Agent 存储加载配置和 SOUL，不依赖核心代码注册。上传解析复用现有解析器，并把提取资料安全地传入同一会话。DeerFlow 未启用或未配置时保留 Supervisor；两条路径共用兼容的结果整理、会话/审计持久化和错误语义。MCP 工具仍是 Gateway 运行时共享能力，不承诺 Agent 级隔离。

**Tech Stack:** Python 3.11/pytest、FastAPI/SSE、DeerFlow Harness/MCP、OPIEos Skill Storage、现有前端 React 卡片渲染。

**Spec:** `docs/superpowers/specs/2026-09-24-kechuang-backend-flow-hardening-design.md`

## Global Constraints

- 所有项目文件只在 `D:\Work\kechuang` 下修改，不触碰 `D:\Work\kc-rag-agent`。
- 保留当前脏工作区和用户已有改动；不 reset、不清理、不覆盖无关 hunk、不提交代码。
- 不改现有 `/api/v1/*` 路由和必需请求参数；现有响应字段保持兼容，新增字段须为可选且确有必要。
- 不删除旧 Supervisor；仅 DeerFlow 关闭或未配置时回退。DeerFlow 已启用但运行报错时按错误返回，禁止静默切旧路径。
- 不启动/重启科创、OPIEos、数据库、Milvus、MinIO 或其他用户服务；不修改 `.env.local`、数据库结构或依赖版本。
- 不调用真实外部模型；优先用 fake runner/storage 验证事件和持久化行为。
- 业务测试使用 `D:\Anaconda\envs\py311\python.exe`；OPIEos 测试优先用 `OPIEos-main\backend\.venv\Scripts\python.exe`，均关闭 pycache。

## Review Focus

1. 缺少 `draft_type` 时把供给找需求错误改成需求草稿 —— Task 1 的方向/动作回归测试。
2. SSE `error` 被忽略、无终止事件仍成功 —— Task 1 的事件终止测试。
3. 上传材料中的提示文本被当作 Agent 指令，或上传线程与聊天线程不一致 —— Task 6 的上下文安全和 thread continuity 测试。
4. 同一用户/会话并发或同幂等键重试造成串数据、重复副作用 —— Task 5 的重复请求与并发测试，Task 6 的会话隔离测试。
5. 用户已有个人 Skill 后全局 `kc-*` 可能消失、共享 Skill 的只读 UI 未覆盖 —— OPIEos 标准行为限制，Task 3 旧运行时改动已撤回；当前缺口见状态说明，不能视为通过项。

## Files and Ownership

- Task 1：`backend/api/v1/execution/deerflow_adapter.py`、`backend/agents/supervisor_agent/records/response_contract.py`；仅编辑 `backend/tests/test_deerflow_adapter.py` 与 `backend/tests/test_deerflow_selection.py`。
- Task 2：`backend/api/v1/execution/supervisor.py`；新建 `backend/tests/test_deerflow_completion_persistence.py`。
- Task 3（历史实现，当前已撤回）：当时计划修改 OPIEos Skill storage/router；不得据此认定用户级 Skill 与仓库 Skill 当前可以并存。
- Task 4（历史实现，当前已撤回）：当时计划代码注册 Agent 并添加工具 allowlist；当前改用标准文件式 Agent，MCP 仍由 Gateway 共享。
- Task 5：`backend/runtime/mcp_tools/draft.py`、`backend/runtime/mcp_tools/customer_profile.py` 及必要的现有 business-record service；仅编辑 `backend/tests/test_mcp_draft_tools.py`、`backend/tests/test_mcp_customer_profile_tools.py`。
- Task 6（协调者）：`backend/api/v1/chat/routes.py`、必要时 `frontend/src/pages/supervisor/BusinessBlocks.jsx`；新建独立 `backend/tests/test_deerflow_material_flow.py`，检查当前快速入口、已发布草稿上下文、匹配卡片契约、Milvus/MinIO 业务配置读取及会话隔离。除发现明确缺陷外不改其他模块。

并行边界：Task 1/2/3/4/5/6 文件集合互不重叠；Task 6 负责跨任务接口整合。任何 agent 开始前须查看自己要改文件的现有 diff，只修改任务相关 hunk。单元测试对已有实现已通过时不为“制造红灯”去回退用户代码；先判断当前测试是否真的覆盖该验收点，再补最小测试。

## Task 1: DeerFlow 事件与匹配方向契约

**Owner:** Agent A

**Files:** 上述 Task 1 所列 adapter、response contract 和两个 backend 测试文件。

- [x] 添加 SSE 上游 `error` 事件回归用例，断言输出公开 `error` 且状态失败。
- [x] 添加流缺少 `end` 的回归用例，断言不产生成功 `done`；随后实现最小失败事件转换。
- [x] 添加 `supply_to_demand` 且输入无 `draft_type` 的用例，断言不回退成 `demand` 草稿；若方向与目标草稿都明确，断言“立即补充”类型正确；类型不足时断言不提供误导性动作。
- [x] 对相反匹配方向和明确草稿类型添加对照断言，确保现有需求方流程不回归。
- [x] 相关适配/选择测试通过（132 项集成回归中包含）。

## Task 2: DeerFlow 完成结果、审计与耗时

**Owner:** Agent B

**Files:** 上述 Task 2 所列文件。

- [x] 用 fake runner、session store、match store 和 audit sink 添加成功、失败、存储异常三种完成路径测试。
- [x] 断言 DeerFlow 成功时会话、匹配结果及审计摘要各写一次，`trace_id`/`thread_id`/user 归属一致；session/audit 写入失败不抹掉已生成答案，但产生带 trace 的日志。
- [x] 断言 `processing_elapsed_ms` 是完整运行总耗时，检索和首 token 时间不被混作总耗时；不改变已有前端字段语义。
- [x] 在共用的结果完成位置实现最小持久化收口，防止 DeerFlow 和旧 Supervisor 路径重复写入同一结果。
- [x] 相关完成持久化、trace 和 elapsed 回归通过（全量业务后端 483 项通过）。

## Task 3: 全局 Skill 与用户 Skill 共存（历史改动已撤回）

> 以下勾选项保留早期实现记录；当前 OPIEos 标准存储仍有用户个人 Skill 覆盖仓库共享 Skill 的限制，不应作为已验收能力。

**Owner:** Agent C

**Files:** 上述 Task 3 所列 OPIE Skill storage、router 和测试文件。

- [x] 构造“仓库有 `kc-*`、用户目录另有 Custom Skill”的存储测试，先复现仓库 Skill 消失。
- [x] 保留共享 Skills 的 `LEGACY`/只读语义，让 discovery 同时包含仓库共享 Skills 与用户个人 Skills，并继续尊重各自启用状态。
- [x] 为 `/skills/custom` 增加共享 Skill 可见、`editable=false` 的路由测试；共享 Skill 的 PUT/DELETE 仍拒绝，个人 Custom Skill 的编辑行为不变。
- [x] 更新当前 13 个科创 Skill 的清单断言，删除已不存在 Skill 的旧期望；不要把 Skill 数量写死到运行逻辑里。
- [x] Skill 存储、路由和 Agent 解析回归通过。

## Task 4: kechuang Agent 工具可见性与公开输出（旧代码隔离方案已撤回）

> 以下 Agent 注册与工具 allowlist 勾选项仅代表曾实施过的旧方案，相关 OPIEos 核心改动已撤回。当前 Agent 通过标准文件式存储加载，MCP 工具不保证按 Agent 隔离。

**Owner:** Agent D

**Files:** 上述 Task 4 所列 OPIE Agent runtime/配置和新回归测试；不得改 Skill storage/router。

- [x] 追踪代码注册 `kechuang` Agent 的工具 allowlist、扩展配置及 thread/run 运行时解析，添加测试证明授权后的 `kc_platform` 工具在 Agent 请求中可见；未授权用户仍不可调用。
- [x] 添加 Agent/runtime 回归测试，确认已授权的科创对话能看到 `kc_match`、`kc_retrieval` 等实际启用工具；未授权请求仍受原有身份与 allowlist 限制。
- [x] 检查公开流只输出面向用户的 token/progress/done/error，不暴露专用 reasoning channel 或工具内部诊断；最小更新科创 Agent/匹配 Skill 指令，使其在工具可用时直接调用并给简洁业务结论，不把编排判断过程或“工具未提供”内部诊断当答复。
- [x] 修复只在测试证实 runtime 过滤/流转换导致工具不可见或内部内容泄漏时进行；不新增 Agent API，不改变其他项目 Agent 的 allowlist。
- [x] 科创 Agent、认证、工具授权与 Skill 路由定向套件：47 项通过。

## Task 5: MCP 副作用、重试幂等与并发

**Owner:** Agent E

**Files:** 上述 Task 5 所列工具/service 与两份 MCP 测试文件。

- [ ] 检查草稿发布与客户拜访保存已有 `idempotency_key` 的传递、持久化和重试结果，补同 key 重试测试，确保返回既有结果而不创建第二份业务记录。
- [ ] 补同一会话/同 key 并发执行测试；若存在重复写竞态，仅使用当前存储层已提供的原子能力收口，不新增 schema、外部锁服务或公共请求字段。
- [ ] 持久化已发布草稿的 publish 幂等键及结果，使相同 key 重试返回原成功响应；目前已发布重试仍可能返回状态冲突（不会重复发布副作用）。
- [ ] 跨进程/多 worker 的“同一会话/同 key”原子去重尚未保证；当前客户拜访并发保护为进程内 single-flight，补充业务 MySQL 现有 user/thread 更新行为测试后再评估是否需要数据库原子锁/唯一约束。
- [x] 验证无 idempotency key 的草稿创建仍按现有语义运行，MCP annotations 不声称并不存在的幂等保证。
- [x] 为幂等冲突/存储失败保留可定位的 `trace_id` 和稳定错误返回，不泄露数据库细节。
- [x] MCP 草稿、客户拜访与发布测试通过（业务后端全量测试覆盖）。

## Task 6: 上传统一编排与端到端兼容（协调者）

**Owner:** 协调者；等待 Task 1/2 的事件及持久化接口稳定后完成整合。

**Files:** `backend/api/v1/chat/routes.py`、新建 `backend/tests/test_deerflow_material_flow.py`；仅在已有卡片 renderer 确实不兼容时最小修改 `frontend/src/pages/supervisor/BusinessBlocks.jsx`。

- [x] 用 fake parser/runner 覆盖 DeerFlow 启用时上传解析进入同一 Agent、同一用户 `thread_id`，并确认解析摘要作为非可信资料上下文继续对话。
- [x] 覆盖 DeerFlow 关闭/未配置时旧 Supervisor 仍能处理上传；覆盖 DeerFlow 已启用但 runner 失败时返回错误而不静默 fallback。
- [x] 覆盖流式/非流式解析返回相同业务结果、保留快速入口既有流程、发布草稿后普通追问恢复草稿方向/内容，并避免重复写入历史。
- [x] 验证匹配结果字段满足既有卡片组件所需结构；既有 renderer 保持不变，仅修后端 normalization。
- [x] 只读核对 `.env.local` 的加载入口与 Milvus/MinIO 运行配置选择；相关配置读取测试通过，未读取或编辑 `.env.local`。
- [x] 审查同用户不同 thread 与不同用户相同 thread 请求的隔离，补复现测试；未引入全局串行锁。
- [x] 相关上传、会话、流式认证及存储一致性回归通过（全量业务后端 483 项通过）。

## Final Verification

- [x] 检查改动范围；未编辑 `D:\Work\kc-rag-agent`、`.env.local`、数据库 schema 或无关前端代码。
- [x] 业务后端全量：483 passed。
- [x] OPIEos 科创 Agent/Skill 定向套件：47 passed。
- [x] OPIEos 改动文件 Ruff、业务后端改动文件 Ruff、`git diff --check` 均通过。
- [x] 未修改前端 renderer，因此未运行前端构建。
- [ ] 真实 MySQL/Milvus/MinIO/DeerFlow 服务流尚未端到端验证。OPIEos 全量测试在约 6% 处无输出超过一分钟，本轮已中断；只报告通过的定向测试。
