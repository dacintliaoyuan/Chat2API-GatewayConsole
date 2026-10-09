# chat2api 控制台

公开版本提供 chat2api 账号与模型管理、ZCode 配置同步、费用、运行日志和分享管理。Open WebUI 目录可以提供不同厂商的模型，统一经 chat2api 接入。同步预览、应用和回读只处理 chat2api 到 ZCode 的受管范围。

项目介绍、部署、自动重连和日志排查方法见 [根目录 README](../../README.md)。

## 本地构建

```powershell
npm ci --ignore-scripts
npm run check
npm run check:logs
npm run check:scope
npm run build
```

`dist/` 是构建结果，由控制台后端提供静态页面。页面读取同源 `/api/summary` 和 `/api/console/*`，不会把凭据写入构建配置。

`npm run check:logs` 检查日志页的分类与时间轴呈现：事件原文身份（重复时间戳 / 重复内容各自可分、不随列表位置漂移）、分类筛选唯一入口、空表句与详情计数点名实际筛选轴，以及错误红 / 警告黄 / 通过绿 / 信息蓝在两主题下的行级样式。它直接绑定 `src/workbench.ts`、`src/workbench.css` 与 `src/workbench-log-presentation.ts` 的实际内容。

`npm run check:logs:browser` 是同一批判据的**真实浏览器验收**：把 `dist/` 起在本地随机端口（夹具是脚本内的合成值，不连任何真实服务），用 Playwright 真点页面——刷新间复制精确性、原文折叠跨重绘、分类筛选与空表句、重复事件各自展开、剪贴板拒绝、两主题与窄屏行级分类色条、详情筛选计数。它需要先 `npm run build`，并需要一个 Playwright 模块（本树不是它的依赖，按下面方式传入）：

```powershell
npm run build
$env:PLAYWRIGHT_MODULE = "<path to>/playwright/index.mjs"
npm run check:logs:browser
```

## 独立预览

```powershell
npm run dev
```

开发服务固定为 `127.0.0.1:5178`，端口占用时直接失败；默认没有后端代理。需要联调时，在启动前显式设置 `CONSOLE_BACKEND_URL` 为已选定的隔离后端 origin。不要把凭据放进此 URL。构建预览端口为 `4178`。

未配置代理或后端不可用时，页面显示实际读取失败与未知状态。独立页面构建和离线检查不代表真实上游账号、ZCode 客户端写入或分享链已验证。

字体许可证保留在 `public/fonts/`，详见其 `NOTICE.txt` 与各字体许可证。
