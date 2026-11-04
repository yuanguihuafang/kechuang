# Milvus 进程级连接复用设计

**状态：已实现；完整后端测试 506 项通过**  
**日期：2026-09-27**

## 目标

消除科创 Backend 在不同 HTTP 请求中重复创建 Retrieval Agent 和 Milvus 连接的问题。管理控制台与智能问答保留各自的业务服务、权限和操作边界，但在同一 Backend worker 内复用连接到相同 Milvus 地址、数据库和凭据的一个连接。

## 变更前问题

- `backend/agents/retrieval_agent/routes.py` 通过默认值为空的 `ContextVar` 懒创建 `_RouteContext`；请求上下文没有已有值时会重新创建 `RetrievalAgent`。
- `SearchService` 为每个公开 Collection 和私有 Collection 分别创建 `MilvusService`；`MilvusService` 当前为每个实例生成随机连接别名，并在首次操作时调用 `connections.connect()`。
- 日志样本中一次 `mysql-counts?refresh=true` 出现五条同一 trace 的“Milvus 连接成功”；多次草稿删除也分别出现连接日志。
- 应用关闭流程当前没有统一关闭这些 Milvus 连接的步骤。

## 决策

1. **业务层继续分开。**问答通过检索服务执行查询；管理控制台通过管理服务执行计数、删除和重建，并保留各自的授权及访问策略。
2. **连接按进程共享，而不是按 Collection 或业务入口拆分。**同一 worker 中，使用同一连接配置的所有 Collection 服务共用一个进程级 Milvus 连接管理器和 alias。Collection 名称、过滤表达式及各业务方法仍由各自的 `MilvusService` 管理。
3. **继续懒连接。**应用启动时装配复用对象，不因启动而强制连接全部 Collection。每个 worker 在第一次实际访问 Milvus 时建立连接，后续请求复用；多个 worker 各自维护自己的进程内连接。
4. **共享连接由单一所有者管理。**连接建立和断开由连接管理器串行化；Collection 专属的客户端状态仍按 Collection 保存。连接恢复后，旧 Collection 句柄必须能被识别并重新获取。不得让某个 Collection 服务单独断开共享 alias。
5. **应用生命周期负责释放。**Backend lifespan 正常关闭时统一断开连接一次。请求结束不关闭连接。已有 Milvus 短暂故障重试策略保留；对共享连接的断开/重连由连接管理器协调。
6. **不改变业务合同。**HTTP、SSE、MCP 请求和响应、权限过滤、数据库配置、Collection 命名、向量数据均不变；不改 DeerFlow/OPIEos，不增加运行时依赖，不提供静默降级或兼容分支。

## 实际模块边界

- `backend/agents/retrieval_agent/routes.py`：持有 worker 级、可复用的 Retrieval Agent 上下文；配置的任务存储和 Runner 仍供问答及管理共用。
- `backend/agents/retrieval_agent/retrieval/search_service.py`：创建并共享一个连接管理器给不同 Collection 的 Milvus 服务。
- `backend/agents/retrieval_agent/application/retrieval_service.py`：关闭时委托 SearchService 释放共享连接；不再创建独立私有 Collection 连接。
- `backend/services/vector/milvus/milvus_service.py`：Collection 专属操作委托给共享连接管理器；处理每个 Collection 的句柄缓存与恢复代次。
- `backend/runtime/contracts.py`、`backend/runtime/container.py`、`backend/runtime/embedded_agents.py`：显式声明带资源关闭能力的 Retrieval endpoint。
- `backend/runtime/bootstrap.py`：在应用生命周期结束时关闭共享 Milvus 连接；保留当前工作区已有的 DeerFlow 认证改动。

实际文件集合见[实施计划](../plans/2026-09-27-milvus-connection-lifecycle.md)；未重构其他存储或请求路径。

## 验收标准

- 同一 worker 内不同 Collection 服务使用相同连接配置时只触发一次底层 `connections.connect()`；不同 Collection 仍调用各自的集合，不串集合名和查询条件。
- 同一 Collection 的重复请求、管理台数量刷新和问答检索复用已建立的连接；并发首次访问不会重复建立连接。
- 连接故障后的既有重试行为保留；连接重建后各 Collection 可继续操作，不使用失效句柄。
- 正常关闭应用时共享连接恰好断开一次；连接清理异常不能静默吞掉。
- 管理控制台与问答仍分别经过原业务入口、权限检查和用户/知识范围过滤；不改变任何前端 API/MCP 参数与响应。
- 针对性单元测试及后端相关回归通过；在本机已运行服务可用时，再分别验证管理计数、管理删除和问答检索的连续请求没有重复连接初始化日志。

## 未纳入范围

- 为管理控制台与问答设置独立 Milvus 连接池、独立数据库或独立限流队列。
- 修改前端、DeerFlow/OPIEos、Milvus 数据结构、环境变量或部署端口。
- 未经性能证据引入连接池规模参数、缓存层、后台管理任务或新的依赖。

## 验证结果

- Conda `py311`：`python -m pytest -q`，506 passed，1 条 Starlette/httpx 弃用警告。
- 定向 Ruff `F/E4/E7/E9` 与改动 import 检查通过。
- 本轮没有重启本机服务或执行在线 Milvus E2E；服务重启后仍需观察一次管理计数、草稿操作和问答检索，确认同一 worker 只有首次访问出现连接成功日志。
