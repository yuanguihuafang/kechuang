# 业务草稿字段契约

## 范围

需求、技术方案、科研成果的智能问答草稿只使用 `draft_contracts.py` 定义的草稿字段；管理控制台继续使用完整 MySQL 字段。两种提取模式已经存在，本次只收紧 MCP 草稿工具字段，不改提取流程或数据库结构。

## 决策

- `backend/core/contracts/draft_contracts.py` 的 `draft_fields(type)` 是智能问答草稿 `fields`、`mysql_fields` 和 MCP 工具 schema 的唯一字段来源；模型拒绝草稿合同之外的字段。
- `business_fields(type)` 仍是管理控制台 `full` 提取模式使用的完整 MySQL 字段来源；不将草稿 MCP 工具扩成管理台全字段表单。
- 聊天草稿沿用 `DRAFT_EXTRACTION_MODE`（默认 `draft_only`）；管理控制台沿用 `ADMIN_EXTRACTION_MODE`（默认 `full`）。本次不改变这两个模式或其调用入口。
- 草稿必填项仍由 `DRAFT_REQUIRED_FIELDS` 决定；本次不另造必填规则。
- 创建时按请求的 `draft_type` 校验；保存时读取用户拥有的持久化草稿类型后再校验，调用方不额外传递类型。
- `title` 等草稿元数据、附件和来源块继续使用原有形状；不改变 HTTP API、SSE、数据库结构或前端参数。
- 业务 Skill 已要求按各自 MySQL 字段合同生成内容；本次以 MCP 类型化 schema 和后端校验作为强制边界，不改写无关流程指令。

## 实施步骤

1. 为需求、技术方案、科研成果分别建立来源于 `draft_fields(type)` 的严格字段模型，并通过测试校验模型字段与草稿合同一致。
2. 将模型接入创建与保存 MCP 输入；保存必须先根据已持久化草稿类型拒绝不匹配字段，且不改变既有保存/版本控制路由。
3. 检查 FastMCP 公布的 JSON Schema，覆盖三个类型的允许字段、`additionalProperties: false` 和跨类型拒绝。
4. 运行定向 MCP 草稿测试、相关全量后端测试及 Ruff；记录验证结果。

## 验收

- 三种智能问答草稿的 `fields`、`mysql_fields` 只允许其自身 `draft_fields(type)` 字段；不使用完整 MySQL 字段或三类字段并集。
- 管理控制台 `full` 提取仍按 `business_fields(type)` 提取完整 MySQL 字段；聊天 `draft_only` 仍按 `draft_fields(type)` 提取。
- 创建和保存均拒绝不属于当前草稿类型的字段；合法字段仍按原有草稿 API 持久化。
- 不改变公开 HTTP API、前端流程、数据库字段、OPIEos/DeerFlow 核心或现有发布逻辑。

## 实施与验证

- 原实现曾按 `business_fields(type)` 生成 MCP 字段模型；本次修正后模型改由 `draft_fields(type)` 生成，MCP `fields` 和 `mysql_fields` schema 均逐项描述草稿字段并设置 `additionalProperties: false`。
- 问答 `draft_only` 与管理台 `full` 的字段提取职责保持分离；管理台完整 MySQL 字段提取不受 MCP 草稿白名单收紧影响。
- 创建按传入类型校验；保存先读取当前用户拥有的草稿，再用持久化类型校验两个结构化字段集合。不要求保存调用额外传 `draft_type`。
- 测试覆盖三类字段集合、FastMCP 实际 schema、跨类型字段在创建和保存时被拒绝，以及原有草稿保存链路。
- 后端完整测试：563 passed，1 条既有 Starlette/httpx 弃用警告；全量 Ruff 检查通过。
- 未改前端、公开 HTTP/SSE、数据库、业务 Skill、OPIEos/DeerFlow 核心；未做连接真实存储的运行时端到端写入。
