# 科创服务助手

当前项目位于 `D:\Work\kechuang`，以 `OPIEos`（DeerFlow）作为 Agent 运行底座。科创业务后端仍负责现有 `/api/v1/*` API、身份/权限、检索、草稿、文件和审计；DeerFlow 负责 `kechuang` Agent 的 Harness、上下文、Skill 编排和运行流。

## 当前调用边界

```text
科创测试前端 :8794
  └─ HTTP SSE：POST /api/v1/chat/stream
       └─ 科创后端 :8793
            ├─ HTTP：POST /api/threads
            ├─ HTTP SSE：POST /api/threads/{thread_id}/runs/stream
            └─ MCP Streamable HTTP：/mcp/（Agent 调用科创业务工具）
```

聊天运行流是 OPIEos Gateway 的 HTTP/SSE API，不是 MCP Streamable HTTP。前端只访问科创业务 API，不直接访问 DeerFlow Gateway 或 MCP。后端对前端继续使用既有 `start`、`progress`、`token`、`done`、`error` 五类 SSE 事件；MCP `/mcp/` 是独立的工具通道。

`kechuang` Agent 资产位于 `OPIEos/agents/kechuang/`，运行副本和 Skill 加载路径以部署环境的 owner、配置及挂载为准。科创 Backend 与 DeerFlow 通过配置连接，科创业务 MCP 的身份由 Backend 根据会话恢复。Gateway 不提供 Agent 级 MCP 工具隔离，数据权限由 Backend 校验。见[双服务器部署步骤](docs/科创与DeerFlow双服务器迁移部署.md)、[迁移方案](docs/方案/DeerFlow迁移与Skill接入方案.md)和[OPIEos 修改记录](docs/方案/OPIEos-main修改记录.md)。

另有独立的只读知识库 MCP 项目 `D:\Work\kechuang-knowledge-mcp`，不随本后端自动启动，也不是本后端的数据访问必经层；其六个工具和部署说明见该项目的 `kechuang-MCP.md`。它允许调用方自行选择内外部查询模式，这不是身份认证，部署时应限制网络访问范围。

问答只运行 DeerFlow；旧科创意图/规划/推理/Supervisor 执行图已删除，无配置时明确返回 503。会话状态在 `services/application/conversation_state.py`，共享合同在 `core/contracts`，资料处理和管理服务仍归业务后端。

## 本机启动

首次配置后端时，可将 `backend/.env.example` 复制为 `backend/.env`，按实际环境填写数据库、模型及服务地址和凭据；已有 `.env` 时不要覆盖。`backend/.env.local` 可用于本机覆盖，实际配置和密钥不要提交到 Git。

服务由用户手动启动。PostgreSQL、Milvus、MinIO、etcd 和 Attu 使用 `backend/docker-compose.local.yml`；OPIEos/DeerFlow Gateway 在 `OPIEos` 运行 `make dev`；科创后端在 Conda `py311` 运行 `python main.py`。科创测试前端可选，默认使用端口 `8794`。

完整命令、运行目录、环境变量、健康检查和故障排查见[Windows 本机启动说明](docs/项目启动说明.md)。

## 历史在线快照（2026-09-24，不代表当前运行状态）

最近一次只读探测中，科创后端 `http://127.0.0.1:8793/api/v1/health` 返回 HTTP 200；8001 有监听进程，但绕过代理请求 OPIEos `http://127.0.0.1:8001/health` 在 4 秒内未收到响应。此前的 Gateway 健康检查成功属于较早的运行快照，不代表当前进程状态。服务尚未重启，当前没有重启后的问答端到端验收结论。

## 文档入口

- [项目文档索引与当前状态](docs/README.md)
- [文档总目录 Agent.md](docs/Agent.md)
- [DeerFlow 迁移与 Skill 接入方案](docs/方案/DeerFlow迁移与Skill接入方案.md)
- [前后端 API 接口文档](docs/前后端API接口文档.md)
- [问答界面前端契约](docs/前端API/问答界面功能说明.md)
- [意图路由与 Skill 拆分方案](docs/方案/意图路由与Skill拆分方案.md)

`D:\Work\kc-rag-agent` 是旧的独立项目，仅供历史参考；独立只读知识库 MCP 位于 `D:\Work\kechuang-knowledge-mcp`，与本仓库分别维护。
