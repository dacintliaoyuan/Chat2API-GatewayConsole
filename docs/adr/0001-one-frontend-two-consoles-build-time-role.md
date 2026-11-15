# ADR 0001：一套前端、两个控制台，角色在构建时选定

- 状态：Accepted
- 日期：2026-11-12
- 迁移说明：本决策随整树迁入本仓库后按当前路径重写；原记录为 SelfUseAI 的 ADR 0025（2026-10-31）。

## 背景

UM 的 API 服务迁往云机后，维护者要同时管理本机自用服务与云端 API 服务。本机控制台宿主是 `usagesrv.py`（`UM_SHARE_ROLE=local`，`127.0.0.1:8001`）；云端控制台宿主是同一个 `usagesrv.py`（`UM_SHARE_ROLE=cloud`，`127.0.0.1:8096`；一个发布周期内旧 `18096` 仍是显式兼容值）。控制台接口只信任本机回环地址上打开的同源页面；经 SSH 转发访问云端时，浏览器地址同样是 localhost。

## 决策

- 同一套 TypeScript/Vite 前端构建出两个控制台：本机控制台只管理本机自用服务，云端控制台只管理云端 API 服务。两者各自托管，数据、操作目标与访问边界各自归属，不做跨机统一管理代理。
- 控制台角色在构建时选定：`web/src/console-entry.ts` 只动态装入 `web/src/local/local-console.ts` 或 `web/src/cloud/cloud-console.ts` 之一，另一分支及其 chunk 不进入产物。`__CONSOLE_ROLE__` 是构建期常量，`web/vite.config.ts` 在构建前校验 `CONSOLE_ROLE` 与候选模式。
- 页面加载后与后端回报的部署角色核对：`web/src/console/role-gate.ts` 读取 `GET /api/summary` 的 `role`，对不上或无法确认时不开放写操作。云端后端明确回报 `cloud`；本机后端没有对应标记，本机以“没有 cloud 标记且能力表含本地专属动作”确认，明确的 `local` 标记列为后端依赖。
- 不按访问地址推断角色。
- 生产构建必须显式声明 `CONSOLE_PRODUCTION_BUILD=1` 与 `CONSOLE_ROLE=local|cloud`；本机角色另需 `CONSOLE_LOCAL_SHARE_RETIRED=1`，声明分享已由云端接管。缺少声明、角色冲突，或直接写 `web/dist`、`web/dist-cloud` 时，构建在 vite 输出前失败。
- 每个产物根写 `candidate-build.json`，记录 `role`、`line`、`mode`、构建输入 sha256、聚合 `sourceHash` 与 `gitHead`。`usagesrv.py` 只托管角色与 line 都匹配的树；`make-manifest.py`／`package-release.sh` 用 `--web-role cloud` 拒绝本机角色树。

## 备选

- **一个统一控制台经跨机代理同时管理两台机器**：否决。两机的会话、凭据与故障域会互相牵连，还要新建常驻代理。
- **运行时读取部署方放置的角色文件**：未采用。构建时选定更难装错，产物也能直接核对。
- **按访问地址推断角色**：否决。SSH 转发后本机与云端的浏览器地址都是 localhost，地址不携带角色。

## 后果

- 发布方必须为每台机器构建对应角色的产物：本机生产页写 `web/dist`，云端生产页写 `web/dist-cloud` 并打包为发布包内的 `web/dist`。
- 候选线按角色分开：`candidate-local` 写 `web/release/candidate/local/`，`candidate-cloud` 写 `web/release/candidate/cloud/`；旧默认 `candidate` 写 `web/release/candidate/` 并保留角色子目录，只用于 8099 只读预览与日志门禁。
- 两端共享 `web/src/shell/`、`web/src/console/backend/` 与 `web/src/console/role-gate.ts`，角色专用的页面与样式互不引用。
- 本机生产默认入口装入 UM 三页与数据控制器 `web/src/local/um-data-controller.ts`。云端使用独立的五页入口。
- 本机生产页不含 API Key 与 API 服务管理；云端接管分享管理前发布它可能失去唯一管理入口，因此 `CONSOLE_LOCAL_SHARE_RETIRED=1` 是显式声明而不是默认值。

## 端口配对（目标契约，Rust 切换时生效）

- `8000` 本机网关（`chat2api`）、`8001` 本机控制台（`usagesrv` local）；`8002` 云端控制台（`usagesrv` cloud）、`8003` 云端网关（`chat2api.py` 合并进程）；`8004–8009` 预留。`8000–8009` 因此从“会话适配段”改为“网关/控制台配对段”。
- `8097` display x11vnc 与 `8099` 候选预览保持不变；`5173` 前端开发服务保持不变。
- `um-share-broker.service` 在目标契约中更名为 `um-share-gateway.service`；`um-share-brokerctl.py` 更名为 `um-share-gatewayctl.py`。broker 仍是内部拉起者，不新增第二个进程。
- 旧 `8100`、`8096`、`18096`、`5901` **不保留兼容别名**；下一次 Rust 部署一次性切换，`install.sh` 强制写入 `USAGESRV_PORT=8002` 与 `CONSOLE_DISPLAY_VNC_PORT=8097`。这取代 #40 原先“一个发布周期兼容”的决定。
- 本 ADR 只冻结目标契约；当前 Python 代码仍监听 8100/8096，Rust 重写时一起改。

## 相关实现

- 前端入口：`web/src/console-entry.ts`
- 构建守卫与产物目录：`web/vite.config.ts`
- 后端角色校验：`usagesrv.py`
- 角色门：`web/src/console/role-gate.ts`
