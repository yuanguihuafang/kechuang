# 小规模线上生产 MVP 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 OPIE OS 部署并加固为支持约 20 个用户、管理员复制邀请注册、用户数据严格隔离的单机 Docker Compose 生产 MVP。

**Architecture:** 单台云服务器运行 Frontend、Nginx、单实例单 Worker Gateway 和独立 Sandbox 服务；PostgreSQL 与 Redis 使用托管服务。Gateway 通过认证上下文解析用户身份，所有用户资源访问均进行 owner 校验；注册仅允许管理员创建的一次性邀请链接。

**Tech Stack:** FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL、Redis、Docker Compose、Next.js、Nginx、pytest、Playwright、httpx。

**Spec:** `docs/superpowers/specs/2026-09-12-small-scale-production-mvp-design.md`

## Global Constraints

- Gateway 保持单实例、单 Worker；不通过增加 Worker 实现并发。
- 生产数据库使用 PostgreSQL，`run_events.backend` 使用 `db`。
- 生产 Redis 使用 TLS 和认证，不暴露公网。
- 保持 `sandbox.allow_host_bash: false`，不把 Gateway 变成宿主机命令执行器。
- Sandbox 允许既有配置所需的公网出站访问，但不能访问宿主机、Gateway、PostgreSQL、Redis、Docker API 或其他用户 Sandbox。
- 注册只允许管理员创建的一次性邀请链接，默认 72 小时过期。
- 所有新增 ORM 模型必须提供 Alembic migration；Gateway 启动时执行迁移。
- 所有跨用户访问失败必须返回 403 或 404，不能泄露资源存在性。
- 不在本计划中加入多租户、计费、SCIM、Gateway 多副本或 DLP。

---

### Task 1: 生产配置和托管服务接入

**Files:**
- Create: `config.production.example.yaml`（PostgreSQL、持久化 run events、Redis bridge 的生产覆盖配置）
- Modify: `.env.example`（生产密钥、PostgreSQL、Redis、域名和代理变量）
- Modify: `docker/docker-compose.yaml`（单 Worker、内部网络、健康检查和生产环境变量）
- Modify: `README.md`（Compose PostgreSQL/Redis 生产说明）
- Modify: `AGENTS.md`（生产配置和服务拓扑说明）
- Test: `backend/tests/test_production_config.py`
- Test: `backend/tests/test_compose_default_bind_host.py`

**Interfaces:**
- Consumes: 现有 `AppConfig`、Gateway startup validation、Docker Compose 环境变量。
- Produces: 一组可复制的生产配置约束；启动前能拒绝 SQLite、内存事件存储、缺失密钥和缺失托管服务 URL。

- [ ] **Step 1: 写配置失败测试**：覆盖生产 profile 缺少 `DATABASE_URL`、缺少 Redis URL、`database.backend=sqlite`、`run_events.backend=memory` 和 `GATEWAY_WORKERS>1` 的拒绝行为。
- [ ] **Step 2: 运行配置测试确认失败**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_production_config.py -q`，确认新断言尚未满足。
- [ ] **Step 3: 增加生产覆盖配置**：新增 `config.production.example.yaml`，选择 PostgreSQL、数据库 run events、Redis stream bridge，并保持本地开发默认值不变。
- [ ] **Step 4: 更新 Compose**：加入无宿主机端口的 PostgreSQL 服务，为 Redis 开启密码认证，让 Gateway 使用 `GATEWAY_WORKERS=1` 并依赖两个服务的健康检查。
- [ ] **Step 5: 运行测试和配置校验**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_production_config.py tests/test_compose_default_bind_host.py -q`，再运行 `docker compose -f docker/docker-compose.yaml config`。
- [ ] **Step 6: 更新文档**：在 `README.md`、`AGENTS.md` 和 `frontend/src/content/zh/application/deployment-guide.mdx` 增加 Compose PostgreSQL/Redis、HTTPS、单 Worker 和生产变量说明。
- [ ] **Step 7: Commit**：`git add config.production.example.yaml .env.example docker/docker-compose.yaml backend/tests/test_production_config.py backend/tests/test_compose_default_bind_host.py README.md AGENTS.md frontend/src/content/zh/application/deployment-guide.mdx docs/superpowers/plans/2026-09-12-small-scale-production-mvp.md && git commit -m "chore: add production compose baseline"`

### Task 2: 管理员复制邀请注册

**Files:**
- Create: `backend/packages/harness/deerflow/persistence/invitations/__init__.py`
- Create: `backend/packages/harness/deerflow/persistence/invitations/model.py`
- Create: `backend/packages/harness/deerflow/persistence/invitations/sql.py`
- Create: `backend/packages/harness/deerflow/persistence/audit/model.py`
- Create: `backend/packages/harness/deerflow/persistence/audit/sql.py`
- Create: `backend/app/gateway/auth/invitations.py`
- Modify: `backend/app/gateway/auth/models.py`
- Modify: `backend/app/gateway/auth/errors.py`
- Modify: `backend/app/gateway/routers/auth.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0013_invitations_and_audit.py`
- Test: `backend/tests/test_auth_invites.py`
- Test: `backend/tests/test_auth_invite_migration.py`
- Modify: `frontend/src/app/(auth)/login/page.tsx`
- Create: `frontend/src/app/(auth)/invite/[token]/page.tsx`
- Create: `frontend/src/app/workspace/admin/invitations/page.tsx`
- Create: `frontend/tests/unit/core/auth/invite.test.ts`
- Create: `frontend/tests/e2e-auth/invite-registration.spec.ts`

**Interfaces:**
- Consumes: `UserRepository`, `get_current_user_from_request`, existing admin role checks, existing `RegisterRequest` password validation and session-cookie helpers.
- Produces: `POST /api/v1/auth/admin/invitations`, `GET /api/v1/auth/admin/invitations`, `DELETE /api/v1/auth/admin/invitations/{id}`, and `POST /api/v1/auth/register-by-invite`.

- [ ] **Step 1: 写邀请模型测试**：覆盖 72 小时过期、已使用、已撤销、邮箱规范化、Token 哈希不保存原文和同一邀请并发消费只成功一次。
- [ ] **Step 2: 运行测试确认失败**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_auth_invites.py -q`，确认模型、Repository 和接口尚未存在。
- [ ] **Step 3: 添加 ORM 模型与迁移**：定义邀请状态、目标邮箱、创建者、Token 哈希、过期时间、消费用户和审计字段；在迁移中创建索引和唯一约束，确保 PostgreSQL 与 SQLite 测试路径都可升级。
- [ ] **Step 4: 实现邀请 Repository/Service**：使用安全随机 Token，仅返回原始 Token 一次；消费时使用事务、行锁或等价条件更新保证一次性；过期和撤销邀请不可消费。
- [ ] **Step 5: 实现管理员 API**：所有管理接口要求 `admin`；列表只返回 Token 前缀/状态/时间，不返回可复用的原始 Token；创建和撤销写入 audit 表。
- [ ] **Step 6: 改造注册 API**：保留 `/register` 作为明确的 disabled 响应或迁移兼容入口；新增邀请注册流程，创建用户、消费邀请和设置登录 Cookie 必须在一致的事务边界内完成。
- [ ] **Step 7: 添加前端页面**：登录页隐藏公开注册入口；邀请页展示目标邮箱、过期/失效状态和密码设置表单；管理员邀请页支持创建、复制、撤销和状态刷新。
- [ ] **Step 8: 运行后端与前端测试**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_auth_invites.py tests/test_auth_invite_migration.py -q`；运行 `cd frontend && pnpm rstest run invite`；运行 `cd frontend && pnpm exec playwright test tests/e2e-auth/invite-registration.spec.ts`。
- [ ] **Step 9: Commit**：`git add backend frontend && git commit -m "feat: add admin invitation registration"`

### Task 3: Redis 限流、并发配额和模型用量保护

**Files:**
- Modify: `backend/app/gateway/routers/auth.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/services.py`
- Modify: `backend/app/gateway/routers/runs.py`
- Modify: `backend/app/gateway/routers/thread_runs.py`
- Modify: `backend/packages/harness/deerflow/config/auth_config.py`
- Modify: `backend/packages/harness/deerflow/config/app_config.py`
- Modify: `config.example.yaml`
- Create: `backend/app/gateway/rate_limit.py`
- Create: `backend/app/gateway/quota.py`
- Test: `backend/tests/test_redis_rate_limit.py`
- Test: `backend/tests/test_run_quota.py`
- Test: `backend/tests/test_thread_token_usage.py`

**Interfaces:**
- Consumes: Redis URL、当前用户上下文、RunManager admission、已有 token usage 持久化。
- Produces: 共享的 `RateLimiter` 和 `QuotaService`，在注册/登录/邀请/运行创建入口执行统一限流和配额检查。

- [ ] **Step 1: 写限流与配额测试**：覆盖同一 IP 的登录失败、同一邮箱的邀请创建、单用户第三个并发运行、全局超过 10 个运行和超过每日 Token 上限。
- [ ] **Step 2: 运行测试确认失败**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_redis_rate_limit.py tests/test_run_quota.py -q`，确认服务接口尚未实现。
- [ ] **Step 3: 实现 Redis 限流器**：使用带 TTL 的原子计数/脚本，key 至少包含场景和规范化主体；返回剩余等待秒数；Redis 不可用时对注册、登录和新运行采用 fail-closed 或明确的临时不可用响应。
- [ ] **Step 4: 实现运行配额**：在运行真正 admission 前检查用户并发和全局并发；终态运行释放计数；异常退出和 Gateway 重启通过数据库状态恢复计数。
- [ ] **Step 5: 实现 Token 配额**：从现有 token usage 记录按用户/UTC 日期聚合，超限时阻止新的模型运行，并向前端返回可识别错误码。
- [ ] **Step 6: 运行后端测试与格式检查**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_redis_rate_limit.py tests/test_run_quota.py tests/test_thread_token_usage.py -q` 和 `make lint`。
- [ ] **Step 7: Commit**：`git add backend config.example.yaml && git commit -m "feat: add shared rate limits and run quotas"`

### Task 4: 独立 Sandbox 容器和网络边界

**Files:**
- Modify: `config.example.yaml`
- Modify: `docker/docker-compose.yaml`
- Modify: `docker/docker-compose.dood.yaml`
- Modify: `docker/provisioner/app.py`
- Modify: `backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py`
- Modify: `scripts/setup-sandbox.sh`
- Create: `backend/tests/test_production_sandbox_policy.py`
- Create: `scripts/validate_production_sandbox.py`
- Modify: `docker/provisioner/README.md`
- Modify: `frontend/src/content/zh/application/deployment-guide.mdx`

**Interfaces:**
- Consumes: 现有 AIO Sandbox/Provisioner provider、`sandbox.allow_host_bash`、线程用户数据路径和 Provisioner API key。
- Produces: Gateway 不直接执行宿主机命令；Provisioner 在私有网络中创建受限 Sandbox；公网出站可用但内部地址不可达。

- [ ] **Step 1: 写 Sandbox policy 测试**：断言生产配置保持 `allow_host_bash=false`、无宿主机敏感目录挂载、Provisioner API key 必填、Sandbox 资源上限存在、Gateway 不直接暴露 Docker Socket。
- [ ] **Step 2: 运行测试确认当前差距**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_production_sandbox_policy.py -q`，记录当前 compose/config 的不满足项。
- [ ] **Step 3: 隔离容器创建权限**：让需要 Docker 创建权限的路径仅存在于独立 Provisioner 服务；Gateway 通过内网 API 和 API key 调用，不直接挂载 Socket。
- [ ] **Step 4: 固化 Sandbox 约束**：设置 CPU、内存、PID、临时磁盘、最大输出、执行时间、全局并发和清理策略；拒绝访问 Postgres、Redis、Gateway、Docker API、宿主机和云元数据地址。
- [ ] **Step 5: 保留现有公网出站能力**：允许现有搜索/MCP 所需的 80/443 出站，增加私网/回环/元数据地址阻断测试；不改变现有工具权限集合。
- [ ] **Step 6: 运行 Sandbox 测试**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_production_sandbox_policy.py tests/test_sandbox_tools_security.py -q`，再运行 `PYTHONPATH=backend uv run python scripts/validate_production_sandbox.py`。
- [ ] **Step 7: Commit**：`git add config.example.yaml docker scripts backend/tests/test_production_sandbox_policy.py frontend/src/content/zh/application/deployment-guide.mdx && git commit -m "feat: harden isolated sandbox deployment"`

### Task 5: 全资源用户隔离回归

**Files:**
- Modify: `backend/tests/test_owner_isolation.py`
- Modify: `backend/tests/test_uploads_router.py`
- Modify: `backend/tests/test_stateless_runs_owner_isolation.py`
- Modify: `backend/tests/test_memory_storage_user_isolation.py`
- Modify: `backend/tests/test_update_agent_e2e_user_isolation.py`
- Modify: `backend/tests/test_scheduled_task_service.py`
- Create: `backend/tests/test_cross_user_http_isolation.py`
- Create: `frontend/tests/e2e-auth/cross-user-isolation.spec.ts`
- Modify: `backend/app/gateway/routers/threads.py`
- Modify: `backend/app/gateway/routers/runs.py`
- Modify: `backend/app/gateway/routers/artifacts.py`
- Modify: `backend/app/gateway/routers/uploads.py`
- Modify: `backend/app/gateway/routers/memory.py`
- Modify: `backend/app/gateway/routers/agents.py`
- Modify: `backend/app/gateway/routers/scheduled_tasks.py`

**Interfaces:**
- Consumes: 现有 `require_permission(..., owner_check=True)`、用户上下文和所有用户资源路由。
- Produces: 可重复运行的 Alice/Bob HTTP 隔离测试矩阵；所有失败响应均为 403/404 且不包含对方资源内容。

- [ ] **Step 1: 写跨用户 API 测试**：创建 Alice/Bob 会话和资源，逐一替换 Thread、Run、文件、Memory、Agent、Scheduled Task、SSE 资源 ID，断言 Bob 无法读取、修改、删除或订阅 Alice 数据。
- [ ] **Step 2: 写身份伪造测试**：在请求 metadata/body/config 中提交 `user_id`、`owner_id`、`is_internal`、`authz_attributes`，断言服务端使用认证上下文而不是客户端值。
- [ ] **Step 3: 运行隔离测试确认现有行为**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_cross_user_http_isolation.py -q`，将任何失败分类为“缺测试保护”或“需要修复路由”。
- [ ] **Step 4: 修复缺失 owner check**：只在测试证明缺失的具体路由增加服务端 owner 检查；不做无关重构；对资源不存在和非 owner 统一使用不泄露存在性的响应。
- [ ] **Step 5: 增加浏览器 E2E**：用两个浏览器上下文验证邀请注册后的两个用户不能跨用户访问 Thread、文件和 Agent 页面。
- [ ] **Step 6: 运行完整隔离回归**：运行 `cd backend && PYTHONPATH=. uv run pytest tests/test_cross_user_http_isolation.py tests/test_owner_isolation.py tests/test_uploads_router.py tests/test_stateless_runs_owner_isolation.py tests/test_memory_storage_user_isolation.py tests/test_update_agent_e2e_user_isolation.py tests/test_scheduled_task_service.py -q`；再运行 `cd frontend && pnpm exec playwright test tests/e2e-auth/cross-user-isolation.spec.ts`。
- [ ] **Step 7: Commit**：`git add backend frontend && git commit -m "test: verify cross-user resource isolation"`

### Task 6: 备份、监控、运维和发布检查

**Files:**
- Modify: `frontend/src/content/zh/application/deployment-guide.mdx`
- Modify: `frontend/src/content/zh/application/operations-and-troubleshooting.mdx`
- Modify: `README.md`
- Create: `scripts/production_preflight.py`
- Create: `scripts/production_smoke_test.py`
- Create: `scripts/mvp_load_test.py`
- Create: `docs/runbooks/production-mvp-backup-restore.md`
- Create: `docs/runbooks/production-mvp-incident-response.md`
- Create: `docs/runbooks/production-mvp-release-rollback.md`
- Test: `backend/tests/test_production_preflight.py`

**Interfaces:**
- Consumes: Compose 配置、`/health`、认证接口、PostgreSQL/Redis URL、已有日志和运行事件。
- Produces: 可重复的上线前检查、双用户 Smoke Test、20 用户并发脚本、备份恢复和回滚手册。

- [ ] **Step 1: 写 preflight 测试**：覆盖域名/HTTPS 变量、固定密钥、托管数据库/Redis URL、单 Worker、禁止 Host Bash、镜像不可为 `latest`、备份目标存在。
- [ ] **Step 2: 实现生产 preflight**：让脚本在部署前输出逐项 PASS/FAIL，并在关键安全条件失败时返回非零退出码。
- [ ] **Step 3: 实现 Smoke Test**：创建管理员、创建邀请、完成两个用户注册、创建 Thread、上传文件、运行最小 Agent、验证历史读取和退出登录。
- [ ] **Step 4: 实现并发测试脚本**：使用 `httpx.AsyncClient` 创建 20 个测试会话，最多同时启动 8～10 个运行，记录 P50/P95、错误率、SSE 断开和资源配额拒绝。
- [ ] **Step 5: 编写备份恢复 Runbook**：记录 PostgreSQL 恢复、`.deer-flow` 文件恢复、密钥恢复、Gateway 重启、Redis 重建和验证顺序；明确 RPO 24 小时、RTO 4 小时目标。
- [ ] **Step 6: 编写故障和回滚 Runbook**：覆盖 Gateway、数据库、Redis、Sandbox、模型服务、磁盘空间和证书异常；保留上一版本镜像和配置回滚步骤。
- [ ] **Step 7: 运行发布前检查**：运行 `PYTHONPATH=backend uv run python scripts/production_preflight.py`、`PYTHONPATH=backend uv run python scripts/production_smoke_test.py` 和 `PYTHONPATH=backend uv run python scripts/mvp_load_test.py --users 20 --max-concurrency 10`。
- [ ] **Step 8: Commit**：`git add README.md frontend/src/content/zh/application docs/runbooks scripts backend/tests/test_production_preflight.py && git commit -m "docs: add production MVP operations and release checks"`

### Task 7: 全量验证和上线门禁

**Files:**
- Modify: `.github/workflows/backend-unit-tests.yml`
- Modify: `.github/workflows/e2e-tests.yml`
- Modify: `.github/workflows/container.yaml`
- Create: `.github/workflows/production-mvp-gate.yml`
- Modify: `backend/Makefile`
- Modify: `frontend/package.json`

**Interfaces:**
- Consumes: Tasks 1～6 的测试、preflight、Smoke Test、负载测试和固定镜像构建结果。
- Produces: 合并和发布前的自动化门禁，失败时不会误报“可上线”。

- [ ] **Step 1: 写 CI 门禁配置测试**：断言生产 gate 执行后端隔离测试、前端邀请 E2E、Compose config 校验、镜像构建和 preflight。
- [ ] **Step 2: 添加 workflow**：将生产 MVP gate 绑定到 production 配置、Docker Compose、认证、Sandbox、路由和迁移相关变更；为长时负载测试设置明确超时。
- [ ] **Step 3: 添加 Makefile 入口**：在 `backend/Makefile` 增加 `test-production-isolation`，在 frontend/package scripts 增加邀请与跨用户 E2E 的稳定命令。
- [ ] **Step 4: 运行全量验证**：运行 `cd backend && make lint && make test`、`cd frontend && pnpm check && pnpm test`、`cd frontend && pnpm exec playwright test`、`docker compose -f docker/docker-compose.yaml config`。
- [ ] **Step 5: 进行 Staging 演练**：固定镜像部署到 staging，执行 preflight、Smoke、双用户隔离、20 用户负载、Gateway 重启、备份恢复和回滚。
- [ ] **Step 6: 生成上线记录**：记录镜像版本、数据库迁移版本、配置校验结果、压测结果、恢复演练结果和回滚点。
- [ ] **Step 7: Commit**：`git add .github backend/Makefile frontend/package.json && git commit -m "ci: gate production MVP release"`

## 依赖顺序

```text
Task 1
  ↓
Task 2 ─────┐
  ↓         │
Task 3      │
  ↓         │
Task 4      ├── Task 5
            │      ↓
            └── Task 6
                   ↓
                 Task 7
```

Task 1 必须先完成，因为邀请、限流和运行配额都依赖托管 PostgreSQL/Redis。Task 2 和 Task 4 可以并行开发，但 Task 5 必须等资源边界稳定后执行。Task 6、Task 7 在功能和隔离测试完成后执行。

## 交付顺序

1. 先完成 Task 1，确保 staging 可以使用 PostgreSQL/Redis 启动。
2. 完成 Task 2，关闭公开注册并上线管理员复制邀请流程。
3. 完成 Task 3，防止 20 用户互相争抢资源或造成成本失控。
4. 完成 Task 4，确认 Sandbox 不扩大当前权限。
5. 完成 Task 5，形成可重复的跨用户隔离证明。
6. 完成 Task 6，建立恢复和上线操作能力。
7. 完成 Task 7，通过自动化门禁后再部署第一批真实用户。
