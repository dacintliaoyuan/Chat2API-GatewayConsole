# AI 网关控制台

本机仅保留 chat2api（UM，8000）、必要管理配套（8001 控制台宿主）与云端分享链。提供账号认证、模型同步、运行日志、Token 与参考费用，以及独立分享链管理。Qoder 国内/国际、Gemini CLIProxy、M365 与旧 DSH web 已退役，历史账本与日志保留。

| 服务 | 端口 | 位置 | 说明 |
|------|------|------|------|
| chat2api（UM） | 8000 | 本目录 `chat2api/`（源码 + token.json + .edge-profile，凭据不入库） | 把 Open WebUI 实例（UM）包装成 OpenAI 兼容 API；单实例直连客户端（ZCode `local:openwebui`），不再作为 M365 内部上游 |
| 控制台后台 (usagesrv) | 8001 | usagesrv.py + web/dist/ | 管理 API、统计与当前控制台页面；后台宿主不在服务明细里重复显示费用服务 |

Qoder 国内/国际、Gemini CLIProxy、M365 及两套旧 Qoder 源码快照已从主工作区删除，对应历史验证目标一并移除。历史源码可通过 Git 查阅；退役账本、运行日志与 qoder-native 参考克隆曾备份到仓库外 `D:/local-backups/SelfUseAI-retired-20261102/`，本机当前未找到该目录，恢复材料须另行核对，不随工作区保留。退役管理动作、专用测试、配置样例与快捷启动已移除；启停仍按退役身份拒绝。云端分享链以 `um-cloud-deploy/` 为准；`um-share-deploy/` 是旧 Windows 本机分享链的运维/恢复入口，保留到旧链退役。

## 入口与产物

按本机、云端和共用实现分开；完整入口、产物目的地与历史归属见 [docs/gateway-entrypoints.md](docs/gateway-entrypoints.md)。

| 角色 | 常用入口 | 生产产物 |
|------|----------|----------|
| 本机 | `Open-GatewayConsole.bat`（桌面）→ 8001；`invoke-gateway-service.ps1`（启停）；`Recover-ShareControl.bat`（分享恢复） | `web/dist`（local 生产页，8001 托管） |
| 云端 | `Open-CloudConsole.bat`（SSH 隧道 → 18096）；`um-cloud-deploy/` 的安装/更新/回滚 | `web/dist-cloud`（cloud 生产页，打包为 `web/dist`） |
| 共用 | `usagesrv.py` 按 `UM_SHARE_ROLE` 装配；`web/src/console-entry.ts` 按构建期角色装入 | `web/src/shell/`、`web/src/console/backend/`、`share_*.py`、`chat2api/` |

控制台页面还提供只读的候选预览（8099）。候选与生产目录的构建规则、角色守卫和源码绑定方式见 [ARCHITECTURE.md](ARCHITECTURE.md#产物与构建角色)。

## 提交与推送

改动验证通过后在本地提交。推送需要维护者明确要求；一次推送授权不视为持续自动发布授权。

## 文件

- `Open-GatewayConsole.bat/.ps1`：本机桌面网页入口；必要时启动 8001 宿主，申请一次性引导码后打开控制台。命令行启停用 `invoke-gateway-service.ps1`，控制台 JSON 运行时用 `invoke-console-runtime-json.ps1`。`Open-CloudConsole.bat/.ps1`：云端控制台桌面入口，经专用 SSH 配置建 `127.0.0.1:18096` 隧道后用 Edge 打开。
- `lib/gateway-runtime.ps1`、`invoke-console-runtime-json.ps1`、`lib/console-runtime-bridge.ps1`：服务身份、启停、状态与资源锁。M365 仅保留历史统计身份，拒绝启停。
- `usagesrv.py`、`console_operations.py`、`console_adapters.py`：本机管理路由、任务队列、幂等与取消、动作注册及网关适配；`usagesrv.py` 在 `UM_SHARE_ROLE=cloud` 时只装配云端分享控制台。
- `web/index.html → src/console-entry.ts`：按构建期角色装入本机或云端控制台（ADR 0025）。本机装入 `src/local/local-console.ts` → UM 三页（`src/local/um-ui/`），数据由 `src/local/um-data-controller.ts` 提供，`?console=workbench` 保留旧工作台 `src/workbench.ts`；云端装入 `src/cloud/cloud-console.ts`。`src/console/backend/` 提供共用数据契约。`web/dist` 是本机生产托管目录，`web/dist-cloud` 是云端生产目录，`web/release/candidate/` 是旧默认候选（8099 只读预览）。
- `chat2api/`：当前唯一的模型网关（UM，8000）。`qoder-cn/`、`qoder-native/`、`cliproxy/` 已退役并删除：源码与启动器已删除，历史账本、日志与参考克隆已清理、不保留备份；凭据不入库。
- `model_sync.py`、`model_sync_core.py`、`model_config_adapters.py`：目录镜像、受管合并与客户端适配；草稿、预览、应用与读回由 `model_sync.py` 管理。同步不自动发计费探测。
- `console_model_actions.py`：模型读面、设置与同步的动作入口；上下文验证由 `model_sync.py` 的 `probe_context_queue` 独立执行，人工设置、声明值、实测下界分开展示。
- `share_store.py`、`share_management.py`、`share_admin.py/.bat`：分享 Key 和设置的权威存储。`share_runtime.py`、`share_broker_*.py`、`share_public_supervision.py` 实现分享运行链；Windows 运维/恢复入口在 `um-share-deploy/`，云端在 `um-cloud-deploy/`，边界见 `SHARING.md`。
- `um-cloud-deploy/`：UM 分享上云（S2）的离线发布与运维材料。`make-manifest.py` 用 AST 从当前源码字节冻结发布闭包并生成 `RELEASE-MANIFEST.json`（当前 `release_version` 为 s2-20261107a，冻结 **43 个产品 Python 文件**、27 个云端运行文件与 36 个部署文件；`web_dist` 为 787 个文件的云端生产树；数量与哈希一律以 manifest 为准），`package-release.sh` 打包到 `output/um-share-release*/`，`install.sh`／`update.sh`／`rollback.sh` 安装、升级与回滚，`backup_restore.py` 只做显式备份/恢复，`verify-release.ps1` 在 Windows 侧按 manifest 复算哈希，`run-linux-checks.sh` 在 WSL/Linux 跑隔离链；预算保护与自动备份已从默认装配移除（不再安装 `um-share-backup.timer`），见 [um-cloud-deploy/README.md](um-cloud-deploy/README.md)。
- `um-share-deploy/`：旧 Windows 本机分享链的受限部署、broker 安装、看门狗与边界验收入口；仍有恢复说明和测试消费者，保留到旧链退役。日常启停与单层维护用旧工作台分享页（`?console=workbench`），见 [um-share-deploy/README.md](um-share-deploy/README.md)。
- Cloudflare Workers 候选分享已退役并删除；正式云端分享走 `um-cloud-deploy/`，源码可在 Git 历史中查阅。
- `share_metering.py`、`console_stats.py`、`console_log_reader.py`：费用投影、Token 统计与运行事件读取。历史账本保留，不因旧实现删除而清零。
- `assets/AI网关控制台.ico`：桌面与前端图标来源；项目、字体和第三方许可继续保留。
- `tests/`、`web/scripts/backend-contract/`、`web/scripts/console-gate/`：隔离回归、夹具与浏览器验收。旧模拟页面、3D、档案展示和壁纸工程已退出当前控制台。

## 使用约定

| 服务 | 入口 | 端口健康检查 |
|------|------|------------|
| chat2api（直连，UM） | `Open-GatewayConsole.bat` 或 `invoke-gateway-service.ps1 -Key chat2api -Action Start` | `http://127.0.0.1:8000/health` |
| 费用统计 | `Open-GatewayConsole.bat` 或 `invoke-gateway-service.ps1 -Key usage -Action Start` | `http://127.0.0.1:8001/health` |

- 全启只纳入 chat2api 与后台宿主；退役来源不再有活动菜单项、启停或健康检查目标。每天 06:00 目录镜像不唤醒电脑、错过不补跑。
- 由控制台拉起的服务一律清空代理环境变量并设 `NO_PROXY=*`，网关间转发、上游访问和模型体检都不走 VPN/系统代理；`start_chat2api.bat` 单独双击运行时同样生效。网络策略由本机配置维护，清理不修改代理或 TUN 规则。
- chat2api 把“服务运行状态”和“上游认证状态”分开：服务在跑但上游需要重新登录时，会自动弹出 Edge 登录窗口，失败后不再反复弹窗。隐藏服务的脱敏日志见 `chat2api/chat2api.log`。认证状态取值、恢复轮次、等待预算与 503 语义见 [ARCHITECTURE.md](ARCHITECTURE.md#使用约定与直连)。
- 消费方保留本机已配置的 DSH 与 ZCode；退役 provider 的手工配置不在此次清理中删除。

## 费用统计

- **口径**：chat2api 按价目表估算，页面标"参考费用"；Qoder 与 M365 的历史积分/估算账本仅作退役来源追溯，不参与当前默认统计；DSH 流量经 chat2api 网关时已由 `data/usage-chat2api.jsonl` 逐条记账，DSH 独立成块展示的是 **2026-09-02 账本日快照**（`data/dsh-ledger-20260902.json`，从账本存档冻结生成——现行插件账本已丢失该日，快照按用户 2026-09-04 决定恢复该日统计；USD 计价、按汇率换 CNY），不与估算费用合并。顶部"累计参考费用"额外并入该日费用（固定日期常量 `DSH_MERGE_DAY`，2026-09-03 用户决定只并入这一天、不做每日自动并入；换统计日需重新生成快照并同步改常量）。
- **数据源**：当前逐请求记账 JSONL 为本目录 `data/usage-chat2api.jsonl`；退役来源 `qoder/data/usage.jsonl`、`m365/data/usage.jsonl` 与 `data/usage-cliproxy.jsonl` 已清理，不保留备份、不再写入；另有一份 DSH 历史日快照 `data/dsh-ledger-20260902.json`（冻结，仍供 DSH 块使用）。
- **改价格**：查官网后手工编辑 `data/prices.json` 并更新文件顶部 `checkedAt`（维护方法见文件内 `_说明` 键）；补缺用 `data/price.override.json`（最高优先级）。`checkedAt` 超过 30 天页面黄条提醒核对官网。价格引擎、峰谷时段与币种口径见 [ARCHITECTURE.md](ARCHITECTURE.md#费用统计口径)。
- 当前页面由 TypeScript/Vite 控制台承载；统计接口为 GET /health、GET /api/summary，查询未知和真实零分别显示。

## DSH 桌面 App（web 已退役，2026-09-13）

现用入口是桌面快捷方式 `DeepSeek Harness.lnk` → `deepseek-harness/apps/desktop/.desktop-build/targets/win-x64/artifacts/win-unpacked/DeepSeek Harness.exe`（未签名 exe，SmartScreen 首次拦截需「更多信息 → 仍要运行」）。桌面 App 与已退役 web 版共享 `~/.dsh`（会话/设置/凭据/storages），使用独立 `profiles/desktop`（未装载第三方插件），运行时零监听端口；模型路由仍走本机网关。

旧 web 版已卸载（全局 npm 包 `@deepseek-ai/dsh` removed 517 packages，3080 不再监听），旧快捷方式与启动链已删除；`~/.dsh/profiles/web` 仅作 bundles 历史记录保留。控制台不再管理 DSH，模型同步也不联动重启它。退役细节见 [ARCHITECTURE.md](ARCHITECTURE.md#dsh-桌面-appweb-已退役2026-09-13)。

## UM 熟人分享试验

独立入口 `share_gateway.py` 固定本机监听 8100，云端公网入口是 Cloudflare Tunnel（`https://dacintapi.de5.net/v1` → `127.0.0.1:8100`），上游必须显式指向**专用分享后端** 8110，绝不静默转发进私人 8000。它不加入控制台一键全启，也不自动启动原 UM。密钥在管理服务的 SQLite（`data/share-store.db`）里，明文只在受限交付文件 `data/um-share-friend*.txt`。

- **本机分享链**：部署、启停、验证与密钥命令在 [um-share-deploy/README.md](um-share-deploy/README.md)。
- **云端分享链**：以 `um-cloud-deploy/` 的 broker/console unit 为准，与本机链不共用运行目录。
- **归因密钥**：生成、ACL 与轮换流程见 [SHARING.md](SHARING.md)。轮换只重启 8100。
- **Anthropic 客户端**：公网入口支持 `POST /v1/messages`、`GET /v1/models` 与 `POST /v1/chat/completions`，鉴权用分享 Key。字段白名单、并发与请求体上限、错误码和已知限制见 [ARCHITECTURE.md](ARCHITECTURE.md#anthropic-messages-客户端用法与限制2026-09-30)。

## 下一步读什么

| 想知道 | 读 |
|---|---|
| 构建角色、8099 预览、私网访问、模型设置与分享链的运行契约 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 端口分配 | [PORTS.md](PORTS.md) |
| 分享归因密钥的生成与轮换 | [SHARING.md](SHARING.md) |
| 产品定位、用户与能力边界 | [PRODUCT.md](PRODUCT.md) |
| 术语 | [GLOSSARY.md](GLOSSARY.md) |
| 完整入口、产物目的地与历史归属 | [docs/gateway-entrypoints.md](docs/gateway-entrypoints.md) |
