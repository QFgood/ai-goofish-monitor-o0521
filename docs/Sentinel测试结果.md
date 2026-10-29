# Goofish Sentinel v1.0 Cloud 测试结果

日期：2026-10-30（Asia/Shanghai）。功能分支：`feat/goofish-sentinel-v1`，从本 Fork 的 `77f8d758` 开发，交付前同步了 master 的 `f8256f2e`（已合入 LAN 网关 PR #1），保留原有局域网配置。

**最终结果：175 项 Python 测试通过，3 项 live 外部服务测试按任务要求排除，0 失败、0 跳过；2 项前端单元测试通过；TypeScript/Vite 生产构建通过；本地源码 Docker 镜像构建与实际容器冒烟通过。**

## 命令与结果

```bash
# 仓库 requirements.txt 安装在独立虚拟环境，无需真实 Cookie 或 API Key。
SENTINEL_BROWSER_TESTS=1 SENTINEL_DOCKER_TESTS=1 SENTINEL_CAPTURE_SCREENSHOTS=1 \
  python -m pytest -m 'not live and not live_slow' -q
# 175 passed, 3 deselected in 14.16s

npm --prefix web-ui run test:market
# 2 passed, 0 failed
npm --prefix web-ui run build
# vue-tsc -b && vite build：通过

python -m py_compile src/services/*.py src/api/auth.py src/api/routes/market.py src/app.py scripts/sentinel_backup.py
git diff --check
# 通过

docker build -f Dockerfile.sentinel \
  --secret id=proxy_ca,src="$CODEX_PROXY_CERT" \
  --secret id=proxy_bundle,src=/etc/ssl/certs/ca-certificates.crt \
  -t goofish-sentinel:cloud-test .
# 构建成功，最终本地 image ID：
# sha256:6981b0a76021b1b4a0e04b90c8fb8a170a78ddd7816431fccc2ecf1878376485
```

Cloud 使用 Python 3.12、Docker Engine 28.4.0、Compose 2.40.3。运行 API TestClient、Node worker、Chromium 和 Docker 时需要 Cloud 本地执行权限；普通 sandbox 事件循环测试会停滞，已诊断并使用授权本地执行环境运行，没有将停滞标为成功。宿主机不支持这些能力时，测试 opt-in 会显式跳过，不应声称执行通过。本次两个 opt-in 都实际开启。

## 分阶段验证

1. Phase 1：18 项市场生命周期/迁移/分类基础测试通过；与原价格服务合测 19 项通过。
2. Phase 2：25 项市场测试通过，包括卖家分组、持久化认领、崩溃租约恢复、登录失效暂停与原始状态适配。
3. Phase 3：34 项市场/统计/服务端认证 API 测试通过，包括金额语义、去重、空样本、分页、CSV、跨站写入和 WebSocket 权限。
4. Phase 4：前端 TypeScript 与生产构建通过；真实 Chromium 完成登录、配置选择、不同配置比较、价格曲线、商品历史、移动端无页面溢出、深色模式和新建任务入口检查，未发现未处理的页面 JS 异常。
5. Phase 5：补充完整 MVP 流程、租约并发隔离、浏览器失败重排、旧任务与 SQLite 备份、501 行 CSV 分页流式导出和 Docker 重启测试，最终全部合跑为上述结果。

## 核心证据

| 范围 | 实际验证 |
| --- | --- |
| 商品发现 | 稳定 item_id 去重；跨任务共享主记录；昵称变化仍按 seller_id；无法取得卖家 ID 保留待补全 |
| 生命周期 | 在售多次降价后已售，参考价取最后有效在售价；已售展示价单独存；actual_transaction_price 为空；重复已售不重复入样；重新上架留事件；未知、缺失、失败不判已售 |
| 到期队列 | 同卖家多商品仅读取一次；同一计划不能被两个并发工作者认领；新仓储实例从数据库恢复；过期租约恢复；旧工作者不能覆盖新租约；失败保留进度并排次日 |
| 追踪策略 | 新商品次日 09:30；暂停不认领；期满结束；连续 7 次未知进入 stale_unconfirmed；浏览器启动失败写 unknown 而非每分钟无限重试 |
| 不使用 LLM | 完整发现→次日降价→再次回访明确已售→统计曲线流程中，将 AIClient._call_ai 替换为会报错的 Mock，流程成功且断言未调用 |
| 统计 | 均值、中位数、实际样本数；无样本为 null；首次已售日期只计一次；7日滚动按原始样本计算；配置隔离；历史价格区间按当时观察值筛选；CSV 单位与防公式注入 |
| SQLite | 迁移重复执行保留原 tasks/price_snapshots/app_metadata；外键检查通过；删除市场任务保留商品/观察历史；SQLite backup 文件 0600 且原数据可读，缺源拒绝初始化 |
| 身份认证 | 未登录的新旧 API 401；弱默认密码拒绝登录；HttpOnly/Strict Cookie；篡改/过期 Cookie 拒绝；登录限速；跨站写入 403；未认证和跨站 WebSocket 拒绝；有效会话可连接 |
| 通知 | Mock 渠道验证每日摘要去重、空样本不编造价格；不调用真实企业微信或其他外部渠道 |
| Docker | 原始 Compose 展开仅发布 127.0.0.1:8000、九项挂载、健康检查；源码构建、无 privileged/host 网络/socket；真实 UID 1000 运行；Chromium 在非 root 容器打开 data URL；未认证 API 401；重启后市场任务、回访计划、签名密钥与登录会话仍在 |
| 源码一致性 | 容器中 market_runner、market_statistics_service、item_lifecycle_service 的 SHA256 与当前工作区相同，避免误测旧上游镜像 |
| 前端 | Native SVG 对 null 断线，不补零；分转换为元；真实 Chromium 桌面与移动截图均来自明确标注的离线 Fixture |

Docker 测试网络设置为 internal，浏览器仅访问 data URL，HTTP 请求在容器自身回环接口进行，不产生外网平台流量。Docker 内部网络可能不生成宿主机端口 NAT，因此宿主机绑定以 Compose 展开及 HostConfig 检查；测试没有把 internal 网络测试用配置交付给用户应用（正式 Compose 保留原有外网采集能力）。

## 原有测试基线与构建问题

在未经修改的 `77f8d758` 临时检出中独立复测，原 128 项离线测试为 126 通过、2 失败：`.dockerignore` 含已废弃 web-ui/dist 路径，及批量结果加载返回 UI 附加字段破坏原始记录约定。保留原测试并分别修复：移除无效忽略项；批量加载/导出保持原始记录，分页 UI 查询仍保留显示元数据。最终原测试全部通过，未删除或改弱原测试。

构建过程中 Cloud vfs 存储曾耗尽，导致一次最终重建和相应 Docker 测试失败。之后把源码及前端先组装成小体积 bundle，再一次 COPY 到 Chromium runtime，避免多次复制大运行时；仅按记录的本次两个旧镜像 ID 及对应 cache ID 回收，未执行全局 Docker 清理，最终重建和全套测试成功。

## Cloud 未验证项与限制

- 没有连接 ubuntu24 或 `192.168.9.202`，没有执行生产迁移、修改宿主机服务、防火墙或其他项目。
- 没有真实登录闲鱼、搜索、访问真实卖家或确认真实交易。适配器仅对现有 Fixture 合同验证，线上字段、分页、隐藏已售列表、登录态和平台许可需本地低频核实。
- 没有发送企业微信等真实通知、没有调用真实 LLM API。真实通知投递质量未验证。
- 没有 N100 实机性能与长期稳定性数据，也没有家庭局域网设备验收。当前测试证明本地源码流程、协议边界和数据语义，不证明长期在线采集可达率。
- 仅确认 0/1 平台状态映射；不为未知代码猜测 offline/sold。规则不能确定机型真伪，模糊配置保留 unknown，人工更正有审计。
- 每批公开卖家回访使用一个启用任务的账号；各账号独立可见范围、严格一次外部通知交付、历史永久删除和可选 AI 分类不在本 MVP 中实现。详见功能说明中的边界。

最终交付状态：**MVP 已实现并通过 Cloud 离线/容器/浏览器验证，待真实平台低频适配验收与服务器部署。**
