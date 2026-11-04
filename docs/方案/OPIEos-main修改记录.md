# OPIEos 修改记录

> 本文按日期记录历史实现。`OPIEos-main`、`skills/custom` 和旧的共享 Agent 路径仅在对应历史条目中保留；当前源码目录为 `OPIEos/`，运行数据/Skill 加载路径需以目标进程的 `DEER_FLOW_HOME`、`skills.path` 和容器挂载核对。本轮 Backend 审计未修改 OPIEos 源码。

## 2026-09-28：科创 MCP 连接标识统一为 kechuang

- OPIEos 的 `extensions_config.example.json`、本机运行时 `extensions_config.json` 和科创 Backend 的 FastMCP 服务名统一使用 `kechuang`；科创 MCP 工具仍保留原始 `kc_*` 名称。
- `KC_PLATFORM_MCP_URL` 继续使用；当前 MCP 请求只映射 `X-KC-Thread-ID`、`X-Trace-ID`，不映射 `Authorization`。实际运行配置受 Git 忽略，服务器迁移时须在服务器自己的 `extensions_config.json` 单独同步连接键。
- 同步更新文档索引和迁移说明；未运行测试、未启动服务。

## 2026-09-28：kechuang Agent 注册脚本验证

- `register_translator.py` 将 Agent 文件复制到当前用户目录后，通过文件存在性验证复制结果，不请求不存在的 `/api/agents/private` 路由，也不为脚本验证开启 Agent 管理 API。
- 每次运行均同步源目录文件；登录用户名在运行时输入，密码使用隐藏输入，不在脚本中保存明文凭据。

## 2026-09-28：共享技能与阶段计时

- 13 个科创业务技能改放 `skills/public/kc-*/SKILL.md`，仍用 Agent 白名单按需发现；无需修改原生技能存储器。有/无个人技能的原生加载回归均通过。同名个人 Skill/Agent 仍可覆盖共享版本，部署需按实际用户核查。
- Gateway 增加 `app/gateway/run_timing.py`，`services.py` 为 run 接入模型/工具开始、结束及错误回调。日志 `opie_stage_timing` 包含 run ID、阶段、名称、毫秒耗时及结果，不记录提示词、工具输入或输出。
- SOUL 统一项目业务范围、快速入口优先、无过程旁白、检索自然短答和结构化卡片边界；科创 Backend 的旧问答图已移除，不是 OPIE 内核新增另一套编排。
- 资产发布与验证以[迁移方案 §19](DeerFlow迁移与Skill接入方案.md#19-本机测试与服务器迁移配置)为准；共享 Skill 用 public 目录，更新 SOUL/config 后同步到实际 DEER_FLOW_HOME。未启动或重启业务进程，代码检查不是服务器在线验收。
- 以下日期条目为历史记录，其中 custom 目录与旧入口行为不再代表当前版本。

## 2026-09-25：pnpm 补丁与内部认证 401 修复

- `OPIEos-main/frontend/package.json`：移除 pnpm 11 不再读取的 `pnpm.patchedDependencies`；在 `frontend/pnpm-workspace.yaml` 声明 `next-themes@0.4.6` 补丁映射，并从 `frontend/` 执行 `pnpm install` 更新 `pnpm-lock.yaml`。保留 `frontend/patches/next-themes@0.4.6.patch`。
- `OPIEos-main/backend/app/gateway/internal_auth.py`：将内部 Token 与 Owner Header 常量改为合法 HTTP Header 名 `X-OPIE-OS-Internal-Token`、`X-OPIE-OS-Owner-User-Id`，与科创 Backend 现有请求一致。401 根因是 Gateway 重迁后仍读取带空格的旧 Header 名；已确认双方 Token 配置相同。
- 同步更新 Gateway 应用层引用注释、`backend/docs/API.md`、`backend/docs/AUTH_DESIGN.md`，并新增 Header 名合法性回归测试。
- 验证：OPIEos `test_internal_auth.py` 与 `test_auth_middleware.py` 共 68 项通过；科创 Backend `test_deerflow_gateway.py` 4 项通过；`pnpm install` 成功，锁文件含补丁 hash，弃用警告消失。另有 3 项无关 deprecated subdependencies 提示。
- 边界：未修改 `backend/packages/harness/deerflow/**`、CSRF 中间件、科创 API 或其前端传参。未重启 Gateway；应用新 Header 常量并验证真实对话前，需要重启 OPIEos Gateway。

## 2026-09-24 状态更正

- 本文以下旧条目记录此前阶段的历史变更；当前源码状态以本节及文末“本机接入修复”记录为准。
- OPIEos/DeerFlow 核心仍保持标准 Agent 加载方式；HTTP 内部认证使用合法连字符 Header。本次 403 由科创 Backend 适配 OPIEos 现有双提交 CSRF 校验解决，不修改 OPIEos CSRF 核心；没有加入科创 Agent 注册或工具过滤逻辑。
- 科创 Agent 源资产为 `agents/kechuang/config.yaml` 与 `agents/kechuang/SOUL.md`；本机文件存储运行目录已安装该 Agent，并将 13 个 `kc-*` Skill 设为 Agent 白名单。MCP 工具仍按 OPIEos 标准运行时工具集提供，不声称按 Agent 隔离。
- 科创 Backend 到 DeerFlow 的 HTTP 客户端已关闭环境代理继承，避免本机回环地址请求被系统代理接管。
- 本机 OPIEos 源码的 Header 修复不等于服务器现有版本已修复；服务器维护方仍需应用相同的合法 Header 约定并把 Agent 资产放入服务器实际 `DEER_FLOW_HOME`，或用该部署支持的 Agent 管理方式注册。
- CSRF 双提交适配随科创 Backend 部署，不要求服务器维护方修改 OPIEos CSRF 核心；服务器仍须使用与 Backend 一致的合法内部认证 Header。

## 记录范围

- 项目目录：`D:/Work/kechuang/OPIEos-main`
- 记录日期：2026-09-24（本轮修改与测试）
- 状态：本文既包含较早阶段记录，也包含当前未提交变更；各文件是否仍有工作区差异以对应日期的状态说明和 Git 状态为准。
- 本记录只描述 OPIEos/DeerFlow 底座的修改，不包含 `D:/Work/kc-rag-agent`，也不包含科创业务 Backend 的独立修改。
- 本文件存放在 `D:/Work/kechuang/docs`，不属于 OPIEos-main 源码目录。

## 修改目的

让 OPIEos-main 作为 DeerFlow 底座接入科创业务 Backend，同时保留底座原有能力。改动集中在认证协议、MCP 扩展映射、Docker 启动配置、Skill 接入和必要的测试/文档同步，没有改动科创业务接口设计。

## 代码与认证修改

### 内部认证 Header 修复

原内部认证 Header 名包含空格，`httpx` 无法发送。已统一改为合法的 HTTP Header 名：

- `X-OPIE-OS-Internal-Token`
- `X-OPIE-OS-Owner-User-Id`

涉及文件：

- `backend/app/gateway/internal_auth.py`：更新 Header 常量和内部认证定义。
- `backend/app/gateway/authz.py`、`backend/app/gateway/services.py`：只同步注释中的 Header 名，没有修改授权逻辑。
- `backend/tests/test_internal_auth.py`：增加两个 Header 均为合法 HTTP token 的断言。

本次不修改 `backend/app/gateway/csrf_middleware.py` 或其测试；CSRF 适配放在科创 Backend 的 Gateway HTTP 客户端，发送匹配的随机 Cookie/Header pair，真实身份仍由内部 Token 验证。

### 早期方案：科创私有 Agent 注册与隔离（已撤回）

- `backend/packages/harness/deerflow/config/agents_config.py`：在代码中固定注册 `kechuang` 身份；15 个业务 Skill 和 MCP 工具清单由代码维护，Agent 常驻指令从源码内 `agents/kechuang/SOUL.md` 读取。
- `backend/app/gateway/services.py`、`backend/app/gateway/routers/threads.py`：只依据校验后的 `auth_source=internal` 接受私有 Agent；在线程创建时绑定 `assistant_id=kechuang`，后续 run、branch 不允许用 body/context/config 改绑。内部认证状态由 Gateway 写入运行上下文，不信任调用方传入的同名字段。
- `backend/app/gateway/routers/agents.py`、`admin_agents.py`、`assistants_compat.py`：普通 Agent 列表隐藏 `kechuang`，并阻止通用 API 创建、读取、修改或删除同名用户 Agent。
- `backend/packages/harness/deerflow/agents/lead_agent/agent.py`：在工具 schema 和执行注册前执行 Agent allowlist；`kechuang` 只获得科创 `kc_*` 工具及 Skill 发现所需框架工具，其他 Agent 不获得 `kc_*` 工具或 `kc-*` Skill，原有 DeerFlow 工具保持可用。
- 科创联网搜索由 `kc-web-search` Skill 调用业务 Tavily MCP `kc_web_search` / `kc_web_fetch`；`kc_platform` 配置关闭工具名前缀以保留 `kc_*` 原名。DeerFlow 原生搜索仍供其他 Agent 使用。
- 回归覆盖包括 Agent 公开/管理 API、内部认证、thread 绑定、运行配置伪造、Skill 可见性与工具 schema/执行注册。业务 Backend 的旧 Supervisor 回退及 SSE 清洗不属于 OPIEos 底座改动，见迁移方案。

### DeerFlow 数据库迁移标识

- `backend/packages/harness/deerflow/persistence/migrations/versions/0024_merge_public_agent_and_user_preferences.py`：修正合并迁移的 Revision ID，使迁移标识保持简洁且唯一。

## MCP 与 Skill 接入

- `extensions_config.example.json`：增加可选的 `kc_platform` HTTP MCP 扩展示例。
- `skills/custom/kc-knowledge-search/SKILL.md`、`kc-supply-demand-match/SKILL.md`：声明使用科创检索与匹配工具；不写宿主机路径、数据库连接或请求密钥。
- `skills/custom/kc-document-parse/SKILL.md`：声明通过 `kc_document_*` 查询本人材料并触发解析；文件上传与原件访问继续使用现有 HTTP API。
- `skills/custom/kc-demand-draft/SKILL.md`、`kc-solution-draft/SKILL.md`、`kc-achievement-draft/SKILL.md`：使用 `kc_draft_*` 生命周期工具。
- `skills/custom/kc-customer-visit/SKILL.md`、`kc-enterprise-profile/SKILL.md`、`kc-expert-search/SKILL.md`、`kc-demand-discovery/SKILL.md`：使用 `kc_customer*` 或 `kc_profile` 工具并保留内部/外部权限边界。
- `agents/kechuang/SOUL.md`：承载 `kechuang` 私有 Agent 的常驻编排、上下文、权限和输出指令；`kc-orchestrator` 不再作为 Skill 暴露，业务 Skill ID 均以 `kc-` 开头。
- `skills/custom/kc-web-search/SKILL.md`：通过业务 Tavily MCP 执行 `kc_web_search` / `kc_web_fetch`；不使用 DeerFlow 内置搜索。

实际接入通过 DeerFlow 配置完成，Skill 本身不硬编码 `backend/skills` 或其他业务项目路径。

## Docker 与运行配置

- `docker/docker-compose-dev.yaml`
  - 将 `provisioner` 放入 `kubernetes` profile，默认本地启动不拉起它。
  - 将 DeerFlow Gateway 暴露到宿主机 `127.0.0.1:8001`。
  - 向 Gateway 注入 `KC_PLATFORM_MCP_URL`，默认指向宿主机科创 Backend 的 MCP 地址。
- `backend/Dockerfile`：将源码内 `agents/` 一并复制到镜像，保证容器运行的 `kechuang` Agent 能读取其专属 SOUL。
- 当前检出不含额外 OPIEos Docker 稳定模式 Compose 覆盖；本地开发以 docker-compose-dev.yaml 和 make dev 为准。
- `docker/provisioner/README.md`：同步本地启动和 provisioner 配置说明。

当前本地测试约定：DeerFlow Gateway 通过宿主机 `make dev` 启动，科创 Backend 使用宿主机 Conda `py311` 的 `python main.py` 运行；业务 Backend 默认端口为 `8793`，Gateway 默认端口为 `8001`。科创数据依赖由 `D:/Work/kechuang/backend/docker-compose.local.yml` 管理；OPIEos Compose 仅用于 DeerFlow 自身的 Docker 本地运行配置（例如按需启动 Redis）。服务器部署复用已运行的 DeerFlow Gateway，不新增一套 Gateway/前端/Nginx。

## 生成文件

- `.extensions_config.json.lock`：运行时生成的扩展配置锁文件，不属于业务源码。

## 2026-09-24 本次增补

- `agents/kechuang/SOUL.md` 与六个快速入口相关 `kc-*` Skill：明确每轮重新识别意图；入口提示只决定默认流程，不锁定后续对话；保留“我有需求”“我有方案/成果”“供需匹配”“需求发现”“企业画像”“客户拜访”的原始首步、角色限制和可改道行为。
- 意图路由与普通对话两个通用 Skill 已收敛进 `SOUL.md`，当前不部署独立的 `kc-intent-routing` 或 `kc-conversation`；`config.yaml` 与磁盘清单保持 13 个业务 Skill。
- DeerFlow Skill 源文件继续使用 `skills/custom/<skill-name>/SKILL.md`。`.skill` 是 ZIP 导入/导出归档，不替代运行时目录和 `SKILL.md`。
- `backend/tests/test_kechuang_skill_manifest.py`：将旧的通用表单断言改为原快速入口选项回归检查。
- 同步服务器部署方式与前端合同说明见 `docs/DeerFlow迁移与Skill接入方案.md`；业务 Backend 的 SSE/UI 结果适配记录在对应业务后端代码与测试，不列为 OPIEos 源码修改。
- 验证：`uv run --no-sync pytest tests/test_kechuang_skill_manifest.py -q`，4 项通过。未启动 Gateway、未做在线模型验证。

## 未涉及范围

- 未修改 `D:/Work/kc-rag-agent`。
- 未删除旧的 `kechuang` Docker 资源或旧项目数据。
- 未把科创 Skill 路径硬编码进 OPIEos Skill。
- 未提交 Git，也未推送远程仓库。

## 2026-09-24 本机接入调整与在线阻断

### 原因与处理

- `backend/runtime/deerflow_gateway.py`：内部 Gateway 请求设置 `trust_env=False`，不再从进程环境继承 `HTTP_PROXY` / `HTTPS_PROXY`。原始堆栈显示请求经过 `httpcore` 代理传输，因此这是需要消除的代理变量；但后续绕过代理直连 OPIEos `/health` 仍超时，说明代理不是当前无响应的唯一根因。该设置只针对 Backend 到其显式配置的 DeerFlow 地址，不影响 DeerFlow Agent 自己的联网搜索。
- `backend/runtime/deerflow_gateway.py`：当配置内部 Token 时，为同一轮 thread 创建与 run stream 两个 POST 生成并复用随机 CSRF 双提交 Cookie/Header。该 pair 只满足 OPIEos 现有中间件检查；实际认证仍由内部 Token 完成，不修改 OPIEos CSRF 源码。
- `OPIEos-main/backend/app/gateway/internal_auth.py`：将 `X-OPIE OS-Internal-Token` 和 `X-OPIE OS-Owner-User-Id` 改为合法的 `X-OPIE-OS-Internal-Token` 和 `X-OPIE-OS-Owner-User-Id`。两端使用同一常量约定后，HTTP 客户端可以正常发送认证与用户隔离信息。
- 同步 OPIEos 注释与 `backend/docs/API.md`、`backend/docs/AUTH_DESIGN.md`；没有改认证语义、密钥值或科创 API 合同。
- `OPIEos-main/agents/kechuang/config.yaml`：新增标准文件式 Agent 配置，显式允许当前 13 个 `kc-*` Skill；不注册 MCP 工具、不改 DeerFlow Agent 路由代码。
- 本机运行资产已复制到 `OPIEos-main/backend/.deer-flow/agents/kechuang/`。这是被 Git 忽略的 DeerFlow 运行数据，不应当作源码提交；服务器部署时需把 `agents/kechuang/` 安装到该服务器实际 `DEER_FLOW_HOME/agents/kechuang/`，并确认 `agent_storage.backend: file`。若服务器使用数据库式 Agent 存储，则由维护方通过该版本支持的管理方式创建/发布 Agent。
- 当前 OPIEos 标准 `UserScopedSkillStorage` 仍只在用户没有个人 Custom Skill 时加载仓库 `skills/custom` 下的共享 Skill；用户已有个人 Skill 时，`kc-*` 可能不可发现。`/skills/custom` 仅列个人 `CUSTOM` Skill，不列共享 `LEGACY` Skill。相关 OPIEos Skill 存储/列表运行时修改不属于当前底座源码改动，不能把早期计划中的测试项当作已交付保证；需由维护方决定受支持的解决方式。

### 修改文件

- 科创 Backend：`backend/runtime/deerflow_gateway.py`、`backend/tests/test_deerflow_gateway.py`。
- OPIEos 最小源码/配置/文档：`OPIEos-main/backend/app/gateway/internal_auth.py`、`authz.py`、`services.py`、`backend/docs/API.md`、`backend/docs/AUTH_DESIGN.md`、`backend/tests/test_internal_auth.py`、`OPIEos-main/config.yaml`（更正 Agent 存储注释）、`agents/kechuang/config.yaml`。
- 项目说明：本文件、`docs/DeerFlow迁移与Skill接入方案.md`、`docs/项目启动说明.md`。

### OPIEos 当前工作区改动清单

以下是 2026-09-24 只读核对到的 OPIEos Git 工作区差异，区分核心代码与集成资产：

- 核心代码：`backend/app/gateway/internal_auth.py` 的两个 Header 常量改为合法名称；`authz.py`、`services.py` 仅同步注释；没有 Agent 注册、工具过滤或 Skill runtime 改动。
- 认证配套：更新 `backend/docs/API.md`、`backend/docs/AUTH_DESIGN.md` 和 `backend/tests/test_internal_auth.py`。
- MCP/Agent 配置：`extensions_config.example.json` 增加 `kc_platform` 示例；`agents/kechuang/config.yaml`、`agents/kechuang/SOUL.md` 是科创 Agent 资产，不是 DeerFlow 核心注册代码。
- 镜像/仓库集成：`.dockerignore`、`.gitignore`、`AGENTS.md`、`backend/Dockerfile` 调整为保留并打包 Agent/`kc-*` Skill 资产；`docker/docker-compose.local.yaml` 移除 Gateway 强制 PostgreSQL URL，保留 OPIEos 默认 SQLite。
- Skill 资产：当前 `skills/custom` 使用 `kc-*` 命名；旧的未加前缀目录从工作树移除。自定义 Skill 的目录加载限制见本记录前文，未通过 OPIEos runtime 修改绕过。
- `.extensions_config.json.lock` 是运行时生成文件，不作为源代码或部署资产。
- 未修改 `D:/Work/kc-rag-agent`、`D:/桌面/OPIEos-main`、`backend/.env.local` 或 OPIEos `.env`；没有在文档中记录认证密钥。

### 验证与剩余步骤

- 科创 Backend 的 `test_deerflow_gateway.py`、`test_deerflow_adapter.py`、`test_deerflow_selection.py`：59 项通过；覆盖流式映射、适配和直连客户端配置。
- OPIEos 的 `test_internal_auth.py`、`test_auth.py`、`test_threads_router.py`：224 项通过，1 条第三方依赖弃用警告；覆盖合法 Header、认证和 thread 路由。
- 当前科创 Backend `tests/test_deerflow_gateway.py`：4 项通过；OPIEos 原有 `tests/test_csrf_middleware.py`：18 项通过，验证原双提交机制保持不变。未做在线 Gateway/模型端到端验证。
- 使用 OPIEos Agent 加载器按 `user_id=5` 读取本机 `kechuang`：成功，读取到 13 个 Skill 白名单及 SOUL。
- 最近一次只读在线探测：8001 有监听进程，但 `curl.exe --noproxy "*" --max-time 4 http://127.0.0.1:8001/health` 无响应；这是 OPIEos Gateway 当前应用层无响应的证据，尚未定位该进程内部卡住的原因。科创 Backend `8793/api/v1/health` 返回 HTTP 200（约 2.4 秒）。本轮未停止或重启服务。
- 聊天实际经过 OPIEos Gateway 的 `POST /api/threads` 与 `POST /api/threads/{thread_id}/runs/stream`（HTTP/SSE），不是 MCP Streamable HTTP；`/mcp/` 仅用于 Agent 的业务工具调用。Backend 每轮创建 AsyncClient，同轮复用连接池，不是每个 token 新建 TCP；默认 300 秒超时可能使 thread 请求无响应时前端长时间停留在处理中。
- Gateway 恢复后需由用户手动重启 OPIEos，再先验证 `/health` 快速返回，随后从科创问答发起请求；以 `POST /api/threads` 成功、`/runs/stream` 收到事件、工具调用成功为最终验收。
- 服务器现有 OPIEos 无需 CSRF 核心修改；科创 Backend 发送匹配的双提交 pair 后仍须通过 Gateway 内部 Token 认证。服务器 Header 名需与 Backend 使用的合法名称一致，并部署 Agent 资产。

## 2026-09-25 Agent/Skill 跟踪与 MCP Trace 配置

- `.gitignore` 增加窄范围例外，使 `agents/kechuang/config.yaml`、`agents/kechuang/SOUL.md`、`skills/custom/kc-*/SKILL.md` 和本机 `extensions_config.json` 可被私有 Git 仓库跟踪；没有取消其余通用配置和运行目录的忽略规则。
- `extensions_config.example.json` 补入默认关闭的 `kc_platform` 示例，URL 从 `KC_PLATFORM_MCP_URL` 注入；`extensions_config.json` 保持本机已启用状态，并补上 `X-Trace-ID -> kc_trace_id`。缺失上下文时仍 `deny`。
- 从 `OPIEos-main/backend/.deer-flow` 仅清理 32 个遗留 `*.lock` 文件及 pytest 固定用户 `users/test-user-autouse/skills/_skill_states.json`；SQLite 主库/WAL/SHM、用户会话、Agent、检索数据和密钥均保留。该目录总共释放不足 200 字节，主要是移除测试/锁文件残留。
- 本次没有修改 OPIEos Gateway/Harness Python 核心代码，没有提交或推送。
- 验证：`backend/tests/test_deerflow_gateway.py` 4 项通过；示例和实际 JSON 均可解析，`kc_platform` trace 映射齐全；Agent、13 个 `kc-*` Skill 和本机 MCP 配置已不再被 Git 忽略。
