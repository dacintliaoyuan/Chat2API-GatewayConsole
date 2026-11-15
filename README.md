# Chat2API 网关控制台

Chat2API 网关控制台：同一套前端按构建期角色产出本机与云端两个控制台。本机角色管理本机 UM（8000）与控制台宿主（8001）；云端角色管理云端分享 API（8096）。仓库根就是应用根。

## 先读

| 要知道 | 读 |
|---|---|
| 项目事实、验证命令、Agent 入口 | [AGENTS.md](AGENTS.md) |
| 领域词汇 | [GLOSSARY.md](GLOSSARY.md) |
| 一套前端两个控制台的决策 | [ADR 0001](docs/adr/0001-one-frontend-two-consoles-build-time-role.md) |
| 云端部署与运维 | [um-cloud-deploy/README.md](um-cloud-deploy/README.md) |
| 分享链边界 | [SHARING.md](SHARING.md) |

## 角色与入口

| 角色 | 常用入口 | 生产页 |
|---|---|---|
| 本机 | `Open-GatewayConsole.bat` → 8001；`invoke-gateway-service.ps1` 启停 | `web/dist` |
| 云端 | `Open-CloudConsole.bat` → SSH 隧道 → 8096；`um-cloud-deploy/` 安装/更新/回滚 | `web/dist-cloud`（打包为 `web/dist`） |

控制台宿主是 `usagesrv.py`：`UM_SHARE_ROLE=local` 时监听 8001，`cloud` 时监听 8096。前端入口 `web/index.html → web/src/console-entry.ts` 按构建期角色只装入 `web/src/local/local-console.ts` 或 `web/src/cloud/cloud-console.ts`。云端端口在一个发布周期内仍接受显式 `USAGESRV_PORT=18096`（expand-contract），入口脚本可用 `-CloudConsolePort 18096` 指向旧端口。

## 文件

- `usagesrv.py`、`app/`：控制台宿主入口与后端装配；`usagesrv.py` 在 `UM_SHARE_ROLE=cloud` 时只装配云端分享控制台。
- `chat2api/`：UM 网关（8000）与合并后的分享入口（8100）；纯分享 helper 在 `share_auth.py`。
- `console/`：本机管理路由、任务队列、统计、日志与分享动作。
- `share/`：分享存储、管理、运行链、broker 与监督；边界见 [AGENTS.md](AGENTS.md) 的分享链边界。
- `model_sync/`：模型目录与 context probe。
- `local/`、`shared/`：本机组合根与双端纯规则；见各自的 README。
- `web/`：TypeScript/Vite 前端；`tests/`：Python 回归与隔离包。
- `um-cloud-deploy/`：云端部署/运维材料。旧 Windows 本机分享链的部署材料已从仓库删除。

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

构建、候选预览与门禁的完整命令见 [web/README.md](web/README.md)。

## 边界

- 生产构建必须显式声明 `CONSOLE_PRODUCTION_BUILD=1` 与 `CONSOLE_ROLE=local|cloud`；本机角色另需 `CONSOLE_LOCAL_SHARE_RETIRED=1`。构建守卫拒绝直接覆盖 `web/dist`、`web/dist-cloud`。
- 凭据、运行数据与构建产物不入库：`data/`、`profile/`、`tmp/`、`output/`、`chat2api/token.json`、`chat2api/.edge-profile/`、`web/dist*`。
- 分享 Key 明文只在受限交付文件出现；日志、交接与提交只记录存储类别。
- 推送需要维护者明确要求；一次推送授权不视为持续自动发布授权。
