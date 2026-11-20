# 本机与云端的进程监督边界

本机 8000/8001 与云端 8003/8002 使用同一套 Rust 网关/控制台二进制，但监督者不同：本机是 `lib/gateway-runtime.ps1` 的 PowerShell 启动器，云端是 systemd unit 加 Python broker。这个差异是运行环境和失败边界决定的，不是迁移遗漏。

## 本机：用户会话与进程身份

本机是已登录 Windows 用户会话里的桌面程序，不是无人值守服务器。

- 人工接管要打开可见 Edge，Duo 要人在同一个会话里确认。Windows Service 跑在 session 0，不能提供用户桌面上的可见窗口；所以本机入口只能是用户会话里的 PowerShell 启动器。
- 本机可能同时存在生产 8000、candidate parity、cloud shape 与 Python 回滚实例。停止不能按同名进程或端口一刀切，否则会误杀 candidate 或云端实例。启动描述符用 `ProcessName`、`CommandLinePattern`、`InstanceScript` 与 `ForeignArgumentPatterns` 识别自己的实例；8000 用 `GracefulHttp /admin/shutdown`，8001 用 `Port`。
- “故意停止不被恢复”由 `DesiredStateTracked` 加计划任务 `SelfUseAI AI Gateway Chat2API Watch` 实现：启动时 arm，故意停止时 release，异常退出才留给 watch。它没有 broker 的 drain、持久 stop intent、投影发布和实例通道。
- 回滚仍是只改 `StartPath` 回 Python 批处理，不需要重装或改端口。

## 云端：systemd 与 broker

云端是 Linux 常驻服务，8003 是分享链的唯一数据面。

- 8003 只能由 broker 拉起：`um-share-gateway.service` 的 `ExecStart` 是 `share/broker_ops.py`，`ExecStartPost` 调 `um-share-gatewayctl.py start`；控制台 unit 用 `NoExecPaths` 挡住，不能成为第二个 8003 启动者。
- broker 负责投影发布、Key 撤销、drain、emergency-stop、持久 stop intent `/run/um-share/share-host-desired.json`、维护租约和实例通道；systemd 只负责 unit 生命周期和 cgroup 围栏。
- 手动 `systemctl restart` 会先写 stop intent，重启后普通 `start` 被拒绝，必须 `start-explicit` 或走 `update.sh`。
- broker 仍是 Python 是 ADR 0009 的范围决定：Rust 迁移只覆盖常驻数据面，broker 是控制面，另开 spec。ADR 0010 也明确 broker 不在 Python 回滚退役范围内。

## 不变量

每个角色最多一个生产实例；操作员故意停止后，watchdog/broker 不得自动恢复；云端停止前必须 drain。

本机和云端是这条不变量的两种实现，不是两套产品语义。Review 时先问实现是否守住这三句，再问具体机制。

## 后果

- 两套监督模型、两套排障语言：本机看 PowerShell 描述符和计划任务，云端看 systemd/broker。
- 本机进程身份依赖命令行匹配，二进制改名或参数变化要同步描述符；`tests/gateway-runtime.Tests.ps1` 已覆盖 Rust/Python、candidate/cloud foreign 与故意停止。
- 云端 broker 与 Rust 网关两种语言并存；这是范围决定，不是语言纯度问题。
- 本机没有 broker 的 drain、投影和实例通道；云端没有 Windows 用户会话的可见 Edge。

## 后续候选

- 本机长期可用命名 Job Object 或实例 token 替代命令行匹配，减少对参数文本的依赖。
- broker 迁 Rust 单独立项；见对应 issue，不与常驻数据面迁移混在一起。
