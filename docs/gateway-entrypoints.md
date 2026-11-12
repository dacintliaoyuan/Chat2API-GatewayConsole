# 网关入口与产物归属

本文件记录 ai-gateway 的现行入口、构建产物目的地与历史材料归属。端口以
[PORTS.md](../PORTS.md) 为准，分享链语义见 [SHARING.md](../SHARING.md)，
云端部署步骤见 [um-cloud-deploy/README.md](../um-cloud-deploy/README.md)。

本文件只记录归属和建议。除文档本身外，本次没有移动、删除、部署或重启任何入口、
产物或运行数据；表中的“建议”都须另行授权。

## 1. 本机（local 角色）

| 入口 | 启动/调用 | 目的地 | 说明 |
|------|-----------|--------|------|
| 桌面控制台 | `Open-GatewayConsole.bat` → `Open-GatewayConsole.ps1` | 浏览器打开 `http://127.0.0.1:8001/` | 必要时先启动 8001 宿主；已运行时不重启 |
| 命令行启停 | `invoke-gateway-service.ps1 -Key usage\|chat2api -Action Start\|Stop` | 对应本机服务 | 与控制台共用资源锁；停止会写期望状态，守护不拉起 |
| 控制台 JSON 运行时 | `invoke-console-runtime-json.ps1` | 8001 宿主 | stdout 只输出一个 JSON 对象；控制台任务层调用 |
| 控制台宿主 | `usagesrv.py`（未设置 `UM_SHARE_ROLE` 或为 `local`） | `127.0.0.1:8001`，托管 `web/dist` | 根路径要求 `web/dist/candidate-build.json` 的 `role=local`、`line=production` 与有效 `sourceHash`；缺失、候选行或角色不符返回 500 |
| UM 自用实例 | `start_chat2api.bat` → `chat2api/chat2api.py` | `127.0.0.1:8000` | 唯一的模型网关；不随本任务重启 |
| 分享入口 / 专用后端 | `chat2api/share_gateway.py` / `chat2api.py --port 8110` | `127.0.0.1:8100` / `127.0.0.1:8110` | 旧工作台分享页（`?console=workbench`）启停；Cloudflare Tunnel 只连 8100 |
| 分享控制恢复 | `Recover-ShareControl.bat` → `Recover-ShareControl.ps1` | 8001 与 `umshare-broker`／`umshare-watchdog` | 默认 `-Action Check`；只恢复后台响应，不装配分享存储 |
| 模型同步 | `invoke-model-sync.ps1`、`manage-model-sync-task.ps1` | 每天 06:00 计划任务 `SelfUseAI AI Gateway Model Sync` | 手动/计划/预览/探测；不自动重启客户端 |
| 服务守护 | `lib/register-chat2api-watch.ps1`、`lib/register-share-backend-watch.ps1` | 计划任务 `SelfUseAI AI Gateway Chat2API Watch` 等 | 只拉起确认意外退出的实例，不处理上游错误 |
| 候选预览 | `web/scripts/ensure-candidate-preview.ps1` | `127.0.0.1:8099`，只托管 `web/release/candidate` | 只读：一切非 GET/HEAD 在本地 403 |
| 开发服务 | `web/Open-PrototypeConsole.bat` → `web/scripts/ensure-prototype-server.ps1` | `127.0.0.1:5173` | vite 开发服务；代理可执行管理操作，须明确目标后端 |
| 端口审计 | `check_port_bindings.py` | 只读监听快照 | 对照 PORTS.md 报告治理段内未登记监听 |
| 私网入口 | Tailscale HTTPS Serve | `https://<node>.<tailnet>.ts.net/` → `127.0.0.1:8001` | 精确来源/登录身份/设备地址校验；不使用 Funnel |

## 2. 云端（cloud 角色）

| 入口 | 启动/调用 | 目的地 | 说明 |
|------|-----------|--------|------|
| 桌面入口 | `Open-CloudConsole.bat` → `Open-CloudConsole.ps1` | Edge 打开 `http://127.0.0.1:18096/#overview` | 经专用 SSH 配置建 loopback 隧道；不读写密钥、不启动 Tailscale |
| 控制台宿主 | `usagesrv.py`（`UM_SHARE_ROLE=cloud`） | `127.0.0.1:18096`，托管发布包内 `web/dist` | 构建身份必须 `role=cloud`、`line=production` 与有效 `sourceHash`；角色/候选行不符返回 500 |
| 分享入口 | systemd `um-share-broker.service` → `share_broker_ops.py` → `share_gateway.py` | `127.0.0.1:8100` | broker 是唯一 8100 启动者；控制台的 `share.start/stop/estop/retry_stop` 为 `enabled=False` |
| 分享后端 | systemd `um-share-console.service` → `chat2api.py` | `127.0.0.1:8110` | 按需启动；由控制台 `share.backend_login/restart` 管理 |
| 公网 API | Cloudflare Tunnel | `https://dacintapi.de5.net/v1` 只反代 `/v1/models`、`/v1/chat/completions`、`/v1/messages`、`/v1/responses` 到 `127.0.0.1:8100` | 入口配置由 Cloudflare 侧维护；`Caddyfile.example` 只是历史回退样例；不反代 18096/5901 |
| 私网控制台 | Tailscale Serve | `https://<node>.<tailnet>.ts.net/` → `127.0.0.1:18096` | `display/configure-tailscale-serve.sh`；不使用 Funnel |
| 显示/VNC | `um-share-display.service` + `display/` | `:99`、`127.0.0.1:5901`、同源 `/display/` | 只 loopback；人工登录窗口 |
| 安装/升级/回滚 | `um-cloud-deploy/install.sh`、`update.sh`、`rollback.sh` | `/opt/um-share/current` | 运行数据在 `/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share`，不在 release 内 |
| 打包/清单/校验 | `package-release.sh`、`make-manifest.py`、`verify-release.ps1` | `output/um-share-release*/` | 本机生成；不写生产 `dist/`、不执行真实部署 |
| Linux 隔离检查 | `run-linux-checks.sh` | WSL/Linux 临时端口 28100/28110/28096/28999 | 仅 Linux；Windows 上报告 SKIP |
| Windows/WSL 引导 | `bootstrap-wsl.sh` | 仅 WSL 开发环境 | 不是云机安装器；云端只用 `install.sh` |

### 2.1 排障读面（只读）

云机上按这个顺序读，都是只读：

| 读什么 | 命令 | 判据 |
|--------|------|------|
| 控制台状态 | `curl -s http://127.0.0.1:18096/api/console/share-management/status` | `gateway_running` **缺失 = 未确认**（不是 `false`）；`share_url_source=configured` 才说明公网地址已配 |
| 登录/显示状态 | `curl -s http://127.0.0.1:18096/api/console/share-management/display-state` | `state=not_started/logging_in/valid`；`detail` 带 `manual_takeover:<reason>` 说明自动化已交回人工 |
| 登录窗口可用性 | `curl -s http://127.0.0.1:18096/api/console/share-management/display-session` | 裸回环请求返回 `display_authorization_required` 是**设计如此**，不是故障 |
| broker 状态 | `sudo -u um-share /opt/um-share/bin/um-share-brokerctl.py status` | **必须以 `um-share` 跑**；root 读 0600 的 `broker-control.key` 会报 `control_key_unusable` |
| 8110 生命周期 | `cat /var/lib/um-share-console/share-backend/backend-lifecycle.log` | 最后一行 `started ... by=console` 才是 8110 当前实例 |
| 停止意图 | `cat /run/um-share/share-host-desired.json` | `state=stopped, reason=manual` 时普通 start 被拒；只有 `um-share-brokerctl.py start-explicit` 能清意图 |
| 三个 unit | `journalctl -u um-share-broker -u um-share-console -u um-share-display --since "-30 min"` | 排空、8110 子进程、显示连接都在这里 |
| 端口 | `ss -ltn \| grep -E ':(5901\|8100\|8110\|18096)'` | 8110 不在听 = 专用后端没起；5901 是 VNC，不是 HTTP，别用 `curl http://` 判它 |

## 3. 共用实现（两侧同一份源码）

| 实现 | 位置 | 本机用途 | 云端用途 |
|------|------|----------|----------|
| 部署角色与端口 | `usagesrv.py` 的 `UM_SHARE_ROLE`／`UM_SHARE_DEPLOYMENT`、`USAGESRV_PORT` | 默认 `local`，8001 | 显式 `cloud`，默认 18096 |
| 前端入口 | `web/index.html` → `web/src/console-entry.ts` | 装入 `src/local/local-console.ts` | 装入 `src/cloud/cloud-console.ts` |
| 前端外壳与契约 | `web/src/shell/`、`web/src/console/backend/`、`web/src/console/role-gate.ts` | 两侧共用 | 两侧共用 |
| 分享 Python 核心 | `share_*.py`、`chat2api/`、`console_share_*.py` | Windows 装配 | cloud 角色只装配 UM 分享动作 |
| 统一验证 | `verify.ps1`、`ai-gateway/tests/` | Python/PowerShell 回归 | Linux 链走 `run-linux-checks.sh` |

## 4. 构建产物与目的地

| 产物 | 生成入口（在 `web/` 下） | 目的地 | 消费端 |
|------|--------------------------|--------|--------|
| 本机生产页 | `CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=local CONSOLE_LOCAL_SHARE_RETIRED=1 npm run build` | `web/dist/` | 8001 `usagesrv.py`；角色不符时拒绝托管 |
| 云端生产页 | `CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud npm run build` | `web/dist-cloud/` | `package-release.sh --web-dist web/dist-cloud --web-role cloud` → 包内 `web/dist` → 云端 18096 |
| 本机角色候选 | `npx vite build --mode candidate-local` | `web/release/candidate/local/` | 手动检查；控制台门禁 `--role local` |
| 云端角色候选 | `npx vite build --mode candidate-cloud` | `web/release/candidate/cloud/` | 云端控制台门禁；打包前预览 |
| 旧默认候选 | `npx vite build --mode candidate` | `web/release/candidate/` | 8099 只读预览与日志门禁；重建不清空根目录，保留 `local/`、`cloud/` 子目录 |
| 构建身份 | 每次构建自动写入 | 各产物根 `candidate-build.json` | `usagesrv.py` 校验 `role`/`line`/`sourceHash`；`make-manifest.py`／`package-release.sh` 校验 `--web-role` |
| 云端发布包 | `um-cloud-deploy/package-release.sh` | 默认 `<source>/output/um-share-release/` 的 `um-share-<version>.tar.gz` + `.sha256` | 云端 `install.sh`／`update.sh` |

`web/dist`、`web/dist-cloud`、`web/release/` 都在 `web/.gitignore` 中；`ai-gateway/output/`
在 `SelfUseAI/.gitignore` 中。`npm run build` 单独运行会被输出守卫拒绝，不会默认覆盖
`dist/`；`CONSOLE_LOCAL_SHARE_RETIRED=1` 只声明云端已接管分享管理，不改变页面权限。

## 5. 历史材料归属

| 路径 | 身份 | 现行消费端 | 处置 |
|------|------|------------|------|
| `um-cloud-deploy/` | 现行云端发布/运维材料 | `RELEASE-MANIFEST.json`、`package-release.sh`、`install.sh`／`update.sh`／`rollback.sh`、`verify-release.ps1`、`run-linux-checks.sh` | 保留；当前 manifest 为 `s2-20261107a` |
| `um-share-deploy/` | 旧 Windows 本机分享链的运维/恢复材料 | `SHARING.md`、`chat2api/SHARING.md`、`tests/install-share-broker.Tests.ps1`、`tests/d4-assembly.Tests.ps1`、`tests/repair-limits.Tests.ps1`、`tests/run-d4-experiment.Tests.ps1`、`tests/watchdog-*.Tests.ps1`、`tests/test_d4_*.py`、`tests/test_share_*.py`、`tests/test_console_share_production.py`、`share_broker_ops.py` 的 `repair-limits.ps1` | 保留；仍有恢复说明和测试消费者，不整目录删除 |
| `output/` | 本机构建/诊断输出 | 当前 `output/um-share-release-r17/um-share-s2-20261105a.tar.gz` 是新的云端候选包；源 `RELEASE-MANIFEST.json` 的 `web_dist.files` 指向 `web/dist-cloud` | 其余按 §6 处置 |
| `tmp/` | 本机诊断、提取与日志 | `tmp/r16-hotfix-1/display-full-canvas.png` 被 r17 交接引用；`tmp/s2-20261103e/`、`tmp/um-cloud-*/` 被归档交接引用 | 其余按 §6 处置 |
| `um-share-deploy/logs/` | 部署/看门狗诊断日志 | `SHARING.md` 的部署/排障说明引用 `deploy-*.log` 与 `canary-*.json` | 保留最近一轮用于追溯；其余可清理 |

## 6. 无消费者、无独有恢复价值的清单

判定口径：在 Git 跟踪文件、`verify.ps1`、`tests/`、`um-cloud-deploy/`、`SHARING.md`
和现行交接中都搜不到消费端（§5 明确保留的项除外），且能从源码或 Git 历史重建。
以下项目本次**没有**移动或删除。

| 路径 | 内容与引用 | 消费端 | 唯一恢复价值 | 移动映射 | 恢复方法 |
|------|------------|--------|----------------|----------|----------|
| `output/ai-gw-round18-20260929/`（2 个已跟踪 .md） | round 18 退出记录/监督候选设计；无引用 | 无 | 无（设计已被后续实现取代） | 归档到 `docs/proposals/archive/ai-gateway-round18-20260929/`（跨模块，须单独授权） | `git show ac622902:SelfUseAI/ai-gateway/output/ai-gw-round18-20260929/<file>` |
| `output/s2-20261103c/` | 2026-11-03 云端候选包、web-dist 与检查脚本；无引用 | 无 | 无（已被 `s2-20261105a` 取代） | 删除（`output/` 不入库） | 按包内 `web-dist/candidate-build.json` 的 `gitHead=54cbf45b` 重建 |
| `output/um-share-release-r16/um-share-s2-20261104a..g.tar.gz` + `.sha256` | 2026-11-04 的中间候选包；a–h 均已被 `s2-20261105a` 取代；a–g 的 `web/dist/candidate-build.json` 均为 `candidate-cloud`／`gitHead=0d6ff49` | 无 | 无（被 `s2-20261105a` 取代） | 删除 a–g | 按 `gitHead=0d6ff49213e3f2d794ef79648c2e8b01b38fea3f` 重建（a–e 的源码哈希不同，只能重建近似候选） |
| `output/um-share-release-r17/um-share-s2-20261105a.tar.gz` + `.sha256` | 当前本地候选包；包含 r17 修复 | 现行交接 | 有（尚未部署的唯一候选包） | 保留 | 本机已有 |
| `output/um-share-release-r16/web-prod-cloud-h/` | 旧 r16 h 候选的 web 源树 | 历史交接 | 无（新 manifest 已改指 `web/dist-cloud`） | 删除 | 从旧 h 包重解压 |
| `tmp/r14-private/`、`tmp/r14-review/`、`tmp/r14-rework/` | 旧轮次提取、坏包与验证脚本；无外部引用 | 无 | 无（旧轮次已被取代） | 删除（`ai-gateway/tmp/` 不入库） | 从对应 tarball 重解压，或按 `candidate-build.json` 的 `gitHead` 重建 |
| `tmp/s2-20261103d/`、`tmp/s2-20261103e/` | 旧轮次提取与验证输出；`s2-20261103e` 被归档交接全文快照引用 | 归档交接（历史） | 无（已被 `s2-20261105a` 取代） | 先更新归档引用，再删除 | 从 `output/` 中对应 tarball 重解压 |
| `tmp/r16-verify/` | 2026-11-04b 候选提取；无引用 | 无 | 无（被 `s2-20261105a` 取代） | 删除 | 从 `output/um-share-release-r16/um-share-s2-20261104b.tar.gz` 重解压 |
| `tmp/r16-hotfix-1/` | 显示端到端截图与临时 `console_display_proxy.cloud.py`；r17 交接第 178 行引用 `display-full-canvas.png` | 现行交接（显示证据） | 有（真实显示截图） | 保留 | 不可精确重建；按需另存到稳定证据目录后再改引用 |
| `tmp/um-cloud-console-handoff-20261104/`、`tmp/um-cloud-joint-20261101/` | 复现输出与 sweep JSON；被归档交接全文快照引用 | 归档交接（历史） | 有（当时复现输出） | 先更新归档引用，再删除 | 原始 JSON/日志保留在归档交接中 |
| `tmp/inst-logs/`、`tmp/inst-logs2/`、`tmp/inst-logs3/`、`tmp/inst-logs4/` | 2026-11-01 安装隔离日志；无引用 | 无 | 无 | 删除 | 重跑安装/隔离测试生成 |
| `tmp/commit-final*.log`、`tmp/full-python-regression-20261101-final.log`、`tmp/regression-first-failure*.log`、`tmp/fake-um-server-test.log`、`tmp/r16-chat2api-reauth.log` | 测试与提交日志；无引用 | 无 | 无 | 删除 | 重跑对应测试生成 |
| `tmp/r16-manifest-now.json` | manifest 快照；无引用 | 无 | 无 | 删除 | `python um-cloud-deploy/make-manifest.py ...` 重新生成 |
| `tmp/r16-a5-readiness.md` | 2026-11-04 加载前门槛记录；无外部引用 | 无 | 无（已被 r17 交接取代） | 删除 | 内容来自 r17 交接与当时只读读数 |
| `tmp/r16-display-diag/` | RFB 帧与 noVNC 探针；无引用 | 无 | 无（一次性诊断） | 删除 | 重跑 `test_greeting_frames.py`／`ws_probe.py` |
| `tmp/um-share-backup.service` | 临时 unit 副本；测试只引用 `backup_restore.systemd_units()` 生成的同名 unit | 无 | 无（可由代码生成） | 删除 | 由 `backup_restore.systemd_units()` 重新生成 |
| `um-share-deploy/run-broker.cmd` | 旧固定路径 broker 包装器；`install-share-broker.ps1` 从参数生成新的 `run-broker.cmd`，`deploy-elevated.ps1:824` 读的是运行目录副本 | 无（只消费运行目录生成物） | 无（旧包装器含过期 `--share-task`，不能重放） | 无移动；`git rm`（须授权），Git 历史保留 | `git show HEAD:SelfUseAI/ai-gateway/um-share-deploy/run-broker.cmd` |
| `um-share-deploy/uac-probe.ps1` | 一次性 UAC 探针；0 引用 | 无 | 无 | 无移动；`git rm`（须授权），Git 历史保留 | `git show HEAD:SelfUseAI/ai-gateway/um-share-deploy/uac-probe.ps1` |
| `um-share-deploy/key_lifecycle_20260906.py` | 自声明 SUPERSEDED；仅 `verify_8100.py` 注释提及 | 无 | 无 | 无移动；`git rm`（须授权） | `git show HEAD:SelfUseAI/ai-gateway/um-share-deploy/key_lifecycle_20260906.py`；现行场景在 `share_admin.py` 与 `tests/test_share_management*.py` |
| `um-share-deploy/tcp_hold.py` | 一次性 TCP 监听测试缝；仅自引用 | 无 | 无 | 无移动；`git rm`（须授权） | `git show HEAD:SelfUseAI/ai-gateway/um-share-deploy/tcp_hold.py` |
| `um-share-deploy/maintenance-now-elevated.ps1`、`revoke2-elevated.ps1`、`redeploy-elevated.ps1` | 一次性历史动作；仅自引用；归档交接只警告“旧 wrapper 不能盲重放” | 无 | 无（动作已完成，配置已无对应对象） | 无移动；`git rm`（须授权），Git 历史保留 | `git show HEAD:SelfUseAI/ai-gateway/um-share-deploy/<file>` |

不在此清单、必须保留的旧 Windows 入口：`deploy-elevated.ps1`、`watchdog-umshare.ps1`、
`wfp_helper.ps1`、`install-share-broker*.ps1`、`run-deploy-guarded.ps1`、
`share_guard.py`、`canary_um.py`、`escape_probe.py`、`repair-limits.ps1`、`verify_8100.py`、
`verify_redeploy*.py`、`verify_revoke2.py`、`test_stability.py`、`verify-scenarios-elevated.ps1`、
`verify-scope-live-elevated.ps1`、`udp_receiver.py`、`udp_send.py`、`d4-*.ps1`、`run-d4-experiment.ps1`。

## 7. 剩余已知不一致（本次未改）

- `web/src/console/role-gate.ts` 的 `LOCAL_ONLY_ACTION_PREFIXES` 仍列出 `qoder.`／`cliproxy.`／
  `model.`；退役来源已删除，云端注册表也不再装配它们。角色判定不因此放宽，本次未改代码。
- `chat2api/SHARING.md` 第 88 行仍引用已清理的 `tmp/p0-umshare/`；本机分享链的现行入口是
  `um-share-deploy/` 与 `SHARING.md`，该句只作历史。本次未改 `chat2api/SHARING.md`。
