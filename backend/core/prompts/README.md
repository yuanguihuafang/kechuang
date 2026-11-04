# 提示词模块

本目录只存放仍由 Backend 调用的领域提示词常量；Agent 意图编排由 DeerFlow `kechuang` Agent 负责。

- `perception.py`：资料解析后的内容理解与业务字段提取提示词。
- `document_parsing.py`：文档解析与图片内容处理提示词。
- `field_extraction.py`：草稿字段增量提取提示词。
- `admin_field_extraction.py`：管理端字段提取提示词。
- `retrieval.py`：检索查询改写提示词。
