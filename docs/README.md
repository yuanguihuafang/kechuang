# 科创服务助手方案文档

最新知识数据操作规范见[本地知识数据标准操作流程](方案/本地知识数据标准操作流程.md)。

文档总索引见 [Agent.md](Agent.md)。

本目录以 `D:\Work\kechuang\OPIEos` 的 DeerFlow 版本为唯一迁移基线。原 `kc-rag-agent` 的旧架构、旧 Agent 编排和历史实施计划不再作为当前方案依据。

## 当前工作区与执行状态

- 历史记录（2026-09-29）：本机 OPIE 自定义 MCP 拦截器修复及验证见[修复记录](方案/DeerFlow-MCP运行时注入修复.md)；其中部署状态只代表当时，当前状态需现场核验。

- 科创业务项目工作区为 `D:\Work\kechuang`；独立只读知识库 MCP 位于 `D:\Work\kechuang-knowledge-mcp`，不随业务后端启动，工具和接入说明见该项目的 `kechuang-MCP.md`。调用方可自行选择内外部查询模式，这不是身份认证，应限制网络访问范围。
- `D:\Work\kc-rag-agent` 仅作为历史参考，禁止写入、清理或同步覆盖。
- 双服务器部署方案为 3090 运行 DeerFlow/OPIEos Gateway、4090 运行科创 Backend/前端；地址和实际运行状态需按部署环境核验。步骤见[双服务器迁移部署](科创与DeerFlow双服务器迁移部署.md)。
- 源码中的 13 个业务 Skill 位于 `OPIEos/skills/public/kc-*`；实际运行加载位置由部署中的 `skills.path`、容器挂载及 Agent 白名单共同决定。迁移时先核对 Gateway 的有效配置，不从旧 `skills/custom` 文档路径推断当前部署。
- Skill 不硬编码宿主机路径；通过 DeerFlow 配置映射 Skill 根目录。
- 前端仍以 `/api/v1/*` 为唯一业务入口，优先保持现有请求字段、Token-only 身份、`thread_id` 和五类 SSE 事件不变。
- DeerFlow 流式完成后的会话状态持久化 bug 已修复并通过定向回归。本机 OPIEos 核心继续使用原 CSRF 中间件；内部请求通过科创 Backend 适配现有双提交校验，不要求服务器修改 OPIEos CSRF 核心。本机 `kechuang` Agent 按 `DEERFLOW_AGENT_OWNER_USER_ID` 对应的用户目录加载，当前本机 UUID 为 `62ddcc4d-f2b5-4152-be26-78a1a7554a86`；Agent 配置含 13 个 `kc-*` Skill 白名单。MCP 工具仍是 Gateway 运行时共享工具，业务身份由 Backend 根据会话恢复；MCP 不映射 `Authorization`。服务器部署使用独立 owner UUID `e458d2e2-30ad-46f8-86f9-535d0cdec89e`，详情见迁移方案和[OPIEos 修改记录](方案/OPIEos-main修改记录.md)。

- 共享 Skill 已改用原生 `skills/public/kc-*`，不修改 OPIEos 存储器。原生加载测试覆盖有/无个人 Custom Skill 的用户；个人同名 Skill 仍按平台规则覆盖共享内容。部署须检查实际用户加载路径，完整列表看 `/api/skills`，不能以 `/api/skills/custom` 数量判断共享技能。

此前的 Agent 隔离规格与实施计划作为历史设计记录保留，不代表当前 OPIEos 核心已实现 Agent 注册或隔离。当前状态以[DeerFlow 迁移与 Skill 接入方案](方案/DeerFlow迁移与Skill接入方案.md)为准。

## 文档目录

| 文档 | 用途 |
|---|---|
| [DeerFlow迁移与Skill接入方案](方案/DeerFlow迁移与Skill接入方案.md) | 运行时、Agent SOUL、业务 Skill、MCP、会话、权限和部署边界 |
| [Windows 本机启动说明](项目启动说明.md) | 本机依赖、启动顺序、地址、日志和故障排查 |
| [OPIEos 当前改动说明](OPIEos问题说明.md) | 当前 Agent、Skill、MCP、认证与包管理改动及代码行号对比 |
| [方案库完整修复与重建执行方案](方案/方案库完整修复与重建执行方案.md) | `solution` 来源核查、草稿/MySQL 去重、附件补救、影子库恢复和管理员确认切换 |
| [方案库恢复问题跟踪](方案/方案库恢复问题跟踪.md) | 按文档/附件逐项跟踪方案库原件、图片、解析产物和草稿附件关联的修复状态 |
| [OPIEos 历史修改记录](方案/OPIEos-main修改记录.md) | 历史修改、测试和服务器部署提示 |
| [意图路由与Skill拆分方案](方案/意图路由与Skill拆分方案.md) | `kechuang` Agent 常驻指令与各业务 Skill 的职责和调用规则 |
| [问答界面功能说明](前端API/问答界面功能说明.md) | 小程序问答、SSE、草稿发布/下架/删除、图片展示与个人中心 API 契约 |
| [前后端API接口文档](前后端API接口文档.md) | 后端接口和前后端联调说明 |
| Backend 模块与入口 | 下方“Backend 模块与主要入口” |
| [Milvus 连接生命周期设计](superpowers/specs/2026-09-27-milvus-connection-lifecycle-design.md) | 检索 Agent、Collection 连接共享、恢复与 Backend 关闭边界 |
| [PostgreSQL 连接池预算与生命周期](superpowers/specs/2026-09-27-postgres-pool-lifecycle.md) | PostgreSQL 池上限、worker 预算与 Backend 关闭边界 |
| 历史迁移、计划与设计稿 | [并行迁移计划](superpowers/plans/2026-09-22-deerflow-parallel-migration.md)、[宿主机运行计划](superpowers/plans/2026-09-23-host-backend-deerflow-runtime.md)及其[设计稿](superpowers/specs/2026-09-23-host-backend-deerflow-runtime.md)、[旧 Agent 隔离计划](superpowers/plans/2026-09-23-kechuang-agent-isolation.md)及其[设计稿](superpowers/specs/2026-09-23-kechuang-agent-isolation-design.md)、[后端加固计划](superpowers/plans/2026-09-24-kechuang-backend-flow-hardening.md)及其[设计稿](superpowers/specs/2026-09-24-kechuang-backend-flow-hardening-design.md)；均为历史执行/设计记录，当前状态以迁移方案为准 |

后端 API 的详细接口见[前后端 API 接口文档](前后端API接口文档.md)；问答与个人中心交互见[问答界面功能说明](前端API/问答界面功能说明.md)。其他测试页面以当前实现为准。

## Backend 模块与主要入口

| 模块 | 职责 / 入口 |
|---|---|
| `backend/main.py` | `create_app()` 组装 FastAPI、系统路由、业务路由、管理路由与可选 MCP。 |
| `backend/api/` | `/api/v1/*` 身份、聊天、材料、草稿、业务记录、系统和生命周期 HTTP 边界；总装配在 `api/v1/router.py`。 |
| `backend/admin/` | 管理端 API、治理、文档目录和清理用例；独立于聊天编排。 |
| `backend/agents/` | `perception_agent` 负责材料解析/字段提取，`retrieval_agent` 负责检索；路由聚合在 `agents/routes.py`。 |
| `backend/core/` | 跨模块合同、访问策略、审计/脱敏/指标和提示词等共享基础规则。 |
| `backend/runtime/` | 配置、容器装配、DeerFlow Gateway、MCP 工具上下文和长任务工作流。 |
| `backend/services/` | 应用用例及 MySQL/PostgreSQL、Milvus、MinIO、文档解析、LLM、记忆、任务队列、观测与联网搜索适配。 |
| `backend/migrations/`、`backend/evaluation/` | Alembic 数据库版本；检索质量与 RAG 评估入口。 |

问答入口为 `api/v1/execution/supervisor.py`，实际调用 DeerFlow 适配器；文件解析器统一在 `services/document_parsing/`，不要在 Agent 包内重复实现。旧 Supervisor Agent 执行图已删除。

## 当前统一边界

- DeerFlow Harness 负责唯一的问答 Agent 运行、上下文管理、Skill 加载和流式执行。旧 Supervisor/意图/推理执行图已删除；未配置聊天运行时返回 503，不回退旧链路。确定性会话服务在 `services/application/conversation_state.py`，共享合同在 `core/contracts/`。
- 本机 `kechuang` Agent 的运行资产位于 `DEER_FLOW_HOME/users/62ddcc4d-f2b5-4152-be26-78a1a7554a86/agents/kechuang/`；4090 调用 3090 时 owner UUID 使用服务器用户 `e458d2e2-30ad-46f8-86f9-535d0cdec89e`，不得使用科创用户 ID。
- `skills/public` 同时承载平台公共 Skill 与 13 个 `kc-*` 业务 Skill；Agent 白名单控制发现范围，数据权限由 Backend 校验。
- 原有 PostgreSQL、MySQL、Milvus、MinIO、文档解析和业务接口保留，通过 MCP 或受控远程工具提供给 DeerFlow。
- 管理控制台与智能问答保留独立业务服务和权限边界；同一 Backend worker 的各 Collection 共用一条懒连接 Milvus 会话，生命周期见 [Milvus 连接设计](superpowers/specs/2026-09-27-milvus-connection-lifecycle-design.md)。
- 同一会话使用同一个 `thread_id`；不同会话不得共享草稿、附件、检索条件、排除集合和权限上下文。
- 服务器存在其他 DeerFlow 项目时，当前项目必须使用独立运行配置、运行状态目录、端口和数据库 Schema。
- 本目录中的迁移方案、启动说明和 API 契约是当前执行依据。历史计划/规格保留用于追踪决策，若与当前方案冲突，以迁移方案为准。

## Windows 本机运行边界

科创数据依赖统一使用 `backend/docker-compose.local.yml`，包括 PostgreSQL、Milvus、MinIO、etcd 和 Attu；科创业务 Backend 在 Conda `py311` 中以前台方式运行。DeerFlow Gateway、前端和 Nginx 在 OPIEos 目录通过 `make dev` 原生启动，不需要通过 Compose 启动。

OPIEos 的 `docker/docker-compose-dev.yaml` 是 DeerFlow 自身的 Docker 本地运行配置，不放科创数据服务或业务 Backend。当前原生 `make dev` 不会自动启动 Redis；确实需要 Redis 时，可在 OPIEos 目录仅启动该服务：

```powershell
Set-Location 'D:\Work\kechuang\OPIEos'
docker compose --env-file .env -f docker/docker-compose-dev.yaml -p kechuang up -d redis
```
OPIEos 内部持久化使用 config.yaml 中的默认 SQLite；OPIEos 本地 .env 不设置 DATABASE_URL。科创业务 Backend 的 PostgreSQL 配置独立管理。

当前检出未包含额外的 OPIEos Docker 稳定模式 Compose 覆盖；原生 make dev 不使用 Docker Compose。不要在原生模式下无服务参数启动完整 Compose，以免额外启动容器版 Gateway、前端和 Nginx。

科创 Backend 健康接口为 `http://localhost:8793/api/v1/health`，OPIEos 页面为 `http://localhost:2026`。本机 Gateway 通过 `make dev` 启动，因此 `$KC_PLATFORM_MCP_URL` 使用 `http://127.0.0.1:8793/mcp/`。只有 Gateway 在容器内时，回调宿主机 Backend 才使用 `http://host.docker.internal:8793/mcp/`。

本地 MySQL 测试使用 `backend/.env.local`；Windows 宿主机运行的 Backend 将 `MYSQL_HOST` 和 `MYSQL_IDENTITY_HOST` 指向 `127.0.0.1`。服务器部署时，科创 Backend 接入服务器上已经运行的 DeerFlow Gateway，不额外部署一套 DeerFlow。

历史 Docker 模式的服务连通与基础检索曾验证过。MCP 配置资产使用连接 ID `kechuang` 和原始 `kc_*` 工具名。本机 `kechuang` Agent 按 owner UUID 对应的用户级文件存储加载，Agent 配置白名单 13 个 `kc-*` Skill；MCP 工具仍是 Gateway 运行时共享工具集，不承诺 Agent 级工具隔离。服务器当前状态以本次部署记录为准，不能用本段历史测试推断在线可用性。

### 历史迁移与测试记录（非当前运行状态）

以下任务编号、测试数字和 Docker 探测均为当时记录；服务器、容器或代码更新后必须重新验证，不能据此认定当前部署通过。

- Task 0-4 的业务接口契约、后端适配和身份校验代码有定向测试；没有在 OPIEos 核心加入科创 Agent 注册或 MCP 工具隔离代码，本机 `kechuang` 通过标准文件式 Agent 存储配置加载。`kc_context` 不作为 MCP 暴露，会话上下文使用 DeerFlow 原生 thread/message。
- Task 5B（草稿生命周期）和 Task 5C（文档/附件）已完成定向实现与回归测试。
- Task 5A 的 Skill/MCP、检索调用链和容器网络曾完成历史验证；当前本机 Compose 边界已调整为科创数据依赖由 `backend/docker-compose.local.yml` 管理，OPIEos Compose 不再承载科创 Backend 或数据服务。
- 客服转接使用 `CONTACT_WECOM` 响应动作和小程序原生 `open-type="contact"` 按钮；不作为 MCP 暴露，不创建待处理工单。
- DeerFlow 适配保留 `knowledge_type`、公共结果字段清洗和超时错误语义。2026-09-24 本轮定向回归：业务 Backend 的 flow controls 42 passed，DeerFlow completion/material/selection/adapter 63 passed；恢复后的 OPIEos 核心相关测试 132 passed、3 个已知基线失败用例被排除。以上是代码测试，不是服务器 Agent 的在线端到端验收。
- Task 6 的历史 Docker Gateway smoke 已验证：`DEERFLOW_ENABLED=true` 下原 `/api/v1/chat/stream` 走 DeerFlow 并保持 `start/done` 公共 SSE；完整旧链路/新链路相同输入对比和可命中业务数据验证仍待补齐。
- 历史最小 Docker 运行验证覆盖当时的 Compose 配置、OPIEos 页面和宿主机 backend 健康接口；Compose 文件边界已调整，历史验证不代表当前宿主机 Gateway 启动方式的业务问答链路已端到端验收。
- 较早的一次本机启动快照中，`8001/health/ready` 返回 `200/ready`，科创前端 `8794` 返回 `200`，业务 Backend 返回 `200/degraded`；这不是当前进程状态，也未验证专属 Agent/MCP 真实调用。
- 2026-09-24 最近一次只读探测：8001 有监听进程，但直连（禁用代理）请求 OPIEos 简单 `/health` 在 4 秒内无响应；8793 `/api/v1/health` 返回 HTTP 200（约 2.4 秒）。该历史快照不能说明当前进程状态，遇相同故障应检查 OPIEos Gateway 进程；服务没有被本次检查重启，重启后的在线问答、Agent 与 MCP 仍待验收。
- 聊天通过 OPIEos Gateway 的 `POST /api/threads` 与 `/api/threads/{thread_id}/runs/stream`（HTTP/SSE），不是 MCP Streamable HTTP；MCP `/mcp/` 是 Agent 调用业务工具的独立通道。服务器接入还需部署 Agent 资产及合法 Header 约定，见迁移方案。

### Docker 基础镜像说明

`ghcr.io/astral-sh/uv:0.11.1`、`python:3.12-slim-bookworm` 和 `node:22-alpine` 是历史 Docker 构建中的基础镜像，不是独立业务服务。当前 Windows 本机通过 `make dev` 启动 DeerFlow 应用进程，不需要启动 Docker Gateway、Frontend 或科创 Backend 容器。

## 当前测试：业务 backend 前台运行

当前本机测试不启动 Docker 里的业务 Backend 或 DeerFlow Gateway。科创 PostgreSQL、Milvus、MinIO、etcd 和 Attu 由 `backend/docker-compose.local.yml` 管理；Redis 如确有需要，从 OPIEos Compose 单独启动。DeerFlow Gateway 由 Windows 本机 `make dev` 启动，科创业务 backend 在终端前台启动，便于直接查看日志：

```powershell
cd D:\Work\kechuang\backend
python main.py
```

科创前端仍按原接口访问 `http://localhost:8793/api/v1/*`。前端单独启动时可显式设置（默认也是 `8793`）：

```powershell
cd D:\Work\kechuang\frontend
$env:VITE_API_BASE_URL="http://localhost:8793"
npm run dev
```

### 本机测试配置

业务 backend 的本机配置在 `backend/.env.local`，关键项如下：

| 配置 | 本机测试值 | 作用 |
|---|---|---|
| `DEERFLOW_ENABLED` | `true` | 将问答执行切到 DeerFlow 适配器 |
| `DEERFLOW_BASE_URL` | `http://127.0.0.1:8001` | 业务 backend 调用本机 Gateway 进程 |
| `DEER_FLOW_INTERNAL_AUTH_TOKEN` | 与 DeerFlow 端同名配置的值相同 | backend 调用 Gateway 的内部认证，不提交 Git |
| `KC_MCP_ENABLED` | `true` | 暴露业务 MCP |
| `KC_MCP_PATH` | `/mcp` | MCP 地址为 `http://localhost:8793/mcp/` |
| `KC_MCP_ALLOWED_HOSTS` | 包含 `host.docker.internal:*` | 允许 Docker DeerFlow 回调宿主机 backend |
| `DATABASE_URL` | `127.0.0.1:5432/kechuang` | 宿主机进程访问科创本地 PostgreSQL；与 Compose 默认数据库名一致 |
| `PUBLIC_API_BASE_URL` | 按环境填写公开 API 根地址，如 `https://api.example.com` | 后端返回完整的 MinIO 图片接口 URL；不要带 `/api/...` 路径 |
| `POSTGRES_POOL_MIN_SIZE` / `POSTGRES_POOL_MAX_SIZE` | `1` / `8` | 每个 worker/DSN 的同步 psycopg 池最小连接数与上限 |
| `POSTGRES_SQLALCHEMY_POOL_SIZE` / `POSTGRES_SQLALCHEMY_MAX_OVERFLOW` | `4` / `2` | 每个 worker/DSN 的 SQLAlchemy 池基础连接数与溢出上限 |
| `LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE` / `LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE` | `1` / `4` | 每个 worker 的 Checkpointer 异步池最小连接数与上限 |
| `MILVUS_URI` | `http://127.0.0.1:19530` | 宿主机进程访问 Milvus |
| `MINIO_ENDPOINT` | `127.0.0.1:9000` | 宿主机进程访问 MinIO |
| `MYSQL_HOST` / `MYSQL_IDENTITY_HOST` | `127.0.0.1` | 宿主机进程访问本地 MySQL |

DeerFlow 侧的映射分成两处：

1. `OPIEos/extensions_config.json` 的 `kechuang.url` 使用 `$KC_PLATFORM_MCP_URL`，不在 Skill 中写主机路径。
2. 当前本机 `make dev` 从 OPIEos 根目录 `.env` 读取 `KC_PLATFORM_MCP_URL=http://127.0.0.1:8793/mcp/`。如果另行切换到 Docker Gateway，容器内的 `127.0.0.1` 指向容器自身，必须将该变量改成 `http://host.docker.internal:8793/mcp/`；两种运行模式不要混用同一个地址。

本机 Gateway 监听 `127.0.0.1:8001`，供宿主机 backend 调用；日志位于 `OPIEos/logs/gateway.log`。只有 Docker Gateway 模式才通过 Compose 暴露 `8001`。

本机 Compose 职责保持分离：`backend/docker-compose.local.yml` 只启动科创数据依赖；OPIEos `docker-compose-dev.yaml` 管理 DeerFlow Docker 本地运行环境（需要时可只启动 Redis）；当前检出不含额外 OPIEos 稳定模式覆盖。服务器复用已有 DeerFlow，科创业务 Backend 单独部署并通过配置连接现有 Gateway。

## 后续迁移到服务器已有 DeerFlow

迁移时不改科创前端 API，也不把服务器地址写进源码或 Skill。只在服务器运行环境覆盖以下变量：

```dotenv
# 科创 backend -> 服务器 DeerFlow Gateway
DEERFLOW_ENABLED=true
DEERFLOW_BASE_URL=http://<deerflow-host>:<gateway-port>
# DeerFlow 内部调用认证；必须与 DeerFlow 的 DEER_FLOW_INTERNAL_AUTH_TOKEN 相同
DEER_FLOW_INTERNAL_AUTH_TOKEN=<runtime-secret>
# 如果服务器 Gateway 另要求 Bearer/PAT/JWT，再设置；不要提交到 Git
DEERFLOW_API_KEY=<runtime-secret>

# 服务器 DeerFlow Gateway -> 科创 backend MCP
KC_PLATFORM_MCP_URL=http://<backend-host>:8793/mcp/
# 填 DeerFlow 发起 HTTP 请求时使用的 Host:Port，逗号分隔
KC_MCP_ALLOWED_HOSTS=<backend-host>:8793
```

服务器侧还要确认：

- DeerFlow Gateway 能访问 `KC_PLATFORM_MCP_URL`；防火墙只放行必要的 backend MCP 端口。
- 科创 backend 能访问 `DEERFLOW_BASE_URL`；如果 Gateway 不是 `8001`，只改环境变量，不改 `runtime/deerflow_gateway.py`。
- DeerFlow 的 `extensions_config.json` 中 `kechuang.enabled=true`，`url` 保持 `$KC_PLATFORM_MCP_URL`；`headers_from_context` 只映射 `X-KC-Thread-ID -> kc_thread_id` 和 `X-Trace-ID -> kc_trace_id`，缺失时拒绝调用，不转发 `Authorization`。
- backend 调用 Gateway 使用 `X-DeerFlow-Internal-Token` 与 `X-DeerFlow-Owner-User-Id`（由环境变量配置 Header 名），并为同一轮的 thread 与 run POST 发送相等的随机 `csrf_token` Cookie 和 `X-CSRF-Token`。该 pair 只满足 OPIEos 现有双提交校验，不替代内部 Token 身份认证；无需修改 OPIEos CSRF 核心。
- backend 每次运行前幂等调用 `POST /api/threads` 创建/确认线程，再调用 `/api/threads/{thread_id}/runs/stream`；这是当前 OPIEos Gateway 的线程权限约束。
- 追踪关联由科创 backend 请求上下文中的 `trace_id` 发起：backend 将其作为 `kc_trace_id` 传给 DeerFlow，DeerFlow 通过上述映射把 `X-Trace-ID` 传回 MCP；标准化 DeerFlow thread ID 和 Gateway 响应提供的 run ID（若有）与该会话关联。科创审计事件可用 `GET /api/v1/admin/audit-events?trace_id=<trace-id>` 按同一 ID 筛选。不要从公开聊天请求体读取 trace ID。
- DeerFlow 服务器的 `config.yaml`、`DEER_FLOW_CONFIG_PATH`、`DEER_FLOW_EXTENSIONS_CONFIG_PATH`、`DEER_FLOW_HOME` 继续使用服务器自己的路径；不要把本机 `D:/Work/kechuang` 路径复制过去。
- 服务器上的 Skill 根目录以 DeerFlow 有效配置中的 `skills.path` 和容器挂载为准；源码包内业务 Skill 位于 `OPIEos/skills/public/kc-*`，不要假设部署后的宿主机目录一定是根目录 `skills/`。

历史联调证明过“宿主机 backend :8793 ↔ Docker DeerFlow :8001 ↔ MCP”链路；当前已切为宿主机 Gateway。切到服务器后仍须单独验证服务器网络、Gateway 鉴权和远程 MCP 回调，不能把本机 `200` 当成服务器链路已验收。
