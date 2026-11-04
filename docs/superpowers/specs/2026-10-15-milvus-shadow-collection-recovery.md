# Milvus 影子 Collection 恢复规格

日期：2026-10-16。状态：影子 Collection 恢复流程已在本地实现并通过定向测试；未连接真实 Milvus、未部署或操作服务器。

## 本地实现进度（2026-10-16）

- `rebuild` 只读盘点已发布草稿/文档、附件和 MySQL 来源，持久化快照及缺失项指纹；不会清空活动 Collection。
- `restore` 必须提交对应预检 ID 和缺失项指纹；在影子 Collection 按现有草稿/文档索引路径与分块策略回灌，并仅补充未由成功恢复草稿覆盖的 MySQL 记录。
- 影子库完成 flush/load、schema、行数与预期主键校验后进入 `READY_TO_SWITCH`，此时不切换稳定 Alias，并释放恢复写锁；管理员单独确认后重新取得排他锁，重验来源快照、旧 Alias 目标和影子库状态，再切换。失败时旧 Alias 不变，旧物理库保留。
- 本地后端定向回归 161 项、前端测试 7 项和生产构建通过；真实 PostgreSQL/MinIO/Milvus、服务器部署、资源容量和浏览器端到端恢复未验证。

## 目标

重建单个知识库前先做纯只读预检，逐条列出无法恢复的 PUBLISHED 草稿、发布文档和附件来源。管理员只能接受预检报告明确列出的缺失项；未接受、来源变化或出现新的恢复失败时，活动 Alias 不得切换。恢复在影子 Collection 完成、加载并验证后，才原子切换稳定 Alias；旧物理库保留。

## 当前约束

- 查询与日常写入通过 `RetrievalAgent.public_milvus[knowledge_type]` 进入 `MilvusService`，知识类型映射到稳定 Alias；首次接入会把现有物理 Collection 挂到 Alias。部署时需统一切换所有后端进程，避免新旧版本分别按 Alias 和物理名读写。
- 管理端已有分开的 `rebuild`、`restore` API；rebuild 改为只读预检并生成带来源指纹的报告，restore 必须携带该报告 ID 与管理员接受的不可恢复项指纹。恢复成功验证后由独立 `activate` API 执行管理员确认切换。
- 可恢复来源由 PG/MinIO 的已发布文档、确认分块和草稿，以及 MySQL 业务表组成；文档/草稿索引必须沿用现有分块和字段构建逻辑。
- PostgreSQL advisory lock 作为跨进程写栅栏：恢复持有排他锁，普通公开库写入尝试共享锁；被维护窗口挡住时快速失败，查询不加锁。影子写入使用显式绕过该锁的内部目标。
- 同一 Milvus 服务名不能同时作为物理 Collection 和 Alias。Alias 初始化若发现物理库与 Alias 状态冲突、Alias 指向未知对象或目标 schema 不兼容，必须失败关闭，不能猜测或 drop 现有数据。

## 流程与行为

1. 管理员发起预检。全量枚举 PUBLISHED 草稿、发布文档、附件引用及 MySQL 业务记录；验证 PostgreSQL 版本/状态、确认索引请求、MinIO 对象和草稿附件关联。预检不得写业务 PG 数据或 MinIO/Milvus；允许保存维护任务状态与检查报告，不得创建或删除集合。
2. 预检报告列出可恢复来源数量、无法恢复来源及原因，并返回对来源快照和缺失项分别计算的指纹。若没有缺失项，可直接确认该报告；若有缺失项，管理员必须明确接受该精确缺失清单。报告之外出现的任何新缺失不能自动被视为已接受。
3. restore 时重新执行预检；来源指纹或缺失清单发生变化则拒绝继续并要求重新预检。恢复期间取得 PostgreSQL 排他 advisory lock，阻止所有进程对该公开知识库的发布、下架、删除、索引和 MySQL 同步写入；读取仍使用旧 Alias。
4. 在新影子物理 Collection 按当前权威来源恢复：优先使用 PostgreSQL 中管理员确认的草稿字段与 `source_blocks`；缺少管理员修改内容时使用 MinIO 已保存解析原文/确认索引请求；沿用既有文档类型分块策略、字段构造和 embedding。MySQL 只补充未由成功恢复的 PUBLISHED 草稿覆盖的业务主键。重复 PK 按既定业务优先级去重并记入审计。已接受的预检缺失项可按报告跳过；未接受的新缺失、版本变化、索引错误或批次失败都令任务失败且不切换。
5. 完成后 flush 并 load 影子库；验证 schema、Loaded 状态、实际行数和预期主键集合。任何校验不通过保留旧 Alias，任务失败并留下原因。校验通过后记为 `READY_TO_SWITCH`，释放 advisory lock；等待管理员检查时不阻塞业务写入。当前实现未做代表性语义查询抽样。
6. 管理员调用 `POST /api/v1/admin/knowledge/collections/{knowledge_type}/activate` 并确认任务 ID。服务重新取得 PostgreSQL 排他 advisory lock，确认 Alias 仍指向恢复开始时的旧 generation、来源快照未变化且影子库仍已加载/行数正确；变化时拒绝切换，旧 Alias 继续服务，须重新预检并恢复。切换时先持久化备份物理名，再将旧活动库改名保留；若原名被另一旧 Collection 占用，也先改名保留；然后将新影子库改为原 Collection 名，最后让稳定 Alias 指向该原名。任何失败均不删除 Collection，Alias 在最终切换前仍服务旧活动数据。任务记录新旧物理名，完成后释放 advisory lock；若新库已接收写入，不得直接切回陈旧库。
7. 进程中断不得激活半成品；PG advisory lock 在连接关闭时释放。失败影子库保留供诊断，不触碰活动库。

## API / 运维兼容

- 保留当前 `rebuild`、`restore` URL；新增 `POST /api/v1/admin/knowledge/collections/{knowledge_type}/activate`，请求体为 `{"confirm":true,"task_id":"<READY_TO_SWITCH任务ID>"}`。`restore` 仅恢复并验证影子库，返回任务最终停在 `READY_TO_SWITCH`；`activate` 负责复核并执行旧库备份、影子库原名提升和 Alias 切换。任务详情展示来源清单、缺失原因、接受状态、active/shadow/backup generation、阶段和错误。
- 当前启动期的 collection 初始化、健康检查、读写、删除及 MySQL 同步目标全部改为稳定 Alias；部署前仍需核对所有运行进程已升级。
- 不自动保留无限历史库；本次只保留上一个活动物理库，删除旧库需后续显式、单独的管理操作，不纳入自动切换。
- 维护标记需跨进程持久化并由所有写入入口执行；进程内 `_MYSQL_SYNC_GATE` 只用于本进程串行化，不作为全局保证。

## 验收与安全

- 故障注入：预检、来源重验、insert/flush/load/校验/Alias 更新失败、进程中断时，除管理员明确接受的预检缺失项外，任何 PUBLISHED 来源无法恢复都使活动 Alias 保持旧库；等待切换确认期间允许写入，确认时若来源已变化则拒绝切换；查询持续可用，写入栅栏自动释放。
- 成功路径：影子库仅含当前有效来源数据，计数和抽样查询核验后 Alias 指向新库；其它知识库及 PG、MySQL、MinIO 内容不变。
- 并发验证：维护期间所有跨进程写入口均被阻止，读请求持续命中旧库；切换边界无请求读到半成品。
- 预检幂等与只读：不得更新草稿 `source_id` 或其它 PostgreSQL 字段；相同快照产生相同指纹，数据变化后旧接受指纹无效。
- 本地测试须覆盖管理 API/任务状态、Alias 与 generation 持久化、索引目的地、来源去重/重复审计、回退保护和异常清理。不得连接生产库或自动部署。

## 已知风险

- 需要一次性 Alias 接入和应用部署；多个正在运行的后端版本混用物理名与 Alias 会破坏一致性，必须安排统一切换。
- 影子库暂时增加一份向量存储与加载内存，恢复期间需核算 Milvus 磁盘、内存和加载容量；容量不足必须在创建前拒绝任务。
- Alias 切换不能回滚已发生的业务写入；因此写维护状态必须覆盖全部写入进程并贯穿切换。旧库仅是回退基线，不是自动双写副本。
- 恢复与来源存储不是跨库事务；应用异常退出后的维护状态、任务和影子库必须能通过持久化状态诊断与显式续作/清理。
- 恢复持有 PostgreSQL 排他 advisory lock 时，公开库写入会等待该锁；长时间回灌可能影响写入可用性，查询继续使用旧 Alias。当前未自动预检资源，真实 Milvus/PyMilvus Alias 切换和资源峰值未经验证，不能据此直接启动生产恢复。

## 官方接口依据

- Milvus Alias 用于把稳定逻辑名切换到新物理集合；恢复切换使用 `alter_alias`，而不是先删除旧集合：[Manage Aliases](https://milvus.io/docs/manage-aliases.md)、[PyMilvus 2.6 `alter_alias`](https://milvus.io/api-reference/pymilvus/v2.6.x/MilvusClient/Collections/alter_alias.md)。
