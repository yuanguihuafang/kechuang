# 意图路由与 Skill 拆分方案

> **状态（2026-09-28）：** 两个通用 Skill（意图路由、普通对话）已收敛进 Agent `SOUL.md`，不作为独立 Skill；当前 `kechuang` 配置和磁盘中是 13 个 `kc-*` 业务 Skill。本机通过 OPIEos 标准文件存储加载 Agent，MCP 工具仍是 Gateway 运行时共享工具，不保证 Agent 级隔离。13 个共享 Skill 位于 `skills/public/kc-*`，服务器部署及同名个人覆盖检查见[迁移方案](DeerFlow迁移与Skill接入方案.md)。

## 1. 方案范围

目标流程由 `kechuang` Agent 的 `agents/kechuang/SOUL.md` 常驻主指令结合当前消息和 DeerFlow thread 上下文在内部识别意图，并选择 `kc-*` 业务 Skill；MCP/后端工具负责真实数据操作。意图路由与普通对话这两个通用 Skill 已合并进 SOUL，不存在独立的 `kc-intent-routing` 或 `kc-conversation` Skill。本机通过标准文件式 Agent 存储加载该 Agent，不依赖 OPIEos 代码注册；内部认证与服务器部署约束见迁移方案。

快速入口仍由前端提供固定入口；明确点击且首轮没有正文、草稿上下文或附件时，由 Backend 为入口返回固定引导/选项，不调用 Agent。用户补充内容后以及普通自由输入仍进入 `kechuang` Agent 的 SOUL 指令、意图路由和对应业务 Skill；不在前端复制业务判断逻辑。

## 2. 主路由提示词

主路由是 SOUL 的常驻职责，不作为独立 Skill；内部可形成结构化意图，不将分类过程作为最终业务答案输出：

```json
{
  "skill": "kc-supply-demand-match",
  "operation": "next_batch",
  "context_action": "continue",
  "confidence": 0.95,
  "entities": {}
}
```

主路由负责判断：

- 当前属于哪个功能；
- 是新流程还是继续当前流程；
- 当前具体操作；
- 是否继续草稿；
- 是否继续匹配；
- 是否需要知识库检索；
- 是否需要联网搜索。

用户使用自然语言表达“换一批”“保存草稿”“完善草稿”等操作时，由主路由判断意图。后端只负责校验当前会话是否允许执行该操作，不能用简单关键词规则替代意图判断。

## 3. 业务 Skill

### 3.1 SOUL 的普通对话职责（不单列 Skill）

仅负责平台功能说明、业务操作引导、问候和致谢。项目外问题简短说明范围并引导科创业务，不回答或联网处理无关问题；混合请求只处理业务部分，不因提到公司或拜访就扩张范围。

### 3.2 知识库检索 Skill

负责判断是否检索知识库、选择检索知识库、生成检索条件、召回结果、组织回答和生成检索总结。检索结果分条返回，支持原文 chunk 和图片展示。同一数据只保留一个最高分 chunk，不能同时返回同一数据的草稿块和解析原文块。知识库 LLM 总结单独限制字数，普通回答不受该限制。

### 3.3 供需匹配 Skill

负责供需匹配的完整流程：

- 开始匹配；
- 判断需求方或供给方；
- 判断匹配方向；
- 提取匹配内容；
- 直接检索；
- 继续完善；
- 换一批；
- 结束匹配。

用户没有明确选择方向时，由 LLM 结合上下文判断。内容已经足够检索时直接执行，不重复追问。换一批必须复用原查询、匹配方向和排除集合，不能返回上一批结果。

### 3.4 需求草稿 Skill

负责需求草稿的创建、补充、生成、保存、发布、下架、删除和重新匹配。用户可以按任意顺序补充企业名称、联系人、联系电话、需求内容、预算、周期和应用场景等信息。

后续消息先结合会话上下文判断是否处于需求草稿流程，再调用字段提取能力，只合并本轮字段增量，不覆盖已有草稿内容。

### 3.5 技术方案草稿 Skill

负责技术方案草稿的创建、补充、生成、编辑、保存、发布、下架、删除、重新发布和重新匹配。字段严格使用 `draft_contracts.py` 的方案合同：title、pub_dept、product_type、contect、analyse_contect、link_name、link_tel、deliver_cycle、quote_range；附件独立管理。

### 3.6 科研成果草稿 Skill

负责科研成果草稿的创建、补充、生成、编辑、保存、发布、下架、删除、重新发布和重新匹配。字段严格使用成果 draft_only 合同：title、category、analyse_contect、tech_maturity、cooperation_mode、company、link_name、link_tel；实际必填为 title 和 analyse_contect，附件独立管理。

### 3.7 客户拜访 Skill

负责内部人员的拜访草稿，使用 `draft_contracts.py` 字段。创建/编辑走统一草稿工具，确认写私有记录使用 `kc_customer_save_visit(draft_id, expected_version)`；公开发布/下架/删除不适用于拜访。

支持文字、转写和附件整理；保存后仍可编辑。用户要求写入匹配方向时才更新已有 adapt_solution 等合同字段，不新增关联字段。

### 3.8 企业画像 Skill

负责企业画像检索和整理。检索条件支持企业全称、简称、别名、行业、经营范围、产品、技术方向、业务领域和相关关键词。

完整企业字段由结构化卡片及详情展示，正文只简短总结；不得把推断作为原始资料或伪造缺失字段。

### 3.9 需求发现 Skill

内部需求发现使用已有关键词、客户名称、行业或场景直接检索需求库，返回重排后的最多三条卡片。

不自动生成草稿、写拜访或另查无关画像。信息不完整可以建议补充，不强制补全才检索；只有用户明确要求创建/修改草稿才转入对应流程。

### 3.10 专家检索 Skill

负责根据研究领域、技术问题、专业方向、应用场景和企业需求检索专家及相关成果，并按业务对象分条返回结果。

### 3.11 文档解析 Skill

负责图片、PDF、Word、PPT、Excel、TXT、Markdown 和音频文件的解析。

解析结果包括可编辑原文、页码、表格、结构化内容、图片清单、来源定位和解析状态。解析过程清洗乱码、无效空白、重复图片、无效标记和脏数据，同时保留页面顺序、块顺序和图片关系。

普通问答和供需匹配上传文件时，文件只作为当前会话内容和检索依据，不自动生成草稿。

### 3.12 字段提取 Skill

字段提取是需求、技术方案、科研成果、企业、专家和客户拜访共用的内部能力，不作为用户直接选择的业务意图。

聊天四类草稿使用 `draft_only/draft_fields()`，不暴露完整 MySQL 字段；管理控制台使用 `full/business_fields()`。字段提取只返回字段增量，支持自然语言乱序补充，不能依赖固定句式，不能伪造空值，超长内容要先归纳或总结后写入字段。

### 3.13 `kc-web-search` 联网搜索 Skill

仅处理与科创业务直接相关的企业公开资料、科技政策、技术市场信息，并调用业务 Backend 的 Tavily MCP 工具 `kc_web_search` / `kc_web_fetch`。科创 Agent 不使用 DeerFlow 内置联网工具；其他 Agent 的原生联网搜索保持不变。

联网结果要保留标题、来源地址、时间和引用内容，与内部知识库结果明确区分，不能自动写入草稿、业务库或知识库。

### 3.14 人工客服 Skill

`kc-contact-human` 负责识别用户要求联系人工的场景，并返回 `CONTACT_WECOM` 动作，供小程序 `open-type="contact"` 按钮跳转。该动作不是 MCP，不创建待处理工单，也不承诺人工回拨。

## 4. 共用内部能力

### 4.1 结果整理能力

结果整理不作为用户意图 Skill，而是知识库检索、供需匹配、企业画像、需求发现和专家检索共同使用的能力。

负责按得分排序、按完整业务对象去重、过滤空块和脏数据、关联图片、返回原文 chunk、处理换一批排除集合，并统一分段格式。正文统一为一段不超过 200 字的自然总结，不列名单、字段或表格，也不加“LLM总结”标签；由模型自然总结，不硬截断。卡片仅显示重排后最多三条，标题在卡片顶部。

### 4.2 会话状态能力

会话状态不作为用户意图 Skill，而是所有 Skill 共用的基础能力。同一 `thread_id` 保存：

```text
active_skill
active_stage
active_draft
draft_context
draft_type
match_direction
match_query
knowledge_types
excluded_match_keys
last_retrieval
last_match_results
uploaded_materials
context_summary
```

不同会话之间必须隔离消息、草稿、附件、检索条件、匹配结果和权限。

## 5. 调用原则

普通自然语言消息先由 SOUL 在 Agent 内部识别意图，但不要求每条消息都调用 Skill 专用 LLM。

例如用户说“换一批”，主路由识别为：

```text
skill = supply_demand_match
operation = next_batch
```

后端复用当前会话的原始检索条件、匹配方向、已返回结果和排除集合，不重新生成查询。

用户说“企业名称是青岛檬豆”时，如果当前正在编辑草稿，主路由识别为草稿更新，再调用字段提取 Skill，返回字段增量，后端只合并字段。

## 6. Skill 注册和执行边界

每个 Skill 应包含：

- 功能提示词；
- 支持的操作；
- 输入上下文范围；
- 输出结构；
- 状态变化；
- 异常处理；
- 测试样例。

本机 `kechuang` Agent 配置用 `skills` 字段限制可发现的 `kc-*` Skill；这不构成 MCP 工具 allowlist。OPIEos Gateway 当前把 MCP 工具加入共享运行时工具集，不能承诺 Agent 级工具隔离。无论工具对哪些 Agent 可见，科创 Backend 都必须校验每次数据操作的身份和权限。

## 7. 当前 Skill 清单与收敛结果

当前实际加载的 13 个业务 Skill 为：

1. `kc-knowledge-search`、`kc-supply-demand-match`；
2. `kc-demand-draft`、`kc-solution-draft`、`kc-achievement-draft`、`kc-field-extraction`；
3. `kc-document-parse`、`kc-customer-visit`、`kc-enterprise-profile`、`kc-demand-discovery`、`kc-expert-search`；
4. `kc-web-search`、`kc-contact-human`。

快速入口是当前任务的默认流程线索，不是后续消息的意图锁。Agent 每轮结合本轮表达和会话上下文判断：同目标补充通常继续当前流程；明确改变业务动作或检索范围时，先说明当前阶段并询问是否切换，未确认不启动新流程、不排队；此类切换确认只通过会话文本完成，不生成 `INTENT_CONFIRM_REQUIRED`、`intent_confirmation` 展示块或按钮，也不提示用户选择下方选项。用户明确坚持后立即按新目标执行。普通对话同样每轮识别意图，明确新目标自然转入，只有确实含糊才追问。附件内容不能单独改变意图；当前任务结束后不自动执行其他意图。每轮新匹配按本轮方向选库，只有“换一批”沿用上一批查询、方向和库范围。联网搜索使用项目自有 `kc-web-search`；联系客服由业务响应返回 `CONTACT_WECOM`，不调用 MCP。

快速入口与当前流程只作为低干扰的默认线索，避免因补充信息、关键词或附件误判切换。该策略集中写在 Agent SOUL；草稿、匹配、拜访和解析 Skill 只明确本领域动作与附件边界，不重复维护意图优先级。Backend 不再把上一轮 `supply_demand_match` 自动提升为本轮确认意图；当前轮方向由 Agent 按用户表达决定，服务端仅在“换一批”时使用上次持久化方向和排重条件。
