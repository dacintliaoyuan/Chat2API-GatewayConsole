# AI 网关控制台前端

同一套源码按构建期角色产出两个控制台（[ADR 0025](../../docs/adr/0025-one-frontend-two-consoles-build-time-role.md)）：本机控制台展示网关运行状态、账号认证、模型目录与客户端同步、Token 与费用及运行日志；云端控制台管理云端 API 服务（API Key、用量与请求、服务与登录、运维与恢复）。页面使用 TypeScript、Vite 和共享外壳 `src/shell/`，真实数据由各自后端提供。

## 本地开发

安装依赖后运行 `npm run dev`（`127.0.0.1:5173`，见 [../PORTS.md](../PORTS.md)）。入口是 `index.html → src/console-entry.ts`，按构建期角色只装入 `src/local/local-console.ts` 或 `src/cloud/cloud-console.ts` 之一。开发服务按本机角色、非候选构建，打开由 `src/local/um-data-controller.ts` 驱动的 UM 控制台；`?console=workbench` 打开旧工作台。开发服务只监听本机；开发代理可执行管理操作，使用时需明确目标后端。

## 本机控制台（local）

- 三个页面：总览、请求与诊断、模型与设置；数据控制器 `src/local/um-data-controller.ts` 读取 `/gw/usagesrv/api/console/*` 与 `/api/summary`，视图只渲染状态、发出事件，不自行 fetch。
- 生产构建也装入这个控制台；`?console=workbench` 保留旧工作台（`src/workbench.ts`）作为兼容与门禁入口。
- `?um-preview=normal|attention|down|zero`：带醒目标注的离线合成预览，只在候选构建里提供，只在浏览器内模拟，不发请求；生产 URL 不能显示合成数据。
- 视图状态与操作事件定义在 `src/local/um-ui/contract.ts`，只做加性演进；登录与启停先在控制台确认，测速与上下文探测点击即发。
- 离线查看：构建 `candidate-local` 后运行 `node scripts/console-gate/fixture-server.mjs --build release/candidate/local`，打开其地址并带上述参数。

## 云端控制台（cloud）

- 五个页面：总览、API Key、用量与请求、服务与登录、运维与恢复；数据来自云端后端自己的读面，不经本机转发。
- 总览首屏保留服务/认证状态、需要处理的问题、今日请求/Token/参考费用、配置并发上限与趋势排行；分层证据与端口收进折叠区。用量页的失败汇总按成功/失败/已取消/未判定分列（后端未提供时显示「未提供」），点击任一结果会打开带 `result=` 的逐请求明细。
- API Key 长期有效，页面不提供停用/启用；新建默认并发 16，单 Key 上限 48，全局上限 64。撤销会显示 terminated/released/aborted/upstream_closed 计数。
- 服务与登录主区是整链一键启动 / 普通停止 / 强制停止；单层进程与登录动作收在「高级维护」折叠区。私有登录窗口显示 display-state 分段、操作权状态与打开/重连/取消登录/返回控制台/接管；取消登录只在登录中或等待 MFA 时可用。
- 图表时段为近 7/14/30 天，指标可切换请求/Token；缺失日显示短标「无数据」，全零时段明确说明，部分记录标为下界。公网 API 地址读数过时时显示「上次读数，已过时」并禁用复制。
- 预算保护与自动备份已从默认装配和 UI 移除（`GET /api/console/cloud-ops` 只返回安全读面）。
- 候选构建 `--mode candidate-cloud` 是只读的：写操作在前端被拒绝并提示去生产页；可写控制台必须携带云端生产构建（`CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud`）。

## 构建与验证

分享页分别显示本机链路、Funnel 映射、公网 DNS 和真实模型调用结果。公网检查沿用密钥行的“测试”按钮，会产生少量模型用量；页面刷新不自动测试，结果只在当前页面保留并绑定密钥版本和分享地址，5 分钟后标为陈旧。“读取分层状态”同时查询受限后台的看门狗状态、路径一致性和历史任务结果；缺少支持或查询失败保持未知。

分享页固定页头和分享状态区同时显示公网实测摘要：没有当前地址的测试样本时为“公网未验证”，只有新鲜的成功样本才显示绿色；TLS、模型目录或推理失败明确告警，陈旧结果也不会保持绿色。全局“主链可用”、本机就绪与映射已开启仍是独立事实，不能代替公网模型回复。

- `npx tsc --noEmit`：类型检查。
- `npm run check:contracts`：隔离后端契约夹具检查。
- `npm run check:logs`：运行事件与图表辅助逻辑回归。
- `npm run check:um`：UM 控制台视图在不合契约的数据下（坏时间、契约外取值、带标记的文本、重复 ID）不抛错、全部转义、写「未知」，且按钮与点击用同一个禁用原因；「最近测试」只给紧凑摘要（数字连同单位不拆开，完整原文在详情）；趋势颜色跟随当前时段的模型，速度线不因颜色用完而丢失。
- `npm run check:cloud-console`：云端控制台读面、写操作与区域路由回归。
- `npx vite build --mode candidate-local` / `--mode candidate-cloud`：分别输出到 `release/candidate/local/`、`release/candidate/cloud/`。旧默认 `--mode candidate` 仍写 `release/candidate/`（本机角色，8099 预览读取）；重建不清空根目录，保留 `local/`、`cloud/` 子目录（根目录旧文件可能残留）。
- `npm run check:console`：候选页面隔离浏览器门禁，详见 `scripts/console-gate/README.md`。

生产产物按角色分开：`dist/` 是本机生产页（8001 托管），`dist-cloud/` 是云端生产页（打包为云端发布包的 `web/dist`）。构建守卫默认拒绝覆盖；生产构建需要单独授权并显式设置 `CONSOLE_PRODUCTION_BUILD=1` 与 `CONSOLE_ROLE=local|cloud`，本机角色另需 `CONSOLE_LOCAL_SHARE_RETIRED=1`。每次构建都写 `candidate-build.json`（角色、`sourceHash`、`gitHead` 与入口资产），后端按角色拒绝托管错误的树。候选预览仅转发 GET/HEAD，管理写请求在本机拒绝；页面在确认后端角色前也不发出写请求。

网页品牌、浏览器标签和手机桌面图标统一来自 `../assets/AI网关控制台.ico`。在本目录运行 `node scripts/build-icons.mjs` 可重新生成源码资源；获准更新网页图标后，加 `--sync-hosted-icons` 只同步现有 `dist/`、`release/candidate/` 的图标和入口引用，不替换其 JS/CSS 包或重启服务。源码更新后的完整候选仍使用上述候选构建入口生成。

## 目录

`src/` 保存当前页面、样式和数据适配；`public/` 保存图标、字体及许可；`scripts/backend-contract/` 和 `scripts/console-gate/` 保存有效验收入口；`docs/console-feature-spec.md` 保存控制台功能说明。`dist/` 是本机生产托管目录，`dist-cloud/` 是云端生产托管目录，`release/candidate/` 是候选产物（`local/`、`cloud/` 为角色子目录）。历史模拟页面、3D、档案展示和壁纸工程已移除。

项目及第三方资源许可继续保留在 `LICENSE` 和 `public/licenses/`、各字体目录。

旧 PWA 已退役。`public/sw.js` 仅供曾安装旧 worker 的浏览器更新后清理该 worker 的资源缓存并注销，不接管新请求、不刷新页面、不清理其他缓存；候选构建会复制此兼容端点。可运行 `node --test scripts/check-retired-worker.mjs` 检查清理范围与失败行为。

云端独立图标原图为 `../assets/cloud-console-icon-source/source.jpg`，透明导出源为同目录 `cutout.png`；运行 `python -B ../tools/build-cloud-icons.py` 导出云端 ICO 与 `/icons/cloud/` 资源。云端挂载时替换标签、侧栏和应用图标，本地继续使用原图；详见[源图说明](../assets/cloud-console-icon-source/README.md)。
