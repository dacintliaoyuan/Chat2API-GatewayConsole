# 网关控制台前端

- 入口是 `index.html → src/console-entry.ts`，按构建期角色只装入本机 `src/local/local-console.ts` 或云端 `src/cloud/cloud-console.ts`（ADR 0001）；本机生产构建与开发服务装入 UM 控制台 `src/local/um-ui/`，`?console=workbench` 只保留旧工作台作逃生入口。保留既有布局、样式、导航、动效及真实后端接线。
- `src/local/um-ui/` 只渲染 `contract.ts` 的视图状态并发出事件，不自行 fetch；读取、任务与真实动作归上云主任务的数据控制器。缺能力时真实禁用并写明原因，不用合成数据冒充真实模式。
- `src/console/backend/` 是数据契约与异步边界，`ui.ts`、`operation-feedback.ts` 为主页面共用模块，不是旧模拟页面。
- `dist/` 为生产托管目录，`release/candidate/` 为候选产物；未经对应部署授权不得覆盖。构建守卫和候选只读代理必须保留。
- 本地验证使用 TypeScript、`scripts/backend-contract/`、`scripts/check-log-presentation.mjs`、`scripts/check-um-views.mjs`（`npm run check:um`）与 `scripts/console-gate/`。夹具通过不代表生产闭环。
- 云端控制台（`src/cloud/`）回归用 `scripts/check-cloud-console.mjs`（`npm run check:cloud-console`），它按真实启动路径装配 `RoleGate` + `installRoleGate` + `mountCloudConsole`。
- 角色门在 `src/console/role-gate.ts`：云端标记只来自 `GET /api/summary` 的 `role`；云端页的 `refresh()` 必须读它，能力表（`/api/console/actions`）只能证明「不是本地」。改云端读计划时先看这条契约。
- 不恢复已退役的 3D、档案展示、Wallpaper Engine、外部托管发布或旧 `console.html` 模拟入口。
- 生产构建须显式声明 `CONSOLE_PRODUCTION_BUILD=1`；不擅自重启服务。
- 遵循上级仓库规则，保留项目与资源版权、字体许可，不发布本机凭据。
