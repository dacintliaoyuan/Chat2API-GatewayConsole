# AGENTS.md

`Chat2API-GatewayConsole` 的项目级入口。本文件只写本项目特有的事实和导航。

## 1. 项目特有事实

- **仓库根就是应用根**：`usagesrv.py`、`chat2api/`、`web/`、`tests/`、`um-cloud-deploy/`、`um-share-deploy/` 都在根级，没有上一级目录。
- **两个角色**：`UM_SHARE_ROLE=local`（本机 8001，Windows）与 `cloud`（云端 18096，Linux）；前端按构建期角色装入 `web/src/local/local-console.ts` 或 `web/src/cloud/cloud-console.ts`（ADR 0001）。
- **生产目录**：`web/dist` 是本机生产页，`web/dist-cloud` 是云端生产页（打包为 `web/dist`）；两者都在 `.gitignore` 里，构建产物不入库。
- **凭据与运行数据不入库**：`data/`、`profile/`、`tmp/`、`output/`、`chat2api/token.json`、`chat2api/.edge-profile/` 都由根 `.gitignore` 覆盖。
- **验证**：`web/` 下 `npm run check:cloud-console`、`npm run check:um`；Python 回归 `python -m unittest discover -s tests -p "test_*.py"`。

## 2. 导航

| 要做什么 | 读 |
|---|---|
| 架构、角色、产物与构建守卫 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 领域词汇 | [GLOSSARY.md](GLOSSARY.md) |
| 端口分配与入口归属 | [PORTS.md](PORTS.md)、[docs/gateway-entrypoints.md](docs/gateway-entrypoints.md) |
| 分享链边界与运维 | [SHARING.md](SHARING.md)、[um-cloud-deploy/README.md](um-cloud-deploy/README.md) |
| 操作 issue | [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md) |
| 查标签含义 | [docs/agents/triage-labels.md](docs/agents/triage-labels.md) |
| 领域文档消费规则 | [docs/agents/domain.md](docs/agents/domain.md) |

## Agent skills

### Issue tracker

本仓库的 issue 与 spec 使用 GitHub Issues（origin：`dacintliaoyuan/Chat2API-GatewayConsole`），通过 `gh` CLI 读写。见 `docs/agents/issue-tracker.md`。

### Triage labels

五个处理状态使用默认名称：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文：根 `GLOSSARY.md` + `docs/adr/`。见 `docs/agents/domain.md`。
