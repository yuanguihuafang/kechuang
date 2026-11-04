# 科创专属 Agent 设计规格

> **状态（2026-09-24）：已被当前接入决策取代。** 以下代码注册和 Agent 级工具隔离是历史目标设计，不代表当前源码或服务器能力。本机仅为内部认证 Header 做了最小合法化修改；`kechuang` Agent 通过标准文件存储加载，MCP 工具仍由 Gateway 全局共享。当前状态见 `docs/DeerFlow迁移与Skill接入方案.md`。

## 目标

在 OPIEos / DeerFlow 中通过代码注册 `assistant_id=kechuang`，供科创业务后端专用。DeerFlow 已启用并配置时，科创问答由该 Agent 使用 DeerFlow harness 执行；DeerFlow 关闭或未配置时保留旧 Supervisor 回退。现有科创前端 API 与 SSE 格式保持不变。

## 运行与隔离边界

- 复用现有 DeerFlow Gateway 进程，不另起一套 harness。隔离是 Agent 级的运行时隔离，不宣称为独立操作系统进程或容器隔离。
- DeerFlow 关闭或未配置时，问答继续走旧 Supervisor；DeerFlow 已启用时固定走 `kechuang` Agent。已选择 DeerFlow 执行后若调用失败，只返回明确错误，不自动改走 Supervisor，避免同一请求重复产生副作用。
- `kechuang` 由 OPIEos 代码内置注册，不依赖 Agent 管理 CRUD API；不启用公开 Agent 管理 API，不进入公开 Agent 列表或发布流程。
- 仅科创业务后端通过现有可信内部认证路径创建/运行此 Agent。普通用户请求不能自行指定或切换到它；线程创建后固定使用其 Agent 身份。认证失败、未知 Agent 或隔离配置缺失时拒绝执行，不回退到默认 Agent。
- `agents/kechuang/SOUL.md` 作为 `kechuang` 主 Agent 的源码常驻编排指令；它不作为可发现 Skill。其他科创业务 Skills 使用 `kc-` 前缀，由这个 Agent 按意图按需启用，不另建一个 Agent 来调用每个 Skill。
- SOUL 中的工具路由说明必须与实际注册的 `kc_*` 工具一致；不保留不存在或已明确不接入的工具组假设。联网搜索使用用户维护的 `kc-web-search` Skill，通过科创业务后端 `kc_platform` MCP 的 `kc_web_search` / `kc_web_fetch` 工具执行；不调用 DeerFlow 内置 `web_search` / `web_fetch`。后端工具复用现有 Tavily 客户端；客服继续使用业务响应动作，不作为 MCP 工具。
- 仅向此 Agent 提供显式允许的科创 Skills 和 `kc_platform` MCP 工具；模型可见的工具 schema 与实际工具执行两处都必须实施同一 allowlist。`allowed-tools` 提示文本不作为安全边界。
- `kc_*` MCP 工具只对 `kechuang` Agent 可见和可执行；默认及其他 Agent 均须排除这些工具，但各自原有能力不变。DeerFlow 内置联网工具可继续供其他 Agent 使用，不进入 `kechuang` 的 allowlist。
- MCP 模型工具名使用服务原始 `kc_*` 名称，不加服务器名前缀；MCP 服务器连接标识仍为 `kc_platform`。关闭该服务器的名称前缀，并确保所有启用的 MCP 服务器间工具名唯一；Agent allowlist 使用无前缀名称。
- 默认 Agent 与其他项目 Agent 不获得科创专属 MCP 工具或 Skills；不修改它们现有的提示词、工具集、技能集或调用行为。
- MCP 仍由业务后端校验用户身份、租户和数据权限。Agent allowlist 只限制可调用能力，不替代业务授权。
- 联系客服继续走现有业务响应动作，不新增客服 MCP 工具。

## 调用流程

科创前端 → 现有科创 API → 科创业务后端 → 带可信内部身份的 DeerFlow 请求（`assistant_id=kechuang`）→ 专属 harness → allowlist 内的 Skill/MCP 工具 → 业务后端现有鉴权与数据服务。

普通 DeerFlow 客户端及其他项目仍走其原有 Agent 路径，不经过科创 Agent 配置。

## 流式输出

- 保持 `/api/v1/chat/stream` 现有的 `start`、`progress`、`token`、`done`、`error` 五类 SSE 事件；DeerFlow 内部事件继续由业务后端适配，不直接透传内部事件协议。
- 可以通过 `progress.text` 展示面向用户的处理阶段和简短进度摘要，例如意图识别、知识检索、联网搜索、匹配和整理答案。
- 不向前端传输模型原始隐藏推理链、系统提示词、原始 `values` 状态快照、未筛选的内部事件或敏感工具参数/结果。DeerFlow 的原始中间推理不能直接作为“思考过程”展示。

## 兼容与命名

- 前端请求和响应契约不变；Agent 选择由科创业务后端在 DeerFlow 内部请求中固定指定。
- 当前源码、配置、测试和说明中，科创专属 Agent 的名称统一为 `kechuang`；不保留旧 Agent 名称的别名、兼容分支或示例。科创项目 Skill ID 统一使用 `kc-` 前缀；此前缀不参与运行时隔离判断。
- `kc_platform` 的说明文字使用科创业务语义；其工具名称不包含 MCP 服务器名前缀。
- 不重写 Git 历史；与 Agent 身份无关的历史记录不在本次范围内。

## 验收要求

1. 可信科创后端可创建并运行 `kechuang` 线程，后续轮次保持相同 Agent 身份。
2. 非可信调用方不能创建或运行该 Agent，也不能通过请求参数切换 Agent。
3. DeerFlow 关闭或未配置时，问答走旧 Supervisor；DeerFlow 已启用时走 `kechuang`，其执行失败返回明确错误且不自动切换引擎。
4. `kechuang` 只能发现并调用明确允许的科创 Skills/MCP 工具；不在 allowlist 中的工具即使已在 Gateway 注册，也不能被模型看到或被执行。
5. 默认 Agent 和其他 Agent 的工具、Skills 与现有行为不变，且无法调用科创专属工具。
6. 未知 Agent、越权调用或隔离依赖不可用时 fail closed，不静默使用默认 Agent。
7. 科创前端 API/SSE 契约、业务后端对 MCP 请求的身份与数据授权保持不变。
8. OPIEos 的 Agent 管理 API 维持关闭；Agent 无需手工发布即可由科创后端调用。
9. 主 Agent 常驻加载 `agents/kechuang/SOUL.md`，其他 15 个 `kc-` 业务 Skills（包括 `kc-web-search`）按需加载；联网搜索/页面提取只使用科创业务后端注册的 `kc_web_search` / `kc_web_fetch`；当前 MCP 工具名保持 `kc_*`，且在所有已启用服务器间无冲突。
10. SOUL 提及的工具能力均已注册；默认及其他 Agent 无法看到或调用 `kc_*`；客服动作不要求 DeerFlow 调用客服 MCP。
11. 流式响应只含既有五类 SSE 事件；进度事件展示安全的用户可见摘要，不包含原始隐藏推理或未筛选的内部状态。

## 本次不做

- 不修改科创前端界面或前端 API。
- 不改造其他项目 Agent、默认 Lead Agent 或全局 MCP/Skill 配置的业务语义；只在 Agent 运行时过滤中隔离科创专属 `kc_*` 工具，不移除其他 Agent 的 DeerFlow 原生搜索工具。
- 不新增独立 Gateway、容器、数据库或 Agent 管理界面。
- 不将 MCP/Skill allowlist 当作租户或用户级数据授权。
- 保留 DeerFlow 关闭或未配置时的旧 Supervisor 回退；不保留旧 Agent 名称或旧 Skill ID 的兼容别名；不删除仍被其他现行功能使用的模块。
