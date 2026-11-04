# 宿主机业务后端接入 DeerFlow 运行方案

## 目标

科创前端继续使用现有 `/api/v1/*` 接口；业务 backend 由宿主机执行 `python main.py`，日志直接输出到终端；问答执行通过配置连接 DeerFlow Gateway。当前 Windows 测试由宿主机 `make dev` 启动 Gateway，正式部署时改为服务器已有 DeerFlow 地址。

## 边界

- 只修改 `D:/Work/kechuang`。
- 不删除、不修改 `D:/Work/kc-rag-agent`。
- 不改变前端请求路径、请求字段和 SSE 公共响应契约。
- 当前本机测试中，DeerFlow Gateway 与业务 backend 都由宿主机启动（分别为 `make dev` 和 `python main.py`）；Docker 只负责 PostgreSQL、Milvus、MinIO、etcd 等基础设施。
- DeerFlow MCP 的业务回调地址通过 `KC_PLATFORM_MCP_URL` 配置，不能写死业务服务名。

## 运行拓扑

```text
科创前端 :8794
    │ /api/v1/*
    ▼
宿主机 backend :8793  ← python main.py，终端查看日志
    │ /api/threads/{thread_id}/runs/stream
    ▼
DeerFlow Gateway（本机宿主机 :8001 或服务器地址）
    │ MCP
    ▼
宿主机 backend /mcp
```

## 配置约定

- `DEERFLOW_ENABLED=true` 开启 DeerFlow 适配器。
- `DEERFLOW_BASE_URL` 指向 DeerFlow Gateway；本地测试使用 `http://127.0.0.1:8001`，正式环境填写服务器可访问地址。
- `KC_PLATFORM_MCP_URL` 指向业务 backend 的 MCP 地址；当前本机宿主机 Gateway 使用 `http://127.0.0.1:8793/mcp/`。若 Gateway 改为 Docker 容器运行，则使用 `http://host.docker.internal:8793/mcp/`；服务器部署使用服务器能访问的 backend 地址。
- `KC_MCP_ALLOWED_HOSTS` 控制 MCP Host 允许列表，默认保留已有容器服务名、回环地址和 `host.docker.internal`。
- 宿主机 backend 使用宿主机端口访问 PostgreSQL、Milvus、MinIO 和本地 MySQL。

## 验收标准

1. `python main.py` 前台启动，健康检查可用，终端能看到日志。
2. 科创前端不改 API 调用方式即可发起问答。
3. backend 的问答流进入 DeerFlow Gateway，而不是旧内嵌 Supervisor。
4. DeerFlow 能通过 MCP 回调业务 backend 的知识检索和供需匹配工具。
5. 未配置真实服务器地址时，不宣称服务器链路已验证；本地验证宿主机 Gateway、backend 与 Docker 基础设施之间的链路。
