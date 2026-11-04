# 科创 Agent 模块

- `perception_agent`：上传资料解析、解析任务与结构化字段提取。HTTP 适配在 `perception_agent/routes.py`，解析器实现统一位于 `services/document_parsing/`。
- `retrieval_agent`：检索请求、字段策略、向量召回与重排。检索 HTTP 适配在 `retrieval_agent/routes.py`，应用与检索流程分别位于 `application/`、`retrieval/`。
- `agents/routes.py` 汇总两个 Agent 的路由，由 `api/v1/router.py` 挂载。
- 聊天编排不在本目录：`api/v1/execution/supervisor.py` 调用 DeerFlow 适配器；旧 `supervisor_agent` 执行图已移除。

依赖方向：HTTP 路由调用 Agent/应用服务；解析、检索实现由 `services/` 与对应领域模块提供。业务服务不得反向依赖 HTTP 路由。
