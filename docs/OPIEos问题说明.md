# OPIEos 改动说明与服务器迁移核对

> 本文记录本机 OPIEos 科创接入改动及 3090/4090 服务器迁移核对。本机测试配置单独记录于第 5 节，不应复制到服务器。服务器 Agent/Skill 实际部署用户目录为 `OPIEos/.deer-flow/users/e458d2e2-30ad-46f8-86f9-535d0cdec89e/`；服务器文件状态以服务器现场检查为准。

## 1. 增加科创专属 Agent 与业务 Skill

**修改原因**：让 OPIEos 按标准 Agent/Skill 文件发现机制加载科创业务指令；不在 DeerFlow Harness 中写科创路由或注册逻辑。`kechuang` Agent 的 Skill 白名单限定其业务 Skill；MCP 工具仍由 Gateway 扩展配置提供，不代表 MCP 工具获得 Agent 级隔离。

**修改文件与位置**

- `agents/kechuang/config.yaml:1-16`（新增）：声明 `name: kechuang` 和 13 个 `kc-*` Skill。
- `agents/kechuang/SOUL.md:1-94`（新增）：Agent 身份、意图识别、业务流程、权限和工具使用约束；快速入口默认流程位于 `25-38`，工具边界位于 `58-69`。
- `skills/custom/kc-*/SKILL.md:2-3`（新增 13 个）：每个文件用 `name`、`description` 声明对应业务 Skill。名称为 `kc-achievement-draft`、`kc-contact-human`、`kc-customer-visit`、`kc-demand-discovery`、`kc-demand-draft`、`kc-document-parse`、`kc-enterprise-profile`、`kc-expert-search`、`kc-field-extraction`、`kc-knowledge-search`、`kc-solution-draft`、`kc-supply-demand-match`、`kc-web-search`。
- `skills/custom/*/SKILL.md`（删除旧版 16 个未加前缀的 Skill）：成果草稿、联系客服、会话、客户拜访、需求发现、需求草稿、文档解析、企业画像、专家检索、字段提取、意图路由、知识检索、orchestrator、方案草稿、供需匹配、联网搜索。业务 Skill 改为 `kc-*`；旧 `conversation`、`intent-routing`、`orchestrator` 的编排职责收敛到 Agent 的 `SOUL.md`，不再作为独立 Skill。
- `.gitignore:46-52`：原 `skills/custom/*` 忽略规则保留，同时仅放行科创 Agent 文件、`extensions_config.json` 和 `skills/custom/kc-*/SKILL.md`，避免私有 Git 提交漏掉运行所需资产。

**代码对比**

修改前：Git 基线没有 `agents/kechuang/`；业务 Skill 是无项目名前缀的独立目录，例如 `skills/custom/orchestrator/SKILL.md`。

修改后（`agents/kechuang/config.yaml:1-16`）：

```yaml
name: kechuang
description: 科创项目业务智能体
skills:
  - kc-achievement-draft
  - kc-contact-human
  # 其余 kc-* Skill 见 config.yaml:6-16
```

## 2. 科创 MCP 连接配置

**修改原因**：向 OPIEos 扩展示例说明如何把科创业务 MCP 服务加入运行时工具集，并通过请求上下文传入会话 ID 和 trace ID；科创用户认证仍由科创 Backend 负责，不把用户令牌写入 Skill。

**修改文件与位置**

- `extensions_config.example.json:38-52`：科创 HTTP MCP 配置键为 `kechuang`。模板默认 `enabled: false`，地址引用 `$KC_PLATFORM_MCP_URL`；会话与 trace 上下文映射到 MCP Header，缺少上下文时使用 `on_missing: deny`。模板不映射 `Authorization`。
- `backend/runtime/mcp_server.py:316`：MCP 服务名同步为 `kechuang`；模型可见工具仍保留 `kc_*` 原名。
- `extensions_config.json`：本机实际配置文件（当前为未跟踪文件）；可按本机/服务器环境启用并提供地址。出于凭据安全，本文不复制其配置值。服务器部署时应使用服务器自己的 MCP URL 和密钥。

**代码对比**

修改前：`mcpServers` 中没有科创 MCP 项；原连接标识 `kc_platform` 已统一改为 `kechuang`。

修改后（`extensions_config.example.json:38-53`）：

```json
"kechuang": {
  "enabled": false,
  "type": "http",
  "url": "$KC_PLATFORM_MCP_URL",
  "tool_name_prefix": false,
  "headers_from_context": {
    "enabled": true,
    "headers": {
      "X-KC-Thread-ID": "kc_thread_id",
      "X-Trace-ID": "kc_trace_id"
    },
    "on_missing": "deny"
  }
}
```

## 3. 修复 pnpm 11 的 patchedDependencies 弃用警告

**修改原因**：pnpm 11 不再读取 `package.json` 内的 `pnpm.patchedDependencies`，导致安装警告且补丁无法可靠进入锁文件。按 pnpm workspace 配置方式将映射移至 `pnpm-workspace.yaml`，重新安装以更新锁文件；补丁文件本身保留。

**修改文件与位置**

- `frontend/package.json:131`：保留 `packageManager`，移除旧的 `pnpm.patchedDependencies` 对象（修改前位于该文件末尾 `packageManager` 后）。
- `frontend/pnpm-workspace.yaml:2-3`：新增补丁映射。
- `frontend/pnpm-lock.yaml:10-11` 及对应 importer/snapshot：记录补丁 hash 和依赖解析结果。
- `frontend/patches/next-themes@0.4.6.patch`：原补丁文件保留，未删除。

**代码对比**

修改前（`frontend/package.json`）：

```json
"pnpm": {
  "patchedDependencies": {
    "next-themes@0.4.6": "patches/next-themes@0.4.6.patch"
  }
}
```

修改后（`frontend/pnpm-workspace.yaml:2-3`）：

```yaml
patchedDependencies:
  next-themes@0.4.6: patches/next-themes@0.4.6.patch
```

验证：从 `frontend/` 执行 `pnpm install` 成功；锁文件记录 `patch_hash`，原 `pnpm.patchedDependencies` 警告消失。安装器另外提示的 3 项 deprecated subdependencies 与本次补丁配置无关。

## 4. 修复 MCP 工具运行时上下文未注入

**问题与原因**：MCP 工具被包装为同步 `.func` 后，通用包装函数没有保留原异步工具的签名和注解。LangGraph 因而无法识别 `runtime` 注入参数，请求级上下文没有到达 OPIEos MCP 拦截器，检索/草稿请求会在离开 OPIEos 前报 `MCP authentication required`。

**修改内容**：同步包装器通过 `functools.wraps(coro)` 保留异步工具的签名和注解；MCP 拦截器从当前调用的 `runtime.context.secrets` 读取配置绑定的值，按 `extensions_config.json` 的 `headers_from_context.headers` 写入本次 MCP 请求 Header。已有未绑定 Header 保留；缺少、空白或含换行的绑定值继续拒绝调用，不使用固定身份或全局令牌兜底。科创配置只传 `X-KC-Thread-ID` 和 `X-Trace-ID`，不传 `Authorization`；科创 Backend 仍负责用户认证。

**修改文件**

- `backend/packages/harness/deerflow/tools/sync.py`：同步包装器保留两个分支上的原协程元数据，使 LangGraph 能识别并注入运行时参数。
- `backend/packages/harness/deerflow/config/extensions_config.py`：新增 `headers_from_context` 配置模型，校验绑定结构并将缺失策略限制为 `deny`。
- `backend/packages/harness/deerflow/mcp/interceptors.py`：新增请求级 Header 注入拦截器；按 MCP server 名隔离配置，并校验每次调用的上下文值。
- `extensions_config.example.json`：增加默认关闭的 `kechuang` MCP 示例以及可选、默认关闭的 `parallel-search` 示例。实际部署配置仍应按服务器地址和运行环境单独维护。
- `backend/tests/test_mcp_sync_wrapper.py`、`backend/tests/test_mcp_custom_interceptors.py`：增加运行时参数识别/转交、并发请求上下文隔离和缺少上下文时拒绝调用的回归用例；按当前要求未运行测试，待重迁后现场验收。

**服务器重迁核对**：迁移 OPIEos 源码时需包含上列三个 Harness 源文件和 MCP 配置模板；保留服务器自己的 `extensions_config.json`，确认 `kechuang` 已启用、URL 指向服务器可访问的科创 MCP 地址，且 `headers_from_context` 映射与科创 Backend 传入的 secret 名一致。重启后分别实际验证知识库检索与草稿创建；本地新增回归用例尚未执行，运行链路也尚未验收。

## 5. 本地科创实例 DeerFlow 配置

**修改原因**：本地 `make dev` 读取的根目录 `config.yaml` 原为单机生产部署配置，使用 PostgreSQL、Redis、远端 sandbox 和 SIDUS 密钥变量；本地 `.env` 没有 `DATABASE_URL` 等变量，Gateway 启动时在解析 `$DATABASE_URL` 阶段失败。科创本地 `.env` 已有 `KC_LLM_API_KEY`、`KC_LLM_BASE_URL`、`KC_LLM_MODEL`，本地无需生产数据库、Redis 或 provisioner。

**修改前后对比**

- 数据库：修改前 `postgres` + `$DATABASE_URL`，本地缺少该变量而无法启动；修改后用 SQLite，数据保存在 DeerFlow 本地数据目录。
- Stream bridge：修改前使用 Redis 和 `$DEER_FLOW_STREAM_BRIDGE_REDIS_URL`；修改后使用单进程内存模式，适用于本地开发。
- 模型：修改前读取 `$SIDUS_API_KEY`；修改后注册为 `kc-main`，并读取本地已有的三个 `KC_LLM_*` 变量，供科创 Agent 调用。
- Sandbox：修改前依赖远端 provisioner 及 `$PROVISIONER_API_KEY`；修改后使用本机 `LocalSandboxProvider`，仍关闭 host bash。
- 联网工具：移除本地配置中的 `web_fetch`（它依赖未配置的 Crawl4AI 服务和 `$CRAWL4AI_TOKEN`）；保留 `web_search`。科创项目的联网搜索由其已有搜索能力提供。
- `.env` 未修改，模型密钥值未写入配置文件。

**配置边界**：`config.yaml` 是单个 DeerFlow 实例内共享的运行配置，不是 per-Agent 配置；本次只改本地副本。根目录 `config.yaml` 被 `.gitignore` 忽略，服务器必须保留自己的 PostgreSQL/Redis 等部署配置，不要用本地文件覆盖。

**验收状态**：已核对配置中只剩 `KC_LLM_*` 环境变量引用；按当前要求未启动服务或运行测试，需由本地启动验证。

## 6. 3090/4090 服务器迁移变更记录

以下区分了已由命令输出或用户确认的事项，以及尚未取得服务器复核证据的事项。服务器运行配置不等于本机 Git 工作区中的同名文件。

### 6.1 3090 OPIEos 文件与用户资产

**已放置或更新的文件**

- `/home/slave1/codes/random/OPIEos/backend/packages/harness/deerflow/tools/sync.py`：修正同步包装器，使 MCP 工具的运行时上下文可注入；服务器宿主机文件 SHA-256 为 `7cad2275e2890d99d223715b938dd301313d1ce3b36aa1c931e6ea6fa22ce7da`。后续 `docker exec` 检查显示容器文件哈希与宿主机一致。
- `/home/slave1/codes/random/OPIEos/backend/my_package/mcp/auth.py`：从暂存目录放到服务器 OPIE 后端，且已用 `cmp` 确认与暂存文件一致。
- `/home/slave1/codes/random/OPIEos/config.yaml`：更新为包含 `kc-main` 的服务器配置；该模型关闭思考模式。
- `/home/slave1/codes/random/OPIEos/.deer-flow/extensions_config.json`：加入 `kechuang` MCP 配置，同时保留已有的 `textile-erp` 配置；未将 `textile-erp` 迁入科创用户目录。
- `/home/slave1/codes/random/OPIEos/.deer-flow/users/e458d2e2-30ad-46f8-86f9-535d0cdec89e/agents/kechuang/config.yaml`、`SOUL.md`：增量放置科创 Agent 文件。
- 同一用户目录下 `skills/custom/`：增量放置 13 个 `kc-*` Skill；暂存内容还包含 `hr-talent/SKILL.md`、`LICENSE` 和 `.skillhub.json`。复制使用 `cp -an`，保留目标目录中已有的同名文件。
- `/home/slave1/codes/random/OPIEos/docker/docker-compose.yaml`：Gateway 需要把宿主机 `backend/my_package` 只读挂载到容器 `/app/backend/my_package`。活动容器后来已能执行该模块，但本次没有看到 Compose 挂载列表的最终输出；此项标为待复核，不能仅凭当前容器可导入就断定重建后仍可见。
- `/home/slave1/codes/random/OPIEos/.env`：3090 运行配置，包含内部令牌和 `KC_PLATFORM_MCP_URL` 等变量；本文不记录密钥明文。迁移到 8793 后 URL 应为 `http://27.223.9.106:8793/mcp/`，服务器 `.env` 是否已改为该值仍需现场核验。

**容器源码挂载问题**

服务器宿主机曾有 `backend/my_package/mcp/auth.py`，但 Gateway 容器内 `/app/backend/my_package/mcp/auth.py` 不存在，日志因此报 `ModuleNotFoundError: No module named 'my_package'`。这说明仅把文件放到宿主机源码目录，不代表容器能看到它。Gateway 后续日志已显示从 `/app/backend/my_package/mcp/auth.py` 执行拦截器，表明活动容器后来已能读取该模块；但当前 Compose 是否保留了只读 bind mount、重新创建容器后是否仍存在，尚未取得最终检查输出。应核对 `docker inspect deer-flow-gateway` 的 Mounts，确保服务器 Compose 配置将 `backend/my_package` 挂载到 `/app/backend/my_package:ro`；否则需通过镜像构建纳入文件，并在重建后验证模块导入。

`sync.py` 也曾出现宿主机与容器哈希不同：宿主机为 `7cad2275…`，容器一度为 `740c7180…`。随后两者哈希已一致，但尚未确认该文件是通过持久挂载还是仅更新了当前容器。因此 `scripts/deploy.sh down/start` 或重新创建 Gateway 后，应再次比较两端哈希；若不一致，修复没有持久化到镜像或挂载配置。

### 6.2 内部令牌配置与 MCP 会话上下文

- OPIE Gateway 读取的变量名为 `DEER_FLOW_INTERNAL_AUTH_TOKEN`；4090 科创后端应使用同名配置，并通过 `X-DeerFlow-Internal-Token` 请求头访问 Gateway。文档不记录令牌明文。
- 4090 配置文件为 `/home/zyx/kechuang-deerflow/backend/.env`；令牌变量名已按 `DEER_FLOW_INTERNAL_AUTH_TOKEN` 对齐。只记录名称，不记录值。
- 曾因启动 Compose 的 shell 环境变量覆盖 `.env` 中的值，导致 3090 Gateway 与 4090 后端的有效令牌不一致，`POST /api/threads` 返回 401。用两端有效令牌的 SHA-256 指纹核对后，使用 `env -u DEER_FLOW_INTERNAL_AUTH_TOKEN` 启动 Compose，避免继承 shell 中的旧值；指纹一致后 Gateway 请求恢复成功。以后重建 Gateway 后仍应比较指纹，不要仅检查 `.env` 文件内容。
- 内部令牌一致只解决 OPIE Gateway 的 401，不会补上科创 MCP 的请求级上下文。随后 `kc_draft_create` 日志报 `Required MCP context secret is missing: kc_thread_id`，定位到 MCP 工具包装器没有向 LangGraph 暴露运行时参数；需同时确保容器运行的是修正后的 `sync.py`，并从科创页面发起带有当前会话上下文的请求。

### 6.3 4090 服务端口调整

- 调整对象：`kechuang-deerflow.service` 后端从 `8795` 改为 `8793`；`kechuang-deerflow-frontend.service` 前端从 `8796` 改为 `8794`。用户确认已完成调整。
- 调整前，原 `kechuang.service` 和 `kechuang-frontend.service` 已停止，`8793/8794` 无监听；当时 DeerFlow 仍监听 `8795/8796`。修改后的监听状态尚未由本次文档更新操作重新检查。
- 4090 systemd 实际 unit/drop-in 文件路径未记录；后续可用 `systemctl show ... -p FragmentPath -p DropInPaths -p ExecStart` 补齐具体文件名。
- 3090 OPIE 的 `KC_PLATFORM_MCP_URL` 应指向 `http://27.223.9.106:8793/mcp/`。该服务器 `.env` 更新及重启后的实际连通性尚未在本次操作中复核。

## 修改边界

- 本说明覆盖 OPIEos 科创 Agent/Skill、MCP 配置与上下文注入、本地开发配置，以及 3090/4090 部署中已报告的令牌、源码可见性和端口调整。
- 未修改 OPIE Gateway 内部认证 Header 常量；因此不再把“修复内部认证 Header 导致的 401”列为代码改动。该 401 是两端有效令牌不一致造成的部署配置问题。
- MCP 工具的会话上下文修复与 Gateway 内部令牌修复是两件事：令牌对齐解决 `/api/threads` 的 401；`sync.py` 运行时参数暴露解决 `kc_thread_id` 未注入。容器文件重建后的持久性、端口调整后的监听与真实 MCP 检索/草稿链路需分别现场验收。
