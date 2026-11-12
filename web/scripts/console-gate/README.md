# 控制台门禁（console gate）

网关控制台（`index.html → src/console-entry.ts` → 按构建期角色装入的控制台，[ADR 0001](../../../docs/adr/0001-one-frontend-two-consoles-build-time-role.md)）每次改动后的离线验收入口。它覆盖页面布局、诚实性、日志事件处理、界面状态、悬停与对比度，以及本机 UM 控制台的视图与交互。

## 用法

先按最终源码构建对应角色的候选（不写生产 `dist/`）：

```bash
cd web
npx tsc --noEmit && npx vite build --mode candidate-local
```

再跑门禁：

```bash
node scripts/console-gate/run.mjs --role local
```

- `--role local`：在随机端口起离线夹具服务器，服务 `release/candidate/local/` 与 `fixtures/api.json`，跑全部检查（4 个视口 × 浅深主题），结果写到仓库根 `output/console-gate/<时间戳>/result.json` 和截图，任何失败以退出码 1 结束。H1 同时核对候选清单的角色。
- 本机候选默认打开 UM 控制台，旧工作台的各组（`gate,honesty,logs,states,hover`）一律带 `?console=workbench` 打开；`--role local` 另加 `um` 组，覆盖 UM 控制台的真实数据控制器模式与离线预览。
- `--role cloud`：改用 `release/candidate/cloud/` 与 `fixtures/cloud-api.json`（`fixtures/cloud-synth.mjs` 生成的合成数据）。
- 不带 `--role`：沿用旧默认目录 `release/candidate/`（本机角色）。
- `--quick`：只跑 1600×900 一个视口（迭代用；`um` 组用 1366×768）。
- `--only gate,honesty,logs,states,hover,um`：只跑其中几组。
- `--url <origin>`：浏览器检查改为指向一个已在运行的地址（例如候选预览）。对 8001 这类真实服务要配 `--settle 16000 --lane-wait 6000`；读取是真实的，但所有非 GET 仍在本地答 403。
- `--binding skip`：跳过候选绑定（H1）并如实记为「跳过」，不是通过。

Playwright 是 `web` 的开发依赖；默认取 `web/node_modules/playwright/index.mjs`，可用 `PLAYWRIGHT_MODULE` 指定其它模块。浏览器用本机 Chrome。

## 隔离边界

- 每个浏览器上下文在第一次导航前注册守卫：非 GET / HEAD 一律本地应答（默认 403，情景可指定本地的拒绝状态），从不转发；目标源以外的主机一律中止；禁用 Service Worker。
- 24 个字符以上、形似密钥的字符串在页面内被打码后才读取或截图；情景里注入的错误码要短于 24 个字符。
- 夹具服务器只回答 `/gw/usagesrv/*` 的读取，其余 `/gw/*` 一律 404；它不代理、不连接任何服务。

## 文件

| 文件 | 内容 |
| --- | --- |
| `run.mjs` | 统一入口与参数 |
| `core.mjs` | Playwright 加载、守卫、打码、开页与切换车道 |
| `fixture-server.mjs` | 离线夹具服务器（也可单独运行：`node fixture-server.mjs --port 18199`） |
| `gate.mjs` | 布局、对比度、控件、命中区、可访问名、字体与窄屏判据 |
| `honesty.mjs` | 候选绑定、读取失败、费用下界、分享失败与页面报错判据 |
| `logs.mjs` | 日志页事件处理判据：复制精确性、原文折叠、筛选、重复事件、剪贴板拒绝与详情计数 |
| `states.mjs` | 只能由 POST 或失败读取产生的界面状态 |
| `um.mjs` / `um-state.mjs` | 本机 UM 控制台的视图、交互与状态处理 |
| `cloud.mjs` | 云端控制台读面与区域路由 |
| `hoverscan.mjs` / `contrast-lib.mjs` | 悬停扫描与对比度测量 |
| `fixtures/api.json` | 本机合成夹具（生成物，不要手改） |
| `fixtures/cloud-api.json` | 云端合成夹具（生成物，不要手改） |

## 夹具来源

`fixtures/api.json` 由 `fixtures/sanitize.mjs` 从一次本机只读采集生成，可以入库：端点、JSON 形状与类型和采集一致；Key 名称、掩码、分享地址、本机路径、日志正文、各类 ID 与令牌都换成合成值，并保持跨端点一致；汇总类 Token / 费用 / 积分 / 耗时按同一常数缩放。

`fixtures/capture.mjs` 只转发 GET，拒绝单个 Key 与明文路径；采集文件必须留在被忽略的 `output/` 下。`fixtures/shift.mjs` 在每次加载时把所有日期与时间戳整体平移到今天（Asia/Shanghai），近 14 天 / 24 小时 / 7 天窗口因此不会过期。

后端读取接口的形状变化后，先构建候选，再只读采集并重新生成夹具，用 `git diff` 复核后提交。
