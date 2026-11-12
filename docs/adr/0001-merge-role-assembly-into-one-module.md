# 合并两个角色的后端装配到 console_backend_app

`cloud/backend/app.py` 与 `local/backend/app.py` 各自复制了同一套 PID 身份、有界日志、控制令牌与 HTTP 服务装配（12 个同名顶层函数，行相似度 0.459），并且已经漂移：cloud 版把同一个 FILETIME 两次传给 `GetProcessTimes`，local 版已修；local 版还多了「新鲜半写 pid 文件不删、过期废弃状态可替换」的规则。决定把共享部分收进根级 `console_backend_app.py`（10 个 helper + `BackendHTTPServer` + `BackendHandler`，统一取 local 的修复版），两个角色模块只保留自己的路由、app 生命周期与 `build_app`/`main`。

`build_app`/`main` 是 composition root，两个角色的差异（app 类、可选 broker 启动、POSIX 检查、异常元组、消息前缀、traceback）属于角色本身，不合并。

## Considered Options

- **全合并**：`build_app`/`main` 也进新模块，用一个 `RoleProfile` 承载七项角色差异。否决：那个 interface 比实现还宽，是浅模块；而 composition root 的差异正是角色定义。
- **保持两份**：否决，漂移已经发生，且 cloud 版的 `GetProcessTimes` 是真实 bug。

## Consequences

- cloud 角色继承 local 的 `GetProcessTimes` 修复与 pid 文件过期规则——这是行为变化，cloud 侧没有对应断言，`tests/test_r19_cloud_app.py` 在 Windows 上整体跳过（需要 POSIX）。
- `console_backend_app.py` 是根级模块，会进入 `um-cloud-deploy/make-manifest.py` 的 `_module_map` 与发布闭包，`RELEASE-MANIFEST.json` 需要重新生成。
- `tests/test_r19_shared_boundary.py` 的 known-deferral 断言跟着搬家：凭据 reader 现在由 `console_backend_app` import `share_secret_protection`，两个角色模块 import 共享模块。
