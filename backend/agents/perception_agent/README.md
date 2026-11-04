# Perception Agent

负责上传材料解析、解析任务状态、文档资源访问，以及根据对话和解析原文提取结构化草稿字段。

```text
perception_agent/
├── agent.py                         # PerceptionAgent 的公开入口
├── schemas.py                       # 解析请求、结果和任务响应合同
├── routes.py                        # 上传、解析任务、结果及资源 HTTP 适配
└── application/
    ├── perception_service.py        # 解析编排与 PerceptionResult
    ├── field_extraction.py          # 结合对话/原文提取草稿字段
    └── draft_extractor.py           # 从解析材料提取事实
```

PDF、PPT/PPTX、图片、Office 和文本解析实现统一位于 `backend/services/document_parsing/`，由文件类型分发；不要在 Agent 目录重复维护解析器。上传存储和文档权限依赖 `services/storage/` 与 `services/application/document_access.py`。

运行时由 `backend/runtime/bootstrap.py` 创建 `PerceptionAgent`，HTTP 路由由 `backend/agents/routes.py` 聚合，并通过 `backend/api/v1/router.py` 挂载。路由层负责 HTTP 与任务适配，解析和字段抽取逻辑留在应用服务中。
