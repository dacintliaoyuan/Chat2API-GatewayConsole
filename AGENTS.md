# AGENTS.md

`Chat2API-GatewayConsole` 的项目级入口。本文件只写本项目特有的事实和导航。

## 1. 项目特有事实

- **仓库根就是应用根**：`usagesrv.py`、`check_port_bindings.py`、`chat2api/`、`console/`、`share/`、`model_sync/`、`app/`、`web/`、`tests/`、`um-cloud-deploy/` 都在根级，没有上一级目录。根级模块按域收进包：控制台路由在 `console/`，分享链在 `share/`，模型目录在 `model_sync/`，后端装配在 `app/`。
- **两个角色**：`UM_SHARE_ROLE=local`（本机 8001，Windows）与 `cloud`（云端 18096，Linux）；前端按构建期角色装入 `web/src/local/local-console.ts` 或 `web/src/cloud/cloud-console.ts`（ADR 0001）。
- **生产目录**：`web/dist` 是本机生产页，`web/dist-cloud` 是云端生产页（打包为 `web/dist`）；两者都在 `.gitignore` 里，构建产物不入库。
- **凭据与运行数据不入库**：`data/`、`profile/`、`tmp/`、`output/`、`chat2api/token.json`、`chat2api/.edge-profile/` 都由根 `.gitignore` 覆盖。
- **验证**：`web/` 下 `npm run check:cloud-console`、`npm run check:um`；Python 回归 `python -m unittest discover -s tests -p "test_*.py"`。
- **契约夹具**：改 `usagesrv.py`/`console/adapters.py`/`console/model_actions.py`/`share/metering.py`/`console/stats.py`/`console/log_reader.py` 后运行 `python tests/generate_frontend_contract_fixture.py`；`check-generated` 会因夹具过期失败。
- **implement-spec 工作流**：`CreateWorkflow` 用 `saved: { name: "feature-retirement-cleanup" }` 运行本项目的 implement-spec 编排；脚本已保存为 project workflow，不重写。

## 2. 导航

| 要做什么 | 读 |
|---|---|
| 领域词汇 | [GLOSSARY.md](GLOSSARY.md) |
| 云端部署与运维 | [um-cloud-deploy/README.md](um-cloud-deploy/README.md) |
| 旧 Windows 分享链（已退役） | Git 历史；当前云端见 [um-cloud-deploy/README.md](um-cloud-deploy/README.md) |
| 分享链旧/新名字与边界 | [docs/share-chain-map.md](docs/share-chain-map.md) |
| 提交前检查 | [scripts/check.sh](scripts/check.sh)；首次运行 `bash scripts/install-hooks.sh` |
| Review 判断规则 | [CODING_STANDARDS.md](CODING_STANDARDS.md) |
| 操作 issue | [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md) |
| 查标签含义 | [docs/agents/triage-labels.md](docs/agents/triage-labels.md) |
| 领域文档消费规则 | [docs/agents/domain.md](docs/agents/domain.md) |

## 3. 端口与入口

| 端口 | 服务 | 绑定 |
|---|---|---|
| 8000 | chat2api（UM） | 127.0.0.1 |
| 8001 | usagesrv 本机控制台宿主 | 127.0.0.1 |
| 8100 | 分享链 um-share 网关 | 127.0.0.1 |
| 18096 | usagesrv 云端控制台宿主 | 127.0.0.1 |
| 5901 | display x11vnc（按需） | 127.0.0.1 |
| 5173 | vite 开发服务 | 127.0.0.1 |
| 8099 | 候选预览（vite preview） | 127.0.0.1 |

`8000-8009` 是会话适配段，`8090-8099` 是控制台/管理段，`8100-8119` 是分享段（唯一允许非 loopback 的段，偏离须登记），`8300-8399` 是协议网关段。`5901` 是 display 的按需监听，落在治理段外，不参与端口审计；迁移或新增时同步本表与 `console/display_proxy.py`。新增服务先选段内未占用端口；迁移同步更新本表、服务配置、消费者、测试与文档。`check_port_bindings.py` 对照本表审计治理段内未登记监听。

## 4. 分享链边界

- 8100 合并进程只由 broker 拉起：`um-share-broker.service` 的 `ExecStartPost` 调用 `um-share-brokerctl.py start`；旧 Windows 本机分享动作已退役、不再注册，`share.gateway_restart` 走 broker restart。云端不安装第二套 watchdog。
- 18096 只监听 loopback；公网入口只反代分享白名单到 8100，不反代 18096/5901。
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
