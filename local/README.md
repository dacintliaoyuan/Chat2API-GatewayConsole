# local 端（本机组合根）

`local/` 是本机角色的**唯一入口包**：它只装配本机需要的部分，不 import 云端、
旧宿主或分享链的模块。计划出处：`tmp/r19-recon/plan.md` 的 E19-1-04 / E19-1-05。

## 1. 入口

```text
python -I local/run_local.py --config <绝对路径.json> [--check-config | --print-runtime-info]
```

- `-I` 是**硬要求**：`local/run_local.py` 的守卫在任何非内置 import 之前执行，
  缺 `-I` 时 rc 2 并给出要求的命令行。原因是被继承的 `sys.path`/`PYTHONPATH`
  里可能有植入模块，而本机宿主不得加载它。
- `--check-config` 只校验配置：不绑端口、不建目录、不起子进程。
- `--print-runtime-info` 打印实际 `sys.path`、`PYTHONPATH`、`module_file`、
  每个模块的解析文件（`module_files`）、配置路径、`home_dir`、OS 级 `real_home`、
  父进程导出的 `original_home_env` 与已加载模块名。

| 退出码 | 含义 |
|---|---|
| 0 | `--check-config` / `--print-runtime-info` 成功，或正常退出 |
| 2 | 配置缺失/非法/未知字段/路径非法/端口非法/未隔离/真实 HOME/真实资料/生产 runtime wrapper |
| 1 | 启动失败（控制令牌缺失、UM 未就绪、端口占用、bind 失败） |

## 2. 模块边界

`local/backend/app.py` 拥有自己的路由、鉴权、配置与生命周期，**不 import**
`console_adapters`、`share_management`、`share_store`、`share_gateway`、
`share_dispatch`、`console_display_proxy`、`usagesrv`、`cloud`。唯一的 `share_*`
依赖是控制令牌校验器 `share_secret_protection`，隔离包会携带它。

`local/backend/stats_env.py` 只用 `shared.pricing_rules` 的公开名；价目缓存
（`_PRICE_CACHE`）与文件读取仍留在端内，**不**移入 `shared`。

## 3. UM 子进程契约

- `um_runtime.home_dir` 是**必填**字段，必须是绝对路径且等于或位于
  `LocalConfig.home_dir` 之下；子进程的 `HOME`/`USERPROFILE`/`TMPDIR`/`TEMP`/`TMP`
  全部取自它，不再由 `dirname(profile_dir)` 推导。
- 上游 token 通过 `CHAT2API_UPSTREAM_TOKEN` 环境变量传给 `chat2api` 子进程，
  **不**出现在 argv（同机进程可读 `/proc/<pid>/cmdline`）。
- `runtime_control.mode=powershell` 指向的脚本必须是包内受控测试脚本，且内容不得
  含 `Start-Process` / `Invoke-ConsoleRuntimeStart` / dot-source
  `gateway-runtime.ps1`；生产 wrapper 的 basename 与包外副本都被拒绝。

## 4. 已知冲突（本阶段只记录，不裁）

### 4.1 一个 `model.catalog` 读动作携带 7 个 ZCode/同步模块

`local/backend/registry.py` 只注册 `model.catalog`，但 `console_model_actions` →
`model_sync` 这条既有调用链在运行期把下面 9 个模块带入 `sys.modules`（本轮实测，
`python -I -B -c "import sys; sys.path.insert(0,'.'); import local.backend.app; ..."`
全部为 `True`）：

```text
zcode_hot_sync            model_sync              console_stats
console_model_actions     model_reconcile         model_config_adapters
model_sync_core           zcode_file_lock        zcode_hot_sync_protocol
```

其中 7 个（`model_config_adapters`、`model_reconcile`、`model_sync`、
`model_sync_core`、`zcode_file_lock`、`zcode_hot_sync`、
`zcode_hot_sync_protocol`）是 R19-5 的**退役目标**。也就是说：本机端做一个模型目录
读取动作，就带上 7 个计划退役的模块，与 R19-5 的方向冲突。

**本阶段不裁**：退役是 E19-3 的范围。这里只把冲突量化，供 E19-3 排期时核对。
`tests/r19/package_local.py` 也把 `console_speedtest.py` 列为禁止项，与 R19-5
"保留测速"的方向存在同类冲突。

### 4.2 前端读面与 local 宿主的差距

`web/src/console/backend/client.ts` 的 9 个 `read*` 方法
（`readGatewayConnection`/`readUmToday`/`readUmTrend`/`readUmRequests`/`readUmLogs`/
`readUmRequestLogs`/`readUmMaintenance`/`readServices`/`readOperations`）与前端默认
前缀 `/gw/usagesrv/api/console`（`transport.ts:10`）在本阶段**尚未**在本机宿主落地：
`local/backend/app.py` 目前只有 `/health`、`/api/summary`、`/api/console/actions`、
`/api/console/operations`、`/api/console/operations/<id>`、`/api/session`、
`/api/session/reissue`、`/admin/shutdown`。

这是 E19-4（前端与连接）的欠账，不是本阶段的完成项。计划 E19-1-04 第 3 步要求
"每个 `read*` 有对应路由或显式降级记录"——此处即为显式记录：**未实现，前端对应
读面在 local 宿主上会拿到 404 `unknown path`**，不得当作已装配。

### 4.3 `summary` 语义

`/api/summary` 不带参数时返回 `role` + `umReference`；带 `from`/`to` 时额外附
`stats`/`totals`。旧宿主的 `/api/summary` **不接受** `from`/`to`（那是
`/api/console/stats`），所以本机端的合并形态是**本端扩展**，不得声称与旧宿主
向后兼容。

## 5. 隔离包

`tests/r19/package_local.py` 把 `local/**`、`shared/**` 与 `chat2api/chat2api.py`
等显式白名单复制到独立的临时目录：

- `verify_package()` 报出被禁止的文件、被禁止的目录（`cloud/`）、glob 与缺失的必需文件；
- `module_files_outside()` / `assert_modules_inside()` 校验 `--print-runtime-info`
  的 `module_files`：每个模块必须解析到包内、标准库或宿主 `site-packages`。

隔离包**依赖宿主 `site-packages`**（`yaml` 等），本阶段不携带 wheelhouse；
wheelhouse 随包归 E19-5。
