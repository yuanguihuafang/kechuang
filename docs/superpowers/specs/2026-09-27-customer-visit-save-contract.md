# 客户拜访保存字段契约与草稿卡片

> 历史方案：2026-09-28 已改为统一草稿创建/编辑，字段以 `draft_contracts.py` 为准；确认保存使用 `kc_customer_save_visit(draft_id, expected_version)`，不再接收重复字段正文。拜访合同没有必填字段，不将企业名称额外设为必填。当前实现与验收见[完整重构验收](2026-09-28-kechuang-final-refactor.md)。下文仅记录当时的问题与方案。

## 问题

内部拜访记录保存因结构化字段超过 8 个而失败，助手随后把内部字段限制转成选择题；保存未成功时也没有 `draft_card`。当前 MySQL `customer_visit` 业务契约有 8 个可写字段，但 `kc_customer_save_visit` 仅公开 `dict[str, str]`，工具 schema 未列出字段名和字段语义；`kc-customer-visit` Skill 也没有给出字段映射规则。

## 约束

- 以 `business_fields("customer_visit")` 对应的 8 个字段为唯一可写结构化字段，不增加 MySQL 列或扩大上限。
- 不静默丢弃或截断用户信息。拜访事实、画像、潜在需求、预算、周期、适配方案、匹配维度、内部备注分别写入对应字段；记录来源、联系人、时间及待核实信息合并到语义相符的文本字段或 `raw_text`。
- 前端 API、SSE 公共响应和 OPIEos/DeerFlow 核心不变。仅科创后端 MCP 输入契约与 `kc-customer-visit` Skill 可调整。
- `kc_customer_save_visit` 成功后继续由现有适配器生成 `customer_visit` 草稿卡片；失败不得伪装为已保存。
- 不要求用户选择字段精简方案；仅在必填企业名称确实缺失或无法确认时追问。

## 实现方案

将保存工具的结构化字段从泛型字典改为明确的、最多包含现有 8 个业务键的嵌套输入模型，并为各字段添加用途描述；额外键由模型 schema 和 `extra=forbid` 拒绝。测试保证输入模型字段与 `business_fields("customer_visit")` 一致。更新 Skill，明确字段映射、信息合并边界和保存动作：信息足够且用户要求保存时直接调用工具，不把校验/映射工作交还用户。

## 验收

1. MCP 工具 schema 清楚列出全部 8 个字段及其用途，不允许第 9 个或不属于业务契约的字段。
2. 既有保存链路只收到已归入契约的字段，原始补充说明可由 `raw_text` 保留；身份、幂等和确认逻辑不回退。
3. 成功工具结果经现有适配器输出 `DRAFT_SAVED` 和 `draft_card`；失败时不产生成功卡片。
4. 对应 MCP、DeerFlow 适配器测试通过；不修改前端、外部 API 和 OPIEos/DeerFlow 核心。

## 实施与验证

- `CustomerVisitFields` 将 8 个业务字段直接发布在 MCP 参数 schema 中，字段集合通过测试与 `business_fields("customer_visit")` 对齐；额外键由 `extra=forbid` 拒绝，可选空值不写入记录。
- `kc-customer-visit` 增加字段语义映射和保存要求；现有保存、身份校验、幂等确认和适配器卡片逻辑不变。
- 后端完整测试：542 passed，1 条既有 Starlette/httpx 弃用警告；定向 Ruff 检查通过。`ruff format --check` 还会报告两个文件中未涉及的既有格式差异，因此未对整文件批量格式化。
- 未改前端、公开 API、OPIEos/DeerFlow 核心；未做连接真实 MySQL 的运行时端到端写入。
