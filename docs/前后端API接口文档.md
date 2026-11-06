# 前后端 API 接口交付文档（DeerFlow 基座版）

> 目标读者：小程序、Web 测试页面和后端联调人员。本文可以作为页面、请求封装和 Postman 联调依据。
>
> 文档依据当前业务网关和 DeerFlow 接入边界整理。前端只调用业务网关；DeerFlow Gateway、Skill 和 MCP 不作为前端直接调用对象。

> **【2026-09-22 Token-only 更新】** 除登录接口外，问答、历史会话、草稿、个人中心、附件和人工对接接口统一从 `Authorization: Bearer <access_token>` 获取用户身份。前端不再传 `user_id`；请求体、查询参数和表单中的同名字段不作为权限依据。

## 0. 先看这三件事

2026-10-06 后端模块更新：用户 HTTP 属于 `assistant/api/v1`，管理 HTTP 属于 `admin/api/v1`，DeerFlow/MCP 属于 `assistant/integrations`；HTTP/MCP 草稿业务共用应用服务。120 个既有公开路径/方法不变，不要求测试前端跟随内部 Python 文件迁移。草稿保存后下一轮使用服务端最新字段；SSE 名称不变，失败/取消/断流会记录本轮终态。过期草稿仍返回 410，但读取不再触发物理删除。鉴权方案按用户决定保持原样；详见[实现与验证记录](方案/后端系统审计与管理员问答分块.md#2026-10-06-实现与验证记录)。

2026-09-28 边界更新：聊天及材料处理只有 DeerFlow 编排，未就绪返回 HTTP 503；不回退旧主 Agent。MCP 检索统一强制重排、去重后最多三条，换批排除本会话已展示对象。模型正文自然总结不超过 200 字，完整字段在卡片/详情，工具前旁白不展示。需求发现默认查询需求库，不自动创建草稿。

四类智能问答草稿遵循 `draft_contracts.py` 的 draft_only 字段；管理台使用 full 合同。MCP 顶层仅允许声明的内容字段、摘要、附件与来源块，不允许模型自造扩展键或内部标记。HTTP 编辑不得改变既有草稿类型（409）；外部用户不能创建/读写客户拜访草稿（403），列表不返回拜访。拜访确认保存用 `kc_customer_save_visit(draft_id, expected_version)`，不使用公开发布/上下架/删除接口（409）。保存已发布的普通草稿并不自动重新发布；公开数据在用户明确发布后更新。

1. 所有请求只访问统一网关，不直接访问 PostgreSQL、MySQL、MinIO、Milvus 或 Agent。
2. 小程序问答使用 `POST /api/v1/chat/stream` 消费 SSE；异步任务轮询问答接口已停用，不要再按旧示例实现。
3. 当前认证是测试身份模式，但已通过 Bearer Token 传递身份。除登录请求外，前端不传 `user_id`，后端统一从 Token 解析并校验用户。
4. 小程序联调细节（SSE 组帧、草稿状态/版本、图片地址与展示规则）以[问答与个人中心 API](前端API/问答界面功能说明.md)为准；生产小程序使用 HTTPS 合法域名，不能硬编码当前 HTTP 联调地址。

---

## 1. 接入配置

### 1.1 环境变量

Postman 新建环境：

| 变量 | 示例值 | 说明 |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:8793` | 本地网关；不要在变量中写 `/api/v1` |
| `userId` | 仅登录联调使用 | 非登录接口不再传递 |
| `threadId` | 留空 | 首次问答留空，服务端生成 |
| `taskId` | 留空 | 从提交接口响应复制 |
| `draftId` | 留空 | 从草稿接口响应复制 |
| `documentId` | 留空 | 从文档或上传接口响应复制 |
| `versionId` | 留空 | 从文档详情复制 |
| `traceId` | `postman-demo-001` | 可选，便于排查日志 |

正式小程序只需要保存后端返回的登录态、`thread_id`、`task_id`、`draft_id`、`document_id`，不要保存数据库主键或内部服务地址。

### 1.2 通用请求头

JSON 请求：

```http
Content-Type: application/json
X-Trace-ID: {{$randomUUID}}
```

文件请求使用 `multipart/form-data`，由 Postman 或小程序自动生成 boundary，不要手写 `Content-Type`。

### 1.3 统一错误响应

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "请求参数校验失败",
    "details": []
  },
  "trace_id": "postman-demo-001"
}
```

| HTTP | code | 前端处理 |
|---:|---|---|
| 400 | `BAD_REQUEST` | 请求格式错误 |
| 403 | `FORBIDDEN` | 无权限，停止当前操作 |
| 404 | `NOT_FOUND` | 资源不存在或不属于当前用户 |
| 409 | `CONFLICT` | 状态冲突，刷新资源后再操作 |
| 415 | `UNSUPPORTED_FILE_TYPE` | 文件格式不支持，按提示转换后重新上传，不自动重试 |
| 422 | `VALIDATION_ERROR` | 按 `details` 定位字段并提示 |
| 429 | `RATE_LIMITED` | 按 `Retry-After` 秒数重试 |
| 502 | `UPSTREAM_ERROR` | 上游服务失败，可重试 |
| 503 | `DEPENDENCY_UNAVAILABLE` | 后端依赖未就绪，展示系统繁忙 |
| 500 | `INTERNAL_ERROR` | 不展示堆栈，只展示 trace_id |

### 1.4 不支持的文件格式

所有文件上传和解析入口统一使用错误码`UNSUPPORTED_FILE_TYPE`。现有成功响应及任务接口HTTP状态不变；新增的`error_code`为可选字段，不能仅用HTTP是否为2xx判断任务成功。

- 管理台`POST /api/v1/admin/files/upload`仍返回HTTP202；上传被拒绝时返回`status=FAILED`、`stage=failed`、`error_code=UNSUPPORTED_FILE_TYPE`，`error`为可直接显示的转换提示。随后查询任务仍保留相同错误码。
- Perception上传和同步解析接口仍返回HTTP200；业务失败体为`status=FAILED`、`error_code=UNSUPPORTED_FILE_TYPE`、`error`。异步解析先返回HTTP202，轮询任务获得上述失败原因。
- 问答`POST /api/v1/chat/materials/parse`对不支持的文档返回HTTP415，统一错误体见下方示例。
- 问答流式接口已进入SSE后，HTTP仍为200；发送`error`事件：`{"code":415,"error_code":"UNSUPPORTED_FILE_TYPE","stage":"upload","message":"..."}`，之后结束，不再发送成功`done`。`code`保留原有数字HTTP语义，前端按`error_code`识别格式问题。
- `.doc`应使用Word/WPS另存为`.docx`；`.ppt`应使用PowerPoint/WPS另存为`.pptx`；`.xls`应使用Excel/WPS另存为`.xlsx`。不能仅修改后缀。其他格式按返回提示选择支持的文件；文档可导出为PDF，压缩包应先解压后选择文件。
- 支持的文档格式仍为`.pdf/.docx/.pptx/.xlsx/.md/.txt/.jpg/.jpeg/.png/.webp/.bmp/.tif/.tiff`，本次不增加格式或改变50MB限制。问答音频继续走原有语音流程。

管理台失败响应示例（仅列关键字段）：

```json
{"task_id":"task_example","status":"FAILED","stage":"failed","error_code":"UNSUPPORTED_FILE_TYPE","error":"暂不支持 .doc 文件，请使用 Word/WPS 另存为 .docx 后上传，不能只修改文件后缀。"}
```

问答HTTP415示例：

```json
{"error":{"code":"UNSUPPORTED_FILE_TYPE","message":"暂不支持 .doc 文件，请使用 Word/WPS 另存为 .docx 后上传，不能只修改文件后缀。","details":{"code":"UNSUPPORTED_FILE_TYPE","message":"暂不支持 .doc 文件，请使用 Word/WPS 另存为 .docx 后上传，不能只修改文件后缀。"}},"trace_id":"trace_example"}
```

前端处理顺序：HTTP失败读取`error.code/error.message`；HTTP成功仍检查业务`status`，`FAILED`时读取`error_code/error`。命中该错误后停止当前上传或解析状态、展示完整提示，等待用户选择转换后的新文件；不继续提取字段或发布。

用户先上传、后确定需求/方案/成果用途时，创建或保存草稿会同步补齐附件文档的空知识类型；发布前也会检查并补齐此类历史记录。补齐仅限同用户、公共范围、当前有效版本且未发布/未建索引的文档，不覆盖已有分类或已发布来源。附件归属、版本、路径或已有分类冲突时仍返回HTTP409，前端展示后端提示；不要求用户重新解析有效附件。

---

## 2. 字段归属规则

### 2.1 前端必须填写

当前 Token-only 契约下，以下字段由前端填写；用户身份不在业务请求中填写：

| 字段 | 出现位置 | 说明 |
|---|---|---|
| `Authorization` | 除登录外的所有用户接口 | `Bearer <access_token>`；后端从 Token 读取当前用户 |
| `message` | 问答 | 用户输入，不能为空，最长 20000 字符 |
| `thread_id` | 继续对话 | 首次不传；后续使用上一次返回的会话 ID |
| `draft` | 草稿写入/修改 | 草稿对象，字段由 `draft_type` 决定 |
| `target_status` | 草稿状态流转 | 只能是 `PENDING_REVIEW`、`PUBLISHED`、`WITHDRAWN` |
| `file` | 文件上传 | 必须是真实文件 |
| `reason_code` | 下架 | 必须使用第 8 节列出的枚举 |

### 2.2 前端可以填写

| 字段 | 作用 |
|---|---|
| `intent_hint` | 快捷入口传入意图，如 `enterprise_profile`、`demand`、`solution`、`achievement`、`supply_demand_match` |
| `intent_confirmation` | 意图确认：`confirmed` 或 `rejected` |
| `flow_action` | 流程按钮：`interrupt`、`back`、`switch`、`next_match_batch`、`immediate_match` |
| `draft_context` | 继续编辑/匹配时携带草稿上下文，通常至少包含 `draft_id` 或 `draft_type` |
| `title` | 重命名会话时填写，最长 80 字符 |
| `limit` | 列表条数，按接口限制填写 |
| `scope_id` | 发布草稿时可填，默认 `public` |
| `replace_existing` | 发布草稿时是否替换已有索引，默认 `true` |

### 2.3 后端自动填写，前端不要传

问答执行时后端从用户身份和会话中补齐：

`user_role`、`user_type`、`company`、`contact_name`、`contact_phone`、`session_id`、`trace_id`、`request_id`、`task_id`、`status`、`stage`、`progress`、`retrieval_status`、`flow_state`。

当前问答请求支持 `knowledge_type`（可选，限定知识类型），但不支持 `top_k`、`use_rerank`、`expand_query` 或 `scope_id` 作为问答参数；不要把草稿发布参数与问答参数混用。

### 2.4 后端内部字段

前端禁止依赖或修改：

`storage_key`、`mysql_id`、`dense_vector`、`chunk_role`、`operation`、`attempt`、`max_attempts`、`timeout_seconds`、`selected_tool`、`evaluation`、`compensation`、`recovery_*`、`request_payload`。

响应中出现这些字段时，只用于调试或后台页面；小程序不要展示。

---

## 3. 小程序完整页面与调用顺序

### 3.1 登录进入首页

```text
POST /auth/login
  ↓ 保存 access_token、expires_in 和 user.user_id
GET /me/profile
GET /me/conversations
  ↓
首页：新建问答 / 历史会话 / 个人中心
```

### 3.2 智能问答

```text
POST /chat/stream
  ↓ 接收 start、progress、token、done、error 事件
读取 done.result.ui_blocks、done.result.actions、done.result.answer
  ↓ 用户点击动作
继续 POST /chat/stream，并携带同一个 thread_id
```

### 3.3 个人中心

```text
GET /me/profile
GET /me/documents
GET /me/drafts
GET /me/visit-records
GET /me/match-results
```

列表为空是正常业务状态，不是接口错误。前端分别展示空状态。

---

## 4. 认证与身份接口

### 4.1 测试登录（当前可测）

```http
POST {{baseUrl}}/api/v1/auth/login
Content-Type: application/json
```

请求：

```json
{ "user_id": "demo-user" }
```

成功 `200`：

```json
{
  "authenticated": true,
  "mode": "test",
  "user": {
    "user_id": "demo-user",
    "name": "测试用户",
    "is_inner_user": false,
    "user_role": "external",
    "company": null,
    "phone": null,
    "user_type": null
  }
}
```

Postman：Body → raw → JSON，填入请求体并发送；将 `access_token` 保存到环境变量 `accessToken`，将 `user.user_id` 保存到环境变量 `userId` 仅用于登录联调和展示。

> 当前接口接收宿主系统提供的 `user_id`，签发本平台测试用 `access_token`；不保存宿主系统令牌。正式登录仍需由宿主系统完成，前端页面不要把当前测试登录当成最终登录方案。

### 4.2 查询当前身份（当前可测）

```http
GET {{baseUrl}}/api/v1/auth/me
Authorization: Bearer <access_token>
```

成功响应结构与登录相同。失败常见为 `404`：用户不存在。

### 4.3 测试用户列表（仅联调）

```http
GET {{baseUrl}}/api/v1/identity/test-users?limit=100
```

只用于测试页选择身份；小程序正式版不展示此接口。

### 4.4 退出（当前可测）

```http
POST {{baseUrl}}/api/v1/auth/logout
```

成功：

```json
{ "authenticated": false, "mode": "test" }
```

---

## 5. 智能问答接口

### 5.1 流式问答（当前唯一问答入口）

`POST /api/v1/chat/stream` 返回 SSE 事件流。用户身份由 Bearer Token 提供，不读取请求体中的 `user_id`、`user_role`。

小程序未获取到 access token 时，应在发起请求前自行提示用户注册或登录；后端接口不会自动弹出登录窗口。

#### 当前实际可传请求体

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `message` | 是 | string | 用户问题，1～20000 字符 |
| `thread_id` | 否 | string | 继续会话时传 |
| `intent_hint` | 否 | string | 快捷入口意图 |
| `intent_confirmation` | 否 | enum | `confirmed` / `rejected` |
| `flow_action` | 否 | enum | 流程控制动作 |
| `draft_context` | 否 | object | 草稿或流程上下文 |

Postman 请求：

```http
POST {{baseUrl}}/api/v1/chat/stream
Accept: text/event-stream
Content-Type: application/json
Authorization: Bearer <access_token>
X-Trace-ID: {{$randomUUID}}
```

```json
{
  "message": "帮我找能做智慧环卫改造的服务商，预算 80 万",
  "thread_id": "",
  "intent_hint": "supply_demand_match"
}
```

事件示例：

```text
event: start
data: {"thread_id":"session-demo-user-a1b2c3d4"}

event: token
data: {"text":"回答片段"}

event: done
data: {"result":{"status":"SUCCESS","answer":"完整回答"}}
```

前端保存 `thread_id`，以便继续同一会话；不再创建或轮询问答任务。

### 5.2 异步问答任务（已停用）

`POST /api/v1/chat/async` 与 `GET /api/v1/chat/tasks/{task_id}` 已停用，所有用户问答统一改用 `/api/v1/chat/stream`。

### 5.3 `result` 前端渲染规则

优先渲染 `ui_blocks`，其次才使用 `answer` 作为兜底文本。

| `ui_blocks[].type` | 前端组件 |
|---|---|
| `answer` | 普通回答气泡 |
| `intent_confirmation` | 意图确认卡片 |
| `draft_information_required` | 缺失字段表单 |
| `draft_card` | 草稿预览/编辑卡片 |
| `recommendation_list` | 推荐列表 |
| `related_recommendation_list` | 相关推荐列表 |
| `evidence_list` | 证据列表，展示来源 |
| `document_references` | 文档引用和下载按钮 |
| `image_references` | 图片网格 |
| `empty_state` | 无结果状态 |
| `notice` | 流程提示 |
| `error_state` | 错误状态 |

常见 `actions`：

| action type | 前端动作 |
|---|---|
| `CONFIRM_INTENT` | 再次提交同一 `thread_id`，带 `intent_confirmation=confirmed` |
| `CHANGE_INTENT` | 重新选择快捷入口，带新的 `intent_hint` |
| `SAVE_DRAFT` | 调用创建/修改草稿接口 |
| `PUBLISH_DRAFT` | 调用草稿发布接口 |
| `START_MATCHING` | 按动作返回的 `request`/`message` 继续问答；只有“换一批”使用 `flow_action=next_match_batch` |
| `CONTACT_WECOM` | 调用人工联系接口 |
| `UPLOAD_MATERIAL` | 打开文件选择后调用材料解析接口 |
| `BACK_FLOW` / `SWITCH_FLOW` | 带对应 `flow_action` 继续问答 |

进入需求、方案或成果建稿流程时，若识别到多个独立事项，最终响应为 `status=DRAFT_MULTI_CONFIRM_REQUIRED`；`answer` 中列出事项并询问拆分或合并，`draft` 为 `null`、`drafts` 为空，且不会返回草稿卡片。前端无需增加专用确认参数：将用户选择作为普通 `message`，沿用同一 `thread_id` 提交。用户确认后，`drafts[]` 按材料中的事项顺序返回已生成的草稿；只有实际返回草稿时才展示卡片，并按 `drafts[]` 分页切换。单事项直接生成一份草稿。客户拜访不适用多事项拆分。

### 5.4 普通同步问答（已停用）

`POST /api/v1/chat` 已停用。

### 5.5 流式问答（当前唯一入口）

```http
POST {{baseUrl}}/api/v1/chat/stream
Accept: text/event-stream
```

事件：

```text
event: start
data: {"thread_id":"session-001"}

event: token
data: {"text":"回答片段"}

event: progress
data: {"stage":"retrieval","text":"正在检索相关资料…","status":"SEARCHING"}

event: done
data: {"result":{"status":"SUCCESS","answer":"完整回答"}}
```

客户端应消费 SSE 事件并在 `done` 时读取最终结果；收到 `error` 时展示错误并允许重试。

### 5.6 用户上传材料并解析/生成草稿

推荐使用流式接口 `POST /api/v1/chat/materials/parse/stream`，返回与问答一致的 SSE 事件；`POST /api/v1/chat/materials/parse` 保留为非流式兼容接口。两者都使用 `multipart/form-data`。

| 表单字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | 文件 |
| `thread_id` | 否 | 当前会话 |
| `message` | 否 | 文件补充说明 |
| `intent_hint` | 否 | 资料类型或快捷意图 |

Postman：Body → form-data：

```text
file        File      选择 example.docx
thread_id   Text      {{threadId}}
message     Text      这是我的技术方案，请整理成方案草稿
intent_hint Text      solution
```

流式接口成功时在 `done.result` 读取 `draft`、`drafts`、`ui_blocks`、`actions`；非流式接口直接返回问答结果对象。当前实现不会直接发布或建立 Milvus 索引，用户仍需保存/发布草稿。

未选择业务入口且未明确建稿意图时，直接上传文件只返回资料摘要，不自动建稿或检索。解析原文在当前会话暂存 30 分钟；用户后续明确进入建稿流程时，后端将完整原文作为上下文继续处理，不能只依赖摘要。确认轮继续使用原 `thread_id` 和普通问答接口。

> 前端不要传 `user_role` 或 `user_id`；用户身份由 Token 注入。知识库类型由当前会话意图和后端路由确定，不能把这些字段作为前端权限依据。

## 6. 会话接口

### 6.1 会话列表

```http
GET {{baseUrl}}/api/v1/me/conversations?limit=50
Authorization: Bearer <access_token>
```

成功：

```json
{
  "sessions": [
    {
      "thread_id": "session-demo-user-a1b2c3d4",
      "title": "智慧环卫服务商查询",
      "user_id": "demo-user",
      "created_at": "2026-09-02T10:00:00Z",
      "updated_at": "2026-09-02T10:05:00Z"
    }
  ],
  "total": 1,
  "user_id": "demo-user"
}
```

### 6.2 会话消息

```http
GET {{baseUrl}}/api/v1/chat/sessions/{{threadId}}/messages?limit=20
Authorization: Bearer <access_token>
```

成功返回数组：

```json
[
  {
    "thread_id": "session-demo-user-a1b2c3d4",
    "role": "user",
    "content": "帮我找智慧环卫服务商",
    "created_at": "2026-09-02T10:00:00Z"
  },
  {
    "thread_id": "session-demo-user-a1b2c3d4",
    "role": "assistant",
    "content": "已找到相关信息。",
    "created_at": "2026-09-02T10:00:05Z",
    "result": {}
  }
]
```

### 6.3 重命名会话

```http
PATCH {{baseUrl}}/api/v1/chat/sessions/{{threadId}}
Content-Type: application/json
```

```json
{ "title": "智慧环卫服务商查询" }
```

成功：`{"session": {...}}`。标题不能为空，最长 80 字符。

### 6.4 删除会话

```http
DELETE {{baseUrl}}/api/v1/chat/sessions/{{threadId}}
Authorization: Bearer <access_token>
```

成功：

```json
{ "deleted": true, "thread_id": "session-demo-user-a1b2c3d4" }
```

---

## 7. 个人中心接口

### 7.1 个人资料

```http
GET {{baseUrl}}/api/v1/me/profile
Authorization: Bearer <access_token>
```

前端展示姓名、企业、电话、用户类型、内外部身份等后端返回字段。不要自己根据 `user_id` 推导姓名或角色。

成功 `200` 示例：

```json
{
  "user_id": "demo-user",
  "is_inner_user": false,
  "source": "mysql.xp_user_card_simple",
  "name": "测试用户",
  "user_type": "1",
  "company": "示例公司",
  "phone": "13800000000"
}
```

字段可能因用户资料为空而缺省；前端用“未填写”占位，不把缺省当成接口失败。

### 7.2 我的文档

列表：

```http
GET {{baseUrl}}/api/v1/me/documents?limit=100
Authorization: Bearer <access_token>
```

成功：

```json
{
  "documents": [
    {
      "document_id": "doc-001",
      "source_name": "技术方案.docx",
      "file_name": "技术方案.docx",
      "status": "PUBLISHED",
      "active_version_id": "ver-001",
      "knowledge_type": "solution",
      "created_at": "2026-09-02T10:00:00Z",
      "updated_at": "2026-09-02T10:10:00Z"
    }
  ],
  "total": 1,
  "user_id": "demo-user"
}
```

详情：

```http
GET {{baseUrl}}/api/v1/me/documents/{{documentId}}
Authorization: Bearer <access_token>
```

成功：`{"document": {...}, "versions": [...]}`。

### 7.3 原文件预览/下载

```http
GET {{baseUrl}}/api/v1/knowledge/documents/{{documentId}}/file?inline=true
Authorization: Bearer <access_token>
```

- `inline=true`：允许预览 PDF；外部用户可预览。
- `inline=false` 或省略：下载原文件。公开已发布文件允许匿名下载；外部用户可下载自己提交的文件或公开已发布文件。
- 访问非公开或未发布文件时携带登录后的 `Authorization: Bearer <access_token>`；`user_id` 查询参数不能代替登录认证。
- 返回二进制文件，不是 JSON；前端使用返回的 `Content-Type` 和 `Content-Disposition`。
- 管理控制台使用 `GET /api/v1/admin/documents/{document_id}/file?inline=true`，沿用现有管理接口部署访问边界，无需普通用户登录。仅提供当前未回收、未彻底删除版本的原件；原件不存在返回404，存储服务未就绪返回503。管理台文档详情和草稿附件下载均使用此入口，普通用户界面继续使用上面的接口。
- 字段提取使用DeepSeek兼容JSON Mode：`response_format={"type":"json_object"}`，输出预算16384 tokens。格式或结构不符时保留原始材料纠正重试一次；空回复和明确截断也只重试一次。最终失败通过任务的`draft_error_code/draft_error`返回`DRAFT_INVALID_JSON`、`LLM_OUTPUT_EMPTY`或`LLM_OUTPUT_TRUNCATED`，前端直接展示并允许重新提取，无需重传原件。JSON Mode不替代业务字段校验，所有校验通过后才生成草稿。
- 明确的模型短句拒答返回`LLM_REFUSAL`，不自动重试、不生成草稿。前端显示`draft_error`中联系供应商或更换字段提取模型的提示；原件和解析原文继续保留。

### 7.4 我的草稿

```http
GET {{baseUrl}}/api/v1/me/drafts?limit=50
Authorization: Bearer <access_token>
```

成功返回草稿数组。每条通常包含：

```json
{
  "draft_id": "draft-001",
  "user_id": "demo-user",
  "draft_type": "demand",
  "status": "PENDING_REVIEW",
  "draft": {
    "title": "智慧环卫改造需求",
    "fields": {}
  },
  "created_at": "2026-09-02T10:00:00Z",
  "updated_at": "2026-09-02T10:05:00Z"
}
```

### 7.5 拜访记录与匹配记录

```http
GET {{baseUrl}}/api/v1/me/visit-records?limit=100
GET {{baseUrl}}/api/v1/me/match-results?limit=100
Authorization: Bearer <access_token>
```

两者返回数组。前端按照返回对象展示，不要把普通聊天记录当成匹配记录；后端只会在实际执行对应业务后写入记录。

匹配记录当前响应字段：

```json
{
  "result_id": "match-001",
  "user_id": "demo-user",
  "session_id": "session-demo-user-a1b2c3d4",
  "query": "寻找智慧环卫服务商",
  "draft_id": "draft-001",
  "result": {
    "status": "SUCCESS",
    "items": []
  },
  "created_at": "2026-09-02T10:10:00+00:00"
}
```

拜访记录复用草稿对象，筛选条件是 `draft_type=customer_visit`，通常包含 `draft_id`、`user_id`、`draft_type`、`status`、`draft`、`created_at`、`updated_at`。`draft` 内的拜访字段见第 8.1 节。

---

## 8. 草稿接口

### 8.1 草稿类型和字段

| `draft_type` | 适用用户 | 必要字段 |
|---|---|---|
| `demand` | 需求方 | `title`、`category`、`company`、`contect`、`analyse_contect`、`link_name`、`link_tel` |
| `solution` | 供应方 | `title`、`product_type`、`contect` |
| `achievement` | 供应方/科研成果 | `title`、`category`、`company`、`analyse_contect` |
| `customer_visit` | 客户拜访 | 当前不要求必填字段 |

字段名 `contect`、`analyse_contect` 是当前后端契约中的既有拼写，前端必须照写，不能改成 `content`。

### 8.2 创建草稿

```http
POST {{baseUrl}}/api/v1/drafts
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "thread_id": "{{threadId}}",
  "draft": {
    "draft_type": "demand",
    "title": "智慧环卫改造需求",
    "fields": {
      "title": "智慧环卫改造需求",
      "category": "智慧城市",
      "company": "示例公司",
      "contect": "需要建设环卫车辆与作业监管平台",
      "analyse_contect": "现有系统分散，需要统一调度",
      "link_name": "张三",
      "link_tel": "13800000000",
      "budget_range": "50-80 万",
      "demand_cycle": "6 个月"
    },
    "attachments": []
  }
}
```

成功 `201` 返回创建后的完整草稿，保存其中 `draft_id`。

### 8.3 查询单条草稿

```http
GET {{baseUrl}}/api/v1/drafts/{{draftId}}
Authorization: Bearer <access_token>
```

### 8.4 修改草稿

```http
PATCH {{baseUrl}}/api/v1/drafts/{{draftId}}
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "draft": {
    "draft_type": "demand",
    "title": "修改后的需求标题",
    "fields": {
      "title": "修改后的需求标题",
      "category": "智慧城市",
      "company": "示例公司",
      "contect": "补充后的需求描述",
      "analyse_contect": "补充后的需求分析",
      "link_name": "张三",
      "link_tel": "13800000000"
    }
  },
  "status": "PENDING_REVIEW"
}
```

草稿保存使用当前详情返回的 `version` 做乐观并发校验（请求头 `If-Match: "<version>"`）。已发布草稿编辑并保存后会回到待发布状态，保存成功不代表公开索引已更新；用户必须再次发布。版本冲突返回 `409`，应重新读取并由用户核对合并，不能自动重发旧请求体。

### 8.5 草稿相似度检查

```http
POST {{baseUrl}}/api/v1/drafts/similarity-check
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "draft": {
    "draft_type": "demand",
    "title": "智慧环卫改造需求",
    "fields": { "contect": "需要环卫车辆调度系统" }
  },
  "threshold": 0.72
}
```

成功：

```json
{ "matches": [], "decision_required": false }
```

### 8.6 状态流转

```http
POST {{baseUrl}}/api/v1/drafts/{{draftId}}/transition
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "target_status": "PENDING_REVIEW",
  "note": "用户确认草稿内容"
}
```

允许状态值：`PENDING_REVIEW`、`PUBLISHED`、`WITHDRAWN`。非法状态或不允许的状态跳转返回 `409`。

### 8.7 发布草稿

```http
POST {{baseUrl}}/api/v1/drafts/{{draftId}}/publish
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "scope_id": "public",
  "replace_existing": true
}
```

成功响应示例：

```json
{
  "draft": { "draft_id": "draft-001", "status": "PUBLISHED" },
  "index_result": {
    "status": "SUCCESS",
    "document_id": "draft-001",
    "indexed_count": 3
  }
}
```

发布会写入业务数据并建立检索索引，前端应显示处理中/成功/失败状态。
只有发布链路完成预检、业务记录写入/更新及索引校验后才显示成功；仅 HTTP 200 或任务受理成功不代表发布完成。重试沿用已有业务身份，不得在前端另建重复记录。

### 8.8 下架草稿

```http
POST {{baseUrl}}/api/v1/me/drafts/{{draftId}}/unpublish
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "reason_code": "NEEDS_REVISION",
  "reason_text": ""
}
```

`reason_code` 可选：`NEEDS_REVISION`、`OUTDATED`、`DUPLICATE`、`PROJECT_ENDED`、`NO_LONGER_PUBLIC`、`UPLOAD_ERROR`、`OTHER`。当为 `OTHER` 时 `reason_text` 必填。

成功后草稿状态为 `WITHDRAWN`，下架原因留在草稿状态/审计记录供管理端追查。若附件版本仍被其他已发布草稿引用，附件原文和版本索引保持可用。

### 8.9 重新上架与删除草稿

重新上架：

```http
POST {{baseUrl}}/api/v1/me/drafts/{{draftId}}/republish
Authorization: Bearer <access_token>
Content-Type: application/json
```

请求体：`{}`。未编辑的下架稿恢复草稿索引及有效附件版本；下架后编辑过的草稿重新走发布写入与索引流程。仅当成功响应确认状态后更新界面。

用户删除草稿：

```http
DELETE {{baseUrl}}/api/v1/me/drafts/{{draftId}}
Authorization: Bearer <access_token>
```

这是逻辑删除，返回 `{draft, status:"DELETED"}`；个人中心应立即隐藏草稿及附件入口，原文和附件不会在该请求中立即物理删除。已发布草稿须先下架。管理员控制台的彻底删除是单独的管理操作。

当前后端在服务运行时按北京时间每天 02:00 检查清理：逻辑删除超过 30 天的草稿及关联资源可进入清理；`CONFIRMED/PENDING_REVIEW` 草稿超过 30 天未更新时，其附件可进入清理，草稿字段保留。清理会检查共享引用和已发布版本保护；自2026-11-05起，未保存临时上传附件有效期为7天，以服务端返回的`expires_at`为准。实际结果取决于服务运行、引用状态和清理执行情况。客户端遇到附件 `retention_status=EXPIRED` 或空 `storage_key` 时应隐藏下载入口。

---

## 9. 用户文档上下架

重新上架：

```http
POST {{baseUrl}}/api/v1/me/documents/{{documentId}}/publish
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{}
```

下架：

```http
POST {{baseUrl}}/api/v1/me/documents/{{documentId}}/unpublish
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "reason_code": "OUTDATED",
  "reason_text": ""
}
```

两者成功响应包含业务结果和可选 `task_id`；`409` 表示当前版本不允许该操作。

---

## 10. 人工联系接口

```http
POST {{baseUrl}}/api/v1/me/contact
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "query": "我想了解这家服务商的落地案例",
  "document_ids": ["doc-001"]
}
```

成功响应示例：

```json
{
  "status": "SUBMITTED",
  "user_id": "demo-user",
  "query": "我想了解这家服务商的落地案例",
  "document_ids": ["doc-001"]
}
```

当前是平台测试承接，不代表已接入真实企业微信；未就绪时返回 `503`。

---

## 11. 可选知识库检索接口

普通用户通常通过问答使用检索。若页面需要独立“知识库搜索”，使用：

```http
POST {{baseUrl}}/api/v1/knowledge/retrieve
Content-Type: application/json
```

```json
{
  "query": "工业视觉检测方案",
  "scope_id": "public",
  "knowledge_type": "solution",
  "top_k": 10,
  "use_rerank": true,
  "expand_query": false,
  "identity": {
    "user_id": "{{userId}}",
    "user_role": "external"
  }
}
```

成功响应核心结构：

```json
{
  "agent": "knowledge_retrieval",
  "status": "SUCCESS",
  "query": "工业视觉检测方案",
  "evidences": [],
  "records": [],
  "images": [],
  "documents": [],
  "trace": {},
  "error": null
}
```

用户侧展示 `records`、`evidences`、`images`、`documents`；不要把 `trace` 渲染给用户。

---

## 12. 小程序页面落地建议

### 12.1 智能问答页

页面至少包含：

- 会话标题和历史入口；
- 文本输入、语音输入、附件上传；
- 用户消息和助手消息气泡；
- 任务处理中进度；
- 意图确认卡片；
- 草稿补充/编辑卡片；
- 推荐结果、证据来源、图片、文档下载；
- 保存草稿、发布、供需匹配、人工联系按钮；
- 无结果、失败、重试状态。

消息状态不要只用 `answer` 猜测，按 `ui_blocks` 和 `actions` 渲染。

### 12.2 个人中心页

建议分为五个入口：

1. 我的资料：调用 `/me/profile`。
2. 我的会话：调用 `/me/conversations`，点击后调用消息接口。
3. 我的草稿：调用 `/me/drafts`，进入详情后可修改、提交审核、发布、下架。
4. 我的文档：调用 `/me/documents`，展示状态和版本；PDF 可预览，原文件按权限下载。
5. 我的记录：分别调用 `/me/visit-records` 和 `/me/match-results`。

### 12.3 前端状态机

问答任务状态：

```text
QUEUED → RUNNING → SUCCEEDED
                 ↘ FAILED
```

草稿状态：

```text
草稿编辑 → PENDING_REVIEW → PUBLISHED → WITHDRAWN
```

---

## 13. Postman 推荐测试顺序

1. `POST /auth/login`，确认 `authenticated=true`。
2. `GET /me/profile`，确认用户资料可读。
3. `POST /chat/stream`，确认收到 `start`、`progress`、`token`、`done` 事件。
4. 从 `done.result` 保存 `thread_id`，再提交一轮 `/chat/stream` 验证上下文连续。
5. `GET /me/conversations` 和 `GET /chat/sessions/{thread_id}/messages`。
7. `POST /drafts` 创建草稿，保存 `draft_id`。
8. `PATCH /drafts/{draft_id}` 修改草稿。
9. `POST /drafts/{draft_id}/transition` 提交审核。
10. `POST /drafts/{draft_id}/publish` 发布并检查索引结果。
11. `GET /me/drafts`、`GET /me/documents` 验证个人中心数据。
12. 用 `POST /me/drafts/{draft_id}/unpublish` 验证下架。

### 13.1 Postman Tests 示例

在问答提交接口的 Tests 中保存任务 ID：

```javascript
const body = pm.response.json();
pm.environment.set("taskId", body.task_id);
pm.environment.set("threadId", body.thread_id);
pm.test("任务已进入队列", () => pm.expect(pm.response.code).to.eql(202));
pm.test("返回 task_id", () => pm.expect(body.task_id).to.be.a("string"));
```

在登录接口的 Tests 中保存用户 ID：

```javascript
const body = pm.response.json();
pm.environment.set("userId", body.user.user_id);
pm.test("登录成功", () => pm.expect(body.authenticated).to.eql(true));
```

---

## 14. 当前接口和上线验收边界

- 本文的业务 API 与前端契约以当前 `/api/v1/*` 路由为准。问答使用 `POST /api/v1/chat/stream`；材料解析使用 `/api/v1/chat/materials/parse/stream`。旧的 `POST /api/v1/chat/async` 和 task polling 问答流程已停用。
- 除登录外，前端通过 Bearer Token 传递身份；问答、会话、文档和草稿接口不应要求前端在请求体或查询参数中传 `user_id` / `user_role`。
- DeerFlow 若启用，前端仍只调用科创 Backend。Backend 内部调用 Gateway 的 thread/run HTTP/SSE API；MCP 是 Agent 工具通道，不是前端问答 API。
- 当前是本机测试身份和本地服务配置，不等于已具备生产级用户登录、正式授权、服务器网络与数据服务部署。上线前须针对目标环境验证认证、数据库/对象存储、模型和 Agent 工具调用。
- API 文档中的 endpoint 示例若与当前代码/OpenAPI 不一致，以代码路由和生成的 OpenAPI 为准；修订公开字段或页面交互时同步更新本文与问答界面契约。

---

## 15. 管理端接口范围（小程序不要调用）

以下接口供 `/admin` 管理端使用，不交付给普通小程序：

- `/api/v1/admin/overview`
- `/api/v1/admin/documents*`
- `/api/v1/admin/tasks*`
- `/api/v1/admin/files/upload`
- `/api/v1/admin/knowledge/*`
- `/api/v1/admin/audit-events`
- `/api/v1/admin/operations*`
- `/api/v1/admin/field-policies`
- `/api/v1/admin/metrics`

`POST /api/v1/admin/tasks/{task_id}/confirm` 确认草稿并写入 MySQL 时，如果 MySQL 返回字段长度超限（错误码 1406），接口返回 HTTP 422，`detail` 为“MySQL 字段长度超限”；不向前端暴露具体字段名或数据库异常原文。其他异常按原错误语义处理。

`GET /api/v1/admin/metrics` 的治理台“平均文件解析耗时”读取持久化指标 `perception.parse`：计时包围 Perception Agent 的文件解析执行阶段，不包含文件上传、MinIO 下载及解析结果落库；均值和 P95 按现有指标时间窗口聚合。尚无样本时显示“—”，不以 0 代替缺失数据。

治理台“平均检索耗时”读取 `retrieval.total`：统计 REST 检索或问答 MCP 调用检索 Agent 的完整执行时间，不包括检索完成后的 LLM 回答生成。检索子阶段指标分别展示，不累加替代总耗时。

治理台阶段指标卡片仅展示平均检索耗时、平均模型调用耗时和平均文件解析耗时。接口不再采集或汇总 Token；请求数量与错误率仍在后端指标中用于运行告警和失败明细，不作为页面汇总卡片展示。指标表继续保留可空的 `input_tokens`、`output_tokens` 列，以兼容既有数据并保持新建环境 schema 一致；运行代码不再写入或读取这两列，也不展示 Token 汇总。

管理端当前同样是测试身份边界，`/admin` URL 本身不是正式权限校验。当前草稿管理员下架路由也没有独立管理员身份依赖；不要将管理端接口暴露为生产级权限边界，正式上线前必须补齐服务端授权。

### 15.1 文档目录分页与按需详情

- `GET /api/v1/admin/documents?page=1&page_size=20&knowledge_type=demand`：数据库分页，默认每页 20 条、最大 100 条。响应包含 `documents`、`total`、`page`、`page_size`、`total_pages` 和 `source`；列表只含文档摘要及当前版本摘要，不返回版本历史、关联草稿或任务记录。
- 文档目录可选 `search` 参数按文件名或文档 ID 做不区分大小写的子串模糊搜索；`published_or_admin_only=true` 时仅返回管理员上传的全部文档及用户已发布文档。筛选在服务端分页前执行；省略或为 `false` 时保持完整目录范围。
- `GET /api/v1/admin/documents/{document_id}`：用户打开单条文档详情时调用，返回该文档的 `versions`、`associated_drafts` 和 `task_records`。
- 管理页面仅展示活动版本一条操作记录。详情与已保存解析原文独立读取，原文可读不以任务是否成功为前提；版本查询失败须显示错误，不能沿用旧版本继续操作。
- 替换上传返回新的递增版本；前端同步更新 `version_id`、`active_version_id` 和对象键。解析、提取、发布只使用文档ID、活动版本、对象键均匹配的文件处理任务；返回 `STALE_SOURCE` 时刷新详情，不重试旧任务。没有有效任务时只读展示原文，提示通过“更新”重新上传处理。
- 文件处理任务以 `operation=file_ingestion` 判别；`task_type` 存在时须为 `document_ingestion`，历史任务缺少该冗余字段不等于任务无效。解析结果缺少 `version_id` 时仍须比较 `storage_key`，避免新版本未解析时显示旧正文。
- 新解析保留旧发布原文的快照及图片映射，不删除已发布封面所引用的旧图片。封面和正文统一使用 `/api/v1/perception/images/{document_id}/{image_id}`；图片ID由服务端返回，前端不得自行推算或替换。
- 上下架只适用于有效且非空的已发布索引；上架或回收站恢复还会检查原件存在。未发布版本的恢复只回到待处理状态，不会变为公开可检索。非活动版本及 `PURGED` 版本不允许操作。
- `DELETE /api/v1/admin/documents/{document_id}` 通过统一生命周期清理关联草稿、业务记录、向量和附件，保留其他文档的共享引用；失败进入补偿状态，不能只删除附件后报告成功。前端以成功响应为准更新列表，迟到响应不得覆盖后来选中的文档。
- `GET /api/v1/admin/tasks?page=1&page_size=100&document_id={document_id}&operation={operation}`：独立分页查询管理任务；`document_id` 和 `operation` 均为可选筛选条件，默认每页 100 条、最大 500 条。
- `POST /api/v1/admin/tasks/{task_id}/parse`：上传后或解析完成待确认时启动/重新启动解析；任务运行中重复请求返回当前状态。重新解析会清空该任务的旧解析预览和草稿关联，不删除已保存的草稿记录。
- `POST /api/v1/admin/tasks/{task_id}/extract-fields?knowledge_type=solution`：对已解析任务按指定知识类型提取草稿；允许 `enterprise`、`demand`、`achievement`、`expert`、`solution`。省略时使用任务已有类型；任务仍无类型时返回 `draft_status=EMPTY`，不调用 LLM、不生成草稿且不报错。`draft_status=RUNNING` 时重复请求返回当前状态，已完成后再次请求会重新生成，并清空任务当前草稿关联，不删除旧草稿记录。

目录列表按当前页的不同 `user_id` 查询用户资料，每个用户最多查询一次。前端翻页只请求对应文档页；任务状态在进入管理文档页或手动刷新时通过任务接口单独加载。

### 15.2 管理台记录列表分页

- 所有会增长的管理台记录列表按需从服务端读取当前页，不允许先加载全量记录再在浏览器切片。通用分页参数为 `page`（从 1 开始）和 `page_size`（默认 20，最大 100）；响应为 `items`、`total`、`page`、`page_size`、`total_pages`。任务列表是例外，使用 15.1 节的默认 100、最大 500。切换知识类型或筛选条件时回到第 1 页。
- `GET /api/v1/admin/knowledge-updates?page=1&page_size=20&published_only=false&knowledge_type=demand`：知识更新/草稿目录按数据库分页；`published_only` 和 `knowledge_type` 在服务端过滤后再分页。管理总览同样使用此接口读取当前页的最新更新。
- `GET /api/v1/admin/conversations?page=1&page_size=20`：会话目录按数据库分页，仅返回会话摘要、消息/草稿/匹配数量；具体内容仍在打开单条会话详情时按需读取。
- `GET /api/v1/admin/audit-events?page=1&page_size=20`：审计事件保持服务端分页，默认每页 20 条。
- 文档目录继续使用 15.1 中的 `documents` 响应字段；分页控件显示总条数和当前页，切页只请求对应数据页。总览、草稿目录、会话目录和审计列表均使用相同的每页 20 条策略。

---

### 15.3 Milvus 索引维护（2026-10-16 更新）

- `POST /api/v1/admin/knowledge/collections/{knowledge_type}/rebuild?confirm=true`：只读预检已发布草稿/文档、附件及 MySQL 来源，不清空或创建 Collection，不重置同步断点。返回 `status=PREFLIGHT_COMPLETE`、`preflight_id`、`snapshot_fingerprint`、`missing_fingerprint`、来源清单和 `status_url`。
- `POST /api/v1/admin/knowledge/collections/{knowledge_type}/restore`：JSON 请求体必填 `{"confirm": true, "preflight_id": "<预检任务ID>", "accepted_missing_fingerprint": "<预检返回的SHA-256>"}`。只允许提交对应知识库、仍有效且快照未变化的预检；缺失来源必须精确确认。返回 `status=QUEUED`、`task_id`、`status_url`。后台只恢复并验证影子 Collection；验证通过后任务进入 `READY_TO_SWITCH`，活动 Alias 仍不变。
- `POST /api/v1/admin/knowledge/collections/{knowledge_type}/activate`：JSON 请求体 `{"confirm": true, "task_id": "<READY_TO_SWITCH任务ID>"}`。管理员明确确认后，后台重新取得排他维护锁，核对来源快照、Alias 仍指向旧 generation、影子库已加载且行数未变化，再切换 Alias。来源或 Alias 已变化时拒绝切换，旧 Alias 保持活动，需重新预检和恢复。
- 恢复优先使用已发布草稿的 PostgreSQL 内容及关联附件原文/已确认索引请求，并沿用现有分块逻辑；MySQL 只补充未被成功恢复草稿覆盖的业务主键。恢复不更新草稿或文档业务数据，也不更改 `source_id`。恢复缺失来源只能在管理员接受对应预检指纹后跳过。
- `GET /api/v1/admin/tasks/{task_id}`：查询 `status`、`stage`、`processed`、`total`、`skipped_count`、`recovery_result`、`active_generation`、`shadow_generation` 和错误。`READY_TO_SWITCH` 表示影子库验证通过、等待管理员确认；`SUCCESS` 才表示 Alias 已切换；`FAILED`/`INTERRUPTED` 不表示恢复完成。服务重启时会对账处于 `SWITCHING` 的任务与当前 Alias 目标。
- 影子库恢复/验证期间公开库写请求通过 PostgreSQL advisory lock 与恢复互斥；等待管理员确认时锁已释放，业务可继续写入。点击切换时再加锁并重验来源快照；等待期间来源变化会拒绝切换。查询始终继续使用当前 Alias。该实现尚未在真实多进程 PostgreSQL/Milvus 环境验证，不代表生产已部署。
- `rebuild-mysql` 仍是独立的 MySQL 角色向量维护入口，不等价于完整 Collection 恢复；完整恢复未完成时禁止执行。

知识类型限 `enterprise`、`demand`、`achievement`、`expert`、`solution`。校验错误为 422，维护依赖/竞争错误为 503；管理端授权限制仍以本节前文为准。首次 Alias 接入需将所有后端进程统一升级，不得混跑仍直连物理 Collection 的旧版本。真实部署前须评估额外磁盘/加载内存并在维护窗口操作；不得仅凭本地测试宣称生产已验证。

完整步骤见[Milvus 加载修复与仅索引重建](方案/Milvus加载修复与仅索引重建.md)。

## 16. 代码依据

- DeerFlow 配置：`OPIEos/config.yaml`
- DeerFlow 科创 Agent 常驻指令：`OPIEos/agents/kechuang/SOUL.md`
- DeerFlow Gateway：`OPIEos/backend/app/gateway`
- Skill 运行时：`OPIEos/backend/packages/harness/deerflow`
- 业务 API 和 MCP 适配层：由当前业务后端按部署环境提供
- 前端调用封装：由业务前端按本文件的网关契约实现
