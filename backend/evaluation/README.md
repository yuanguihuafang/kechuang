# 四类知识检索评测

本目录用于评估企业、需求、成果、专家四类公开知识检索质量，方案库暂不纳入。

## 评测要求与口径

### 评测范围

- 当前只评估企业库、需求库、成果库、专家库；方案库等方案相关数据另行评估。
- 每类知识库使用固定的 100 条问题集。问题必须对应目标知识库，不能用跨库误召回代替正确结果。
- 评测只验证检索链路，不把上传、解析、字段提取和发布流程本身混入检索指标。

### 有效数据

- PostgreSQL 中的文档发布状态是评测范围的准入依据。
- 只有已发布且状态为“可检索”的版本进入评测；“未发布”“已确认但未发布”“已下架”和“已回收”均不得作为有效召回结果。
- PostgreSQL、MySQL 和 Milvus 必须对应同一份文档版本。发布、重新发布或下架后的状态需要在三者的查询结果中保持一致。

### 检索输入完整性

- 一个文档可以生成多个 chunk，不能用“一个文档一个 chunk”作为评测前提。
- 正式案例必须标注具体相关 chunk（`relevant_chunk_ids` 或
  `expected_chunk_source_contains`），Precision、Recall、F1 和 AUC-ROC 均按
  chunk 统计，不按 `record_id`、文档或企业记录合并。
- chunk 应保持原文页码和正文顺序，保证召回结果可以回溯到原文位置。
- 图片或其他多媒体引用只能保留在其所属页或对应 chunk 中，不能把整份文档的引用追加到每个 chunk；重复引用会污染文本和相关性判断。

### 优化与验收

- 先保存当前检索流程的基线结果，再一次只改变一个变量（例如 Top-K、重排、分块或查询改写）进行对比。
- 当前基线固定使用 `step` 检索模式和 rerank；Query Expansion 暂不作为本轮评测变量。
- 每次优化都保留配置、问题集版本和结果文件，分别比较 Precision、Recall、F1、AUC-ROC。
- 对指标下降的案例回看原始问题、命中文档、chunk 内容和发布状态，先区分数据、分块、召回、重排和查询理解问题，再调整算法。

### 四项指标的优化方向

- Precision：减少无关召回。优先检查知识库范围、发布状态过滤、重复 chunk、元数据过滤和重排阈值。
- Recall：减少漏召回。优先检查问题集标注、文档覆盖、chunk 粒度、关键词与向量检索互补、查询改写和 Top-K。
- F1：同时提升 Precision 和 Recall。固定问题集后，围绕 chunk 粒度、Top-K、混合召回和重排逐项做对照实验。
- AUC-ROC：提升相关结果的排序位置。检查召回分数方向、分数归一化、重排输入和相关性判断是否一致。
- 每次只改变一个变量，记录四项指标和下降案例；不要用单项指标提升掩盖其他指标明显下降。

## 执行

在 `backend` 目录运行，后端服务需先启动：

```powershell
python -m evaluation.retrieval_quality_test
```

评测固定读取 `retrieval_questions/` 中四个知识库各 100 条问题，只调用
`POST /api/v1/knowledge/retrieve`，不上传文件、不解析文档、不生成草稿。

默认配置：

- Top-5
- 并发数 2
- 使用 `step` 检索模式
- 固定开启 rerank
- 默认使用测试集中的 chunk 标注；LLM 判断仅作为显式可选的辅助结果

结果位于 `evaluation/output/<knowledge_type>/step_rerank_on`，包含：

- `retrieval_metrics.json`
- `retrieval_results.csv`
- `retrieval_results.jsonl`
- `retrieval_per_query_metrics.csv`
- `llm_judgements.jsonl`（LLM 判断的增量检查点，可断点续跑）

指标只包含 Precision、Recall、F1 和 AUC-ROC。

显式使用固定 chunk 标注：

```powershell
python -m evaluation.retrieval_quality_test --no-llm-judge
```

## 生成测试问题

从当前 MySQL 记录重新生成四类问题：

```powershell
python -m evaluation.generate_retrieval_questions
```

生成脚本产出的是 `candidate`；正式评测前需要人工确认问题与具体 chunk，补齐
`relevant_chunk_ids` 或 `expected_chunk_source_contains` 后改为 `confirmed`。

没有 MySQL 时，基于已有测试问题生成表达变体：

```powershell
python -m evaluation.generate_retrieval_questions --offline
```

## LLM 初标与人工复核

当前题集可以先保持 `candidate`，使用 LLM 对 `step + rerank` 的 Top-20 chunk 做初步相关性判断：

```powershell
python -m evaluation.retrieval_quality_test enterprise --annotate
```

四类知识库全部执行：

```powershell
python -m evaluation.retrieval_quality_test all --annotate
```

标注结果位于 `evaluation/output/annotation/<knowledge_type>/review.xlsx`。人工只需要复核或修改 `review_relevant` 列，不要修改 `chunk_key`；`review_comment` 可填写备注。

复核完成后，保留各知识库目录下的 `review.xlsx`，直接进行正式评测。程序默认读取 `evaluation/output/annotation`，优先读取 Excel，也兼容旧的 CSV：

```powershell
python -m evaluation.retrieval_quality_test all
```

正式评测仍使用默认 Top-5、`step` 和 rerank，原始题集不会被覆盖。

### 单库或全部评测命令

知识库名称参数：`enterprise` 企业库、`demand` 需求库、`achievement` 成果库、`expert` 专家库。

首次生成单个知识库的 Top-20 候选标注表，例如专家库：

```powershell
cd D:\Work\kechuang\backend
python -m evaluation.retrieval_quality_test expert --annotate
```

人工修改对应目录下 `review.xlsx` 的 `review_relevant` 列后，执行单库正式评测：

```powershell
# 企业库
python -m evaluation.retrieval_quality_test enterprise
# 需求库
python -m evaluation.retrieval_quality_test demand
# 成果库
python -m evaluation.retrieval_quality_test achievement
# 专家库
python -m evaluation.retrieval_quality_test expert
```

如果四个库都需要生成候选标注表：

```powershell
python -m evaluation.retrieval_quality_test all --annotate
```

四个库人工复核完成后，执行全部正式评测：

```powershell
python -m evaluation.retrieval_quality_test all
```

单库结果分别位于 `evaluation/output/<knowledge_type>/step_rerank_on/`。
