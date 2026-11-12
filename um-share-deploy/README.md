# 分享链运维入口

本目录提供受限部署、broker 安装、看门狗与边界验收。日常启停、认证和单层维护使用控制台分享页；装配与权限要求见 [SHARING.md](../SHARING.md)。

8100 转发独立的 chat2api 后端 8110，私人 8000 的凭据、并发和账本保持隔离。Tailscale、连接、入口与 Funnel 分层核验，未知不能当作成功。

## 部署与恢复

正常关闭过程中，监听记录与进程身份查询可能跨过进程退出时刻。看门狗会再查询一次端口，只有查询完成且已无监听才按进程缺席处理，并沿用正常关闭保护；端口仍被占用或查询失败时继续按身份未确认处理，停止意图不能豁免安全检查。

`run-deploy-guarded.ps1` 提供 Prep、Watchdog、Start、Stop 阶段；`install-share-broker.ps1` 安装受限运维入口。这些操作涉及管理员权限、任务、WFP、防火墙或进程，先核对当前授权、原状态和回退材料。旧实验回执不能代替现状。

SQLite 分享存储是 Key 与设置的权威来源，运行配置由它投影。Key 管理走控制台或 `../share_admin.py`，不直接改旧副本、不把历史明文文件当作当前 Key 列表。

看门狗任务登记部署时 PowerShell 7 宿主的绝对路径，`Watchdog` 阶段须有界确认任务为 `Running` 才算成功。若最后运行结果为 `0x80070002`，先核对任务实际执行路径；SYSTEM 与任务计划程序可能仍持有安装 PowerShell 7 之前的 PATH。看门狗子检查沿用自身宿主目录，broker 的 Windows 原生命令输出按 OEM 编码读取，PowerShell 7 输出按 UTF-8 读取，避免中文任务状态被乱码误判。更新运行副本后还须重启 broker 并核对真实状态，源码修复不代表运行进程已载入。 模块切换要核对源码、受保护副本与安装清单的哈希，保留旧文件和任务定义用于回退；运行包装器按部署配置生成，不用源码模板直接覆盖。进程身份只对可能匹配的分享入口查询 owner，仍须满足完整路径、owner、创建时间及监听证据；安全复合检查独立限时 25 秒，并保留超时与启动失败的差别。

管理与实例管道共同遵循 Windows `ConnectNamedPipe` 契约：客户端在服务端开始连接前已接入时，`ERROR_PIPE_CONNECTED` 为 535，连接仍然有效；539 是另一类系统状态，不能当作连接成功。更新共享管道模块后，须分别重载 broker 和已运行的 8100 实例，保留 8110 账号会话。[Microsoft 原生接口说明](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-connectnamedpipe)。

## 验收

- `verify_8100.py`、`verify_redeploy.py`、`verify_redeploy2.py`：入口鉴权与模型调用。
- `test_stability.py`：流式、取消与槽位释放。
- `verify-scenarios-elevated.ps1`、`verify-scope-live-elevated.ps1`：隔离与范围。
- `run-d4-experiment.ps1` 及配套文件：受限身份实验。

验收可能计费或改变系统状态，须使用当前窗口与测试 Key。历史身份、编号和脚本存在不代表当前部署或已通过。

电脑需联网、保持唤醒，监督入口与安全限制须有效。网络、认证、进程和公网可达性分开判断；重启不证明闭环。限制丢失时看门狗失败关闭，恢复按当前 SHARING.md 与代码执行。运行目录可能需要管理员权限，凭据、请求正文和账本不得公开。
