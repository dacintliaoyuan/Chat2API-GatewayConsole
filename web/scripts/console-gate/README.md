# 控制台门禁（console gate）

网关控制台（`index.html` → `src/console-entry.ts` → 按构建期角色装入的控制台，ADR 0025）每次改动后的离线验收入口。它合并了 PR11 门禁（G1–G19）、r6 门禁里仍然有效的诚实性 / 稳健性判据（H1–H9）、日志页事件处理判据（C4：复制精确性 / 筛选切换 / 重复事件 / 剪贴板拒绝 / 原文折叠 / 两主题与窄屏分类色条 / 详情计数）、夹具状态截图与悬停扫描，2026-10-06 起入库（Plan-Revision 13 的 G0）；2026-11-01 起另有 UM 控制台判据（`um.mjs`，含状态判据 `um-state.mjs`）。

## 用法

先按最终源码构建对应角色的候选（不写 8001 的生产 `dist/`；新工作优先用 `candidate-local`/`candidate-cloud`。旧默认 `--mode candidate` 仍写根目录，但已保留 `local/`、`cloud/` 角色子目录）：

```bash
npx tsc --noEmit && npx vite build --mode candidate-local
```

再跑门禁（在 `ai-gateway/web/` 下）：

```bash
node scripts/console-gate/run.mjs --role local
```

- `--role local`：在随机端口起离线夹具服务器，服务 `release/candidate/local/` 与 `fixtures/api.json`，跑全部检查（4 个视口 × 浅深主题），结果写到 `SelfUseAI/output/console-gate/<时间戳>/result.json` 和截图，有任何失败就以退出码 1 结束。H1 同时核对候选清单的角色。
- 本机候选默认打开 UM 控制台，旧工作台的各组（`gate,honesty,logs,states,hover`）一律带 `?console=workbench` 打开；`--role local` 另加 `um` 组：UM 控制台的真实数据控制器模式与离线预览（导航、总览统计、请求与诊断、模型目录与测试交互、主题 / 视口 / 偏好 / 键盘、生产隔离）。真实模式若数据尚未接线，页面会自报「数据未接线」，门禁按页面自报状态核对，不伪造读数。
- `--role cloud`：改用 `release/candidate/cloud/` 与 `fixtures/cloud-api.json`（`fixtures/cloud-synth.mjs` 生成的合成数据）。旧工作台各组的判据针对本机页面；云端控制台尚无专属判据（分享网关前端延期）。
- 不带 `--role`：沿用旧默认目录 `release/candidate/`（本机角色）。
- `--quick`：只跑 1600×900 一个视口（迭代用；`um` 组用 1366×768；最终验收跑完整版）。
- `--only gate,honesty,logs,states,hover,um`：只跑其中几组（`logs` 是日志页事件处理判据，默认在内；`um` 只在 `--role local` 时默认在内）。
- `--url <origin>`：浏览器检查改为指向一个已在运行的地址（例如候选预览）。对 8001 这类真实服务要配 `--settle 16000 --lane-wait 6000`；读取是真实的，但所有非 GET 仍在本地答 403。夹具状态组始终离线跑 `--build`。
- `--binding skip`：跳过候选绑定（H1）并如实记为「跳过」，不是通过。只在 `--url` 指向非候选构建时使用。

Playwright 不是控制台的依赖：默认取 `SelfUseAI/.ai-design-qa/tools/node_modules/playwright/index.mjs`，或用环境变量 `PLAYWRIGHT_MODULE` 指定。浏览器用本机 Chrome（`channel: "chrome"`）。

## 隔离边界

- 每个浏览器上下文在第一次导航前注册守卫：非 GET / HEAD 一律本地应答（默认 403，情景可指定本地的拒绝状态），从不转发；目标源以外的主机一律中止；禁用 Service Worker。
- 24 个字符以上、形似密钥的字符串在页面内被打码后才读取或截图。所以情景里注入的错误码要短于 24 个字符。
- 夹具服务器只回答 `/gw/usagesrv/*` 的读取，其余 `/gw/*` 一律 404。它不代理、不连接任何服务。

## 文件

| 文件 | 内容 |
| --- | --- |
| `run.mjs` | 统一入口与参数 |
| `core.mjs` | Playwright 加载、守卫、打码、开页与切换车道 |
| `fixture-server.mjs` | 离线夹具服务器（也可单独运行：`node fixture-server.mjs --port 18199`） |
| `gate.mjs` | PR11 判据 G1–G19（溢出、真实合成底色上的对比度、控件外观、命中区、可访问名、字体、强调色预算、首屏、背景档退役、非 GET、键盘、窄屏、减少动效、切换耗时、外观设置、空心下界点、二次确认跟随、聚合读取中、错误横幅）。2026-11-01 按 2026-10-31 改版的用户决定更新过时的视觉断言（MiSans／JetBrains Mono、强调色从页面令牌读取、页头取代顶栏、背景档退役、外观分组、横幅贴齐内容列），行为与安全断言未删 |
| `honesty.mjs` | H1–H9（候选绑定、当前控制台入口与静态背景、读取失败的如实呈现、费用下界、分享失败只报一次、折叠、图表提示、焦点环、页面报错）；未移植的 PR9 视觉数值见文件头 |
| `logs.mjs` | C4 日志页事件处理判据：刷新间复制精确性、原文折叠跨重绘、分类筛选与空表句、重复事件各自展开、剪贴板拒绝、两主题 / 窄屏分类色条、详情筛选计数；读取用本地 override 延时换数据，不碰真实服务 |
| `states.mjs` | 只能由 POST 或失败读取产生的界面状态，逐个截图 |
| `um.mjs` | UM 控制台判据（前端 handoff r3 的 F1–F7，r4 调整）：未接线真实模式不发读取、操作全关；离线预览四个情景下的导航、五项用量与费用位置、趋势的折线图切换（Token／费用／请求可切换，速度只画折线）、请求表 / 日志 / 维护的失败与拒绝、模型目录与直接测试、停止后续与历史、主题 / 视口 / 偏好 / 键盘与确认；r4 另查「最近测试」紧凑摘要（1280／1366／760、浅深、侧栏展开／折叠：列宽、摘要不超过两行、数字与单位不拆行、未测、长错误进详情）与工具栏遮挡层无拼缝（1920 展开、1366 折叠，逐像素色差）；通过只证明视图与交互，不证明真实 UM 读取 |
| `um-state.mjs` | 由 `um.mjs` 调用：用 esbuild 打包 `src/local/um-ui` 源码，挂在空白页上，像数据控制器那样原地改、给失败、让读取无结果结束，核对控制台自己的状态处理（返回复核 R4—R6：趋势原地更新后重绘并保留焦点与提示；换时段或模型多于颜色数时速度线不消失；三个视图「加载更早」失败可重试、取消后释放、同字面 cursor 互不阻塞）。不加载 CSS 与候选，整个上下文的请求都在本地答空白页 |
| `hoverscan.mjs` | 悬停扫描 |
| `contrast-lib.mjs` | 对比度测量 |
| `fixtures/api.json` | 合成夹具（生成物，不要手改） |
| `fixtures/capture.mjs` | 按页面实际读取从 8001 只读采集（真实数据，只能写到 `output/`） |
| `fixtures/sanitize.mjs` | 从本机只读采集生成 `api.json` |
| `fixtures/shift.mjs` | 把夹具里的日期平移到今天 |
| `fixtures/cloud-synth.mjs` | 按云端后端代码路径生成云端角色的合成夹具 `fixtures/cloud-api.json`（不采集任何真实主机；生成物，不要手改） |

## 夹具

`fixtures/api.json` 由 `sanitize.mjs` 从一次本机只读采集生成，可以入库：

- 端点、JSON 形状、类型与可空性和采集一致。
- Key 名称、掩码、分享地址、本机路径、日志正文、各类 ID 与令牌都换成合成值，并保持跨端点一致。
- 汇总类 Token / 费用 / 积分 / 耗时按同一常数缩放。
- 模型 ID、公开价目及其文档网址、后端说明文字保留。
- 额外补了警告、错误（含回溯）、截断与无法解析的日志行。
- 生成脚本写入前会再扫一遍，发现被替换的私有值或路径、主机、邮箱、长令牌这类模式就拒绝写入。

`_anchor` 记录采集日。每次加载时，`shift.mjs` 把所有日期与时间戳整体平移到今天（Asia/Shanghai），近 14 天 / 24 小时 / 7 天窗口因此不会过期。

**重新生成**（后端读取接口的形状变化后）：

1. 先构建候选。
2. 只读采集：

   ```bash
   node scripts/console-gate/fixtures/capture.mjs --out ../../output/console-gate/capture-<日期>
   ```

   脚本只转发 GET，拒绝单个 Key 与明文路径；采集文件必须留在被忽略的 `output/` 下。
3. 生成合成夹具：

   ```bash
   node scripts/console-gate/fixtures/sanitize.mjs ../../output/console-gate/capture-<日期>/api-capture.json
   ```

4. 用 `git diff` 复核后再提交。

采集读取的是生产数据，要按当前授权执行，过程中不发任何写请求。
