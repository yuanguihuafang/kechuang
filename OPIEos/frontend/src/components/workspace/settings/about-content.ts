/**
 * About OPIE OS markdown content. Inlined to avoid raw-loader dependency
 * (Turbopack cannot resolve raw-loader for .md imports).
 */
import { APP_VERSION } from "@/version";

export const aboutMarkdown = `# 关于 OPIE OS

**OPIE OS（版本 ${APP_VERSION}）** 是面向企业团队的智能体平台，帮助团队安全地构建、运行和管理智能工作流程。

OPIE OS 将智能体、企业知识、业务服务和受控执行环境连接在一起，帮助团队更稳定、更高效地完成复杂工作。

---

## 核心能力

- **连接业务流程**：将团队正在使用的工具、服务和业务流程连接到同一个工作区。
- **专业智能体**：使用面向不同工作场景的智能体，处理各类专业任务。
- **安全执行**：在受控、隔离的环境中执行任务，降低操作风险。
- **持续上下文**：在任务执行过程中保留相关信息，减少重复说明。
- **团队协作与管理**：在统一工作区中管理成员、权限、连接和自动化任务。
`;
