# DeerFlow 本地模型思考模式与输出限制

## 目标

避免 `kc-main` 在本地问答/材料处理时默认进入长思考并生成过长 completion，减少模型阶段等待时间。

## 本地配置

`OPIEos/config.yaml` 的 `kc-main` 使用 OpenAI-compatible Chat Completions。Agent 已配置 `thinking_enabled: false`，但此前模型配置未提供关闭思考的请求参数，不能据此确认 provider 请求关闭了思考模式。

为该模型配置：

- `max_tokens: 4096`：限制单次 completion 上限；它与全局 `token_budget.max_tokens`（整次运行预算）不同。
- `when_thinking_disabled.extra_body.thinking.type: disabled`：按 DeepSeek Chat Completions 格式显式关闭思考模式。

DeepSeek API 文档说明思考模式默认开启；Chat Completions 通过 `extra_body.thinking.type` 切换，`max_tokens` 限制单次生成长度。当前请求经过 Sidus-AI 的兼容端点，因此必须通过一次本地真实请求确认该端点接受这些参数，并检查 usage 中 reasoning tokens 是否下降。

## 验收与风险

1. 确认 Gateway 实际读取的是 `OPIEos/config.yaml`，配置可被 `AppConfig` 解析。
2. 工厂单测确认 `thinking_enabled=false` 时关闭参数和 `max_tokens` 被传给模型客户端。
3. 本地复测相同材料处理流程，检查 reasoning tokens、总耗时、工具调用和最终草稿完整性。

4096 是首轮保守上限；长资料或复杂工具调用可能触发 `finish_reason=length`，出现截断时再按日志证据调高。该配置只改本地运行配置，不代表 Sidus 线上端点已实测支持。

## 参考

- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)
