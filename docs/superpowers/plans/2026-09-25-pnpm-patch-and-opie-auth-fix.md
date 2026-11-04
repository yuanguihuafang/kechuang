# pnpm 补丁配置与 OPIEos 401 修复

## 范围

仅处理两个已报告问题：

1. 将 `next-themes@0.4.6` 的 `patchedDependencies` 从 `OPIEos-main/frontend/package.json` 移到 `OPIEos-main/frontend/pnpm-workspace.yaml`，保留 patch 文件，并在该 frontend 目录运行 `pnpm install` 更新锁文件。
2. 修复重迁后的 OPIEos Gateway 与科创 Backend 内部认证 Header 名不一致：Backend 发送合法连字符 Header，而当前 OPIEos Gateway 源码仍读取含空格 Header。两端配置中的内部 Token 值已确认一致。

## 边界

- 允许修改 OPIEos Gateway 应用层 `backend/app/gateway/internal_auth.py`、与 Header 契约直接相关的注释、测试和认证文档。
- 不修改 DeerFlow Harness 核心：`backend/packages/harness/deerflow/**`。
- 不修改科创 API/前端传参，不输出或记录 `.env` 中的密钥。
- 不删除 `frontend/patches/next-themes@0.4.6.patch`。

## 实施顺序与验收

1. 先增加 Header 名合法性回归测试，确认旧的含空格名称会失败。
2. 更新 OPIEos 内部认证 Header 常量及对应测试/认证文档，使其与 Backend 的 `X-OPIE-OS-Internal-Token`、`X-OPIE-OS-Owner-User-Id` 一致。
3. 移动 pnpm 补丁映射，并从 `OPIEos-main/frontend` 运行 `pnpm install`。
4. 运行最小相关测试；确认安装无该弃用警告、锁文件含 `patchedDependencies` 记录、patch 文件存在。
5. 更新 `docs/agent.md` 和 OPIEos 修改记录。运行时需重启本机 OPIEos Gateway 后再验证真实 HTTP 请求；本计划不自动重启用户服务。

## 实施结果（2026-09-25）

- 已完成两项修复；pnpm 安装未再出现 `pnpm.patchedDependencies` 警告，锁文件记录 `patch_hash`，原 patch 文件保留。
- Gateway 内部认证 Header 已与科创 Backend 一致；配置文件中的 Token 相同（未读取或记录明文）。
- OPIEos 认证相关测试：68 passed；科创 Backend Gateway client 测试：4 passed。
- 本机 Gateway 未由本次操作重启，尚未做真实请求验收；重启 OPIEos 后再验证 `/api/threads` 与流式 run。
