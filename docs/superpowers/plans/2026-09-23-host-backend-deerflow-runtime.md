# 宿主机业务后端接入 DeerFlow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让科创 backend 以前台 `python main.py` 运行，并通过配置接入本地测试或服务器已有的 DeerFlow Gateway。

**Architecture:** 保留科创 backend 的认证、业务 API 和 MCP 边界；只把问答执行切换到可配置的 DeerFlow Gateway。当前 Windows 测试由宿主机 `make dev`（项目 uv 虚拟环境）运行 Gateway、由 Conda `py311` 前台运行业务 backend；科创数据依赖使用 `backend/docker-compose.local.yml`。服务器部署复用已运行的 DeerFlow Gateway。

**Compose 文件边界:** 科创 PostgreSQL、Milvus、MinIO、etcd、Attu 由 `backend/docker-compose.local.yml` 管理。OPIEos `docker-compose-dev.yaml` 只负责 DeerFlow 自身的 Docker 本地运行配置（需要时可以只启动 Redis）；当前 Gateway、前端和 Nginx 由宿主机 `make dev` 启动。stable 文件是 OPIEos Docker 前端的可选覆盖。服务器不部署这套本地 Compose，而是接入现有 DeerFlow。

**Tech Stack:** FastAPI、httpx SSE、MCP Streamable HTTP、Docker Compose、PowerShell。

**Spec:** `docs/superpowers/specs/2026-09-23-host-backend-deerflow-runtime.md`

## Global Constraints

- 只修改 `D:/Work/kechuang`。
- 不修改 `D:/Work/kc-rag-agent`。
- 前端 `/api/v1/*` 接口、请求字段和 SSE 公共契约保持不变。
- 不提交 Git；保留现有工作区改动。
- 服务器 DeerFlow 地址不能写死在源码中，必须由环境变量提供。

## Review Focus

- DeerFlow 地址为空时，backend 不应误认为已经切换成功。
- 前台 `python main.py` 启动时，配置必须从 `backend/.env.local` 生效。
- 容器回调宿主机 MCP 时，Host allowlist 不能误拒绝 `host.docker.internal`。
- 服务器部署时，MCP URL 必须能替换为服务器可访问的 backend 地址。
- 科创业务 Backend 独立运行并对外提供 `8793`；服务器部署时直接连接现有 DeerFlow Gateway，不依赖旧的容器化 Backend 端口。

### Task 1: 配置可切换的 DeerFlow 与 MCP 地址

**Files:**
- Modify: `backend/.env.local`（本地忽略配置）
- Modify: `OPIEos-main/extensions_config.json`（本地运行时配置）
- Modify: `OPIEos-main/extensions_config.example.json`
- Modify: `OPIEos-main/docker/docker-compose-dev.yaml`
- Test: `backend/tests/test_mcp_server.py`

- [x] 先确认当前工作区状态，不覆盖已有改动。
- [x] 将 MCP URL 改为 `$KC_PLATFORM_MCP_URL`，由 DeerFlow 加载时解析环境变量。
- [x] 为 Docker Gateway 注入 `KC_PLATFORM_MCP_URL`，默认值仅用于本机测试。
- [x] 为 MCP Host allowlist 增加 `host.docker.internal:*`，并保留现有服务名和回环地址。
- [x] 用配置解析检查和 MCP 测试验证。

### Task 2: 配置宿主机 backend 前台运行

**Files:**
- Modify: `backend/.env.local`
- Modify: `docs/README.md`
- Modify: `docs/DeerFlow迁移与Skill接入方案.md`
- Modify: `docs/superpowers/plans/2026-09-22-deerflow-parallel-migration.md`

- [x] 开启 `DEERFLOW_ENABLED`，设置本地测试 Gateway 默认地址；正式服务器地址只在部署环境覆盖。
- [x] 将宿主机 backend 的 PostgreSQL、Milvus、MinIO 和本地 MySQL 地址切到宿主机可访问地址。
- [x] 记录测试启动顺序、终端日志方式和正式服务器覆盖变量。
- [x] 不启动或删除 Docker 业务 backend 服务。

### Task 3: 分层验证

**Files:**
- No source changes unless a failed focused test identifies a defect.

- [x] 运行 DeerFlow adapter、Gateway、MCP 相关 focused tests（`19 passed`）。
- [x] 验证 Docker Gateway 健康状态和 `/api/threads/.../runs/stream` 可达性；服务间请求先幂等创建线程。
- [x] 在单独终端以 `python main.py` 启动 backend，验证 `http://localhost:8793/api/v1/health`。
- [x] 登录并通过原有 `/api/v1/chat/stream` 验证请求确实到 DeerFlow；本地 smoke 已返回 `start/done`。
