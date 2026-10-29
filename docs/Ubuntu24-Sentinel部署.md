# Ubuntu 24.04 / Intel N100 源码部署与升级

只在 Ubuntu 或 Codex Cloud 构建/运行 Docker；Mac mini 管理机只负责开发与 SSH，不需要安装 Docker。本次未连接或修改家庭服务器。

`compose.sentinel.yaml` 为独立配置，项目 `goofish-monitor`、服务 app，**本地源码 Dockerfile.sentinel 构建**，不使用上游预构建应用镜像。默认只发布 `127.0.0.1:8000`，通过 SSH 隧道访问；不增加公网监听或端口转发。不要与旧 Compose 或 LAN Compose 叠加，不要同时启动同一项目的两种部署。

此前 Caddy 局域网网关改造是独立 PR。本配置不自动替代或修改服务器的 compose.local.yaml，也不自动迁移 LAN 网关。如果已运行该网关，应由本地 Codex 保留其 app 无宿主机端口和 gateway 绑定，只在审查后改用此 Fork 构建的新应用镜像；不要合并 Compose 端口列表。

## 新环境

1. 检出本 Fork 的 `feat/goofish-sentinel-v1` 或合并后的提交。现有部署升级请先按下节备份，不执行以下初始化步骤。
2. 新建本地 `.env`，从 `.env.example` 选取所需设置，设置 WEB_USERNAME 和独立、至少 12 位的非默认 WEB_PASSWORD。不要把密码写到命令行、聊天、Git 或构建参数里。普通追踪可不设置 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL_NAME；通知也可不启用。用安全编辑器保存，`chmod 600 .env`。
3. 为全新部署准备空 `config.json`（JSON 数组 `[]`）及 data、state、prompts、logs、images、jsonl、price_history 目录。**已有文件不覆盖，已有数据库不初始化。** 不直接复制含示例任务的 config.json.example，以免误启用示例任务。
4. 在原账号管理界面按现有流程导入本人合法登录态，不提交 Cookie。市场任务可选 state 下的账号文件；缺少登录态只会安全暂停，不自动登录或破解验证码。
5. 确认九项挂载可由容器 UID/GID 1000 访问。镜像默认非 root；如原文件所有者不同，可在 `.env` 设置 SENTINEL_UID/SENTINEL_GID 为已有部署用户并确保项目目录、.env、数据、日志可读写。不要用 chmod 777、全局递归 chown 或 root 容器绕过权限问题。检查 `stat -c '%u:%g %a %n' .env config.json data state prompts logs images jsonl price_history`，只修正本项目必要权限。

```bash
cd /home/o0521/ai-goofish-monitor
# .env 和持久化文件已按上文准备，密码不放在命令行中。
docker compose -f compose.sentinel.yaml build app
docker compose -f compose.sentinel.yaml up -d --no-build app
docker compose -f compose.sentinel.yaml ps
```

镜像包括后端、前端生产构建及 Chromium，使用非 root 用户、tini/Compose init、共享内存、健康检查、自动重启与日志轮转；不挂 Docker Socket、不用 host 网络或 privileged。构建上下文使用 Dockerfile 专用允许列表，不发送 .env、state、data、备份或其他个人运行文件。

本机访问或从管理机执行：

```bash
ssh -N -L 8000:127.0.0.1:8000 ubuntu24
```

浏览器访问 `http://127.0.0.1:8000`，使用应用凭据登录；所有 API 仍需服务端会话。需要局域网共享时沿用经过审查的 Caddy 网关方案，不能改成 0.0.0.0 发布。

## 现有部署升级：先备份再首次启动新版本

由本地 Codex 在服务器项目目录执行，先核对运行中容器标签确定真实 Compose 文件、项目、挂载、用户和镜像 ID。服务器可能使用独立 compose.local.yaml，**不能把仓库 docker-compose.yaml 当成实际部署配置**。

1. 在原界面暂停全部市场任务（若已有）和原有 AI/关键词任务、cron。检查没有抓取进程运行。保留 .env、登录态、prompts、data、其他八项配置和挂载；不改模型或通知配置。
2. 保存旧 Compose、源码提交、镜像 ID 以及本项目的容器启动方式。可将当前 image ID 标记为本项目专用回滚标签后再构建新标签，防止覆盖标签导致无法回退；不清理旧镜像。
3. 用**实际旧 Compose**只停止本项目 app。以下按真实文件确认为 compose.local.yaml 举例，文件不符时停止并先核对。

```bash
cd /home/o0521/ai-goofish-monitor
docker compose -p goofish-monitor -f compose.local.yaml stop app
# 请先将 scripts/sentinel_backup.py 从已审查分支取到本项目，不改运行数据。
python3 scripts/sentinel_backup.py --database data/app.sqlite3 --backup-dir .sentinel-backups
```

脚本使用 SQLite backup API，检查源库存在、备份 quick_check，通过独占创建保留历史备份；目录 0700、备份 0600，不会创建空源数据库。备份失败不得启动新版本，先用旧配置及原镜像恢复服务。再用受限本地归档备份 .env、config.json、state、prompts 和真实旧 Compose；不把备份提交 Git，也不打印内容。

4. 核对预升级旧 tasks/result_items/price_snapshots 的行数及配置/账号文件哈希，将比对记录存本机受限文件。然后检出/合入审查后的源码；Git 若报告本地修改冲突必须保留用户修改，不强制覆盖部署文件。
5. 运行 `docker compose -f compose.sentinel.yaml build app`（构建可在停机前完成以缩短停机，但必须保留旧镜像）。确认新镜像来自当前 Fork 源码，而不是旧上游 ghcr 标签。
6. 只有备份完整才运行 `docker compose -f compose.sentinel.yaml up -d --no-build app`。首次启动自动执行添加新表的迁移，不 DROP 或清空旧表、不改旧任务模式。网关部署请按前述独立方案保留端口约束，不直接使用这里的回环发布替换它。
7. 验证健康、服务端登录、旧任务与历史价格数量、账号文件哈希，之后再创建暂停的市场任务检查页面。确认仅 app 所属项目受影响，其他容器 ID/StartedAt 不变。
8. 正式启用真实追踪前在平台许可范围内进行低频人工验收，验证线上字段与 Fixture 一致、登录态有效；不得用绕过风控的方式验证。先暂停通知，检查无 LLM 配置也能运行。通过后由用户决定何时恢复已有任务与通知。

## 回滚

停止本项目新 app，使用之前保留的真实旧 Compose、旧源码和原 image ID 恢复，禁止意外 pull 升级旧镜像。新增表向后兼容，**默认保留当前 SQLite，不覆盖数据库**，旧业务可忽略市场表。所有运行数据、.env、Cookie 和配置仍使用原路径。

如果发生确认的数据库损坏，需要另外审批人工数据库灾难恢复；应先保全当前库、WAL/SHM 和所有诊断文件，再在停机状态下选择备份恢复，不能盲目把旧快照覆盖到迁移后的数据。不要执行 down -v、docker system prune、全局 Docker 清理或删除数据目录。

## 运行与故障检查

```bash
docker compose -f compose.sentinel.yaml ps
docker compose -f compose.sentinel.yaml restart app
```

/health 仅为本地存活检查；保护数据的 /api 请求未登录应为 401，不能用前端登录标记代替验证。首次有效登录后生成 data/.session_secret（0600），需保留以便容器重启后会话继续有效。WEB_PASSWORD / WEB_SESSION_SECRET 更换后旧会话失效。运行服务保持一个 Uvicorn worker，N100 上优先默认 1 页搜索、顺序回访，避免与多个旧抓取任务同时占用资源。

市场页面查看安全错误代码与运行记录。登录失效/限制暂停全部市场任务，人工在原账号管理页修复后再恢复；不自动切账号/代理尝试绕过。浏览器启动失败不改变商品交易状态，次日重试。硬退出后允许最多约 15 分钟租约过期恢复；不要直接改 SQLite 删除租约来强行并发执行。

普通日志不含原始平台响应、Cookie 或凭据。排查时也不要将 `docker inspect` 的完整环境、`docker compose config`、.env 内容写到公开日志。默认健康检查及只读页面不触发抓取或通知。重新启动前暂停已有启用 cron，避免原应用启动调度器恢复真实采集。

Cloud 的代理 CA 通过 BuildKit 临时 secret 传入；普通 Ubuntu 可直接构建，无需这些参数。Cloud 构建示例（CA 路径是运行环境提供的公开信任证书，不应复制进镜像）：

```bash
docker build -f Dockerfile.sentinel \
  --secret id=proxy_ca,src="$CODEX_PROXY_CERT" \
  --secret id=proxy_bundle,src=/etc/ssl/certs/ca-certificates.crt \
  -t goofish-sentinel:cloud-test .
```
