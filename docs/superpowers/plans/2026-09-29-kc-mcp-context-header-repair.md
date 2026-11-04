# 本地科创 MCP 会话上下文修复

## 目标与判断

修复本机 KC Backend 经 DeerFlow 调用 MCP 草稿工具时缺少 `X-KC-Thread-ID`、导致“科创会话上下文缺失/不存在”的问题。最终根因已由真实 MCP 调用复现：自定义拦截器从 `langgraph.config.get_config()` 读取 `context.secrets`，但 MCP 子工具 RunnableConfig 未包含该请求级上下文；MCP 适配器提供的 `MCPToolCallRequest.runtime.context` 才携带本轮运行上下文。拦截器改从该 runtime context 读取 secrets，并继续缺失即拒绝。

## 范围与约束

- 优先只改科创 Backend 或 OPIE 的定制扩展/Agent/Skill；不改 DeerFlow/OPIE 核心实现，不覆盖用户现有运行配置。
- 只为配置了 `headers_from_context` 的 MCP Server 注入请求头；缺少必需 secret 时拒绝该工具调用，不记录 secret 值。
- 科创 Backend 的会话归属校验保持严格，不增加 Authorization 或身份回退。
- 保留工作区已有改动；不提交，不改无关代码。

## 实施与验收

1. 已将回归测试改为覆盖真实 MCP interceptor request 的 `runtime.context` 契约，先观测到 3 项失败，再完成修复。
2. 已重启本机 8793、8794、8001；健康检查均 HTTP 200。
3. 已经真实 MCP 调用验证 `kc_draft_create` 成功，草稿列表可见后删除该条唯一测试草稿；真实调用 `kc_retrieval` 成功并返回 3 条结果。
4. 科创 Backend 定向测试 135 项通过；OPIE 自定义扩展定向测试 4 项通过。未运行全量测试。
5. 已更新 MCP 修复说明、`docs/Agent.md`、`docs/README.md` 和双服务器部署文档。3090/4090 没有执行远程写入；服务器部署仍待逐项核对活动挂载与原件后进行。
6. 最终本地状态复核完成后按用户要求关闭计算机。

## 当前进度

- 已完成：本机 OPIE 自定义拦截器实际加载；真实调用证明 `get_config()` 路径取不到 MCP interceptor 的 request-scoped secrets。
- 已完成：`backend/my_package/mcp/auth.py` 改为读取 `request.runtime.context["secrets"]`；对非科创 MCP 不注入，对科创 MCP 缺失或非法值仍 fail closed。
- 已完成：回归测试先红（3 failed），修改后扩展定向测试 4 passed。
- 已完成：本机科创 Backend 定向集成测试 135 passed；检索真实返回 3 条，草稿真实创建成功并删除测试草稿。
- 已完成：本机 8793/8794/8001 健康检查均 HTTP 200；实际请求均在 DeerFlow 300 秒超时配置内完成。
- 未执行：3090/4090 服务器部署及远程验收。

## 接口与风险

- 生产者：KC `build_deerflow_input` 将会话 ID 与 trace ID 放入 DeerFlow `config.context.secrets`；KC Runner 将其送入 `/runs/stream`。
- 中间链路：OPIE Worker 将本轮上下文放进注入给 MCP 适配器的 `runtime.context`。拦截器应读取 `MCPToolCallRequest.runtime.context`，不能假设子工具的 `get_config()["context"]` 保留 secrets。
- 安全约束：仅向配置的 kechuang MCP 注入 `X-KC-Thread-ID`、`X-Trace-ID`；缺失、空值或 CR/LF 一律拒绝，不记录 secret 值，不转发 `Authorization`。
- 部署影响：本机自定义扩展的最终版本需作为单文件增量部署到 3090 Gateway 并重启；KC Backend 和 MCP 会话归属校验不变。服务器部署尚未执行。
