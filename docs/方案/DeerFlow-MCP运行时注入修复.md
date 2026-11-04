# DeerFlow MCP 运行时注入修复

## 最终根因

2026-09-29 本机调用 `kc_draft_create` 时，MCP HTTP 会话建立成功，但请求级 `X-KC-Thread-ID` 未被正确读取，科创端因无法把 MCP 请求关联到会话用户而拒绝工具调用。早期曾怀疑 OPIE 扩展模块未加载；后续确认扩展已加载，真正问题是拦截器从 `langgraph.config.get_config()` 读取 secrets。MCP 子工具 RunnableConfig 不保证包含本轮 request-scoped context。

MCP 适配器提供的 `MCPToolCallRequest.runtime.context` 才包含本轮 `config.context.secrets`。因此只修复 OPIE 自定义扩展读取位置；科创端会话归属校验和 MCP 权限保持严格，不增加身份兜底。

## 修复范围

- 修改 `OPIEos/backend/my_package/mcp/auth.py`：从 `request.runtime.context["secrets"]` 读取 `kc_thread_id`、`kc_trace_id`，仅向配置的 `kechuang` MCP 请求注入 `X-KC-Thread-ID` 与 `X-Trace-ID`。
- 缺少或含 CR/LF 的必需值时 fail closed；不记录 secret，不注入固定用户 ID 或 `Authorization`。
- 未修改 DeerFlow/OPIE 核心源码。科创 Backend 仍从 `X-KC-Thread-ID` 读取会话 ID，并通过自己的会话存储确定用户身份。

## 本机验收（2026-09-29）

- 修复前回归用例确认 `get_config()` 路径失败；修复后 OPIE 自定义扩展定向测试 **4 passed**。
- 科创 Backend 定向测试 **135 passed**。
- 本机 8793（科创 Backend）、8794（前端）、8001（DeerFlow Gateway）健康检查均返回 HTTP 200。
- 经本机真实聊天链路调用 `kc_draft_create` 成功，草稿在列表中可见后删除了唯一测试草稿；`kc_retrieval` 成功返回 3 条结果。两次调用均在 300 秒超时配置内完成。
- 用户贴出的 02:08 `ToolException` 属于修正前的失败证据；修复后的真实调用已成功。此结论针对本机，不代表服务器已部署或验收。

## 服务器部署边界

3090/4090 未执行远程写入。本机 OPIE 自定义扩展需在核实 3090 服务器活动源码挂载/镜像构建方式后，按增量文件部署并重启 Gateway；不要覆盖服务器 `.env`、活动 `extensions_config.json` 或 `.deer-flow` 运行数据。具体步骤见[双服务器迁移部署](../科创与DeerFlow双服务器迁移部署.md)。
