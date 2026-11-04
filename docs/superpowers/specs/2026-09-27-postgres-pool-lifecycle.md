# PostgreSQL 连接池预算与生命周期

**状态：已实现**  
**日期：2026-09-27**

## 目标与边界

为科创 Backend 每个 worker 的 PostgreSQL 连接设置明确上限，并在 Gateway lifespan 正常关闭时关闭 psycopg、SQLAlchemy 和 LangGraph Checkpointer 连接池。一个资源关闭失败时仍尝试关闭其他资源，最后记录清理错误。

只修改 `D:\Work\kechuang\backend`。不连接/重启服务，不修改 HTTP、MCP、鉴权或前端接口；MySQL、MinIO、Milvus 与 etcd 不在本次范围。OPIEos/DeerFlow 源码不改。

## 池预算

默认每个 worker、每个唯一 PostgreSQL DSN 的最大连接数为：

| 池 | 默认上限 | 环境变量 |
|---|---:|---|
| 同步 psycopg | 8 | `POSTGRES_POOL_MIN_SIZE`、`POSTGRES_POOL_MAX_SIZE`、`POSTGRES_POOL_TIMEOUT_SECONDS` |
| SQLAlchemy | 4 + 2 overflow | `POSTGRES_SQLALCHEMY_POOL_SIZE`、`POSTGRES_SQLALCHEMY_MAX_OVERFLOW` |
| LangGraph Checkpointer | 4 | `LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE`、`LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE`、`LANGGRAPH_CHECKPOINT_POOL_TIMEOUT_SECONDS` |

合计上限为每 worker/DSN 18 条；部署总上限按 `worker 数 × 唯一 DSN 数 × 每 DSN 池上限` 估算。min/max、池容量与超时配置必须合法，否则启动/首次建池时明确报错，不静默修正，也不切换为直连。

## 生命周期与失败语义

- 连接池为进程/worker 内共享资源，请求结束不关闭。
- Gateway 停止后台任务后关闭 Checkpointer，再关闭同步 psycopg 与 SQLAlchemy 池。
- 单个池关闭失败不阻止其他池关闭；清理错误需要记录。Checkpointer 关闭失败也不得跳过同步池关闭。
- psycopg-pool 是必需依赖；删除未安装时改用 `psycopg.connect` 的直连回退。
- 同步 psycopg、SQLAlchemy 与 Checkpointer 的 min/max/overflow/timeout 配置均校验边界；无效值直接报错，不静默调大池容量。

## 验收

- SQLAlchemy 引擎按 DSN 复用，默认及环境变量池限制生效；无效的大小配置被拒绝。
- 关闭时所有已创建 SQLAlchemy 引擎及 psycopg 池都被尝试释放；某一个失败不会跳过其余资源。
- Checkpointer 清理错误不会跳过同步池清理。
- 相关回归测试通过；全量 pytest 本轮运行后长时间停滞并主动中断，未获得完整结果。
- 改动文件 Ruff 的 F/E4/E7/E9 检查通过；本轮未做在线数据库 E2E。

## 实施与验证

- SQLAlchemy 引擎改为按 DSN 显式注册，默认 `pool_size=4`、`max_overflow=2`；关闭时逐个 `dispose()`。
- psycopg-pool 改为必需导入，默认 `min_size=1`、`max_size=8`；移除直连回退。
- Checkpointer 默认 `min_size=1`、`max_size=4`；Gateway 关闭时先尝试关闭 Checkpointer，再关闭同步池；关闭异常会记录且不跳过其他池。
- Conda `py311`：数据库资源相关测试 53 passed；F/E4/E7/E9 检查通过。全量 pytest 曾运行但因长时间停滞被中断，无完整汇总。
