# 网关控制台前端

同一套源码按构建期角色产出两个控制台（[ADR 0001](../docs/adr/0001-one-frontend-two-consoles-build-time-role.md)）：本机控制台管理本机 UM（8000）与控制台宿主（8001），云端控制台管理云端分享 API（8096）。页面使用 TypeScript、Vite 和共享外壳 `src/shell/`，真实数据由各自后端提供。

## 开发

```bash
npm ci
npm run dev   # 127.0.0.1:5173
```

入口是 `index.html → src/console-entry.ts`，按构建期角色只装入 `src/local/local-console.ts` 或 `src/cloud/cloud-console.ts` 之一。开发服务按本机角色、非候选构建运行。开发服务只监听本机。

## 构建

```bash
# 本机角色候选（8099 只读预览）
npx vite build --mode candidate-local

# 云端角色候选
npx vite build --mode candidate-cloud

# 本机生产页（8001 托管）
CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=local CONSOLE_LOCAL_SHARE_RETIRED=1 npm run build

# 云端生产页（打包为发布包内的 web/dist）
CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud npm run build
```

| 角色 | 候选 | 生产 |
|---|---|---|
| local | `release/candidate/local/` | `dist/` |
| cloud | `release/candidate/cloud/` | `dist-cloud/` |

`dist/`、`dist-cloud/`、`release/` 都在 `web/.gitignore` 中。构建守卫默认拒绝直接覆盖生产目录；每次构建写 `candidate-build.json`，记录 `role`、`line`、`mode`、构建输入 sha256、`sourceHash` 与 `gitHead`。`usagesrv.py` 只托管角色与 line 都匹配的树。

## 验证

```bash
npx tsc --noEmit
npm run check:contracts
npm run check:logs
npm run check:um
npm run check:cloud-console
npm run check:console
```

- `check:contracts`：后端契约夹具检查。
- `check:logs`：运行事件与图表辅助逻辑回归。
- `check:um`：本机 UM 控制台视图回归。
- `check:cloud-console`：云端控制台读面、写操作与区域路由回归。
- `check:console`：候选页面隔离浏览器门禁，详见 [scripts/console-gate/README.md](scripts/console-gate/README.md)。

真实模式未接线时页面自报「数据未接线」，门禁按页面自报状态核对，不伪造读数。夹具通过不代表生产闭环。

## 目录

- `src/`：页面、样式与数据适配；`src/console/backend/` 是数据契约与异步边界。
- `public/`：图标、字体与许可。
- `scripts/backend-contract/`：后端契约夹具检查。
- `scripts/console-gate/`：候选页面浏览器门禁。

品牌图标来自 `../assets/AI网关控制台.ico`；云端图标由 `../tools/build-cloud-icons.py` 生成。旧 PWA 已退役，`public/sw.js` 只用于清理旧 worker 缓存。
