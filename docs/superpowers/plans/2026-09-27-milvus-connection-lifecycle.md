# Milvus 进程级连接复用实施计划

> **实施方式：**在当前工作区由主协调者顺序实施；连接、Agent 与 lifespan 属同一生命周期链，不并行修改。

**目标：**每个 Backend worker 复用一个 Retrieval Agent 和一个共享 Milvus 连接，应用关闭时统一断开。

**架构：**检索路由使用线程安全的 worker 级上下文；单个 SearchService 创建一个共享连接管理器并注入所有 Collection 的 MilvusService；Collection 保留独立句柄与过滤逻辑；Gateway lifespan 通过 Retrieval 路由模块的显式关闭接口释放资源。

**范围：**不改变 HTTP、SSE、MCP、鉴权、查询参数、权限过滤和 Milvus 数据；不改 DeerFlow/OPIEos；保留 `backend/runtime/bootstrap.py` 中已有的 DeerFlow 内部认证参数改动。

## 约束

- 每个测试先证明旧行为会失败，再实现对应最小改动。
- 不在请求结束时关闭共享连接；恢复时仅由共享管理器使连接失效并递增代次，各 Collection 下一次访问时刷新自己的句柄。
- lifespan 关闭只断开一次；连接建立仍为懒加载，不在启动时探测或预连所有 Collection。
- 不触碰工作区其他已有变更，不提交代码。

## Task 1: Retrieval Agent worker 级上下文

**Consumes:** 当前 `agents.retrieval_agent.routes._context()` 与 Gateway 的 `configure_task_store/configure_task_runner` 注入入口。

**Produces:** 线程安全、跨请求共享的惰性 Retrieval route context；模块级 `close()` 关闭并清空它。

1. 新增测试 `test_retrieval_route_context_is_shared_across_contextvars` 和 `test_retrieval_route_context_initializes_once_concurrently`：在独立 Context 与并发首次访问下断言 context/Agent 同一且只构造一次。
2. 运行 `pytest -q backend/tests/test_retrieval_route_lifecycle.py`，预期因当前每个 ContextVar 上下文各自创建 Agent 而失败。
3. 用一个模块级惰性 context 和 `threading.Lock` 替换 ContextVar；保留任务存储、Runner 注入接口。
4. 实现 route `close()`，确保 Agent close 后即使抛异常也清空共享 context。
5. 重跑上述测试，预期通过。

## Task 2: Collection 共用 Milvus 连接

**Consumes:** Task 1 的单个 worker Retrieval Agent，以及现有 `MilvusService` 延迟连接和恢复重试。

**Produces:** SearchService 持有唯一 `MilvusConnectionManager`，所有 Collection service 用同一 alias、锁和连接代次。

1. 新增 `test_milvus_services_share_connection_and_refresh_collection_after_reconnect` 与并发首次连接测试；mock 仅限 PyMilvus 外部 I/O。
2. 运行 `pytest -q backend/tests/test_milvus_connection_lifecycle.py`，预期当前独立 alias 会使 connect 次数/alias 断言失败。
3. 在 `milvus_service.py` 增加最小连接管理器；`SearchService` 将同一个实例传给全部公开与私有 Collection；各服务按管理器 generation 清理自己的 Collection 句柄。
4. 将恢复时的 disconnect/invalidate 委托给管理器；普通请求不关闭连接，SearchService close 统一关闭。
5. 重跑新测试及 `pytest -q backend/tests/test_mysql_sync_safety.py backend/tests/test_rebuild_sync_contract.py`，预期全通过。

## Task 3: 明确资源关闭接口并接入 lifespan

**Consumes:** Task 1 的 route `close()`、Task 2 的 SearchService/manager close。

**Produces:** `RetrievalAgentEndpoint.close()` 内部生命周期契约，以及 Gateway `finally` 中可观测、恰好一次的释放。

1. 新增生命周期测试，断言 route close 会关闭 SearchService 管理器且重复调用不重复 disconnect。
2. 扩展 `runtime.contracts` 中的 Retrieval endpoint 类型，在 `GatewayRuntime` 明确使用该类型；不要求 Perception endpoint 实现无关的 close。
3. 在 Gateway lifespan `finally` 中调用 retrieval endpoint `close()`，异常通过 logger.exception 留痕并继续既有清理。
4. 运行路由、manager 和 runtime 相关测试；验证首次 connect 仍由首个 Milvus 操作触发。

## Task 4: 回归与文档同步

**Consumes:** Task 1-3 的最终生命周期与测试行为。

**Produces:** 文档与当前实现一致的说明及可复核验证结果。

1. 运行检索、Milvus、安全和 runtime 相关测试；执行后端语法/导入检查。
2. 检查 HTTP 路由/请求响应模型差异为零；核对 `bootstrap.py` 的预存认证改动仍在。
3. 更新设计文档状态、`docs/Agent.md`、README 或启动/迁移说明中连接生命周期和多 worker 说明；只记录实测结果，不声称未运行的 E2E。

## 重点审查的失效模式

1. **并发首次连接：**锁内只允许一个 connect，其他 Collection 复用其 alias。
2. **连接恢复：**连接代次变化必须清理各自的 Collection 句柄，避免重用失效客户端对象。
3. **错误关闭边界：**单个 Collection 恢复不得留下其他服务仍认为连接有效；请求结束不关闭 worker 连接。
4. **跨应用测试/重启：**关闭后释放 worker Agent，上一个 runtime 的任务存储或 Runner 不得泄漏到下一次 lifespan。
5. **业务合同回归：**连接复用不改变 Collection 映射、访问控制、HTTP/MCP 参数或返回结构。

## 实施后报告

- 列出实际修改的文件和测试结果。
- 若本机服务可用，再由用户重启后检查日志：首次访问每个 worker 只出现一次连接成功；后续管理计数、草稿删除和问答检索不再重复初始化。

## Execution ledger

执行进度与计划偏离记录位于 `.superpowers/sdd/2026-09-27-milvus-connection-lifecycle/progress.md`。
