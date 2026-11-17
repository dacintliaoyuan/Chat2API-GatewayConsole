# Chat2API 网关控制台

本机 UM 网关（8000）与控制台宿主（8001）的生产实现是 Rust；同一套前端按构建期角色产出本机页面 `web/dist`。仓库根就是应用根。

## 快速开始

双击根目录的 `一键部署本机网关.bat`：

1. 检查 Rust / Node 工具链；
2. 构建 `um-gateway` 与 `um-console` release 二进制；
3. 构建本机生产页 `web/dist`；
4. 准备 `data/` 与控制令牌；
5. 启动 8000 与 8001；
6. 创建或修复桌面快捷方式（新快捷方式使用云端图标）；
7. 打开控制台。

重复运行是幂等的：已运行的服务不重启，已有快捷方式不改名字和图标。回滚点仍是 `start_chat2api.bat` / `start_usage_service.bat`：把 `lib/gateway-runtime.ps1` 的 `StartPath` 改回批处理即可。

## 先读

| 要知道 | 读 |
|---|---|
| 项目事实、验证命令、Agent 入口 | [AGENTS.md](AGENTS.md) |
| 领域词汇 | [GLOSSARY.md](GLOSSARY.md) |
| 一套前端两个控制台的决策 | [ADR 0001](docs/adr/0001-one-frontend-two-consoles-build-time-role.md) |
| 分享链边界 | [SHARING.md](SHARING.md) |

## 角色与入口

| 角色 | 常用入口 | 生产页 |
|---|---|---|
| 本机 | `一键部署本机网关.bat` → 部署并打开 8001；`invoke-gateway-service.ps1` 启停 | `web/dist` |

## 文件

- `rust/`：Rust 生产实现：`um-gateway`（8000）、`um-console`（8001）。
- `scripts/`：入口与部署脚本：`deploy-local-gateway.ps1`、`Open-GatewayConsole.ps1`、`检查端口绑定.bat`。
- `start_chat2api.bat`、`start_usage_service.bat`：Python 回滚启动器（Rust 切流后的回滚窗口）。
- `usagesrv.py`、`console/`、`share/`、`model_sync/`：Python 回滚实现与 Rust 本机动作的一次性 sidecar；`usagesrv.py` 不依赖 `app/`。
- `web/`：TypeScript/Vite 前端；`tests/`：Python 回归。
- `um-cloud-deploy/`：云端部署/运维材料。
- 已删除：未上线的 `local/` 组合根与 `app/` 装配（ADR 0009）。

## 验证

在 `web/` 下：

```bash
npm run check:cloud-console
npm run check:um
```

Python 回归（仓库根）：

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Rust（`rust/`）：

```bash
cargo test --workspace
```

构建、候选预览与门禁的完整命令见 [web/README.md](web/README.md)。

## 边界

- 生产构建必须显式声明 `CONSOLE_PRODUCTION_BUILD=1` 与 `CONSOLE_ROLE=local`；本机角色另需 `CONSOLE_LOCAL_SHARE_RETIRED=1`。构建守卫拒绝直接覆盖 `web/dist`。
- 凭据、运行数据与构建产物不入库：`data/`、`profile/`、`tmp/`、`output/`、`chat2api/token.json`、`chat2api/.edge-profile/`、`web/dist*`。
- 分享 Key 明文只在受限交付文件出现；日志、交接与提交只记录存储类别。
- 推送需要维护者明确要求；一次推送授权不视为持续自动发布授权。
