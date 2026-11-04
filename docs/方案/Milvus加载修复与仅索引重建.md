# Milvus 加载修复与仅索引重建

日期：2026-10-16 更新。当前重建流程以本文件“2026-10-16 影子 Collection 恢复操作”为准；本地实现尚未在真实 PostgreSQL/MinIO/Milvus 或服务器验证，不可将旧版破坏性流程直接用于生产。

## 目标与边界

Milvus 是可再生检索索引，不是业务正文的唯一存储。用户要求重建仅影响指定知识类型的 Collection，保留 PostgreSQL 业务记录、文档版本、草稿、会话和 MinIO 原始文件及附件；MySQL 业务表与其他 Collection 同样保留。不改 OPIEos、测试前端，不执行线上操作。

此边界替代《管理端跨存储清理与恢复设计》中第 2 节的连带清理要求；该文档其他删除/purge 设计不在本次范围。

## 内容保存原则

- MySQL 业务表保存业务记录，PostgreSQL 保存文档/草稿状态、关联、用户确认的最终正文或分块及生成参数，MinIO 保存文件和附件。
- 向量是派生数据，不需要再复制一份向量到 PostgreSQL。
- 恢复必须依据当前有效发布版本、知识类型、范围、用户归属和状态生成索引，不能把下架/删除内容重新公开。
- 实施恢复入口前，逐项核实最终正文/分块的持久化位置；不能凭原文件、目录记录或进程内缓存承诺完全恢复。如果最终内容只在 Milvus，先补齐持久化，不能直接删库。
- 若需保存较大的解析产物，可沿用 MinIO 对象存储并由 PostgreSQL 记录对象引用；不新增独立备份系统。

## 修复顺序

1. 修复 Collection 缓存：每次访问验证连接代次；只读检查不缓存成已加载对象；需要查询时显式保证加载，统一有限等待。
2. 修复就绪检查：区分缺库、未加载、加载中与已加载。不把未加载状态当作连接故障，不自动创建、重建或 release/load。
3. 同步预检失败直接拒绝启动；恢复旧 Milvus 后不能静默沿用较新断点。先核对数据和断点，再执行明确的恢复操作。
4. 修复全量同步首批删除：按记录范围更新已处理的业务记录，不在第一批清理全部旧 MySQL 向量；如需删除已不存在的源记录，在完整同步成功后单独完成核对清理。
5. 收紧重建边界：预检保持只读；恢复写入独立影子 Collection，验证完成后才切换稳定 Alias。失败不得改变活动 Alias。
6. 补齐完整回灌：复用现有文档/草稿索引构建链路，从权威持久化来源恢复已发布内容，再补充未被草稿覆盖的 MySQL 记录。

## 2026-10-07 运行期加载状态恢复

进程缓存的“已加载”标志可能因 Milvus 服务端/运维侧释放 Collection 而过期。检索或其他集合操作遇到明确的 `collection not loaded` 时，当前请求清除该标志，有限等待重新加载同一 Collection，并最多重放该操作一次；加载失败直接返回真实错误，不吞异常、不自动 `release/load` 循环，也不重建 Collection。此改动只覆盖运行期状态漂移，不修复服务端 segment、主键列或元数据损坏；`pk_column` 等加载错误仍需按服务端日志诊断。

本地验证：Milvus 生命周期、MySQL 向量替换、检索路由定向测试共 67 项通过。使用测试替身，没有连接真实 Milvus，也未执行服务器重建或数据操作。

## 2026-10-08 加载失败句柄清理

统一加载入口在 `load()` 失败时清除当前句柄及已加载标志，保留共享连接和原始异常；后续请求重新获取句柄并重试，避免持续复用加载失败的对象。该处理不自动重建 Collection，也不代表服务端 Segment 错误已修复。

## MySQL 同步与在线检索优先级

MySQL→Milvus 同步不暂停整批数据处理，也不取消或回滚已提交的写入。每次 MySQL upsert RPC 和同步结束时的 flush 使用低优先级写门闩；同一 Backend worker 的 dense、hybrid、文本前缀检索作为高优先级读操作。已有读请求完成后，排队检索先于下一次同步写 RPC 执行；检索队列清空时，同步自动继续。该控制只协调当前 worker 通过 `MilvusService` 执行的检索与 MySQL 同步，不覆盖其他进程或绕过该服务直接访问 Milvus 的客户端；多 worker 部署仍需跨进程协调才能提供全局优先级保证。

本地验证：Milvus 优先级、MySQL 同步安全、同步预检、向量替换、连接生命周期、集合恢复、检索详情/默认值/路由定向测试共 123 项通过；Ruff 改动文件检查通过（忽略该文件集已有的 PYI046、ASYNC251 告警）。使用并发测试替身，没有连接真实 Milvus，也未部署或重启服务。

## 断点处理

保留 PostgreSQL 业务数据不等于保留旧索引断点。重建目标 Collection 时，仅更新该知识类型的同步/恢复控制状态，使回灌从起点执行，保留历史任务供审计；不删除业务表。控制状态写入失败时不允许继续删库；重建失败时明确提示索引尚未恢复。

## 操作限制

- 不将重建作为加载错误的自动兜底。先诊断连接、资源、加载进度与服务端错误，只有明确确认才重建。
- Collection 重建可能造成该库检索暂时不可用；维护期间由管理员暂停该类型发布、下架、文件清理和检索，恢复任务成功后再开放。不承诺跨 PostgreSQL/Milvus 的原子事务，进程内锁不保护其他后端进程或直连数据库操作。
- 科创 MinIO 原文件与 Milvus 内部使用的对象存储不是同一业务职责。通过指定 Collection 的 API 重建，不删除任何 Docker 数据卷，也不清理 MinIO bucket。
- 重建前如需应急回滚，沿用已验证的整实例一致性备份；不能假定加载失败的 Collection 还能导出向量。

## 验证

每步先添加能复现缺陷的定向测试，修复后运行相关回归。重建测试必须证明不调用业务删除、文档 purge、MinIO 删除或其他 Collection 操作；恢复测试覆盖中途失败、断点、发布状态及重复执行。当前不连接或修改服务器数据库。

## 实施约定（截至2026-10-08；历史流程，当前操作见2026-10-16章节）

- 复用 MinIO 已有 `index-request.json` 保存文档的人工确认请求，复用 PostgreSQL 已发布草稿的字段和 `source_blocks`；不把正文写入任务状态，也不另存向量备份。
- 重建接口保持显式确认，只执行只读预检并持久化来源快照/缺失指纹；不清空 Collection、不重置该类型索引断点。
- 完整恢复使用独立显式入口：在影子 Collection 逐条校验并恢复 PostgreSQL 草稿/发布文档、MinIO 确认文件，再补充未被成功恢复草稿覆盖的 MySQL 记录。存储服务等基础设施故障停止任务，不作为单条来源跳过。恢复失败不 drop 活动库；只有影子库完成验证后才通过 Alias 切换。
- 同步预检查询失败直接拒绝；数量少于持久化断点记录的已索引数量，或有旧断点但索引为空时，拒绝续传并要求显式恢复。数量检查不能证明内容完全一致，不据此宣称已做逐条一致性校验。
- 普通同步每批仅替换本批 MySQL 记录的旧分块，不提前全库删除，不自动清理源表已删除记录。
- restore 必须使用对应预检 ID 和管理员接受的精确缺失指纹；来源变化、新缺失或写入/校验错误均拒绝 Alias 切换。
- 恢复复用当前分块/字段/embedding 逻辑，优先 PostgreSQL 已发布草稿管理员修改内容，其次已确认的原文/索引请求；不改业务草稿、文档或 `source_id`。
- 恢复持 PostgreSQL 排他 advisory lock，公开写操作使用同键共享锁；查询继续经旧 Alias。未在真实多进程 PostgreSQL 验证，部署前必须统一升级所有后端进程，避免旧版本绕过 Alias。
- 影子库验证 flush/load、schema、Loaded 状态、实际行数及预期主键集合后再切换；当前不做语义查询抽样。
- 回灌后，PostgreSQL 中未发布/下架草稿关联的 MySQL 记录向量置为 `UNPUBLISHED`，不重新公开；仍有已发布草稿共用同 ID 时保留。已发布草稿的原文若缺少有效公开文档记录或版本不符，则预检拒绝，不采用旧版本替代。

## 保存位置与恢复入口

| 内容 | 现有持久化来源 | 本次处理 |
| --- | --- | --- |
| 业务表记录 | MySQL | 只读并重新生成向量，不 save/update/delete |
| 已发布草稿字段、原文块、附件关联 | PostgreSQL `platform_drafts` 的 `draft` | 复用正常发布的纯内容构建逻辑，只调用索引，不发布业务记录 |
| 文档版本及发布范围 | PostgreSQL `platform_documents` | 读取当前有效公开版本，不 purge、改状态或删除 |
| 文档人工确认分块 | MinIO `derived/{document_id}/tasks/{task_id}/index-request.json`，PG 版本 `artifact_refs.confirmed_index_request` 保存文档/版本/任务/身份引用 | 只读核验后的权威版本来源；任务过期不影响恢复，不运行时回退旧任务或让 LLM补写 |
| 原文件、解析图片和附件 | 现有科创 MinIO | 不删除、不覆盖 |
| 恢复进度、来源引用、错误 | 现有 PG `platform_tasks`，`operation=milvus_recovery` | 新增维护任务；正文不放任务状态，历史任务保留 |
| MySQL 索引断点 | PG 同步控制表 | 预检不修改断点；恢复将当前 MySQL 快照写入影子 Collection，不清空活动集合 |

这里不是整实例备份工具，也不恢复所有私有库。若历史内容仅存在于 Milvus、没有 PG/MinIO 来源，或来源记录不一致，不能承诺无损恢复；预检会列出缺失来源，管理员未接受其精确指纹时活动 Alias 不得切换。

## 2026-10-16 影子 Collection 恢复操作

以下流程仅适用于已统一部署稳定 Alias 与影子恢复实现的所有后端进程。旧版接口行为不同，未完成部署前不要按此流程操作。先确认 MinIO、PostgreSQL、Milvus 健康并评估额外磁盘/加载内存。影子库恢复/验证期间该知识库查询继续走旧 Alias，公开写请求会被 advisory lock 阻止或等待；恢复验证后等待管理员确认切换时，锁会释放，业务可以继续写入。

1. 调用 `POST /api/v1/admin/knowledge/collections/{knowledge_type}/rebuild?confirm=true` 做只读预检。确认返回 `PREFLIGHT_COMPLETE`，保存 `preflight_id`、`snapshot_fingerprint`、`missing_fingerprint` 和 `recovery_result.skipped_sources`；预检不清空或创建 Collection。
2. 管理员逐项审核无法恢复来源。无缺失时仍需提交预检返回的空缺失指纹；有缺失时，只有明确接受整份清单后才能继续。来源有变化则重新预检，不能复用旧指纹。
3. 调用 `POST /api/v1/admin/knowledge/collections/{knowledge_type}/restore`，JSON 请求体为 `{"confirm":true,"preflight_id":"<预检任务ID>","accepted_missing_fingerprint":"<预检返回的SHA-256>"}`。返回 `QUEUED` 和新 `task_id`。不要只发 `?confirm=true`：本接口需要 JSON 请求体中的确认、预检 ID 与接受指纹。
4. 轮询 `GET /api/v1/admin/tasks/{task_id}`。状态到 `READY_TO_SWITCH` 表示影子库已经验证，但活动 Alias 尚未改变。管理员检查恢复报告后，调用 `POST /api/v1/admin/knowledge/collections/{knowledge_type}/activate`，JSON 请求体为 `{"confirm":true,"task_id":"<恢复任务ID>"}`。
5. 继续轮询任务至 `SUCCESS` 才表示新 Alias 已切换。切换时会重验来源快照、旧 Alias 目标和影子库；等待期间任何来源变化都会拒绝切换，旧 Alias 保持活动，需要重新预检并恢复。`FAILED` 时旧 Alias 保持不变；`INTERRUPTED`/`SWITCHING` 需先重新读取任务，服务启动会按实际 Alias 目标对账。失败影子 Collection 不会自动删除。
6. 切换后抽样核对数量、主键、来源关联和检索结果。旧物理 Collection 暂时保留用于人工检查/回退评估；新集合已接受写入后，不可直接切回旧集合。

知识类型限 `enterprise`、`demand`、`achievement`、`expert`、`solution`。恢复不删除 Docker 卷、PostgreSQL 业务数据或 MinIO 对象。授权与访问限制沿用现有管理接口；`/admin` 页面不是生产授权，勿把维护接口未经访问控制开放到公网。

## 本地验证与线上边界

### 单库检索失败隔离

检索并发调用各 Milvus Collection 时，单库异常会记录 `milvus_knowledge_unavailable` 结构化事件（collection、操作阶段、异常类型/消息、trace 和异常堆栈），并写入检索服务滚动文件日志 `logs/app.log`；仅本次检索跳过该库，其他库继续召回。部分失败时返回 `PARTIAL_SUCCESS`（有证据）或 `DEGRADED`（无证据），`trace.unavailable_collections` 列出不可用 Collection；所有目标均失败时返回降级错误，不伪装为无匹配。日志文件默认保留 15 天，可由 `AUDIT_LOG_RETENTION_DAYS` 调整。该隔离不跨请求熔断，后续请求会重新尝试；它不修复 Milvus segment，持续故障会持续降级，需据日志定位并另行处置。

### 2026-10-08 重建/恢复提交失败日志

重建或索引恢复在创建维护任务前因基础依赖失败时，API 仍返回通用 `503 DEPENDENCY_UNAVAILABLE`，不向客户端暴露内部异常；后端会记录知识库类型与完整异常堆栈，日志格式包含当前请求的 trace_id。若响应失败且没有对应 `milvus_recovery` 任务，先用响应头 `X-Trace-ID` 在科创后端日志中定位异常。单条来源校验失败属于恢复跳过项，不再阻止空 Collection 重建或其他有效来源恢复。本地日志回归不代表线上代码已部署，也不代表已恢复 Collection。

### 2026-10-08 重建与恢复职责分离（历史流程，已由 2026-10-16 影子恢复流程取代）

以下为旧版空 Collection 重建流程记录，不是当前操作说明；当前 `rebuild` 只做只读预检，禁止按本节操作清空活动 Collection。

恢复预检按来源隔离校验失败：无效/缺失的确认文件不生成 `IndexRequest`；发布草稿引用无效文档时跳过该草稿及其原文，不写入 Milvus。恢复任务继续处理其余通过校验的来源并从 MySQL 起点回灌，`skipped_count` 和日志用于识别未恢复内容。MinIO/PostgreSQL 等基础服务访问故障不属于单条来源校验失败，继续按任务失败处理，禁止将依赖故障伪装成跳过。

恢复执行阶段仍在写入前后核验来源。若来源在预检后发生变化，当前恢复任务失败，不写入该条请求；维护期间仍应暂停并发发布、下架和清理。

### 2026-10-07 控制台索引恢复交互（旧版 UI 流程）

以下为旧版 UI 交互记录；当前操作顺序及请求体以“2026-10-16 影子 Collection 恢复操作”和 API 文档为准。

- 单一知识类型下，两步分别确认：先“重建 Collection”清空该库向量，保留 PostgreSQL、MySQL 和 MinIO；状态变为“等待恢复索引”。再点击“恢复索引”启动回灌；恢复失败可重试，不需要再次重建。
- 恢复时展示有效发布草稿/文档、MySQL 回灌、完成等阶段；后端 SUCCESS 才表示恢复完成。MySQL 阶段读取子任务进度，不把正文数量当成总恢复百分比。
- 恢复失败或中断后提供“重试恢复”，只调用 restore，不再调用 rebuild。重建阶段失败单独显示错误，不伪装为待恢复。
- 进入所选类型或刷新页面时分页查询后台 milvus_recovery 任务，以最新任务为准；任务进行中继续轮询。查询失败明确显示错误并提供重新读取，不依据旧状态启动破坏性操作。
- 重建/恢复提交中或恢复未完成时禁止重复重建、普通同步和同步数据清理；“全部”视图仍不显示单表维护按钮。页面禁用不是后端授权或跨进程维护锁，恢复前仍需人工暂停相关发布、下架与清理。

操作：管理控制台 → 数据管理 → 选择需求库/方案库等单一类型 → 重建 Collection → 确认 → 恢复索引 → 再次确认。等待“索引恢复完成”后抽样核对，再开放业务。两步之间该库为空，保持相关写入暂停。

验收：`cd frontend && node --test` 共 17 项通过（本次新增 5 项状态/分页测试）；`npm run build` 通过，仍有既有大于 500 kB 的 bundle 提示。任务目录实际按更新时间排序，因此状态恢复遍历全部分页再比较创建时间，避免旧任务补写状态后遮蔽新任务。完成后刷新数量；轮询错误或提交结果不明确时停止维护操作，先重新读取后台状态。

本次未启动服务、未在本机或服务器执行真正的重建/恢复，未进行浏览器端到端验收。只新增前端恢复 API、局部维护 Hook 与按钮，移除 App 中旧的“重建后只重新同步”处理，保留其他页面既有改动。部署需要更新测试前端并重新构建；后端必须已部署本文的 rebuild/restore 实现。

### 2026-10-15 source_id 与 MySQL 关联恢复

- 新业务来源统一生成为 `source:<哈希>`，不再增加 `mysql:` 前缀；哈希算法不变。已有 `source:mysql:...` 关联保持原值，不在线批量改名或拆开已有原文/草稿关联。

- 已发布草稿按知识类型和唯一 `mysql_id` 关联 MySQL 记录；恢复预检发现多个来源冲突时明确失败，不清空线上集合、不猜测关联。未匹配的纯 MySQL 记录按业务 ID 生成稳定 `source_id`。
- 新生成的 `source_id` 不带 `batch:`；旧 `source:batch:<id>` 在恢复前规范化为 `source:<id>`，并同步更新 PostgreSQL 草稿和 Milvus 写入请求。普通 MySQL 同步优先读取已发布草稿角色的来源，再沿用 MySQL 角色的唯一来源；冲突则停止该批并记录错误。
- 本地定向测试覆盖 source_id 生成、旧 ID 规范化、草稿与 MySQL 关联及普通同步保留。没有执行真实 Milvus 重建、线上写入或浏览器端到端验收。

### 2026-10-06 版本来源迁移前置条件

新代码不再用旧任务列表作为恢复兜底。老版本没有 `artifact_refs.confirmed_index_request` 时，先安排维护窗口，保留成功确认任务和 MinIO 正文，使用与后端相同的已加载环境/解释器运行：

```bash
cd /home/zyx/kechuang-deerflow/backend
python -m scripts.backfill_confirmed_index_sources
```

这条默认 dry-run，只核验实际成功确认任务及正文，报告 `scanned/eligible/updated/rejected`，不启动 Gateway、同步 Worker 或定时清理。它只扫描曾索引/发布且缺少来源的版本，包含有效历史版本；普通未确认上传不当作坏恢复来源。脚本使用已导入进程的环境变量，不自动从 `.env` 启动整个后端。

管理员检查拒绝项并明确批准后，才可在相同维护窗口运行：

```bash
python -m scripts.backfill_confirmed_index_sources --apply
```

apply 仅将核验通过且仍未变化的来源写入对应版本，不改发布状态，不写 MySQL、MinIO 或 Milvus。缺少成功任务、正文、身份或版本不符时拒绝，不能伪造来源。回填不等于完成索引恢复；回填后仍按上方显式维护 API 预检、重建和回灌。本轮两条数据操作均未在本机真实存储或服务器执行。

版本替换现在保留旧版本历史，新确认来源提交后才清理旧版本；引用扫描覆盖当前及有效历史版本。定时清理不再以有限前 N 条列表判断文件是否孤儿，删除前重查失败就停止。仍不承诺跨 PostgreSQL/MinIO/Milvus 原子性，多个进程或其他业务直写时必须暂停相关写入。

定向测试覆盖连接代次、只读健康检查、统一加载超时、首批/续传按记录替换、查询失败预检、旧断点拒绝、控制状态先于删除、来源缺失/版本错误拒绝、恢复失败重试及下架记录隐藏。最终回归使用测试替身和内存/SQLite 仓储；早期运行发现既有发布测试尝试真实 PostgreSQL 审计连接并等待，已停止测试并隔离该依赖。本次未执行服务器重建或数据恢复。

2026-10-06 最终相关回归：151 项通过，1 项既有 Starlette 测试客户端弃用警告。补齐恢复途中来源变化和子任务遗留的复现测试，并隔离既有发布测试的真实 PostgreSQL 审计连接。未执行线上重建或恢复。静态检查保留已有 `SIM102`、测试中的 `ASYNC251` 和检索服务原有 `BLE001` 警告，未进行无关重构。

这些修复避免错误健康状态和同步扩大损失，不代表能够修复 Milvus 服务端损坏的 segment/元数据或保证消除历史 `pk_column` 内部错误。部署后仍需实际服务器加载与检索验收。

### 2026-10-14 单 Collection 加载失败的后台恢复与诊断日志

- 前台现有有限重试仍保留；collection load 失败会记录完整异常堆栈、异常 cause/context 链、traceparent、database、collection、schema 字段、`load_state` 和 query segment 状态信息。诊断快照自身失败也会记录为 `unavailable`，不覆盖原始加载异常。
- 对失败的既有 collection 启动单实例后台恢复：5、15、30、60 秒间隔，最多 4 次；重新确认 collection 存在后再 load，并要求 `load_state=Loaded` 才标记恢复。健康的其他 collection 不受影响；用户当前请求仍按既有降级/失败结果返回，后台恢复成功后后续请求可继续使用。
- 每次后台失败均保存异常堆栈、异常链和状态快照；成功记录 `milvus_collection_load_recovered`，耗尽记录仍可从 `milvus_collection_load_recovery_failed` 检索。日志沿用检索服务滚动文件 `logs/app.log`（默认保留 15 天，可由 `AUDIT_LOG_RETENTION_DAYS` 调整）。
- 自动恢复只重试 `load` 和只读状态查询；不执行 release、compact、删除、重建或数据回灌。进程重启不会延续后台计数，但后续请求失败会重新触发恢复。
- 这些应用日志不能替代 Milvus 容器服务端日志。若再次报错，应同时保存同一时间窗口的 `docker logs --timestamps milvus-standalone`、`milvus-etcd` 与 `milvus-minio` 日志，使用应用日志中的 trace、collection、时间和 segment 状态关联；本次仅完成本地实现和模拟测试，尚未部署/验证生产日志目录与轮转权限。
