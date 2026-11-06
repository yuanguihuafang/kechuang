# 科创服务助手 DeerFlow 与 Skill 接入方案

## 1. 文档目的

本文用于明确科创服务助手以 `OPIEos` 为 DeerFlow 基座后的总体边界、Agent 常驻指令（SOUL）、业务 Skill、工具层、会话状态、权限、数据同步和验收方式。

本文是当前技术对齐方案。迁移原则是保留已经验证过的业务服务和数据链路，使用 DeerFlow Harness 作为 Agent 运行时、Skill 编排、上下文管理和流式执行入口。

当前项目的前端主要作为测试界面，本方案重点描述后端和 Agent 边界。

### 1.1 工作目录与修改范围

本方案及后续迁移工作只针对 `D:/Work/kechuang`。所有代码、配置、Skill、验证和文档变更均在该目录内进行；`D:/Work/kc-rag-agent` 不属于本次工作范围，不对其执行修改或写入。

### 1.2 当前执行状态

当前唯一工作区是 `D:/Work/kechuang`。旧项目 `D:/Work/kc-rag-agent` 只能作为只读历史参考，不得执行写入、清理、回滚或覆盖同步。

截至 2026-09-24，已修复 DeerFlow 流式完成后保存会话状态时读取不存在的 `scope_id` 导致持久化失败的问题；Backend 到 Gateway 的客户端也已设置 `trust_env=False`，避免回环请求继承系统代理。此设置不能单独解释当前无响应：最近一次直连探测中，8001 有监听进程但简单 `/health` 仍在 4 秒内无响应，8793 业务健康接口返回 HTTP 200。当前没有重启服务或完成在线问答验收。

OPIEos Gateway 应用层修改了合法内部认证 Header，并添加模型/工具阶段计时；没有加入科创专属 Agent 路由、工具过滤或 Skill 隔离代码。本机 Agent 资产按 owner UUID 加载：`DEERFLOW_AGENT_OWNER_USER_ID=62ddcc4d-f2b5-4152-be26-78a1a7554a86`，运行目录为 `OPIEos/backend/.deer-flow/users/<UUID>/agents/kechuang/`；13 个 `kc-*` Skill 在 Agent 配置中设为白名单。MCP 工具仍由 OPIEos 标准运行时工具集加载，不保证 Agent 间隔离。

13 个科创 Skill 已迁到原生 `skills/public/kc-*`，避免 legacy custom 目录在用户拥有个人技能时不加载的问题；未修改存储器。`test_kechuang_skill_manifest.py` 验证两种用户场景。共享列表看 `/api/skills`，个人编辑列表 `/api/skills/custom` 不列公共技能。个人同名 Skill 优先，部署必须按实际用户核查，不擅自删除覆盖。

科创 Backend 到 DeerFlow 的 HTTP 客户端现显式设置 `trust_env=False`，不将内部 Gateway 请求发送到系统 HTTP 代理。线上 3090 的 Agent owner UUID 为 `e458d2e2-30ad-46f8-86f9-535d0cdec89e`；OPIEos 必须从 `DEER_FLOW_HOME/users/<UUID>/agents/kechuang/` 加载其 Agent 资产，4090 Backend 通过 `DEERFLOW_AGENT_OWNER_USER_ID` 和两个 `X-DeerFlow-*` Header 指向该固定 Agent 用户，而不是把科创业务用户 ID 当作 OPIEos 用户。双服务器分步部署见[部署步骤](../科创与DeerFlow双服务器迁移部署.md)。

科创后端只使用 DeerFlow 问答编排，旧 Supervisor/intent/reasoning 执行图及专属提示词已删除。未启用或未配置时，聊天在处理附件前明确返回 503；执行失败也不切换旧链路。客服仍通过 `CONTACT_WECOM` 响应动作，不作为 MCP 工具。

此前最小 Docker 运行验证已通过：三个 Compose 配置检查通过，OPIEos 页面和业务 backend 健康接口返回 `200`，基础设施容器为 healthy。该历史检查不代表本轮服务正在运行；当前本机测试仍按启动说明由用户手动启动服务并观察日志。

Docker 中的 `ghcr.io/astral-sh/uv:0.11.1`、`python:3.12-slim-bookworm` 和 `node:22-alpine` 是历史 Docker 构建/运行时使用的基础镜像，不是额外服务。当前本机使用 `make dev` 原生启动 DeerFlow 应用；科创数据服务和 Backend 分别由科创 Compose 与 Conda 进程管理。

## 2. 当前项目基线

### 2.1 当前已有能力

- FastAPI 作为后端入口，统一挂载聊天、文档、草稿、发布、生命周期、管理台和身份接口。
- DeerFlow Harness 提供通用 Agent、thread 上下文、Skill 发现、工具调用和流式执行；`kechuang` 使用 OPIEos 标准 custom Agent 存储加载，不通过核心代码注册。
- `agents/kechuang/config.yaml` 与 `SOUL.md` 是 Agent 资产；当前用户级文件存储需放在 `DEER_FLOW_HOME/users/<DEERFLOW_AGENT_OWNER_USER_ID>/agents/kechuang/`，不能依赖共享目录回退。数据库式存储需由 OPIEos 支持的 Agent 管理流程创建/发布。
- PostgreSQL 保存会话、消息、运行状态、草稿、任务、审计、匹配和运行记录；DeerFlow checkpoint 只服务于 Agent 运行恢复。
- MySQL 保存正式业务数据，包括需求、方案、成果、企业和专家等记录。
- Milvus 保存向量和检索分块，支持 `source_id`、`source_locator`、`mysql_id`、`document_id`、`chunk_role` 等关联字段。
- MinIO 保存原始文件、解析图片和对象资源。
- 文件解析已经覆盖图片、PDF、Word、PPT、Excel、文本和音频场景，并返回原文、页码、表格、图片和解析状态。
- 当前业务 Skill 已迁移到 `OPIEos/skills/public`，Skill 文件采用 DeerFlow 风格的 `SKILL.md`。

### 2.2 当前代码边界

| 当前模块 | 迁移后的职责 |
| --- | --- |
| `backend/main.py`、`backend/api/v1` | 保留业务 HTTP API，由 DeerFlow Tool/MCP 适配层调用 |
| `OPIEos/backend` | DeerFlow Gateway、Harness、线程、流式事件和 Skill 运行时 |
| `OPIEos/agents/kechuang/config.yaml`、`SOUL.md` | DeerFlow custom Agent 源资产；运行副本安装到对应 owner UUID 的 `DEER_FLOW_HOME/users/<UUID>/agents/kechuang/` |
| `OPIEos/skills/public/*` | 需求、方案、成果、检索、解析、企业画像等业务 Skill |
| `backend/agents` | 保留可复用业务能力，逐步包装为 Tool/MCP，不再作为 DeerFlow 主编排入口 |
| `backend/services/document_parsing` | 提供文件解析、清洗、图片和音频处理 |
| `backend/services/mysql` | 提供正式业务数据查询和同步 |
| `backend/services/vector/milvus` | 提供向量索引、删除、更新和查询 |
| `backend/services/storage/minio` | 提供原文件、解析图片和对象资源存储 |
| `backend/services/persistence` | 提供会话、草稿、任务、checkpoint、审计和运行证据 |
| `backend/admin` | 保留管理台业务和权限，不暴露给外部用户 Skill |
| `OPIEos/skills/public` | DeerFlow 可发现的科创业务 Skill 定义 |

## 3. 迁移结论

### 3.1 DeerFlow 负责什么

DeerFlow 负责 Agent 运行时、SOUL 常驻指令加载、业务 Skill 发现、工具调用编排、流式事件输出和运行级错误处理。

DeerFlow 不直接访问 PostgreSQL、MySQL、Milvus、MinIO，也不让模型自行拼接 SQL、删除数据或决定权限。所有业务操作必须通过受控的后端 Tool 或 MCP 接口完成。

### 3.2 当前项目服务负责什么

当前项目服务继续负责身份校验、会话隔离、草稿合并、字段长度限制、文档解析、图片资源、知识库权限、分块、召回、去重、脱敏、MySQL/Milvus 同步和删除补偿。

Skill 不承载数据库、检索或文件处理实现，只描述触发边界、输入上下文、调用顺序和输出规则；业务逻辑统一由 Tool/MCP 和业务服务执行。

### 3.3 当前接入策略

1. 保留现有 `/api/v1/*` 业务接口，前端不需要一次性改造。
2. DeerFlow Gateway 负责 `thread_id`、用户身份、角色和流式事件；业务 Tool/MCP 负责真实数据操作。
3. 六个快速入口仍使用现有问答 API/SSE；由 SOUL/Skill 遵循用户明确选择编排，后端执行权限、合同和持久化校验，不再运行旧串行 Supervisor 图。
4. 运行时由 `kechuang` Agent 处理意图；用户明确选择的快速入口在当前任务和附件处理期间有效，只有用户明确切换才改道。服务器侧须安装最新 Agent 资产并完成在线验收，不能只确认同名目录存在。
5. DeerFlow 负责意图判断和运行时编排，业务写入、查询和原流程步骤仍由科创 Backend 执行；同一轮按原顺序推进状态，避免两个运行链路同时写入同一业务状态。
6. 前端契约冻结：保留现有 `/api/v1/*` 路径、请求字段、Token-only 身份、`thread_id` 语义、草稿/附件参数和 `start`、`progress`、`token`、`done`、`error` 五类 SSE 事件；DeerFlow 原生 LangGraph API 只作为后端内部接口。
7. DeerFlow 内部事件、Agent 状态和 Tool 返回必须在适配层转换为现有前端 `status`、`ui_blocks`、`actions`、草稿与匹配结果等响应，不能把内部实现细节直接暴露给前端或将结构化业务卡片降级为普通文本。

## 4. 目标架构

```text
用户 / 测试前端 / 小程序
          │
          │ 现有 /api/v1/chat/stream 或 DeerFlow Gateway
          │ 可信内部请求：assistant_id=kechuang（本机已配置；服务器需部署同一 Agent 与认证约定）
          ▼
OPIEos 标准 custom Agent：kechuang（本机已加载；服务器待部署）
          │
          ├── agents/kechuang/SOUL.md（常驻主指令）
          ├── kc-knowledge-search / kc-supply-demand-match
          ├── kc-demand-draft / kc-solution-draft / kc-achievement-draft
          ├── kc-customer-visit / kc-enterprise-profile / kc-demand-discovery
          ├── kc-expert-search / kc-document-parse / kc-field-extraction
          ├── kc-web-search / kc-contact-human
          │
          ▼
受控 Tool / MCP 适配层
          │
          ▼
现有业务服务层
          │
          ├── PostgreSQL：会话、草稿、任务、审计、checkpoint、运行证据
          ├── MySQL：正式业务数据
          ├── Milvus：向量、分块和检索索引
          ├── MinIO：原文件、解析图片和对象资源
          └── etcd：基础设施元数据
```

## 5. Agent 常驻指令（SOUL）设计

### 5.1 SOUL 文件

科创 Agent 的常驻指令资产位于：

`OPIEos/agents/kechuang/SOUL.md`

该文件描述 Agent 的常驻业务编排，不是可发现 Skill。OPIEos 不会从源码仓库根目录自动发现它；本机文件式 Agent 运行位置为 `OPIEos/backend/.deer-flow/users/62ddcc4d-f2b5-4152-be26-78a1a7554a86/agents/kechuang/`。服务器使用 Backend 配置的 owner UUID，即 `DEER_FLOW_HOME/users/e458d2e2-30ad-46f8-86f9-535d0cdec89e/agents/kechuang/`。只同步 `config.yaml` 与 `SOUL.md`，不要将 Agent 或 Skill 安装到错误用户下；若服务器使用数据库存储，则由该实例的 Agent 管理流程发布。复制源码资产不代表运行时已加载。

### 5.2 Agent 常驻指令的输入

Agent 每轮至少接收：

- 当前 `thread_id` 和用户身份；
- 当前用户消息和附件引用；
- 用户角色、企业主体和权限范围；
- 最近消息和受限摘要；
- `active_skill`、`active_stage`、`active_draft`、`draft_context`；
- 当前检索方向、知识库、query、已返回对象和换批排除集合；
- 文件解析状态和可用图片/原文引用。

常驻指令不接收没有上限的原文、全部历史消息或完整 Milvus 结果。大对象通过 Tool 查询，返回受限摘要和引用。

### 5.3 Agent 常驻指令的输出

Agent 内部可形成结构化路由和执行结果，外部只输出用户可理解的内容。内部结果至少包含：

```json
{
  "skill": "kc-knowledge-search",
  "operation": "search",
  "context_action": "continue",
  "knowledge_types": ["solution"],
  "query": "工业机器人智能化改造方案",
  "confidence": 0.92
}
```

工具返回的结构化结果必须包含 `status`、`data`、`error`、`trace_id` 和必要的 `next_action`。模型不能通过自由文本猜测工具是否成功。

## 6. Skill 划分与当前项目边界

### 6.1 意图识别与路由（SOUL 常驻职责，不单列 Skill）

`SOUL.md` 每轮结合当前消息、会话状态、草稿状态、附件和最近检索结果，在 Agent 内部识别意图并选择下列 13 个业务 Skill；当前没有独立的 `kc-intent-routing/SKILL.md`：

- 普通回答、知识库检索、联网搜索还是供需匹配；
- 需求、技术方案、科研成果、客户拜访、企业画像、需求发现或专家检索；
- 新流程、继续当前流程、补充字段、生成草稿、保存、发布、下架、删除、换一批、查看详情或联系人工；
- 当前是否需要选择知识库和供需方向。

路由判断不能作为内部分析过程输出给用户，也不能越过 Skill/后端权限直接修改草稿或删除数据。后端仍需校验路由结果是否符合身份、流程和权限。

### 6.2 普通对话（SOUL 常驻职责，不单列 Skill）

平台说明、操作解释、状态解释和不需要内部证据的直接回答由 `SOUL.md` 常驻职责处理；当前没有独立的 `kc-conversation/SKILL.md`。普通对话读取同一会话上下文；当消息中已经出现明确技术对象、产品、场景、合作目标或资料内容时，应转给知识库检索或供需匹配，不因用户没有使用固定按钮而停在追问。

普通回答不使用知识库匹配总结的 200 字限制，也不自动创建草稿。

### 6.3 `kc-knowledge-search`：知识库检索

负责需求库、方案库、成果库、企业库和专家库的检索与证据组织。当前项目的检索输入对应 `RetrievalRequest`，包含 `query`、`scope_id`、`knowledge_type(s)`、`document_id`、`top_k`、`use_rerank`、`expand_query` 和后端注入的 `identity`。

检索必须执行：权限过滤、外部脱敏、空块过滤、父块过滤、脏数据过滤、按得分排序、按完整业务对象去重和图片关联。`full` 检索模式保持现有语义，不因去重逻辑改变；普通分步检索不能直接把父块作为最终结果。

### 6.4 `kc-supply-demand-match`：供需匹配

负责普通对话、文件内容、需求/方案/成果草稿或客户拜访内容的供需匹配。用户未明确选择需求方或供给方时，结合上下文判断；内容足够时直接检索，不等待用户补齐可选字段。

换一批必须复用当前会话的原始 query、方向、知识库范围、排序策略和完整对象排除集合。排除键按 `chunk_role` 先判断：`mysql` 数据以 `mysql_id` 关联；其他数据优先用 `source_id`，再用 `source_locator`、`document_id` 等辅助定位。同一数据的草稿块和解析原文块只保留得分最高的一条，不能返回重复对象，也不能用已排除结果补位。

### 6.5 三类草稿 Skill

`kc-demand-draft`、`kc-solution-draft` 和 `kc-achievement-draft` 分别对应需求、技术方案和科研成果，不能共用一套业务字段。

它们共同遵守：

- 生成或更新后立即保存 `active_draft` 和 `draft_context`；
- 后续自然语言先判断是否仍在当前草稿流程，再调用 `kc-field-extraction`；
- 字段提取返回增量，后端只合并本轮明确字段，不覆盖已有内容；
- 允许乱序补充、跳过助手问题、中途改意图和直接请求匹配；
- 保存、发布、下架、删除是不同操作；
- 发布或重新发布后才同步正式 MySQL 和 Milvus；
- 下架保留记录但移除检索可见性；删除显示为已删除并执行关联清理。

当前项目已经有 `structured_record_draft`、`knowledge_document_draft`、字段提取和生命周期服务，Skill 不应重复实现保存或同步逻辑。

### 6.6 `kc-field-extraction`：字段增量提取

这是内部能力，不是用户直接选择的意图。它接收当前消息、已有草稿、草稿类型、附件和受限原文，按对应 MySQL 业务表字段合同提取增量：

```json
{
  "fields": {
    "company": "青岛檬豆"
  },
  "evidence": ["本轮用户消息"],
  "missing_fields": [],
  "confidence": 0.95
}
```

不同 MySQL 表字段不一致时，必须使用对应字段合同，不能把一个通用字段模型硬套到所有草稿。超长字段要先归纳再写入，不能直接截断；默认用户信息只能作为默认值，不能伪装成文档事实。

### 6.7 `kc-document-parse`：文件解析

负责图片、PDF、Word、PPT、Excel、TXT、Markdown 和音频的解析，返回可编辑原文、页码、表格、图片清单、来源定位、处理状态和具体错误原因。

普通问答和供需匹配上传文件时，文件只作为会话内容和检索依据，不自动生成草稿。管理台修改解析原文后要重新清洗、分块、提取字段和建立索引，保存过程必须按稳定图片标识去重，避免删除图片后保存导致剩余图片重复。

浏览器/小程序即时语音转文字由客户端负责；上传音频文件的转写属于材料解析流程，不再把两种能力混为一个接口。

### 6.8 `kc-enterprise-profile` 和 `kc-demand-discovery`

`kc-enterprise-profile` 只面向内部人员，支持企业全称、简称、别名、行业、经营范围、产品、技术方向和相关领域检索。画像事实、解析原文和潜在需求必须分开；潜在需求不能被写成解析原文。

`kc-demand-discovery` 从企业画像、拜访内容、企业资料和对话中提取潜在需求，返回需求描述、证据、置信度和方向，可写回客户拜访草稿或内部记录，但不能替代原始资料。

### 6.9 `kc-customer-visit`

只面向内部人员，负责客户拜访草稿、企业信息、潜在需求、跟进事项、附件和方案匹配。客户拜访可以通过文字、客户端语音转写和文件补充，方案匹配结果要写回拜访草稿；首次生成拜访草稿时不能虚构适配方案。

### 6.10 `kc-expert-search`

负责按研究领域、技术问题、专业方向、应用场景和企业需求检索专家及相关成果，按业务对象分条返回，遵守内部权限和图片/来源关联。

### 6.11 `kc-web-search`

仅处理最新信息、公开网页、新闻、政策和内部知识库无法覆盖的公开内容。网络结果必须保留标题、来源、时间和引用，与内部知识库结果分开，不自动写入业务库、草稿或 Milvus。

### 6.12 `kc-contact-human`

识别联系人工客服请求。外部用户响应携带 `CONTACT_WECOM` 动作，由小程序原生 `open-type="contact"` 按钮打开客服；不调用 MCP、不创建工单、不承诺回拨。内部用户不返回该动作。

## 7. Tool / MCP 层

### 7.1 为什么需要 Tool 层

Skill 是提示词和执行约束，不是后端业务 API。当前项目已经包含数据库、向量、对象存储和生命周期逻辑，直接让 DeerFlow Skill 操作数据库会导致权限、幂等、删除补偿和跨库一致性失控。

因此应新增最小适配层，将已有服务包装成 DeerFlow 可调用的 Tool 或 MCP Server；Tool 内部复用现有服务，不复制业务逻辑。

### 7.2 当前与后续 Tool 分组

当前 `kechuang` MCP 已实现：

- 对话上下文直接使用 DeerFlow 的 `thread`、消息历史和 Run 状态；结构化业务状态继续由科创 Backend 和数据库负责，不新增通用上下文 MCP。
- 管理员文档、会话、草稿、数据同步和审计等能力继续通过 Backend 管理接口和内部服务提供，不暴露给外部 Agent。
- `kc_retrieval`、`kc_match`：知识检索、供需匹配和换一批。
- `kc_document`：本人文档列表、详情、触发解析和读取有界解析结果；上传继续走现有 REST multipart，图片二进制和原文件下载继续走现有受权限保护的 HTTP 接口。
- `kc_draft`：需求、方案、成果草稿的创建、本人列表/详情、带版本校验保存、发布、下架和逻辑删除。
- `kc_profile`：企业/专家检索；内部用户使用受信任 MySQL 查询，外部用户使用已有权限过滤的公开检索。
- `kc_customer`：读取当前内部用户的拜访记录；`kc_customer_save_visit(draft_id, expected_version)` 确认已持久化的拜访草稿。创建和编辑使用统一草稿工具；不直接提交另一套自由字段。

| Tool 组 | 作用 | 复用当前模块 |
| --- | --- | --- |
| `kc_retrieval` | 检索、结果标准化、去重、详情和图片 | `retrieval_agent`、Milvus、MySQL |
| `kc_match` | 匹配和换一批 | `runtime/mcp_server.py`、匹配仓储 |
| `kc_document` | 本人文档目录/详情、解析和解析结果；文件上传/原件下载仍使用既有 HTTP API | lifecycle、perception、MinIO |
| `kc_draft` | 创建、读取、版本化保存、发布、下架和逻辑删除 | draft/lifecycle services |
| `kc_customer` / `kc_customer_save_visit` | 本人拜访读取 / 按草稿 ID 与版本确认保存 | business records、MySQL、私有 Milvus |
| `kc_profile` | 企业和专家检索 | MySQL business search、retrieval |

### 7.3 Tool 输入约束

- 用户身份、角色、企业主体和权限由 Gateway 注入，禁止模型传入任意 `user_id`、`role` 或管理员标记。
- `thread_id` 由 Gateway 或当前 DeerFlow run 绑定，Tool 不接受模型随意切换会话。
- 文本、字段、query、文件和批量结果都必须执行长度、数量和类型校验。
- 写操作需要幂等键或版本号，发布、下架、删除和同步失败必须返回阶段化错误。
- 工具超时、限流、模型不可用、数据库错误和外部服务错误使用结构化错误返回，不能统一显示成“请稍后重试”。
- 管理员能力不注册为外部 Agent 可见 MCP；如未来需要内部工具，必须单独设置权限边界。

### 7.4 Tool 返回约束

```json
{
  "status": "ok",
  "data": {},
  "error": null,
  "trace_id": "...",
  "next_action": "continue"
}
```

检索 Tool 返回完整业务对象键、chunk 字段、图片资源、来源和权限后的展示字段；草稿 Tool 返回版本、状态、字段变更和同步任务；文档 Tool 返回文件 ID、版本 ID、解析状态和图片引用。

## 8. 会话、上下文和状态归属

### 8.1 同一会话连续，不同会话隔离

同一 `thread_id` 内保持聊天、文件解析、草稿、匹配和结果追问连续；新建会话时不得继承旧会话的 `active_draft`、检索条件、附件、排除集合或匹配结果。

### 8.2 状态字段

当前项目至少需要保持以下状态：

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

DeerFlow 负责本次 Agent run 的临时执行状态；PostgreSQL 负责权威业务状态。关键状态不能只放在 prompt 或内存中。

### 8.3 当前项目上下文边界

当前代码 `backend/core/context_window.py` 已定义统一边界：

- 摘要输入最多 6000 字符；
- 摘要最多 1200 字符；
- 草稿原文最多 8000 字符；
- 来源块单项最多 4000 字符；
- 意图上下文 JSON 最多 16000 字符；
- Reasoning 上下文最多 12000 tokens；
- 单字段默认最多 3060 字符，嵌套值最多 2000 字符。

迁移到 DeerFlow 时继续复用这些边界，不把完整原文、完整历史或全部召回结果注入 Agent。知识库匹配总结最多 200 字，非草稿文件解析总结最多 500 字，均采用先归纳后限长，不使用简单硬截断；普通回答不受这两个总结限制。

## 9. 权限和数据隔离

### 9.1 内部人员

支持企业画像、需求发现、客户拜访、内部供需匹配和内部资料检索。可见范围由后端权限控制，内部草稿和内部备注不能被外部 Agent 读取。

### 9.2 外部人员

支持普通问答、文件检索、需求/方案/成果草稿和供需匹配。外部检索需求库时必须脱敏，不返回需求原始文档下载入口；脱敏要作用于召回结果、模型上下文、详情和下载 Tool，而不是只隐藏前端按钮。

### 9.3 管理员

管理台继续使用 Backend 管理接口和内部服务，支持文档、原文、图片、字段、草稿、会话、附件、索引和审计治理。管理员删除或彻底清理时，按关联关系同步处理 PostgreSQL、MySQL、Milvus、MinIO；etcd 只作为基础设施元数据，不能把它当作单条业务数据删除目标。

## 10. 文件、图片和检索结果

### 10.1 文件解析

文件上传返回文件标识和任务状态，长耗时解析异步执行。解析结果包括文本、页码、表格、图片、分块和来源定位，并保留原始文件与解析版本的关联。

### 10.2 图片统一资源

当前图片来源有两类：MySQL 业务记录中的云端图片，以及 MinIO 中的解析图片。后端结果统一返回资源类型、资源 ID、可访问地址、过期策略和关联对象，前端只按资源对象渲染，不根据路径前缀猜测存储系统。

### 10.3 结果去重

检索结果按完整业务对象去重而不是按 chunk ID 去重：

1. 先读取 `chunk_role`；
2. `chunk_role == "mysql"` 时按 `mysql_id` 归并；
3. 其他 chunk 优先按同一 `source_id` 归并；
4. `source_locator`、`document_id` 和版本信息用于辅助定位；
5. 同一数据只保留得分最高的 chunk；
6. 原文详情返回命中块上下相邻各 3 个块。

`full` 检索模式保持原有语义，不套用分步召回的去重替换逻辑。

## 11. 草稿和跨系统同步

### 11.1 草稿状态

至少区分：编辑中、已保存、已发布、已下架、已删除、同步中、同步失败。下架不是删除，删除不是隐藏；后台和个人中心必须显示真实状态。

### 11.2 发布链路

```text
草稿编辑
  → 保存 PostgreSQL 草稿版本
  → 用户/管理员确认发布
  → 写入 MySQL 正式业务表
  → 生成最终分块和字段
  → 写入 Milvus
  → 保留 MinIO 文件/图片关联
  → 写入同步任务、审计和最终状态
```

任何阶段失败都要保留阶段、错误原因、重试次数和补偿入口，不能只返回一个布尔值。

### 11.3 下架和删除

- 下架：保留草稿/业务版本和历史，但从可检索范围移除。
- 删除：删除业务记录或标记为删除，并清理有权清理的索引、文件和会话关联。
- 版本彻底清理：由管理员执行，按文档版本、草稿、MySQL、Milvus、PostgreSQL、MinIO 关联顺序清理并记录结果。
- 重发布：基于最新版本重新生成字段、分块和索引，不能复用已经失效的旧块。

## 12. 流式输出和错误

### 12.1 流式事件

DeerFlow 的流式事件适配到当前 `/api/v1/chat/stream` 时，至少区分：

- `run_started`：请求和会话开始；
- `intent_detected`：已确定 Skill、操作、知识库和方向；
- `tool_started` / `tool_progress` / `tool_finished`：正在解析、检索、匹配或保存；
- `content_delta`：可展示的回答增量；
- `result`：结构化结果、草稿或操作状态；
- `error`：具体阶段和原因；
- `run_finished`：本轮完成。

不展示模型原始思考链、values 快照或自由文本 custom 事件。SSE 适配器仅输出既有五类事件；进度使用固定文案/白名单阶段，AI token 仅保留文本块并过滤结构化或 `<think>` 推理内容。

### 12.2 错误内容

出现模型 429、超时、Milvus/PostgreSQL 失败、文件解析失败或同步失败时，用户至少应看到：业务阶段、可读原因、是否已经产生部分结果、是否可重试和下一步动作。

### 12.3 前端兼容硬约束

迁移的默认目标是“后端内部替换，前端契约不变”：

- 继续保留 `/api/v1/chat/stream`、材料准备/解析、会话、草稿、文档和人工联系接口。
- 继续接收 `message`、`thread_id`、`intent_hint`、`intent_confirmation`、`flow_action`、`draft_context` 和 `attachment_mode`。
- 继续从 Bearer Token 获取用户身份，前端不恢复传递 `user_id`。
- 继续输出 `start`、`progress`、`token`、`done`、`error` 五类 SSE 事件。
- DeerFlow 的 `values`、`messages-tuple`、`custom`、`end` 事件只能在后端适配层内部使用。
- `done.result` 的 `answer`、`intent`、`match_results`、`ui_blocks`、`actions`、`thread_id` 和 `status` 继续保持兼容。
- DeerFlow 对 `thread_id` 的内部格式限制不得反向破坏历史会话；不兼容时使用外部 ID 到内部 ID 的后端映射。
- 新增能力只能通过可选字段或新接口提供，不修改已有字段类型和含义。

详细的前端契约以 `前端API/问答界面功能说明.md` 为准。

## 13. DeerFlow 配置接入

### 13.1 Skill 目录

当前 DeerFlow Skill 根目录为：

`OPIEos/skills/`（由 DeerFlow 仓库根目录下的相对配置 `skills` 解析）

其中 `skills/public` 包含平台公共技能和科创 `kc-*` 共享技能；个人 Custom Skill 由原生用户存储管理。当前配置通过 `skills.path: skills` 相对 DeerFlow 仓库根目录解析，并用 `skills.container_path: /mnt/skills` 指定容器内路径；迁移到服务器时保留相同映射原则，不要把本机、服务器或容器绝对路径硬编码到 `SKILL.md` 或业务提示词中。

### 13.2 `kechuang` Agent Skill 白名单

`kechuang` 的当前 Skill 白名单与磁盘文件一致，共 13 个业务 Skill：

```text
kc-knowledge-search
kc-supply-demand-match
kc-demand-draft
kc-solution-draft
kc-achievement-draft
kc-customer-visit
kc-enterprise-profile
kc-demand-discovery
kc-expert-search
kc-document-parse
kc-field-extraction
kc-web-search
kc-contact-human
```

`agents/kechuang/config.yaml` 将 13 个 `kc-*` Skill 限定为 `kechuang` Agent 可发现的 Skill；`SOUL.md` 作为常驻指令加载。本机已验证该文件式 Agent 配置可被 OPIEos 加载。服务器仍需将资产安装到实际运行目录并验收。MCP 工具仍属于 Gateway 的运行时工具集，不能据此宣称按 Agent 隔离；业务 Backend 必须独立校验用户身份和数据权限。

管理员能力只通过 Backend 管理接口和内部服务提供；外部 Agent 不获得管理员权限。

### 13.3 Tool/MCP 配置

DeerFlow 配置 MCP Server 或内置 Tool 时，只暴露稳定的业务接口，不暴露数据库连接字符串、Milvus 管理对象、MinIO 密钥或内部服务凭证。密钥只通过运行时环境注入。

当前已实现并接入 DeerFlow 配置映射的 MCP：

- 服务端：`backend/runtime/mcp_server.py` 注册 `kc_retrieval`、`kc_match`，并从 `backend/runtime/mcp_tools/` 注册文档、草稿、画像和客户工具；设置 `KC_MCP_ENABLED=true` 后挂载到 `/mcp`。
- 工具名：`kc_retrieval`、`kc_match`、`kc_draft_create/get/list/save/publish/unpublish/delete`、`kc_document_list/detail/parse_result/parse`、`kc_profile`、`kc_customer`、`kc_customer_save_visit`、`kc_web_search`、`kc_web_fetch`。工具复用现有应用服务、生命周期存储和权限校验。联系客服是客户端原生动作，不注册为 MCP。
- OPIEos MCP 连接 ID 为 `kechuang`，关闭 `tool_name_prefix` 时模型看到原始 `kc_*` 工具名。恢复后的核心将 MCP 工具并入运行时工具集，不实现科创 Agent 专属 allowlist；不能假设其他 Agent 看不到这些工具。工具可见性不是数据权限边界，科创 Backend 必须对每次调用执行身份、租户和权限校验。
- 科创自有联网搜索：`kc-web-search` Skill 使用科创 backend 的 `kc_web_search` / `kc_web_fetch`，分别复用 Tavily Search / Extract；不得调用 DeerFlow 内置 `web_search` / `web_fetch`。Search API Key 由 backend 环境变量 `TAVILY_API_KEY` 提供，Skill 不包含密钥或服务地址。
- 请求身份：MCP 连接只从 DeerFlow 运行时上下文映射 `X-KC-Thread-ID -> kc_thread_id` 与 `X-Trace-ID -> kc_trace_id`，不映射 `Authorization`。MCP 服务端根据线程从科创 Backend 会话存储恢复用户、角色和 trace，不接受 Skill 或模型传入的身份字段。
- 匹配方向：`demand_to_supply` 检索方案/成果/专家/企业，`supply_to_demand` 检索需求；`exclude_result_ids` 复用现有换一批排除语义，并做有限超量召回。
- 依赖：业务 backend 通过可选依赖 `mcp>=1.28,<2` 接入，管理台独立服务不依赖聊天编排；启用 MCP 必须安装该依赖，聊天不提供旧 Supervisor 回退。

启用顺序：先安装 backend 的 `mcp` 可选依赖并设置 `KC_MCP_ENABLED=true`；再以 `extensions_config.example.json` 中的 `kechuang` 为模板，将运行时配置通过 `$KC_PLATFORM_MCP_URL` 映射并启用。模板仍默认关闭；当前本机 Gateway 运行在 Windows 宿主机，使用 `http://127.0.0.1:8793/mcp/`。只有切换到 Docker Gateway 时才使用 `http://host.docker.internal:8793/mcp/`；服务器迁移时覆盖为服务器可访问的 backend 地址。

检索/匹配链路曾完成 DeerFlow Gateway → Skill → MCP → Retrieval/Milvus/MySQL 联调。文档、草稿、画像和客户工具已注册并完成代码级定向回归；联系客服由业务响应返回 `CONTACT_WECOM`，小程序原生按钮完成跳转，不经 MCP。真实 DeerFlow 会话中的逐工具联调、实际业务库写入和旧/新问答链路同输入对比仍未完成。MCP 服务继续保留 Host 白名单和请求级业务令牌校验；本机原生访问使用 `localhost`/`127.0.0.1`，部署时填写 DeerFlow 可访问的科创 Backend 地址。

## 14. 分阶段迁移步骤

### 阶段一：运行时接入

- 配置 DeerFlow Gateway 与当前 FastAPI 的认证、`thread_id`、流式事件和错误格式。
- 加载 `agents/kechuang/SOUL.md`；由 SOUL 每轮在内部识别意图并选择当前 13 个业务 Skill，不依赖独立意图路由或普通对话 Skill。
- 保留现有 `/api/v1/chat/stream` 作为适配入口。

### 阶段二：知识库检索闭环

- 检索阶段接入 `kc_retrieval` 和 `kc_match`；上下文继续使用 DeerFlow thread，管理员能力继续留在 Backend 内部接口。
- 完成知识库选择、权限、分步召回、`chunk_role` 关联、去重、图片和详情。
- 完成“换一批”复用 query 与排除集合。
- 以普通问答和供需匹配各跑通一条端到端链路。

### 阶段三：草稿闭环（MCP 操作已接入）

- `kc_draft` 已支持需求、方案、成果草稿的创建、读取、带版本校验保存、发布、下架和逻辑删除。
- DeerFlow Skill 已映射到对应草稿 MCP 工具；字段提取仍由 `kc-field-extraction` Skill 完成。
- 验证 `active_draft`、`draft_context` 持久化和乱序字段补充。
- 需继续用真实 DeerFlow/数据库验证发布、下架、删除及重新发布的跨库同步。

### 阶段四：文件和内部能力（首批 MCP 已接入）

- `kc_document` 已支持本人文档列表、详情、触发解析和读取有界解析结果；上传、原件下载和图片内容使用既有 HTTP API，MCP 暂不传输二进制。
- 企业/专家使用 `kc_profile`；拜访读取用 `kc_customer`，确认保存用 `kc_customer_save_visit`。需求发现调用 `kc_retrieval` 检索需求库，不生成草稿或主动查询无关拜访。所有知识库结果重排后最多三条，模型只给一段不超过 200 字总结，详情进卡片。
- 验证内部/外部权限、脱敏、下载和管理员隔离。

### 阶段五：稳定运行与回归监控

- 使用相同验收样例验证 `kechuang` SOUL 与各业务 Skill 的路由、上下文和结果质量。
- 监控意图漂移、重复 chunk、状态串会话、工具失败和同步失败。
- 2026-09-28 已迁出共享合同、会话、卡片与草稿装配能力，移除旧主 Agent 及其回退。整体检查状态与测试证据以[完整重构验收](../superpowers/specs/2026-09-28-kechuang-final-refactor.md)为准；未进行服务器在线验收。

### 14.6 并行迁移和多 AI 协作

> **状态：历史任务拆分。** 下表记录早期迁移时的并行边界，不是当前待办或允许继续修改 OPIEos 核心的授权。当前接入方式、已知限制和剩余部署工作以本方案第 19 节及文档首页状态为准。

多个 AI 可以并行实现不共享代码文件的工作流，但必须先冻结第 12.3 节的前端契约。所有 AI 使用独立分支或工作树，禁止直接在同一工作区同时修改同一文件。

| 工作流 | 可并行时机 | 主要范围 | 不得修改 |
| --- | --- | --- | --- |
| A. 运行时与 Skill | 契约冻结后 | `OPIEos/config.yaml`、`OPIEos/skills/public/**`、Skill 加载和白名单验证 | `/api/v1` 路由和业务服务 |
| B. Gateway 兼容适配 | 契约冻结后 | `backend/api/v1/execution`、`backend/api/v1/chat`、DeerFlow 事件到旧 SSE 事件的转换 | Skill 正文和数据库服务 |
| C. Thread、身份和权限 | 契约冻结后 | Gateway 身份上下文、外部/内部 `thread_id` 映射、会话归属校验 | 前端请求字段和业务结果字段 |
| D. Tool/MCP 契约 | 契约冻结后 | `backend/core/contracts/tools.py`、`backend/runtime/tool_adapters/**`、受控 Tool/MCP 注册 | 具体 Skill 路由和前端 SSE |
| E. 知识库检索 | D 完成 Tool 契约后 | `backend/agents/retrieval_agent`、`backend/services/vector`、`backend/services/mysql` 的只读适配 | 草稿写入和发布逻辑 |
| F. 草稿与供需匹配 | D 完成 Tool 契约后 | `backend/runtime/mcp_tools`、`backend/services/application`、`backend/services/persistence`、匹配和草稿服务 | DeerFlow 通用运行时源码 |
| G. 文件解析与附件 | D 完成 Tool 契约后 | `backend/services/document_parsing`、`backend/services/storage`、材料解析路由 | 检索去重和草稿状态模型 |
| H. 契约回归与联调 | A-G 有可合并结果后 | 现有流式、认证、材料、草稿和会话测试；前端契约对照检查 | 不自行改变接口契约 |

以下工作不能并行处理：

1. 第一次确定外部 API 契约、事件字段和 `thread_id` 映射方式。
2. Gateway 适配层与最终 Tool 返回字段的合并。
3. 多个 AI 同时修改 `backend/api/v1/router.py`、`schemas.py`、`backend/api/v1/execution/supervisor.py` 或同一份 `config.yaml`。
4. 回归测试、灰度开关和旧链路清理；这些必须在各工作流合并后顺序执行。

每个并行工作流必须提交：变更文件清单、独立测试命令、接口/状态影响、未完成依赖和回滚方式。集成负责人只合并通过自身测试且不改变前端契约的工作流。

#### Tool/MCP 与知识检索是否可以并行

可以并行，但前提是 Task 0 的请求、身份、`thread_id`、检索输入和 Tool 返回契约已经冻结。

- Tool/MCP 工作流只修改 Tool 合同、适配器和对应测试；负责统一注入后端身份、Thread 和 `trace_id`。
- 知识检索工作流只修改检索 Agent、向量/MySQL 只读检索逻辑和检索测试；复用现有 `RetrievalRequest`，不修改 Tool 合同、Gateway、全局配置或草稿写入逻辑。
- 两个工作流可以同时开发和分别测试；只有在各自测试通过后，才由集成负责人把检索实现接入 `kc_retrieval`。
- 如果知识检索任务必须修改 Tool 适配器或共享合同，则拆成“检索内部实现”和“集成接线”两个阶段，接线阶段不得与 Tool/MCP 合同修改并行。

并行 AI 的执行环境必须是独立工作树；若产品环境只能提供同一个工作区，则只能分派严格不重叠的文件边界，并禁止执行清理、批量删除、恢复旧目录或覆盖同步命令。发现文件缺失时立即停止并报告，不自行修复其他工作流的目录。

## 15. 验收场景

### 15.1 会话和意图

- 新会话不会读取旧会话草稿、附件和匹配排除集合。
- “我有智能化改造需求，帮我推荐方案”能直接进入匹配，不反复追问可选字段。
- “企业名称是青岛檬豆”在草稿流程中进入字段增量，不重新开启普通问答。
- “保存草稿”“下架草稿”“删除草稿”状态明确区分。
- 用户中途改成企业画像、联网搜索或普通问答时，旧流程不误写新数据。

### 15.2 检索和换批

- 同一数据的草稿块和解析原文块不同时出现。
- 每批展示不同的完整数据，并按最高分 chunk 显示。
- 换一批不返回上一批，也不使用过滤掉的结果补位。
- `full` 模式行为保持不变。
- 外部用户检索需求库时脱敏并无下载入口。

### 15.3 文件和草稿

- PDF、Word、PPT、Excel、图片和音频都能返回明确解析状态。
- 删除图片后保存不会复制剩余图片。
- 修改原文后重新分块和字段提取使用新版本，不混入旧块。
- 普通问答上传文件不自动生成草稿；用户明确要求时才生成草稿。

### 15.4 跨库同步

- 发布后 MySQL、Milvus、MinIO 关联和 PostgreSQL 任务状态可追溯。
- 下架后不可检索但历史记录保留。
- 删除和彻底清理失败时能定位到具体存储系统和阶段。

### 15.5 流式和错误

- 页面能看到意图、解析、检索、匹配、保存等阶段。
- 429、超时和数据库异常能显示具体原因，不统一显示“问答处理失败”。
- 不泄露模型原始思考链和内部凭证。

## 16. 不做的事情

- 不把全部业务代码复制到 Skill 文件。
- 不让模型直接连接 PostgreSQL、MySQL、Milvus 或 MinIO。
- 不用关键词规则完全替代自然语言意图判断；固定操作仍由主路由结合上下文判断。
- 不把草稿字段、MySQL 字段、解析原文和潜在需求混成一个大字段。
- 不用前端隐藏按钮代替后端权限校验。
- 不在迁移第一阶段重写文件解析、检索、生命周期和管理台全部代码。
- 不把完整历史、完整原文或全部召回结果放入模型上下文。

## 17. 当前文件对应关系

| 文件 | 作用 |
| --- | --- |
| `OPIEos/agents/kechuang/SOUL.md` | `kechuang` Agent 常驻的主指令 |
| `OPIEos/skills/public/kc-knowledge-search/SKILL.md` | 知识库检索 Skill |
| `OPIEos/skills/public/kc-supply-demand-match/SKILL.md` | 供需匹配和换一批 Skill |
| `OPIEos/skills/public/kc-demand-draft/SKILL.md` | 需求草稿 Skill |
| `OPIEos/skills/public/kc-solution-draft/SKILL.md` | 技术方案草稿 Skill |
| `OPIEos/skills/public/kc-achievement-draft/SKILL.md` | 科研成果草稿 Skill |
| `OPIEos/skills/public/kc-document-parse/SKILL.md` | 文档、图片和音频解析 Skill |
| `OPIEos/skills/public/kc-field-extraction/SKILL.md` | 字段增量提取 Skill |
| `OPIEos/skills/public/kc-customer-visit/SKILL.md` | 客户拜访 Skill |
| `OPIEos/skills/public/kc-enterprise-profile/SKILL.md` | 企业画像 Skill |
| `OPIEos/skills/public/kc-demand-discovery/SKILL.md` | 需求发现 Skill |
| `OPIEos/skills/public/kc-expert-search/SKILL.md` | 专家检索 Skill |
| `OPIEos/skills/public/kc-web-search/SKILL.md` | 科创自有联网搜索 Skill |
| `OPIEos/skills/public/kc-contact-human/SKILL.md` | 联系客服意图与小程序原生动作约束（不调用 MCP） |

## 18. 参考资料

- [DeerFlow Skill Creator](https://github.com/bytedance/deer-flow/blob/main/skills/public/skill-creator/SKILL.md)
- [DeerFlow Configuration](https://github.com/bytedance/deer-flow/blob/main/backend/docs/CONFIGURATION.md)
- [DeerFlow MCP Server](https://github.com/bytedance/deer-flow/blob/main/backend/docs/MCP_SERVER.md)
- [DeerFlow Architecture](https://github.com/bytedance/deer-flow/blob/main/backend/docs/ARCHITECTURE.md)
- [DeerFlow Backend README](https://github.com/bytedance/deer-flow/blob/main/backend/README.md)

## 19. 本机测试与服务器迁移配置

### 19.1 当前本机测试

当前本机测试采用“Windows Conda 宿主机 DeerFlow + 宿主机业务 backend”方式：

```text
科创前端 :8794
    -> 宿主机 backend :8793（python main.py，终端看日志）
    -> DeerFlow Gateway :8001
    -> kechuang MCP -> 宿主机 backend /mcp/
```

本机 Gateway 通过 `make dev` 在宿主机启动；业务 backend 用 Conda 环境在终端前台运行。本机数据依赖使用 `backend/docker-compose.local.yml`；OPIEos Compose 仅用于 DeerFlow 自身的 Docker 本地运行配置，不承载科创数据服务或业务 Backend。服务器部署时复用已有 DeerFlow Gateway，单独部署科创 Backend 并配置 Gateway/MCP 地址。

本机必须配置：

```dotenv
# backend/.env.local
DEERFLOW_ENABLED=true
DEERFLOW_BASE_URL=http://127.0.0.1:8001
DEER_FLOW_INTERNAL_AUTH_TOKEN=<与 DeerFlow 端同名变量相同的运行时密钥>
KC_MCP_ENABLED=true
KC_MCP_PATH=/mcp
KC_MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,host.docker.internal:*
```

当前本机模式下，DeerFlow Gateway 与业务 backend 都在 Windows 宿主机运行：Gateway 通过 `make dev` 监听 `127.0.0.1:8001`，backend 通过 `python main.py` 以前台方式监听 `8793`；PostgreSQL、Milvus、MinIO 和 MySQL 使用 `127.0.0.1`。此模式的 MCP 回调也使用 `127.0.0.1:8793`。仅当 Gateway 放在 Docker 容器中时，容器回调宿主机 MCP 才改用 `host.docker.internal:8793`：

```powershell
cd D:\Work\kechuang\backend
python main.py
```

### 19.2 DeerFlow 侧必须修改的配置

DeerFlow 侧不要把 `backend` 地址写入 Skill，修改的是运行时 MCP 配置：

- `OPIEos/extensions_config.json` 的 `kechuang.url` 使用 `$KC_PLATFORM_MCP_URL`。
- 当前本机宿主机 Gateway 从 OPIEos 根目录 `.env` 读取 `KC_PLATFORM_MCP_URL=http://127.0.0.1:8793/mcp/`；本机 `extensions_config.json` 中 `kechuang` 已启用，只映射 `X-KC-Thread-ID`、`X-Trace-ID`，不转发 `Authorization`。
- `OPIEos/docker/docker-compose-dev.yaml` 的注入仅适用于 Docker Gateway；此模式的回调地址必须覆盖为 `http://host.docker.internal:8793/mcp/`。
- 在服务器 OPIEos 的 `extensions_config.json` 中启用 `kechuang`，URL 使用 `$KC_PLATFORM_MCP_URL`，设置 `tool_name_prefix=false`，并保留 `headers_from_context`：`X-KC-Thread-ID -> kc_thread_id`、`X-Trace-ID -> kc_trace_id`、`on_missing: deny`。MCP 工具仅依赖会话和 trace 上下文；用户身份由科创 Backend 按会话恢复。
- Backend 调用 Gateway 的 Header 名由配置指定，当前使用 `X-DeerFlow-Internal-Token` 与 `X-DeerFlow-Owner-User-Id`；owner UUID 必须是 OPIEos 上实际创建 Agent 的用户 UUID。
- 为保持 OPIEos CSRF 核心不变，科创 Backend 在配置内部 Token 时，为同一轮的 `POST /api/threads` 与 `POST /runs/stream` 发送相等的随机 `csrf_token` Cookie 和 `X-CSRF-Token`，通过现有双提交校验。该 CSRF pair 不是身份凭证；Gateway 仍必须验证 `X-DeerFlow-Internal-Token`。不关闭 CSRF 或认证。
- Backend 请求显式绕过系统代理，再按约定调用 `POST /api/threads` 和 `/api/threads/{thread_id}/runs/stream`，固定 `assistant_id=kechuang`。OPIEos 通过标准文件式或数据库式 Agent 存储解析此 ID；本机使用文件式存储并已安装 Agent 资产，服务器也必须在其实际运行数据目录/管理存储中完成部署。
- Gateway 的 `8001` 端口只需对 backend 所在机器可达；本机测试绑定 `127.0.0.1`，远程部署按网络边界调整。

### 19.3 迁移到服务器已有 DeerFlow

迁移时由部署环境覆盖，不修改业务代码：

```dotenv
# 科创 backend 连接服务器 DeerFlow
DEERFLOW_ENABLED=true
DEERFLOW_BASE_URL=http://<deerflow-host>:<gateway-port>
DEER_FLOW_INTERNAL_AUTH_TOKEN=<与 DeerFlow 端同名变量相同的运行时密钥>
# 可选：仅当目标 Gateway 另有 Bearer API Key 认证要求时配置，不替代内部 Token
DEERFLOW_API_KEY=<optional-runtime-secret>

# 服务器 DeerFlow 回调科创 backend
KC_PLATFORM_MCP_URL=http://<backend-host>:8793/mcp/
KC_MCP_ALLOWED_HOSTS=<backend-host>:8793
```

服务器 OPIEos 侧由维护方将 `kechuang` Agent 文件安装到 `DEER_FLOW_HOME/users/<DEERFLOW_AGENT_OWNER_USER_ID>/agents/kechuang/`，并确保 Gateway 接受配置的合法内部认证 Header；CSRF 继续走服务器现有校验，科创 Backend 会发送匹配的双提交 pair，无需维护方修改 CSRF 中间件。`extensions_config.json` 启用 `kechuang` 并关闭 `tool_name_prefix`，MCP 只映射 `kc_thread_id` 与 `kc_trace_id`，不传用户 Bearer Token。Skill 继续从 DeerFlow 根目录映射的 `skills/` 加载；部署路径通过 `config.yaml` 的 `skills.path` / `skills.container_path` 配置，不修改 Skill 内容写死路径。

#### 服务器部署 kechuang Agent 与 Skills

每次升级都同步 `config.yaml` 与 `SOUL.md`，不要使用“目标目录不存在才复制”的首次安装逻辑。本机曾因此持续加载 9 月 24 日的旧 SOUL，最新中文、短总结与入口约束没有生效。下例仅用于已确认使用文件存储、路径已核实的部署；不运行服务、不覆盖数据库或用户资料：

```bash
# 示例路径须替换为目标服务实际使用的路径。
agent_source=/srv/deerflow/agents/kechuang
agent_runtime="/srv/deerflow/backend/.deer-flow/users/${DEERFLOW_AGENT_OWNER_USER_ID}/agents/kechuang"
mkdir -p "$agent_runtime"
for agent_file in config.yaml SOUL.md; do
  install -m 0644 "$agent_source/$agent_file" "$agent_runtime/$agent_file"
  cmp -- "$agent_source/$agent_file" "$agent_runtime/$agent_file" || exit 1
done
```

核对服务进程的 `DEER_FLOW_PROJECT_ROOT`、`DEER_FLOW_HOME` 与 `skills.path`；容器路径以容器内实际挂载为准。按真实用户调用 OPIE 加载器验证 SOUL 与源码一致、13 个业务 Skill 全部可发现，并检查同名个人 Agent 或 Skill 是否覆盖共享版本。文件相同不等于运行进程已刷新，更新后由运维重启 Gateway，再用新会话验证中文短答、需求发现不建草稿、前三条及换批排重。保留既有用户数据；数据库式 Agent 按该存储的发布流程更新，不能通过复制文件假定生效。

本项目使用用户级文件式 Agent 存储。`DEERFLOW_AGENT_OWNER_USER_ID` 指定的 UUID 必须与 `DEER_FLOW_HOME/users/<UUID>/agents/kechuang/` 一致；本机为 `62ddcc4d-f2b5-4152-be26-78a1a7554a86`，服务器为 `e458d2e2-30ad-46f8-86f9-535d0cdec89e`。安装或更新 `config.yaml` 与 `SOUL.md` 后重启 Gateway，再用 Backend 请求所配置的 owner UUID 验证 Agent 加载。服务器若使用 `agent_storage.backend: db`，则通过该实例的 Agent 管理流程创建并发布同名 Agent。

Agent 配置将当前 13 个 `kc-*` Skill 列入 `skills` 白名单；普通 Skill 可见性因此按 OPIEos Agent 配置收敛。MCP 工具运行时仍可能对其他 Agent 可见，不能把工具隔离当作本方案保证；业务数据权限仍由科创 Backend 校验。部署后需确认 `assistant_id=kechuang` 能创建 thread 并启动流式 run，且实际 `DEER_FLOW_HOME` 下的 Agent 与 Skill 清单正确。

DeerFlow 运行时按目录发现 Skill，服务器文件树应保持如下结构：

```text
/srv/deerflow/skills/
├── public/
│   ├── kc-demand-draft/SKILL.md
│   ├── kc-solution-draft/SKILL.md
│   └── ...其余 kc-* 技能目录
└── custom/
```

`SKILL.md` 是每个 Skill 目录中的运行时入口文件；`.skill` 是 DeerFlow 用于导入/导出的 ZIP 归档包，不是运行时文件名。科创共享部署直接同步上述 13 个 public 子目录；个人 `.skill` 导入不等于共享安装，不可据此认定所有用户可发现。

服务器 `config.yaml` 的 `skills.path` 指向宿主机上包含 `public/` 和 `custom/` 的 skills 根目录；`skills.container_path` 指向 sandbox 内看到的规范绝对路径。示例：

```yaml
skills:
  path: /srv/deerflow/skills
  container_path: /mnt/skills
```

若 Gateway 运行在容器中，需将宿主机 `skills.path` 对应目录挂载到 `container_path`；若使用 DeerFlow 支持的环境变量配置路径，可设置 `DEER_FLOW_SKILLS_PATH`，但应确保容器挂载与 sandbox 路径映射一致。不要把服务器绝对路径写入任一 `kc-*/SKILL.md`。变更 `skills.path`、挂载或 Skill 文件后重启 Gateway，使运行时重新加载目录和 sandbox 映射。

Skill 白名单由 `kechuang/config.yaml` 的 `skills` 字段控制，不需要改 DeerFlow 核心注册代码；同一目录随 Agent 资产部署到服务器即可。MCP 工具仍可能对其他 Agent 可见；需按服务器实际配置验证工具可见性，不能承诺工具隔离。无论 Skill 或工具如何配置，业务数据最终访问控制仍必须由科创 Backend 的身份、租户与权限校验负责。服务器 Agent、Header 或工具配置未完成时，不应把远程 `assistant_id=kechuang` 路由视作已验收。

需要同时满足两个方向的网络连通：

1. 科创 backend -> DeerFlow Gateway：用于 `/api/threads/{thread_id}/runs/stream` 问答流。
2. DeerFlow Gateway -> 科创 backend `/mcp/`：用于 `kc_retrieval`、`kc_match`、`kc_document_*`、`kc_draft_*`、`kc_profile`、`kc_customer`、`kc_customer_save_visit`、`kc_web_search` 和 `kc_web_fetch`。联网搜索由科创 Tavily MCP 提供；客服动作不经过 MCP。

如果服务器 DeerFlow 与 backend 在同一 Docker 网络，可使用服务名；如果 backend 在宿主机，使用 `host.docker.internal` 或服务器实际可解析的 backend 地址。`KC_MCP_ALLOWED_HOSTS` 必须与 DeerFlow 发起请求时的 `Host` 头一致，不要用 `*` 放开全部 Host。服务器自己的 `DEER_FLOW_ROOT`、`DEER_FLOW_CONFIG_PATH`、`DEER_FLOW_EXTENSIONS_CONFIG_PATH` 和 `DEER_FLOW_HOME` 继续使用服务器路径；Skill 继续通过 DeerFlow 根目录 `skills/` 映射加载。

### 19.4 六个快速入口的流程与前端合同

本节定义迁移必须保持的行为合同；文档更新本身不代表代码或服务器链路已完成端到端验收。六个入口仍进入原有科创 Backend 问答 API/SSE，不新增前端调用路径、请求参数或页面步骤：

| 快速入口 | 默认流程标识 | 必须保留的原流程 |
| --- | --- | --- |
| 我有需求 | `demand` | 需求类型/资料引导、逐轮补充、生成与保存需求草稿 |
| 我有方案&成果 | `supply_draft` | 先选择技术方案或科研成果，再按对应原流程补充、生成与保存草稿 |
| 供需匹配 | `supply_demand_match` | 按原流程确认匹配方向和检索条件，执行匹配并返回原匹配结果/后续操作 |
| 需求发现 | `demand_discovery` | 按已有主题直接检索需求库，最多返回重排前三条卡片；不自动生成草稿 |
| 企业画像 | `enterprise_profile` | 按原企业检索流程查询正式记录并返回原结构化画像结果 |
| 客户拜访 | `customer_visit` | 按原拜访记录流程收集企业与拜访内容，解析/补充后继续原保存或匹配步骤 |

用户明确选择的入口在当前任务及附件处理期间持续生效，只有用户明确要求切换才改道；不能根据附件内容二次改成方案/成果。SOUL/Skill 负责意图和工具编排，Backend 只执行确定性合同、权限、状态和数据操作，不另起意图/规划/总结模型。独立新任务按新的明确请求处理。

Backend 继续对外提供原 `/api/v1/chat/stream` 与原 SSE 事件序列 `start`、`progress`、`token`、`done`、`error`。适配层将 DeerFlow 内部运行事件和原业务 API/工具结果映射回前端已使用的 `status`、`intent`、`ui_blocks`、`actions`、`draft`/`drafts`、`match_results` 等字段及原有 block 类型；不能只把匹配卡片、草稿卡片或流程选项降级为一段 Markdown/普通文本。请求字段、会话与 `thread_id` 语义、流式交互和页面流程保持原样，DeerFlow 只增加每轮意图判断及 Agent 运行时编排。

### 19.5 日志与失败边界

- 业务 backend：运行 `python main.py` 的终端直接看日志，`Ctrl+C` 停止。普通 INFO 使用默认前景色、关键阶段绿色、错误红色；非 TTY/NO_COLOR/JSON 不染色。
- 原生 OPIE：PowerShell `Get-Content ./logs/gateway.log -Tail 100 -Wait`；Linux `tail -n 100 -F ./logs/gateway.log`。模型/工具回调记录 `event=opie_stage_timing run_id=... stage=model|tool name=... duration_ms=... outcome=...`，无提示词或正文。阶段嵌套不能重复相加；通过 run_id 与 Backend trace 关联定位延迟。
- Docker DeerFlow：当前开发入口把日志写入容器内 `/app/logs/gateway.log`，另开终端执行 `docker exec deer-flow-gateway sh -c 'tail -f /app/logs/gateway.log'`。
- 日志和管理职责不合并：科创 backend / 管理台继续负责业务身份、数据管理、业务审计与审计查询；DeerFlow 负责 Agent run、Skill/MCP 执行和 Gateway 运行日志。科创业务管理功能不迁成 MCP，也不由 DeerFlow 控制台替代。
- 问答追踪使用 backend 请求上下文中的 `trace_id`：将其作为 `kc_trace_id` 放入 DeerFlow run metadata 和运行时 context secrets，再由 DeerFlow `kechuang.headers_from_context` 映射为 `X-Trace-ID` 传到 MCP。科创会话 state 关联标准化 DeerFlow thread ID，并在 Gateway 返回 `Content-Location` 时记录 run ID；MCP 业务审计事件沿用同一个 `trace_id`，管理员可用 `/api/v1/admin/audit-events?trace_id=<trace-id>` 筛选。公开请求体中的同名字段不作为可信来源。
- `DEERFLOW_ENABLED=false` 或未配置 `DEERFLOW_BASE_URL` 时，聊天返回 503；旧执行图已移除，没有备用问答路径。
- 本机链路验证通过不等于服务器链路验收通过；服务器迁移必须重新验证网络、鉴权、MCP 工具发现和真实知识数据。

### 19.6 聊天流、MCP 通道与连接/等待行为

聊天不是通过 MCP Streamable HTTP 发起。前端只请求科创 Backend 的 `POST /api/v1/chat/stream` 并消费 SSE；Backend 调用 OPIEos Gateway 的 `POST /api/threads` 确认 thread，再调用 `POST /api/threads/{thread_id}/runs/stream` 获取 LangGraph run 的 HTTP/SSE 事件。MCP `/mcp/` 是 Agent 调用科创业务工具的另一条独立通道。

当前 Backend 每轮用户消息创建一个 `httpx.AsyncClient`，在同一个连接池中执行 thread 请求和 run 流请求，轮次结束后关闭；因此不是每个 token 都新建 TCP，但跨轮通常不复用该客户端的 keep-alive 连接。少量本机连接建立开销不是长时间无回复的合理解释。

DeerFlow 请求超时默认值为 300 秒。适配层先向前端发 `start`，随后等待 Gateway 建 thread/启动 run；此等待阶段没有独立的前端 SSE 心跳。内部 SSE 注释心跳也会被 Backend 解析器忽略，不会自动变成前端 `progress`。因此 Gateway 无响应或 Agent 长时间没有可见事件时，前端可能一直显示“处理中”；thread 请求超时后才会收到 `error`，若流持续有内部心跳则也可能没有新的可见进度。

2026-09-24 的最近一次只读探测：`127.0.0.1:8001` 存在监听进程，但 `curl.exe --noproxy "*" --max-time 4 http://127.0.0.1:8001/health` 未收到响应；科创 Backend `8793/api/v1/health` 返回 HTTP 200（约 2.4 秒）。此历史快照不代表当前进程状态；遇到同类故障应检查 Gateway 是否仍能处理请求，不能仅凭端口监听或 Backend 健康状态判定 DeerFlow 可用。本次未停止或重启任何服务。
