# 科创与 DeerFlow 双服务器迁移部署步骤

> 维护日期：2026-09-29。3090/4090 尚未执行任何远程写入。本机 OPIE 自定义 MCP 拦截器已修正为读取 `request.runtime.context`，并通过本机真实草稿创建和知识检索验证；服务器尚未部署。服务器端仍需核对 3090 容器源码挂载/镜像构建方式，再部署增量文件，不能覆盖服务器活动配置或运行数据。

## 1. 服务器拓扑

| 用途 | 服务器 | 运行路径 / 端口 |
|---|---|---|
| DeerFlow / OPIEos Gateway | 3090 | 源码路径 **~/codes/random/OPIEos**；SSH `slave1@27.223.9.106 -p 8101`；外部 Gateway 8199 |
| 科创业务服务 | 4090 | **/home/zyx/kechuang**；Backend 8793，前端 8794 |
| 4090 暂存包 | 4090 | **/home/zyx/kechuang-deerflow** |
| DeerFlow → 科创 MCP | 3090 → 4090 | **http://27.223.9.106:8793/mcp/** |
| 科创 Backend → DeerFlow | 4090 → 3090 | **http://27.223.9.106:8199** |

两台服务器使用同一公网 IP，不代表是同一台机器；SSH 端口映射必须分别确认。

DeerFlow 科创专用用户 UUID 为 **62ddcc4d-f2b5-4152-be26-78a1a7554a86**。4090 Backend 的 `DEERFLOW_AGENT_OWNER_USER_ID` 与 3090 上科创 Agent/Skill 目录必须使用该 UUID；下载后仍需核对服务器实际运行目录和 Backend 当前配置。

## 2. 安全边界

- **/home/zyx/kechuang-deerflow** 是 4090 暂存目录，不是线上目录。
- 先解压和核对 ZIP 内容，确认目录映射后才备份、同步线上文件。
- 不覆盖或上传服务器已有的 **backend/.env**、OPIEos 根目录 **.env**。
- 不删除或覆盖 DeerFlow 的 **.deer-flow** 运行数据、数据库、用户目录或附件。
- 不执行 **docker compose down -v**，不使用 Git。
- 3090 DeerFlow 单独使用 OPIEos 的 KC_LLM_API_KEY、KC_LLM_BASE_URL、KC_LLM_MODEL；不修改 4090 科创模型配置。
- 本地 `OPIEos/config.yaml` 为 SQLite/内存模式的开发配置，属于该 OPIEos 实例共享配置；仅供本机测试，不能拿它覆盖服务器配置。
- bakup.zip 暂作备份资料，只解压到暂存目录；核对内容和用途前，不覆盖线上目录。

## 3. 当前文件与待确认项

4090 暂存目录已有 backend.zip、frontend.zip、bakup.zip，分别由本机 backend、frontend、bakup 打包。ZIP 内部路径尚未核实，不能假定压缩包解开后直接得到同名根目录。

下一步先获取 3090 的原始文件。本地候选改动仅用于之后对照，不直接覆盖服务器：

- backend/packages/harness/deerflow/config/extensions_config.py
- backend/packages/harness/deerflow/mcp/interceptors.py
- backend/packages/harness/deerflow/tools/sync.py
- 本机 `config.yaml` 与服务器版配置的差异（本机 SQLite、内存 stream bridge、本地模型变量、sandbox 和联网工具配置不得整份迁移）。
- 3090 的 OPIEos 根配置、活动 MCP 配置及用户级 Agent/Skill 原件。

D:\Work\server\OPIEos 当前不是服务器源码基线；必须以 3090 正在运行的文件为准。服务器原件下载并对照前，不生成覆盖包，也不执行后续写入步骤。

## 4. 4090：解压到暂存目录并核对

在 4090 执行；只写入暂存目录：

```bash
cd /home/zyx/kechuang-deerflow
unzip -n backend.zip -d .
unzip -n bakup.zip -d .
unzip -n frontend.zip -d .
```

-n 表示遇到同名文件不覆盖。查看解压结构，并检查压缩包是否带有环境文件：

```bash
cd /home/zyx/kechuang-deerflow
find . -maxdepth 3 -type f -printf '%p\t%s bytes\n' | sort
find . -type f \( -name '.env' -o -name '.env.*' \) -print
```

如果发现 ZIP 中带有 .env，不要复制到线上。不要将整个暂存目录复制到 /home/zyx/kechuang。

## 5. 4090：检查线上容器和配置

先识别线上 Compose 文件、服务名和源码挂载：

```bash
cd /home/zyx/kechuang
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
find /home/zyx/kechuang -maxdepth 4 -type f \( -name 'compose.yaml' -o -name 'compose.yml' -o -name 'docker-compose.yaml' -o -name 'docker-compose.yml' \) -print
docker ps -q | xargs -r docker inspect --format '{{.Name}} {{index .Config.Labels "com.docker.compose.project.config_files"}} {{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}'
```

只查看 Backend 配置中的非密钥项：

```bash
cd /home/zyx/kechuang
grep -E '^(DEERFLOW_BASE_URL|DEERFLOW_AGENT_OWNER_USER_ID|DEERFLOW_INTERNAL_TOKEN_HEADER|DEERFLOW_OWNER_USER_ID_HEADER|KC_MCP_ALLOWED_HOSTS)=' backend/.env
```

期望关键值：

```dotenv
DEERFLOW_BASE_URL=http://27.223.9.106:8199
DEERFLOW_AGENT_OWNER_USER_ID=62ddcc4d-f2b5-4152-be26-78a1a7554a86
DEERFLOW_INTERNAL_TOKEN_HEADER=X-DeerFlow-Internal-Token
DEERFLOW_OWNER_USER_ID_HEADER=X-DeerFlow-Owner-User-Id
KC_MCP_ALLOWED_HOSTS=27.223.9.106:*,business-backend:*,kechuang-business-backend:*,localhost:*,127.0.0.1:*,host.docker.internal:*
```

4090 科创后端与 3090 OPIE 均使用环境变量 `DEER_FLOW_INTERNAL_AUTH_TOKEN`，两端配置值必须完全一致。不要把 Token 输出到终端记录或聊天中。4090 现有数据库、模型、MinIO、Milvus 配置保持不动。

## 6. 4090：备份、同步与重启

先根据第 4 节结果确认 ZIP 解压出来的确切源路径，再确定线上覆盖范围：

1. 只备份即将覆盖的线上文件/目录，备份目录使用时间戳。
2. 保留 /home/zyx/kechuang/backend/.env 原样，不使用本机 .env.local 或 ZIP 内环境文件替换。
3. rsync 首次运行加 --dry-run 检查差异，不加 --delete。
4. 确认备份和目标路径无误后，才执行实际同步。

示例模板，源路径必须按 ZIP 实际目录调整：

```bash
rsync -avn /home/zyx/kechuang-deerflow/<已确认的Backend源目录>/ /home/zyx/kechuang/backend/
```

不要将 bakup/、数据目录、模型文件或 .env 当作 Backend 源码同步。同步后使用线上实际 Compose 文件校验：

```bash
docker compose -f <线上Compose文件> config -q
```

只有确认容器使用镜像内源码时才构建对应服务；若容器使用 bind mount，则按现有部署方式重启相关 Backend/Frontend 服务。服务名从 docker ps 或 Compose 标签获取，不重启数据库等无关容器。

## 7. 当前阶段：逐项下载 3090 服务器原件

在本机 PowerShell 执行以下命令。它们只从服务器复制到本地对照目录，不会修改服务器：

```powershell
$remote = 'slave1@27.223.9.106'
$root = '~/codes/random/OPIEos'
$dest = 'D:\Work\server\opieos-server-original'
New-Item -ItemType Directory -Force -Path $dest | Out-Null

scp -P 8101 "${remote}:$root/.env" "$dest\server.env"
scp -P 8101 "${remote}:$root/.env.example" "$dest\.env.example"
scp -P 8101 "${remote}:$root/config.yaml" "$dest\config.yaml"
scp -P 8101 "${remote}:$root/extensions_config.example.json" "$dest\extensions_config.example.json"
scp -P 8101 "${remote}:$root/extensions_config.json" "$dest\extensions_config.json"
scp -P 8101 "${remote}:$root/backend/packages/harness/deerflow/config/extensions_config.py" "$dest\extensions_config.py.server"
scp -P 8101 "${remote}:$root/backend/packages/harness/deerflow/mcp/interceptors.py" "$dest\interceptors.py.server"
scp -P 8101 "${remote}:$root/backend/packages/harness/deerflow/tools/sync.py" "$dest\sync.py.server"
scp -P 8101 "${remote}:$root/backend/.deer-flow/users/62ddcc4d-f2b5-4152-be26-78a1a7554a86/agents/kechuang/config.yaml" "$dest\agent-config.yaml.server"
scp -P 8101 "${remote}:$root/backend/.deer-flow/users/62ddcc4d-f2b5-4152-be26-78a1a7554a86/agents/kechuang/SOUL.md" "$dest\agent-SOUL.md.server"
```

如果 `extensions_config.json` 实际位于 Gateway 的 `DEER_FLOW_HOME` 挂载目录，而不是源码根目录，先通过服务器 `docker inspect` 确认宿主机路径，再单独下载那份正在使用的文件；不要用样例文件代替活动配置。

`.env` 只保存在本机，不要把原件发到聊天或提交到仓库。对照需要时，另做脱敏副本：保留变量名和非敏感设置，将 API Key、Token、密码替换为 `<已脱敏>`。活动 `extensions_config.json`、`config.yaml` 如含密钥也先脱敏再发送。

## 8. 下载用户级科创 Skills 原件

科创 Agent 和 Skill 的目标位置统一为该 DeerFlow 用户目录，不使用共享 `skills/public`：

```text
OPIEos/backend/.deer-flow/users/62ddcc4d-f2b5-4152-be26-78a1a7554a86/
├── agents/kechuang/config.yaml
├── agents/kechuang/SOUL.md
└── skills/custom/kc-*/
```

先把服务器上实际存在的 `kc-*` Skill 名称列出来，再按名称逐个复制完整目录（包括该 Skill 自己的 `references/`、`scripts/` 等文件）。以下命令仅下载已知 13 个目录；若服务器列出的名称不同，先不要运行循环，按服务器清单调整：

```powershell
$skills = @(
  'kc-achievement-draft', 'kc-contact-human', 'kc-customer-visit',
  'kc-demand-discovery', 'kc-demand-draft', 'kc-document-parse',
  'kc-enterprise-profile', 'kc-expert-search', 'kc-field-extraction',
  'kc-knowledge-search', 'kc-solution-draft', 'kc-supply-demand-match',
  'kc-web-search'
)
New-Item -ItemType Directory -Force -Path "$dest\skills" | Out-Null
foreach ($skill in $skills) {
  scp -r -P 8101 "${remote}:$root/backend/.deer-flow/users/62ddcc4d-f2b5-4152-be26-78a1a7554a86/skills/custom/$skill" "$dest\skills\"
}
```

不要下载或复制整个 `.deer-flow`，不要带回 `skills_view` 缓存、数据库、附件或密钥文件。若服务器实际 `DEER_FLOW_HOME` 挂载不是 `$root/backend/.deer-flow`，以挂载确认的宿主机路径为准，并先告诉我再调整下载路径。

## 9. 收到原件后的增量流程

1. 将服务器原件逐项发来；`.env` 和含密钥的配置只发脱敏副本。
2. 按文件对照服务器原件与本机候选版本，列明每处差异、原因和最小补丁；不重写整份配置。
3. Agent/Skill 只更新 `users/62ddcc4d-f2b5-4152-be26-78a1a7554a86/` 下对应科创文件；保留其他用户、其他 Agent/Skill 和 `skills_view`。
4. 服务器 OPIEos 的 `.env`、`config.yaml`、活动 `extensions_config.json` 各自保留原值，只合并确认必要的键；本地 SQLite/内存开发配置不得覆盖服务器 PostgreSQL/Redis 配置。
5. 对照完成并由用户确认后，再编写精确备份、增量复制、重启和验收命令。当前阶段不执行任何服务器写入。

最终验收再检查 4090 Backend 8793、Frontend 8794，3090 Gateway 8199；从科创会话分别验证知识库检索与草稿创建。回滚只恢复本次明确改动的文件，不删除或覆盖整个 `.deer-flow`、数据库或用户目录。

## 10. 本次 MCP 会话头修复的服务器增量（尚未部署）

本机扩展文件：`OPIEos/backend/my_package/mcp/auth.py`。本机活动配置已声明 `my_package.mcp.auth:build_auth_interceptor` 和 `headers_from_context`；3090 服务器活动配置与代码挂载仍须以服务器原件核实，不能据本机配置推断。确认后只合并所需自定义扩展，不要改 DeerFlow 核心源码，也不要覆盖活动 `extensions_config.json`、`.env` 或 `.deer-flow`。

本地打包时保留归档内路径 `backend/my_package/mcp/auth.py`，只打包这个扩展文件。上传后先检查包内路径与容器部署方式：

```bash
cd ~/codes/random/OPIEos
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker ps -q | xargs -r docker inspect --format '{{.Name}} {{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}'
```

- 如果 Gateway 的 `backend` 目录由宿主机 bind mount 提供，将文件解压/复制到已确认的宿主机源码根目录 `backend/my_package/mcp/auth.py`，然后按该项目实际 Compose 文件仅重建/重启 Gateway 服务。
- 如果代码打包在镜像内，不要只用 `docker cp` 写入运行容器（重建后会丢失）；应在确认的源码目录中放入扩展文件，再按现有镜像构建流程重建 Gateway。
- 不猜容器名、Compose 服务名或挂载路径；先依据上面 `docker inspect` 输出和现有部署配置确认，再执行写入。

重启后检查 Gateway 日志中存在 `Loaded MCP interceptor: my_package.mcp.auth:build_auth_interceptor`，再从科创会话验证知识库检索与草稿创建。确认动态请求包含 `X-KC-Thread-ID`、`X-Trace-ID`，且没有 `Authorization`；缺失会话头时科创 MCP 仍应拒绝请求。该节只记录待执行服务器步骤，本次修复仅在本机进行。
