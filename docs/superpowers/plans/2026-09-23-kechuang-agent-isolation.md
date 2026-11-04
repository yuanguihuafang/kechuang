# 科创专属 Agent 实施计划

> **状态（2026-09-24）：已撤销，不要继续执行。** 本计划的代码注册、内部认证特判和 Agent 级工具/Skill 隔离不再是当前方案。本机 OPIEos 仅保留合法化认证 Header 的最小源码修改，`kechuang` 使用标准文件式 Agent 存储，MCP 工具仍是全局共享；服务器接入需由维护方按现有能力部署，状态见 `docs/DeerFlow迁移与Skill接入方案.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task by task. Steps use checkbox syntax for tracking.

**Goal:** 在 OPIEos/DeerFlow 中实现仅供科创业务后端使用的代码注册 Agent `kechuang`，以该 Agent 的 Skills/MCP 执行业务功能；DeerFlow 关闭或未配置时保留旧 Supervisor 回退，前端 API/SSE 不变。

**Architecture:** OPIEos Gateway 内注册私有 Agent，并在 Agent 选择、Skill 发现、工具 schema 暴露和实际调用处做强制隔离。科创后端可信内部请求固定选择该 Agent。进度流由现有适配器输出安全摘要。旧 Supervisor 只用于 DeerFlow 未启用/未配置的情形；已进入 DeerFlow 后失败则向调用方返回错误，不自动重放到旧路径。

**Tech Stack:** Python 3.12、FastAPI、LangGraph/DeerFlow harness、MCP、pytest、现有 SSE 适配。

**Spec:** `docs/superpowers/specs/2026-09-23-kechuang-agent-isolation-design.md`

## Global Constraints

- 所有产品文件仅改 `D:\Work\kechuang`；不访问或改动 `D:\Work\kc-rag-agent`。
- 当前工作区已有大量用户改动。实现前逐文件检查现有 diff，只做最小增量，不覆盖、不清理、不提交。
- 不启动 Docker、Gateway、业务后端或其他服务；由用户自行启动。前端界面和 API 契约不改。
- Agent 身份固定为 `kechuang`；所有科创 Skill ID（包括联网搜索）使用 `kc-` 前缀。不得增加旧 Agent 名称或 Skill ID 的兼容别名。
- `agents/kechuang/SOUL.md` 承载主 Agent 常驻指令，不作为 Skill 暴露；其余 15 个科创业务 Skills 按需发现/加载。联网搜索使用用户自有 `kc-web-search` Skill 和业务后端 MCP 工具 `kc_web_search` / `kc_web_fetch`，复用现有 Tavily 客户端；科创 Agent 不调用 DeerFlow 内置联网工具。
- 科创 MCP 精确 allowlist：`kc_retrieval`、`kc_match`、`kc_draft_create`、`kc_draft_get`、`kc_draft_list`、`kc_draft_save`、`kc_draft_publish`、`kc_draft_unpublish`、`kc_draft_delete`、`kc_document_list`、`kc_document_detail`、`kc_document_parse_result`、`kc_document_parse`、`kc_profile`、`kc_customer`、`kc_customer_save_visit`、`kc_web_search`、`kc_web_fetch`。连接 ID 保持 `kc_platform`，模型可见工具名不加服务器前缀；冲突时启动/解析应失败，不能静默覆盖。
- `kc_*` 工具只对 `kechuang` 可见和可执行；其他 Agent 不得获得科创 MCP 工具，且其自身 DeerFlow 原生联网能力保持不变。
- 隔离必须同时约束模型可见 schema 和执行路径；`allowed-tools` 文本、提示词及 Skill 前缀不是安全边界。科创业务后端仍负责用户、租户和数据授权。
- SSE 只保留 `start`、`progress`、`token`、`done`、`error`。禁止透传隐藏推理、提示词、原始状态快照、未筛选内部事件及敏感工具参数/结果。

## Implementation Tasks

### Task 1: 为业务搜索注册自有联网搜索 MCP

**Files:** `backend/services/web_search/tavily.py`、`backend/runtime/mcp_server.py`、`backend/runtime/mcp_tools/web_search.py`（新增）、`backend/tests/test_tavily_search.py`、`backend/tests/test_mcp_web_search.py`（新增）。

- [x] 为现有 Tavily 适配器添加 Extract 调用，复用既有配置、超时和错误处理约定；限制为单 URL，要求 `https`/`http`，拒绝 localhost 和字面私有/保留 IP，并限制输入/返回长度；对无效 URL、未配置、上游失败返回稳定错误结果。
- [x] 在 `kc_platform` MCP 注册 `kc_web_search` 和 `kc_web_fetch`，从认证请求解析身份/trace 上下文，不从模型工具参数接受身份；搜索与页面提取均使用现有 Tavily 客户端。
- [x] 增加窄测试覆盖实际 Tavily Search/Extract 请求和 MCP 注册/调用输出；先确认失败，再实现。不要改 DeerFlow 全局 `web_search` / `web_fetch` 实现。

**Acceptance:** 业务后端 MCP 可通过自己的已配置 Tavily 服务搜索并提取网页，工具名为 `kc_web_search` / `kc_web_fetch`，没有 DeerFlow 搜索工具依赖。

### Task 2: 统一科创 Skill 标识并配置 MCP 名称

**Files:** `OPIEos-main/skills/custom/*/SKILL.md`、对应 Skill 目录、`OPIEos-main/extensions_config.example.json`、本机运行配置中 `kc_platform` 对象、`OPIEos-main/backend/tests/test_kechuang_skill_manifest.py`（新增）。

- [x] 将 15 个科创业务 Skill 目录和 frontmatter ID 统一改为 `kc-` 前缀（包含 `web-search` → `kc-web-search`）；保留各文件现有用户修改及正文，只更新必要的 ID、工具依赖和相互引用。
- [x] 将 `kc-web-search` Skill 的工具依赖改为 `kc_web_search` / `kc_web_fetch`，并将常驻编排指令迁移到 `agents/kechuang/SOUL.md`；客服保持业务响应动作。
- [x] 为 `kc_platform` 关闭 `tool_name_prefix`，保留连接 ID、现有认证上下文映射与其他配置；同步示例配置。不要删除或改写用户 Skill 的搜索策略。
- [x] 增加回归测试，确认运行时可发现 15 个业务 Skill，且 `kechuang` 能加载源码专属 SOUL；先运行测试确认旧状态下失败，再实施变更并复跑。

**Acceptance:** Skill loader 能发现预期 `kc-` Skill；`kc-web-search` 只引用已注册的业务搜索 MCP；MCP 以无前缀 `kc_*` 名称注册且无冲突。

### Task 3: 注册私有 Agent 并强制执行运行时隔离

**Files:** `OPIEos-main/backend/app/gateway/services.py`、`routers/threads.py`、`routers/thread_runs.py`、`routers/agents.py`、`routers/admin_agents.py`、`routers/assistants_compat.py`、`packages/harness/deerflow/config/agents_config.py`、`packages/harness/deerflow/agents/lead_agent/agent.py`、`tests/test_internal_auth.py`、`tests/test_agent_resolution.py`、`tests/test_custom_agent.py`、`tests/test_authorization_tool_filter.py`、`tests/test_authorization_enforcement.py`。

- [x] 先为可信内部身份、私有 Agent 解析、禁止公开枚举/CRUD/伪造身份、线程 Agent 身份固定、其他 Agent 不受影响添加失败测试。
- [x] 通过 OPIEos 代码注册 `assistant_id=kechuang`；只接受现有可信科创后端内部认证路径，未认证、普通用户、未知 Agent、配置缺失或用户创建同名 Agent 均 fail closed。该 Agent 不加入公开列表或管理 API。
- [x] 新线程创建时固定写入 Agent 身份，后续 run 从线程绑定身份解析，不接受普通调用方切换；已有默认/自定义 Agent 行为保持不变。
- [x] 为 `kechuang` 限制 Skills 到明确的 15 个 `kc-` 业务 Skill 清单，并把 `kc-orchestrator` 正文迁入 `agents/kechuang/SOUL.md`、移除旧 Skill 入口；其他业务 Skills 继续按需加载。
- [x] 限制 `kechuang` 工具为精确科创 MCP allowlist；包括 `kc_web_search` / `kc_web_fetch`，排除 DeerFlow 内置 `web_search` / `web_fetch`。默认和其他 Agent 均排除全部 `kc_*` MCP 工具，但原有工具集（含 DeerFlow 搜索）不变；未允许工具既不进入模型 schema，也不能通过直接/伪造工具调用执行。
- [x] 运行 Gateway 私有 Agent、认证、授权过滤相关的定向 pytest，并检查 Agent 列表/API 与线程创建边界。

**Acceptance:** 可信后端能运行私有 Agent；普通请求不能创建、枚举、选择或调用它；Agent schema 与执行端的 allowlist 一致；其他 Agent 不获得科创能力。

### Task 4: 科创后端固定调用 Agent 并保留旧路径

**Files:** `backend/runtime/deerflow_gateway.py`、`backend/runtime/config.py`、`backend/api/v1/execution/supervisor.py`、`backend/tests/test_deerflow_gateway.py`、`backend/tests/test_deerflow_selection.py`（新增）。

- [x] 先添加/调整测试：DeerFlow 已启用时线程创建和 run 都固定携带 `assistant_id=kechuang`；调用方不能覆盖；关闭或未配置时走旧 Supervisor；DeerFlow 已选中但请求失败时不自动重试 Supervisor。测试放入 `test_deerflow_gateway.py` 与 `test_deerflow_selection.py`，不改 Task 5 所属的 `test_stream_auth_contract.py`。
- [x] 在现有可信内部请求中固定 Agent 身份，不改变前端请求/响应契约与现有用户认证上下文。
- [x] 保留旧 Supervisor 实现及其确有用途的会话存储；只把路径选择条件限定为 DeerFlow 未启用或未配置。
- [x] 运行相关 Gateway、配置、回退测试；不得启动服务验证。

**Acceptance:** 两条路径的选择确定且有测试保护；进入 DeerFlow 后发生错误只返回既有错误事件/响应，不产生重复执行。

### Task 5: 过滤 DeerFlow 流事件并保持 SSE 契约

**Files:** `backend/api/v1/execution/deerflow_adapter.py`、`backend/tests/test_deerflow_adapter.py`、`backend/tests/test_stream_auth_contract.py`。

- [x] 先添加事件适配测试，覆盖五种既有 SSE 类型、允许的安全阶段摘要、未知内部事件丢弃，以及原始推理/`values`/敏感参数不泄漏。
- [x] 将阶段进度转换为固定或白名单的面向用户摘要；最终回答仍走 `token`，完成/失败仍走原 `done` / `error`，不改变字段契约。
- [x] 运行适配器与 SSE 契约定向 pytest。

**Acceptance:** 前端只收到五类事件和安全摘要；适配器不把 DeerFlow 内部协议或原始隐藏推理透传出去。

### Task 6: 同步说明并完成静态验收

**Files:** `docs/DeerFlow迁移与Skill接入方案.md`、`docs/OPIEos-main修改记录.md`、`OPIEos-main/README.md`、`OPIEos-main/backend/AGENTS.md`、`OPIEos-main/backend/app/gateway/AGENTS.md`、`OPIEos-main/backend/packages/harness/deerflow/agents/AGENTS.md`、`OPIEos-main/backend/packages/harness/deerflow/skills/AGENTS.md`。

- [x] 记录 `kechuang` 私有 Agent 的内部调用方式、精确能力隔离、Skill/MCP 命名、用户自有联网搜索 Skill 与 Tavily Search/Extract 配置、DeerFlow 开关与旧 Supervisor 回退条件、服务器接入所需配置及安全流式边界；说明 DeerFlow 原生搜索仍只供其他 Agent 使用。
- [x] 仅补充本次需要的 OPIEos README/模块指南，不重写无关源码文档；核对不出现过时 Agent/Skill 名称或已撤销的 MCP 假设。
- [x] 使用 OPIEos 仓库 `.venv` 与 Conda `py311` 分别运行定向测试；改动 Python 文件的 Ruff lint 已通过。格式检查仅报告基线文件 `services.py` 和 `agent.py` 中原有两处 DEBUG/`is_bootstrap` 格式，不改写无关代码；已运行 `git diff --check` 和 `git status --short`。
- [x] 已记录未验证的真实运行项；未由实现者启动本机服务，未提交代码。用户启动的 `8001` Gateway 进程早于 OPIEos 源码修改，当前 Agent/MCP 运行验收需用户重启后继续。

## Subagent Work Boundaries

由主协调者按下列互不重叠的文件边界分派子智能体，并在每项后单独审查。实现切片按互斥文件集合并行推进，主协调者随后统一集成与复审；共享工作区中的其他用户改动保持不动：

1. **业务 web-search MCP**：只改 Task 1 文件与对应搜索/MCP 测试。
2. **Skill/config**：只改 Task 2 文件与对应清单测试。
3. **OPIEos Agent/security**：只改 Task 3 Gateway/harness 文件与对应测试。
4. **业务 backend/SSE**：只改 Task 4、Task 5 文件与对应测试。

四个实现切片使用本规格已定的名称和 allowlist，不改对方负责文件。主协调者负责核对已有 dirty diff、解决集成问题、更新 Task 6 文档并跑最终定向验证；如出现跨任务文件冲突，暂停后串行整合，不覆盖用户改动。

**本轮验证汇总:** business backend focused tests `109 passed`; OPIEos private-agent/Auth/Skills focused suite `146 passed, 3 deselected`（排除基线已知的 3 个失败用例）；Ruff lint passes。OPIEos format check 仅因 baseline `services.py` / `agent.py` 既有格式不通过。当前本机认证问答可通过 SSE，但用户的 `8001` Gateway 进程启动于 OPIEos 源码修改之前，返回旧通用 Agent；专属 Agent/MCP 与真实检索命中仍需重启 Gateway 后验收。

## Review Focus

- 普通用户是否能通过请求体、线程元数据或管理接口伪造/切换到私有 Agent？
- 未允许的 MCP 工具是否能在模型 schema 中被发现，或绕过 schema 直接执行？
- 默认及其他 Agent 的工具、Skill、提示词和行为是否保持原样？
- DeerFlow 未配置时是否仍可走旧 Supervisor；启用后失败是否避免自动重复执行？
- SSE 是否严格维持五类既有事件并屏蔽原始推理和内部状态？
- 是否意外覆盖/删除了用户已有工作区改动，或触及 `D:\Work\kc-rag-agent`？
