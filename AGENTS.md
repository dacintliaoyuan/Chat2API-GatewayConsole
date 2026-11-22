# AGENTS.md

`Chat2API-GatewayConsole` 的项目级入口。本文件只写本项目特有的事实和导航。

## 1. 项目特有事实

- **仓库根就是应用根**：`usagesrv.py`、`check_port_bindings.py`、`chat2api/`、`console/`、`share/`、`model_sync/`、`web/`、`tests/`、`um-cloud-deploy/` 都在根级，没有上一级目录。根级模块按域收进包：控制台路由在 `console/`，分享链在 `share/`，模型目录在 `model_sync/`。`usagesrv.py` 不 import `app`；未上线的 `local/` 组合根与 `app/` 装配已删除（ADR 0009）。
- **桌面 App 壳**：`desktop/` 是独立 Slint crate（`[workspace]`，不在 `rust/` workspace 内，避免 Slint MSRV 抬高主工作区），二进制 `um-desktop`；#123 做窗口/托盘/单实例与首次打开跳过登录，#124 监督本实例网关，#126 实时读取本实例服务日志并断线补读；保存 `%LOCALAPPDATA%\Chat2API\desktop.json`；不登录（#125）、不做安装包（#127）。
- **两个角色**：`UM_SHARE_ROLE=local`（本机 8001，Windows）与 `cloud`（云端 8002，Linux；Python 回滚 8096）；前端按构建期角色装入 `web/src/local/local-console.ts` 或 `web/src/cloud/cloud-console.ts`（ADR 0001）。
- **生产目录**：`web/dist` 是本机生产页，`web/dist-cloud` 是云端生产页（打包为 `web/dist`）；两者都在 `.gitignore` 里，构建产物不入库。
- **凭据与运行数据不入库**：`data/`、`profile/`、`tmp/`、`output/`、`chat2api/token.json`、`chat2api/.edge-profile/` 都由根 `.gitignore` 覆盖。
- **验证**：`web/` 下 `npm run check:cloud-console`、`npm run check:um`；Python 回归 `python -m unittest discover -s tests -p "test_*.py"`。Rust 全量测试用 `bash scripts/check-rust.sh`（串行，避开既有 login flake），真实 Edge 回归用 `bash scripts/check-rust.sh --edge`（`#[ignore]`，隔离 profile + loopback 夹具，不访问真实 UM）；`scripts/check.sh` 在缺 `web/node_modules` 时先 `npm ci`；`web/package.json` 的 `esbuild` 是前端契约门禁的 devDependency。两者只服务门禁环境，不改变产品行为。
- **契约夹具**：改 `usagesrv.py`/`console/adapters.py`/`console/model_actions.py`/`share/metering.py`/`console/stats.py`/`console/log_reader.py` 后运行 `python tests/generate_frontend_contract_fixture.py`；`check-generated` 会因夹具过期失败。
- **implement-spec 分派**：implement-spec 用 `Agent` 直接分派 subagent，不用 `CreateWorkflow`/`AmendWorkflow`/`ResumeWorkflowRun`。每张票一个独立 worktree/branch，先读 `tdd-mattpocock`；合并、评审、切流也用 subagent。

## 2. 导航

| 要做什么 | 读 |
|---|---|
| 领域词汇 | [GLOSSARY.md](GLOSSARY.md) |
| UM 登录 / headless / 人工接管 | [ADR 0002](docs/adr/0002-headless-first-um-login.md)；[验收记录](docs/headless-login-acceptance.md) |
| 本机登录现场证据（哪个驱动 / 拒绝文本） | [um-login-session-persistence.md](docs/um-login-session-persistence.md) §运行时证据 |
| 云端部署与运维 | [um-cloud-deploy/README.md](um-cloud-deploy/README.md)；SSH `ssh -F ~/.ssh/aliyun-um-ecs.conf um-share-ecs`（别名不在默认 config） |
| 云端 onboarding（仅受信任的同事；agent 读取后逐步教学） | [docs/agents/cloud-onboarding.md](docs/agents/cloud-onboarding.md) |
| 旧 Windows 分享链（已退役） | Git 历史；当前云端见 [um-cloud-deploy/README.md](um-cloud-deploy/README.md) |
| 分享链旧/新名字与边界 | [docs/share-chain-map.md](docs/share-chain-map.md) |
| 本机/云端进程监督边界 | [ADR 0013](docs/adr/0013-process-supervision-boundary.md) |
| 桌面 App 构建/预览/安全重启 | [docs/agents/desktop-preview.md](docs/agents/desktop-preview.md)；[desktop/README.md](desktop/README.md) |
| 桌面 UI 评审偏好与评分口径 | [docs/agents/desktop-review.md](docs/agents/desktop-review.md) |
| 当前前端 worktree 路径 | `git worktree list`；`integration/desktop-122` 在 `.worktrees/122-acceptance`（`.worktrees` 是旧目录，不要用） |
| 提交前检查 | [scripts/check.sh](scripts/check.sh)；首次运行 `bash scripts/install-hooks.sh` |
| Review 判断规则 | [CODING_STANDARDS.md](CODING_STANDARDS.md) |
| 操作 issue | [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md) |
| 查标签含义 | [docs/agents/triage-labels.md](docs/agents/triage-labels.md) |
| 领域文档消费规则 | [docs/agents/domain.md](docs/agents/domain.md) |

## 3. 端口与入口

| 端口 | 角色 | 代码默认 | Rust 目标 | 绑定 |
|---|---|---|---|---|
| 8000 | chat2api（UM；Rust） | 8000 | 8000 | 127.0.0.1 |
| 8001 | um-console 本机控制台（Rust；Python 回滚） | 8001（Python 回滚） | 8001 | 127.0.0.1 |
| 8002 | usagesrv 云端控制台（Rust；Python 回滚 8096） | 8096（Python 回滚） | 8002 | 127.0.0.1 |
| 8003 | chat2api 合并网关（um-share） | 8100 | 8003 | 127.0.0.1 |
| 8097 | display x11vnc（按需） | 8097 | 8097 | 127.0.0.1 |
| 8099 | 候选预览（vite preview） | 8099 | 8099 | 127.0.0.1 |
| 5173 | vite 开发服务 | 5173 | 5173 | 127.0.0.1 |

`8000-8009` 是网关/控制台配对段：8000/8001 本机对，8002/8003 云端对，8004–8009 预留。`7998/7999` 是桌面 App 的测试/本实例回环端口，不属于该配对段；网关 production 已显式允许这两个 local 端口，现有 8000/8001 保留。`8090-8099` 是控制台/管理段，`8100-8119` 是分享段（唯一允许非 loopback 的段，偏离须登记），`8300-8399` 是协议网关段。

本机 8000 已切到 Rust（`um-gateway-candidate.exe --mode production --port 8000 --role local`）：`lib/gateway-runtime.ps1` 的 `StartPath` 指向 Rust 二进制，`/health` 与 `/admin/shutdown` 契约不变。回滚点改为 `tmp/python-rollback-<date>.tar.gz` 离线包（Python 闭包已退役，#90）；parity、切流与回滚演练记录见 [docs/rust-contract.md](docs/rust-contract.md) §2.4。

本机 8001 已切到 Rust（`um-console-candidate.exe --mode production --bind-port 8001 --role local`）：`lib/gateway-runtime.ps1` 的 `usage` 描述符 `StartPath` 指向 Rust 二进制，`--role local` 应用本机默认路径（不需要 `UM_SHARE_ROLE` 环境覆盖），`/health` 与 `/` 契约不变。回滚点改为 `tmp/python-rollback-<date>.tar.gz` 离线包（Python 闭包已退役，#90）；parity、切流与回滚演练记录见 [docs/rust-contract.md](docs/rust-contract.md) §2.5。

`um-cloud-deploy` 的 `RELEASE-MANIFEST.json` 用 `runtime: python|rust` 冻结发布运行时：Rust release 安装 `um-share-gateway.service`（8003）与 8002 console unit，Python release 已冻结（#90），不再从仓库构建；回滚目标是预构建的 `s2-20261117b`。2026-11-17 云端 8002 已切 Rust（`s2-20261117b`）；Python 回滚单元是 `um-share-console-python.service`（8096），回滚目标 release 是 `s2-20261116t`（Rust 8003 + Python 8002）。**当前/回滚端点以 [docs/rust-contract.md §2.1 的表](docs/rust-contract.md) 为准**，本节不再手写第二份组合。`package-release.sh --runtime rust --rust-bin <um-gateway>` 打包 Rust 二进制；`install.sh`/`update.sh`/`rollback.sh` 按 manifest 选择 unit 与健康门端口，`rollback-drill.sh` 保留为工具入口，2026-11-19 决定不做实机演练。

**三态**：表中「代码默认」是当前 Python 进程的默认监听；线上实际监听以 `ss -ltnp` 为准，可能仍是上一个 release 的默认值；「Rust 目标」在 Rust 重写时一次性生效。旧 `18096`/`5901` 是云端控制台与 display 的旧端口，当前 release 仍允许显式配置（`USAGESRV_PORT=18096`、`CONSOLE_DISPLAY_VNC_PORT=5901`），审计记为 `LEGACY-ALIAS`，同一时刻只监听配置指定的一个端口；Rust 切换时旧 `8100`/`8096`/`18096`/`5901` 不保留兼容别名。

目标契约与 unit/CLI 改名（`um-share-broker.service` → `um-share-gateway.service`，`um-share-brokerctl.py` → `um-share-gatewayctl.py`）的决策记录见 [ADR 0001](docs/adr/0001-one-frontend-two-consoles-build-time-role.md) §端口配对；broker 仍是内部拉起者，不新增第二个进程。

迁移或新增时同步本表与 `console/display_proxy.py`；新增服务先选段内未占用端口。`check_port_bindings.py` 对照“代码默认”列审计治理段内未登记监听。

## 4. 分享链边界

- 8100 合并进程只由 broker 拉起：`um-share-broker.service` 的 `ExecStartPost` 调用 `um-share-brokerctl.py start`；旧 Windows 本机分享动作已退役、不再注册，`share.gateway_restart` 走 broker restart。云端不安装第二套 watchdog。
- 8096 只监听 loopback；公网入口只反代分享白名单到 8100，不反代 8096/8097（旧 18096/5901 同理；Rust 目标 8002/8003 见 §3）。
- 分享 Key 鉴权、每 Key 并发准入与撤销在 8100 进程内完成；撤销 Key 会关闭该 Key 的在途请求。归因直接使用鉴权后的 `key_id`，不再有跨进程 HMAC 标记。
- 运行数据在 `/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share`，不在 release 内；预算保护与自动备份已从默认装配移除，恢复只走显式 `backup_restore.py`。
- 旧 Windows 分享链已退役；当前云端分享只走 `um-cloud-deploy/` 与 `chat2api/chat2api.py`。

## 5. 许可

第三方代码与字体的许可证保留在原目录：`chat2api/LICENSE`、`web/LICENSE`、`web/public/fonts/`、`web/public/licenses/`；来源说明见各目录的 README。

## Agent skills

### Issue tracker

本仓库的 issue 与 spec 使用 GitHub Issues（origin：`dacintliaoyuan/Chat2API-GatewayConsole`），通过 `gh` CLI 读写。见 `docs/agents/issue-tracker.md`。

### Triage labels

五个处理状态使用默认名称：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文：根 `GLOSSARY.md` + `docs/adr/`。见 `docs/agents/domain.md`。
