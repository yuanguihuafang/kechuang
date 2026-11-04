# Observability

本目录提供 Backend 的日志、指标、Trace Context、健康检查和运行告警实现；应用入口由 `backend/main.py` 与 `runtime` 装配。

- `logging.py`：结构化日志配置与上下文。
- `trace_context.py`：Trace ID 与 W3C trace 上下文。
- `metrics.py`：进程内运行指标。
- `system.py`：健康状态、运行指标与运维告警汇总。
