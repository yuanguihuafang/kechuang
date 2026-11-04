# Milvus 影子 Collection 恢复实施计划

> **执行方式：** 当前会话内由主代理逐任务实现；不提交代码。

**目标：** 只读预检 PUBLISHED 来源，管理员接受明确缺失清单后才恢复影子库；验证通过后将旧物理库改名保留、新影子库提升为原 Collection 名，再切换稳定 Alias；所有其它失败都保留数据。

**当前进度（2026-10-16）：** 本地实现已覆盖只读预检、精确缺失确认、影子库回灌/校验、Alias 切换、写入栅栏、任务中断恢复和管理页流程。真实 PostgreSQL/MinIO/Milvus 联调、部署版本统一及服务器恢复仍未验证，不能据此直接操作生产。

**架构：** MilvusService 的知识库逻辑名改为稳定 Alias；管理端 `rebuild` 生成带快照指纹的只读预检，`restore` 重新核验并要求管理员确认精确缺失项，再通过 PostgreSQL advisory lock 暂停写入、向影子物理库回灌、验证并切换 Alias。失败影子库不替代活动库。

**技术栈：** FastAPI、PyMilvus 2.6、PostgreSQL、现有检索索引服务与 pytest。

**规格：** `docs/superpowers/specs/2026-10-15-milvus-shadow-collection-recovery.md`

## 全局约束

- 旧 Alias 在影子库全部通过验证前不变。
- 恢复数据沿用当前权威来源、chunking、字段和 embedding 规则；影子写入按 PK 去重后使用 insert。
- 恢复失败不删除活动库、不把半成品设为活动库；不得吞异常或伪报成功。
- 预检不写业务 PostgreSQL、MinIO 或 Milvus；允许持久化维护任务状态和检查报告。只可跳过用户已接受且与预检指纹一致的不可恢复来源。
- 写维护标记必须跨进程；检索保持可用；普通日常更新继续使用 upsert。
- 不连接、修改或部署生产环境；保留工作区已有未提交改动；不提交代码。

## 审查重点

- 应用升级期间旧版本直连物理名时，不能让读写分流到不同 generation。
- PG 与 Milvus 切换之间失败时，Alias 与维护标记必须能被诊断并安全恢复。
- 来源重复 PK、缺失来源、任务中断和跨知识类型错误不能污染活动库。
- 影子库加载资源不足时须失败关闭，查询继续由旧库提供。
- Alias 切换后仍在途的查询应由 Milvus 正常处理，且删除/发布写入不得越过维护栅栏。

---

### 任务 1：稳定 Alias 路由与一次性初始化

**文件：** `backend/services/vector/milvus/milvus_service.py`、`backend/agents/retrieval_agent/retrieval/search_service.py`；测试覆盖 `backend/tests/test_milvus_connection_lifecycle.py` 与新增 Alias 路由测试。

- [x] 覆盖已有物理集合接入 Alias、新建物理集合、schema/命名冲突失败且不 drop 现存数据。
- [x] 查询、索引、删除、健康检查及 MySQL 同步使用稳定 Alias；影子 collection 使用独立物理名。
- [x] Alias 路由和 Milvus 生命周期定向测试通过（测试替身；真实服务未验证）。

### 任务 2：影子 generation 恢复与切换

**文件：** `backend/admin/mysql_sync.py`、`backend/agents/retrieval_agent/application/retrieval_service.py`、`backend/services/vector/milvus/milvus_service.py`；测试覆盖 `backend/tests/test_draft_index_recovery.py`、`backend/tests/test_rebuild_sync_contract.py` 及新增影子恢复测试。

- [x] rebuild 只读生成来源/缺失指纹；未经确认或快照变化的 restore 在创建影子库前拒绝。
- [x] 通过现有索引构建路径恢复，保持 chunking/字段/embedding；按 PK 去重，MySQL 只补未被成功恢复草稿覆盖的业务 ID。
- [x] flush/load、schema、行数和预期主键校验通过后进入 `READY_TO_SWITCH` 并释放维护锁；管理员单独确认后重新校验，旧活动库改名保留、影子库提升为原名，最后 `alter_alias`；失败不删除任何 Collection。
- [x] 故障注入覆盖来源变化、影子加载失败和 Alias 切换边界；未做语义查询抽样。

### 任务 3：跨进程写栅栏与故障恢复

**文件：** PG 维护状态存储及 `backend/admin/mysql_sync.py`、所有 Milvus 写操作入口、相关管理/发布调用方；对应 admin、生命周期、同步安全测试。

- [x] 公开写入使用 PostgreSQL advisory shared lock；恢复持有同键 exclusive lock；影子写入显式指定目标。
- [x] 覆盖锁模式、同线程嵌套、任务中断，以及重启后按 Alias 实际目标对账 `SWITCHING` 任务。
- [ ] 未连接真实 PostgreSQL 验证多进程锁阻塞/释放，也未验证生产全部写入口与其他服务直连 Milvus 的边界。

### 任务 4：管理接口、文档与回归

**文件：** `backend/admin/api/v1/admin.py`、`frontend/src/api.js`（仅当响应契约需要）、已有 Milvus 恢复设计文档与 `docs/Agent.md`；恢复 API/任务状态测试。

- [x] 保留 rebuild/restore URL；restore JSON 必须含 `confirm`、`preflight_id` 和 `accepted_missing_fingerprint`；新增独立 activate 确认接口。
- [x] 管理页展示预检缺失、精确确认、后台阶段、generation 和失败状态；新增显式切换按钮并更新 API、方案和 Agent 文档索引。
- [x] 后端相关定向测试 161 项、前端测试 7 项通过；Vite 生产构建通过，入口包约 387 KB。
- [ ] 生产部署与真实存储端到端恢复尚未执行。

## 验收

- 每项行为按测试先 RED 后 GREEN；失败恢复路径都保留旧活动库。
- 执行受影响的后端 pytest 定向集与相关 lint；未连接真实 Milvus、未部署服务器，报告中明确说明。
- 用户已有的工作区改动和服务器数据不做清理、回退或覆盖。
