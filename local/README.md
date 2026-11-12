# local 端（本机组合根）

`local/` 是本机角色的唯一入口包：只装配本机需要的部分，不 import 云端、旧宿主或分享链模块。

## 入口

```text
python -I local/run_local.py --config <绝对路径.json> [--check-config | --print-runtime-info]
```

- `-I` 是硬要求：`run_local.py` 的守卫在任何非内置 import 之前执行；缺 `-I` 时 rc 2 并打印要求的命令行。被继承的 `sys.path`/`PYTHONPATH` 可能含植入模块，本机宿主不加载它。
- `--check-config` 只校验配置：不绑端口、不建目录、不起子进程。
- `--print-runtime-info` 打印实际 `sys.path`、`PYTHONPATH`、`module_file`、每个模块的解析文件、配置路径、`home_dir`、OS 级 `real_home`、父进程导出的 `original_home_env` 与已加载模块名。

| 退出码 | 含义 |
|---|---|
| 0 | `--check-config` / `--print-runtime-info` 成功，或正常退出 |
| 2 | 配置缺失/非法/未知字段/路径非法/端口非法/未隔离/真实 HOME/真实资料/生产 runtime wrapper |
| 1 | 启动失败（控制令牌缺失、UM 未就绪、端口占用、bind 失败） |

## 模块边界

`local/backend/app.py` 拥有自己的路由、鉴权、配置与生命周期，不 import `console_adapters`、`share_management`、`share_store`、`share_gateway`、`share_dispatch`、`console_display_proxy`、`usagesrv`、`cloud`。唯一的 `share_*` 依赖是控制令牌校验器 `share_secret_protection`，隔离包会携带它。

`local/backend/stats_env.py` 只用 `shared.pricing_rules` 的公开名；价目缓存与文件读取留在端内。

## UM 子进程契约

- `um_runtime.home_dir` 必填，必须是绝对路径且等于或位于 `LocalConfig.home_dir` 之下；子进程的 `HOME`/`USERPROFILE`/`TMPDIR`/`TEMP`/`TMP` 全部取自它。
- 上游 token 通过 `CHAT2API_UPSTREAM_TOKEN` 环境变量传给 `chat2api` 子进程，不出现在 argv。
- `runtime_control.mode=powershell` 指向的脚本必须是包内受控测试脚本，且不得含 `Start-Process` / `Invoke-ConsoleRuntimeStart` / dot-source `gateway-runtime.ps1`；生产 wrapper 的 basename 与包外副本都被拒绝。

## 隔离包

`tests/r19/package_local.py` 把 `local/**`、`shared/**` 与 `chat2api/chat2api.py` 等显式白名单复制到独立临时目录，校验禁止文件、禁止目录、glob 与必需文件，并核对 `--print-runtime-info` 的每个模块都解析到包内、标准库或宿主 `site-packages`。隔离包依赖宿主 `site-packages`（如 `yaml`），不携带 wheelhouse。
