# DeerFlow 匹配结果与草稿同步修复

## 目标

- 一轮供需匹配最多向 Agent 和前端提供 3 条结果；“换一批”基于原查询/方向排除本会话已展示对象，再返回最多 3 条新结果。
- DeerFlow 与旧科创 Agent 的匹配类自然语言总结均由模型按提示控制在 200 字以内；不得在适配层按字符截断。结构化结果卡片不计入总结长度，并保留完整业务详情。
- 从历史消息编辑草稿时，按草稿 ID 重新读取后端持久化的最新版本；加载失败时明确报错，不拿旧卡片内容覆盖新数据。
- 保留旧 Supervisor 实现及未配置 DeerFlow 时的回退路径，不修改 OPIEos/DeerFlow 核心源码。

## 实现边界

- MCP `kc_match` 在工具服务端将模型可见的 `top_k` 限制为 1–3，且不开放模型传入排除 ID；普通匹配最多检索 3 条。明确换批时才按服务端已展示 ID 做内部 over-fetch，最终工具结果仍最多 3 条。
- 以已有会话 `chat_flow.match_batch` 保存的原查询、匹配方向和已展示对象 ID 为换批来源，并用本轮 trace ID 限定一次请求，避免把批次排除条件带入其他消息。
- 在 Agent/Skill 提示中要求匹配摘要不超过 200 字；公共流适配层原样传递模型摘要，不做字符截断；保留 MCP 结构化证据、卡片与动作。
- 编辑入口调用已有的用户鉴权草稿详情 API；成功后用详情接口返回的草稿字段打开编辑器。

## 旧科创路径对照

- 旧 `SupervisorAgent`、`backend/agents/supervisor_agent/flows/answer.py` 和 `SupervisorAgent.answer_question` 均保留；`backend/api/v1/execution/supervisor.py` 在 DeerFlow 未配置时仍调用 `supervisor.answer_question`。这次没有删除旧主 Agent 或回退路径。
- 旧推理服务在 `backend/agents/reasoning_agent/service.py` 用 `MAX_LLM_ANSWER_CHARS = 200` 限制匹配摘要，旧响应合同在 `backend/agents/supervisor_agent/records/response_contract.py` 将匹配结果限制为 3 条。DeerFlow 直连 MCP 工具和流式适配层，不经过旧推理服务；而 `kc_match` 原先允许模型请求 `top_k` 至 50，导致它收到超过 3 条后自行输出。现分别在 MCP 输入/结果边界及 DeerFlow 流适配层重建同一约束。

## 验收

- MCP 返回/匹配卡片最多 3 条，模型请求更大的 `top_k` 不能绕过上限。
- 连续“换一批”不会重复展示已返回对象；普通新匹配不继承旧排除列表。
- 匹配摘要提示模型控制在 200 字以内；流式输出与最终 `answer` 不以硬截断代替总结；匹配卡片保留完整详情。
- 编辑已发布或已保存草稿先加载最新后端版本，加载失败不允许静默编辑旧快照。
- 旧 Supervisor 与其回退测试继续通过；前端构建和相关后端测试通过。

## 本机验证

- `D:\Anaconda\envs\py311\python.exe -m pytest -q`：528 passed；仅有 Starlette/httpx 弃用警告。
- 定向 Ruff 检查通过。
- 使用当前已安装 Vite 直接执行 `vite build --mode server`：构建通过；存在原有 bundle 超 500 kB 提示。`pnpm build` 的依赖预检因 `esbuild` build script 尚未批准而提前退出，未改动 pnpm 安全策略。
- OPIEos/DeerFlow 核心源码未改；仅同步了科创专属 Skill 对“服务端自动排除已展示对象”的说明。
