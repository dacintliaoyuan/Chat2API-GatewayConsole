# Rust 重写契约冻结（#37）

- 状态：冻结（#37 交付；随父 spec #36 评审后生效）；本文件不包含 Rust 实现。
- 基线：`integration/rust-contract` @ `d131333`（2026-11-14 11:26:56 +0800）。
- 测量时点：2026-11-14 19:48（UTC+08:00）。
- 范围：8003 分享网关、云端控制台（默认 8002）、broker、Key 管理、用量账本、display、Cloudflare 检查、部署与备份。
- 不包含：端口重排（已由 #40 完成，本契约只记录配置事实）、前端重写、真实云端 systemd/UM 登录验收；UM 登录执行器的 HTTP/状态边界见 §5.4（#69），真实浏览器与 MFA 仍由维护者验收。

本文件把“保留的最终功能集”写成可测试的 HTTP/API 契约。Rust 只 port 这里列出的行为；任何未列出的端点、字段、错误码都不进入 Rust 轨道。

## 1. 冻结规则

1. **端点以本文件的表为准**。8003 的分享白名单只有 `/v1/models`、`/v1/chat/completions`、`/v1/messages`、`/v1/responses`（`um-cloud-deploy/cloudflared/check-ingress.py:26`）；云端控制台的 `share-management` 面只包含下面列出的固定路由，不做任意 URL/action 代理。
2. **成功、合法空结果、取消、暂时失败、配置错误、数据损坏必须可区分**。未知不折成 0、false 或空列表；读不到就返回错误或 `null` 加原因。
3. **明文只在一次性响应里**。Key 明文只在 create/rotate 的即时 no-store 响应和单 Key readback 里出现；不进入日志、操作记录、列表、URL 或 Rust 错误消息。
4. **副作用只走已列出的边界**。Rust 不得新增第二套 SQLite、第二套 broker、第二个 8003 启动者或第二个 display relay。
5. **端口不混入本轨道**。目标契约是云端控制台 8002、云端网关 8003、display 8097；旧 8100/8096/18096/5901 不保留兼容别名，Rust 只读环境变量，不硬编码端口。

## 2. 端口与进程拓扑

### 2.1 当前部署与仓库默认值

| 端口 | 服务 | 当前云端部署（旧 release，2026-11-14 实测） | 仓库默认值 @ `d131333` | 当前仓库默认值（目标契约） | 兼容窗口 |
|---|---|---|---|---|---|
| 8000 | 本机 chat2api / UM 主链 | 不适用（云端不装配） | 8000（`chat2api/chat2api.py` 默认） | 8000 | 不变 |
| 8001 | 本机控制台宿主（Rust；Python 回滚） | 不适用（云端不装配） | 8001（Python 回滚） | **8001** | 无兼容窗口 |
| 8002 | 云端控制台宿主 | Rust 8002（2026-11-17 切流）；Python 回滚 8096 | 18096（旧） | **8002** | 无兼容窗口 |
| 8003 | 云端网关（合并进程） | Rust 8003（2026-11-17 前已切流） | 8100（旧） | **8003** | 无兼容窗口 |
| 8097 | display x11vnc | 旧部署 5901，仅 loopback | 5901（旧） | 8097 | 无兼容窗口 |
| 5173 | vite 开发服务 | 不适用 | 5173（`check_port_bindings.py` 的 `PORT_ALLOCATIONS`） | 5173 | 不变 |
| 8099 | 候选预览 | 不适用 | 8099（AGENTS.md） | 8099 | 不变 |

说明：

- 目标契约：云端控制台 8002、云端网关 8003、display 8097；旧 8100/8096/18096/5901 不作为 Rust 目标，Rust 切换时一次性生效。
- 没有兼容窗口：同一时刻只监听目标端口；旧端口监听按未登记处理。Python 回滚 release 仍使用 8100/8096，只作回滚目标，不暴露公网。
- 2026-11-18 云端 8003/8002 已切 Rust（release `s2-20261118a`）；Python 回滚 release `s2-20261117b` 仍安装，Python 闭包已从仓库退役（#90）。当前/回滚端点以这张表为准：
- 2026-11-19 16:30 热修复 `fc56c61f…` 覆盖 `/opt/um-share/bin/um-gateway`（含 #112）；它不在 `s2-20261118a` 制品内，下一次 release 包含 #112。观察窗与证据见 [docs/cutover-8003-20261118.md](cutover-8003-20261118.md)。

| 状态 | release | 8003 网关 | 控制台 unit | 控制台端口 |
|---|---|---|---|---|
| current | `s2-20261118a` | `um-share-gateway.service`（Rust `um-gateway`） | `um-share-console.service`（Rust `um-console`） | 8002 |
| release 回滚 | `s2-20261117b` | `um-share-gateway.service`（Rust） | `um-share-console-python.service`（release 自带 `usagesrv.py`） | 8096 |
| 单元回滚 | 同 current | 不变 | `um-share-console-python.service`（release 自带） | 8096 |

`console.env` 的 `USAGESRV_PORT` 随目标 runtime 同步；Python 回滚单元在 `ExecStart` 用 `/usr/bin/env USAGESRV_PORT=8096` 固定端口，不依赖 `console.env` 的旧值。
- 非法端口值继续 fail closed；Rust 不得把旧端口写死，也不得同时监听新旧两个端口。
- 8003 分享段在 Rust production 模式下只允许 loopback 绑定；非 loopback 例外尚未实现，需要时另行登记。

### 2.2 进程与 systemd 边界

```text
systemd
├── um-share-gateway.service  → share/broker_ops.py → um-gateway :8003
├── um-share-console.service  → um-console :8002
├── um-share-console-python.service → usagesrv.py :8096（回滚）
└── um-share-display.service  → Xvfb :99 + x11vnc :8097
```

- 8003 只由 broker 拉起：`um-share-gateway.service` 的 `ExecStartPost` 调用 `um-share-gatewayctl.py start`（目标契约；当前 Python unit 仍叫 `um-share-broker.service`）。
- 云端控制台默认只监听 loopback 8002（旧 18096 不保留兼容别名）；公网入口只反代 8003 分享白名单，不反代控制台或 display（`um-cloud-deploy/README.md`）。
- Python 回滚控制台由 `um-share-console-python.service` 在 loopback 8096 提供；它不删除 Rust unit，回滚只切 unit 与 `console.env` 的 `USAGESRV_PORT`。
- 控制台 unit 对 `/run/um-share` 只读、`NoExecPaths` 禁止直接执行 `chat2api.py`，因此控制台不能成为第二个 8003 启动者（`um-cloud-deploy/systemd/um-share-console.service`）。
- 部署 unit 的 `KillMode=control-group` 是最终 cgroup 围栏；`systemctl stop` 不留下孤儿进程。

### 2.3 Rust 二进制启动模式

- Rust 网关二进制默认以 candidate 模式启动：只绑定临时 loopback 端口（`--port 0`），拒绝 8000/8003/8002/8100 及其他任何非零端口；candidate 不作为生产服务由 systemd 拉起。
- production 模式必须显式选择 `--mode production`：只允许绑定 `127.0.0.1:7998`、`127.0.0.1:7999`、`127.0.0.1:8000`、`127.0.0.1:8003` 或 `127.0.0.1:8002`；非 loopback 主机、端口 0、8001/8100/8096 等其他端口都在 bind 前拒绝。7998/7999 只给桌面 App 的本实例/测试实例，现有 8000/8001 保留。
- 角色由显式 `--role local|cloud` 选择；未指定时按端口推导：7998/7999/8000 → local，8002/8003 → cloud，candidate 的临时端口保持 cloud。非法角色值 fail closed。
- 两种模式共用同一套路由、鉴权、账本和错误契约；模式只改变绑定策略与启动入口，不改变 parity 行为；角色只改变本机专属面是否装配。
- `--mode worker` 与 `--mode ingress` 是本机 8000 的拆分形态，见 §2.7；云端 8002/8003 仍用单进程 production 模式。
- 部署 unit 用 `--mode production --port 8003` 或 `--mode production --port 8002` 启动，并用 `/health`、`/ready` 验收；unit 文件由部署轨道维护，不在本契约的 Rust 代码范围内。
- `um-console` 的 production 模式按角色绑定：cloud 8002、local 8001；candidate 仍只绑临时 loopback 端口。本机 8001 的启动与回滚见 §2.5。

### 2.4 本机 8000 启动器与回滚

- 构建：在 `rust/` 下运行 `cargo build --release -p um-gateway`，产物为
  `rust/target/release/um-gateway-candidate.exe`。
- 启动器：`lib/gateway-runtime.ps1` 的 `chat2api` 描述符 `StartPath` 指向该二进制。
  `StartArguments`（`--mode production --port 8000 --role local ...`）仍是单进程形状，
  只在 `.bat` 回滚或过渡期使用；`StartWorkingDirectory` 固定为仓库根，使 Rust 的
  默认 token/profile 路径与 Python 8000 一致（`chat2api/token.json`、
  `chat2api/.edge-profile`）。`StartAction` 仍是 `HiddenBatch`，健康检查仍是
  `http://127.0.0.1:8000/health`，停止仍是 `StopStrategy=GracefulHttp` +
  `http://127.0.0.1:8000/admin/shutdown`，启动超时与 start/stop/restart 语义不变。
- #133 A 起 8000 拆成 ingress 与 worker 两个进程；Rust 侧接口见 §2.7。
  #133 A2 已把 PowerShell 描述符切到 `StartComponents`：`.exe` StartPath 按
  worker → ingress 启动，`.bat` 回滚仍走单进程 `StartArguments`。
- 停止边界：Rust 的 `/admin/shutdown` 返回 202 `{"status":"stopping"}`，与 Python 的
  202 一致；`StopStrategy=GracefulHttp` 先发送该请求，再由既有的 scoped stop 兜底，
  Rust 私有实例可优雅退出。
- 切流状态（#81，2026-11-17）：本机 8000 **已切到 Rust**（`um-gateway-candidate.exe
  --mode production --port 8000 --role local`）。切流前 parity 用同一份
  `chat2api/token.json` 与 `chat2api/.edge-profile` 启动 Python 8005 与 Rust 8000 对比，
  结果为 `parity=passed`：
  - `/health`、`/v1/models`、通用 `/api/*` GET/POST、`/admin/auth/status` 与
    `/admin/auth/verify` 的状态码、响应体形状与稳定语义一致。
  - 修复项：`f01550c` 让上游 `Host` 头在默认端口省略 `:443`（此前 Rust 对 UM 发送
    `Host: chat.genai.um.edu.mo:443` 被 503）；`45d8808` 把思考内容计入业务输出，
    长思考不再触发 `upstream_prelude_overflow`；`category_for` 把
    `upstream_request_rejected` 归为 `invalid_request_error`。
  - 复现命令：

    ```powershell
    python tests/local_8000_parity.py `
      --python-url http://127.0.0.1:8005 `
      --rust-url http://127.0.0.1:8000 `
      --control-token-file data/admin-control-token `
      --evidence tmp/local-8000-parity-evidence.json
    ```

    探针只读；错误体按 `type`/`code`/`param`/`detail.upstream_status` 归一化，
    顶层附加诊断不计入形状；两侧先请求 `/ready` 让 `admin/auth/status` 的
    `evidence` 有值。
- 切流步骤（已执行）：`pwsh -NoProfile -File invoke-console-runtime-json.ps1
  -Action restart -Key chat2api`，8000 跑 Rust；验证 `/health`、`/v1/models`、
  通用 `/api/*`、`/admin/auth/status|verify` 与 `/admin/shutdown` 均通过。
- 回滚（已演练）：只改 `StartPath` 回 `start_chat2api.bat`，重启后 Python 8000
  `/health` 200、`/v1/models` 200；再改回 Rust 并重启，`/health` 200、
  `/v1/models` 200。`StopIdentities` 同时覆盖 Rust 与 Python，回滚后 stop/restart
  仍按 Python 私有实例工作。
- 认证巡检：Rust 网关新增每 5 分钟 `auth_watch`（拒绝后 2 倍退避到 30 分钟），
  `/admin/auth/status` 的 `evidence` 由巡检写入 `source=auth_watch`；凭据有效时
  不再等请求触发重登。
- 上游故障记录（#103）：本机生产 8000 默认持久写 `data/upstream-faults.json`
  （`CHAT2API_UPSTREAM_FAULT_LOG` 可覆盖）；8001 默认只读同一路径，不探测、
  不写记录。
- 验证：`pwsh -NoProfile -File tests/gateway-runtime.Tests.ps1 -LauncherOnly` 验证
  描述符、健康边界与回滚；真实 8000 的 parity、切流与回滚演练由 #81 执行并记录在本节。

### 2.5 本机 8001 启动器与回滚

- 构建：在 `rust/` 下运行 `cargo build --release -p um-console`，产物为
  `rust/target/release/um-console-candidate.exe`。
- 启动器：`lib/gateway-runtime.ps1` 的 `usage` 描述符 `StartPath` 指向该二进制，
  `StartArguments` 为
  `--mode production --bind-port 8001 --report-port 8001 --role local --gateway-port 8000 --state-file data/console-state.sqlite3 --dist-dir web/dist --usage-log data/usage-chat2api.jsonl --service-log chat2api/chat2api.log`，
  `StartWorkingDirectory` 固定为仓库根。显式 `--role local` 会应用 Rust CLI 的本机
  路径默认值（`data/admin-control-token`、`data/usage-session-code`、
  `data/share-store.db`、`data/console-locks`、`web/dist`），因此与 Python 8001 一致，
  启动器不再需要 `UM_SHARE_ROLE` 环境覆盖。`StartAction` 仍是
  `HiddenBatch`，健康检查仍是 `http://127.0.0.1:8001/health`，停止仍是
  `StopStrategy=Port`。
- 停止边界：`StopIdentities` 同时覆盖 Python `usagesrv.py` 与 Rust
  `um-console-candidate.exe`；候选（`--mode candidate`）与云端（`--role cloud` /
  8002）形状保持 foreign，8001 的 loopback 监听是唯一权威。`Port` 停止只终止
  8001 loopback 上的已确认私有实例，不触碰 Tailscale Serve 的独立监听。
- 切流状态（#85，2026-11-17）：本机 8001 **已切到 Rust**
  （`um-console-candidate.exe --mode production --bind-port 8001 --role local`）。
  切流命令为 `pwsh -NoProfile -File invoke-console-runtime-json.ps1 -Action restart
  -Key usage`；切流后 `/health`、`/`、`/api/summary`、`/api/console/services`、
  `/api/console/um/*`、#86 六个只读面、`/api/session` 与 `model.catalog` 均通过。
- 切流前 parity（#85 AC2）：Python 8001 保持生产页面，Rust candidate 以
  `--mode candidate --role local --bind-port 0 --report-port 8001 --gateway-port 8000
  --dist-dir web/dist` 绑定临时 loopback 端口。两边读同一份 `web/dist`、同一份
  `chat2api/chat2api.log` 与 `data/dsh-ledger-20260902.json`；账本使用最后
  200000 行（Python 8001 `_read_jsonl` 的上限），因此 summary 的
  requests/tokens/models/days 可直接比较。结果 `parity=passed`：
  - 静态页：`/` 1221 字节、`/assets/index-qAUdSyPV.js` 826 字节，SHA-256 一致
    （`6f4188f5…b68ac`、`adc66615…55af6`）。
  - `/api/summary`：`services.chat2api.requests=200000`、models=7、days=14，
    tokens 与 DSH 快照一致。
  - #86 六个只读面：`usage-tokens`、`usage-rates`、`stats`、`log-sources`、
    `log-entries`、`log-overview` 均 200；`log-overview` 803 行，
    `ts`/`level`/`message`/`event` 一致。
  - 其余 local 读面：`services`、`health`、`um/today`、`um/trend`、`um/requests`、
    `um/maintenance`、`gateway-connection?service=chat2api` 与 `operations` 列表
    均 200 且归一化后一致。
  - #115：`um/login-rounds` 只读本机 8000 写入的 `data/login-history.jsonl`
    （7 天 / 4 MiB，轮转读取）；缺失是合法空列表，损坏是错误，读取不探测、
    不登录、不追加。
  - `/api/session`：无效码两边均 403 `{"error":"invalid bootstrap code"}`。
  - `model.catalog`：两边 201 queued → succeeded，7 个模型目录一致。
  - 归一化：移除时钟字段（`checked_epoch`、`generatedAt`、`_generated_epoch`、
    `_cached_age_seconds`、`collected_at`）、不透明游标/行键（`key`、
    `next_cursor`、`next_cursors`、`earlier`、`file_key`）与实现诊断字段
    （`scanned_bytes`、`window_start_offset`、`rows_read`、`ms`、`ts_norm`、`at`）；
    无法解析的行 `event=None` 与全 null 事件对象等价。
  - 已记录但不纳入六面门槛的差异：`actions` 目录（Python 12 个启用动作，
    Rust 本机只启用 `model.catalog`，共享 7 个；其余动作提交返回 403
    `action_disabled`）、`services.process_ids`/`detail`、
    `health.services[].ms`、`gateway-connection.evidence/reason/key_in_use`、
    `summary` 刷新失败时的 `_refresh_error`/`_stale` 附加键、旧 `/api/console/logs`
    （Rust 本机未装配）。`um/logs` 的时间戳形状、游标与 scope label 已按
    Python `console/log_reader.py` 对齐。
  - 证据：`tmp/local-8001-parity-evidence.json`；探针命令为
    `python -I tests/local_8001_parity.py --python-port 8001 --rust-port <候选端口>
    --root . --evidence tmp/local-8001-parity-evidence.json`。
- 会话交换（#85 AC3）：根目录 `一键部署本机网关.bat` 部署后调用 `scripts/Open-GatewayConsole.ps1`：
  宿主未运行则先启动，随后用 `data/admin-control-token` 的 `X-Admin-Control`
  请求 `POST /api/session/reissue`，把一次性引导码放进页面 URL 的 `#s=` fragment，
  浏览器再 `POST /api/session` 换成 HttpOnly `usage_session` Cookie。切流后在 Rust
  8001 上实测：`POST /api/session/reissue` 200（返回 `ok=true` 与一次性 code，
  值不写入证据）、`POST /api/session` 200 `{"ok":true}` 并 `Set-Cookie:
  usage_session=…; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200`；带该 Cookie
  请求 `GET /api/share/status` 200，页面 `/` 200。引导码与 Cookie 值只留在原存储，
  不进入日志、提交或本文件。
- 回滚演练（#85 AC4，2026-11-17）：只改 `StartPath` 回
  `start_usage_service.bat`；`invoke-console-runtime-json.ps1 -Action restart -Key usage`
  返回 `Restarted`，只终止 Rust 私有实例。重启后 Python `usagesrv.py` 监听
  `127.0.0.1:8001`，`/health` 200、`/` 200。再把 `StartPath` 改回 Rust 二进制
  并重启，`/health` 200、`/` 200。回滚不需要重装，也不需要改端口；
  `StartArguments` 对 batch 回滚无影响，`StopIdentities` 仍同时识别 Python 与 Rust。
- 验证：`pwsh -NoProfile -File tests/gateway-runtime.Tests.ps1 -LauncherOnly` 验证
  描述符、健康边界、停止身份与回滚；真实 8001 的 parity、切流与回滚演练由 #85
  执行并记录在本节。

### 2.6 本机 8001 服务动作（#92）

- `service.start` / `service.stop` / `service.restart` 由 Rust 直接调用
  `invoke-console-runtime-json.ps1 -Action start|stop|restart -Key chat2api
  -CallerHoldsLock`；服务身份、启停策略、端口与进程归属校验、停止范围仍在
  `lib/gateway-runtime.ps1`，不经过 Python sidecar。
- 服务锁：operation worker 先持有 `service:chat2api` 资源锁，再以
  `-CallerHoldsLock` 调用入口；第二个动作等待 15 秒后失败
  `resource_busy_timeout`，且不启动入口。Windows 共享冲突（winerror 32）按忙
  重试，不误报 `lock_open_failed`。
- 兼容形状：成功结果透传
  `{key,status,action,detail?,terminated_process_ids?}`；
  `Started`/`AlreadyRunning`/`Stopped`/`AlreadyStopped`/`Conflict` 均来自
  runtime。停止范围无法确认时入口返回 `ok=false`，Rust 记录
  `runtime_unavailable`（与 Python adapter 的 `_invoke_wrapper` 一致）；非 JSON
  输出与超时同样为 `runtime_unavailable`，退出码 0 的 `ok=false` 为
  `runtime_error`。
- 验证（#92，2026-11-18）：`cargo test -p um-console --test
  local_service_actions` 10 passed。真实入口集成用例把仓库脚本复制到临时目录并只
  替换描述符，覆盖 Started → AlreadyRunning → Stopped（含
  `terminated_process_ids`）→ AlreadyStopped 与端口冲突 `Conflict`；
  `python_exe` 指向不存在路径时 `service.start` 仍成功。

### 2.7 本机 8000 入口与 worker（#133 A）

- 同一个 `um-gateway-candidate` 二进制增加 `--mode worker` 与 `--mode ingress`；
  `--mode candidate|production` 仍是单进程形态，云端 8002/8003 不使用拆分形态。
- worker：本机模型处理进程，只绑定 `127.0.0.1` 的 `--port`（生产 `0`）。监听成功后
  用临时文件 + rename 原子写 `--worker-port-file`（十进制端口 + 换行）；固定测试端口
  必须在 1024 以上且不在治理端口表内。优雅退出时删除该文件。
- ingress：本机模型 API 入口，绑定 `127.0.0.1:8000`（测试可 `0`）；每次连接前重新
  读 `--worker-port-file`，不缓存、不猜端口。worker 端口文件出现且能连上就转发。
- 入口参数：`--worker-port-file` 默认 `data/chat2api-worker.port`；
  `--desired-state-file` 可省略，省略时读环境变量
  `SELFUSEAI_GATEWAY_DESIRED_STATE`（文件路径）；`--recovery-wait-seconds`
  默认 `180`；`--recovery-queue-max` 默认 `64`；`--recovery-queue-max-bytes`
  默认 `67108864`（64 MiB）；`--boot-id` 可省略，省略时读
  `SELFUSEAI_GATEWAY_BOOT_ID`。
- 运行意图：入口读 desired-state JSON 的 `state` 与 `boot_id`，仅当
  `state == "running"` 且 `boot_id` 与入口 boot id 一致时排队；`stopped`
  返回 503 `gateway_stopped`，缺失、不可读、格式不符或 boot 不一致返回 503
  `gateway_not_running`。两者都不排队；停止意图在读取请求 body 之前生效。
- 只有模型 API 请求（`/v1/*`、`/api/*`）进入恢复等待队列；`/admin/*`
  （`/admin/shutdown` 单独处理）和 `/` 在 worker 不可连接时立即返回 503
  `gateway_not_running`，不排队。`/health`、`/ready` 仍代理 worker。
- 未转发请求在 worker 不可连接时排队，最多等待 `--recovery-wait-seconds`；
  worker 端口文件出现且能连上后各转发一次。客户端断开时从队列移除且不转发。
- 已向 worker 写出过请求字节后不重新排队、不重发；worker 中途断开返回 502
  `gateway_worker_lost`，`detail.forwarded` 为 true。没有写出任何请求字节时
  `detail.forwarded` 为 false。
- 排队有界：最多 `--recovery-queue-max` 个请求、`--recovery-queue-max-bytes`
  个缓冲 body 字节；超限返回 503 `gateway_recovery_queue_full`。
- 错误契约：

  | 场景 | HTTP | `error.code` | `should_retry` | `Retry-After` | `detail.forwarded` |
  |---|---|---|---|---|---|
  | 等待 `--recovery-wait-seconds` 仍未恢复 | 503 | `gateway_recovery_timeout` | false | — | false |
  | 排队数量或字节超限 | 503 | `gateway_recovery_queue_full` | true | 5 | false |
  | 显式停止 | 503 | `gateway_stopped` | false | — | false |
  | 运行意图缺失/未知 | 503 | `gateway_not_running` | false | — | false |
  | 已转发后 worker 断开 | 502 | `gateway_worker_lost` | false | — | true |

- 错误 `message` 文本包含错误码本身（例如 `gateway_recovery_timeout`），因为
  Claude Code 只显示 message 文本；Claude Code 2.1.327 只把
  `X-Should-Retry: false` 当作停止重试的信号，不解析 body 的 `should_retry`。
  除 `gateway_recovery_queue_full` 外的入口错误都带 `X-Should-Retry: false`。
- `/v1/messages`（含 `/api/v1/messages`）用 Anthropic 错误形状，其余路径用
  OpenAI 形状（复用 `GatewayError::response_for`）；错误响应带
  `Cache-Control: no-store` 与 `X-Request-Id`。
- 转发使用原始 HTTP/1.1 请求字节，并给 worker 发 `Connection: close`；响应转发
  支持 `Content-Length`、chunked、close-delimited 与 SSE，逐块写出，不缓冲整个
  上游响应。客户端 keep-alive 在响应自定界（长度或 chunked 终止块）时保持，
  close-delimited 时关闭。
- `/health`、`/ready` 代理 worker，worker 不在时 503；`/admin/shutdown`
  先校验控制令牌（配置时），再释放排队请求（503 `gateway_stopped`），随后转发
  worker 关闭；worker 不可达也照常退出（`shutdown_request=true`），不因不可达
  拒绝入口退出。
- worker 的优雅停止顺序：先置 shutdown、唤醒 accept 并 join 监听线程（端口先
  释放），再停 login executor 与 auth watcher；避免入口等待端口释放时超时。
- PowerShell 接入（#133 A2，2026-11-21）：
  - `StartComponents` 先 worker 后 ingress；worker 参数为
    `--mode worker --port 0 --role local --worker-port-file <绝对路径> ...`，
    ingress 为 `--mode ingress --port 8000 --worker-port-file <绝对路径>
    --desired-state-file <意图文件> --boot-id <当前 boot> ...`；两个子进程都注入
    `SELFUSEAI_GATEWAY_BOOT_ID` 与 `SELFUSEAI_GATEWAY_DESIRED_STATE`。
  - 启动幂等：两者健康时返回 `AlreadyRunning`；ingress 健康而 worker 缺失时只启动
    worker；两者都缺失时按 worker → ingress 启动；组件残留只按自身身份清理，不重启
    健康组件。worker 端口文件同时接受相对仓库根的描述符值和测试注入的绝对路径。
  - 停止先写 `stopped` 意图，再依次 POST ingress `/admin/shutdown`、worker
    `/admin/shutdown`（worker 端口读 `WorkerPortFile`），最后按身份兜底清理。
  - inspection 在 worker 或 ingress 处于 Present/Residual 时走拆分分支，worker
    `Conflict` 也留在拆分分支（它正面证明拆分形态，不能回落到单进程形状被误认成
    `Residual`）；两者都缺失、或只有 ingress `Conflict`（8000 被未确认进程占用）时
    回落到单进程形状，使 Python 回滚与过渡实例仍可识别。8000 的归属只判断
    loopback 监听，tailnet IPv4/IPv6 监听单独保留；chat2api 的 `/health` 要求 200
    （ingress 在 worker 缺失时返回 503）。
  - worker 与 ingress 的健康分别判断：两者都健康才是 `Running`；任一组件
    `Present/Residual` 但未全健康是 `Degraded`；拆分分支里只有两者都 `Absent`
    才回落到单进程形状（最终可能是 `Stopped`）。worker 缺失而 ingress 仍在
    8000 监听（`/health` 503）时是 `Degraded`，控制台入口只启动缺失的 worker，
    不再走整对停止。
  - 陈旧 `WorkerPortFile`（#133 A2 返修，2026-11-21）：worker 非优雅退出后
    记录可能残留，端口还可能被无关进程复用。没有 worker 身份进程时该记录一律视为
    陈旧：健康探测、watch 进程查询和 inspection 都不把它当作 worker 监听；端口占用
    只有在监听者被确认为 worker 身份时才计入组件状态。`Start-GatewayComponents`
    在启动 worker 前删除该文件；强制停止 worker 后也删除它。
  - 启动参数引用（#133 A2 返修，2026-11-21）：`Start-GatewayObservedProcess`
    对含空白或引号的参数加双引号，并按 Windows `CommandLineToArgvW` 规则转义
    反斜杠与引号；不含空白的固定参数行保持原样。
  - 停止等待（#133 A2 返修，2026-11-21）：组件停止先 POST ingress 与 worker
    的 `/admin/shutdown`，再按 `StopTimeoutSeconds`（默认 5 秒）轮询 worker
    退出，超时才按身份兜底强制清理。组件启动的 worker 等待计入同一个
    `StartTimeoutSeconds` 预算，外层不再追加第二个完整超时。
  - 退出证据范围（#133 A2 返修，2026-11-21）：watch 的
    `Get-DefaultExitEvidence` 只解析 Python 回滚实例的
    `listening host=... port=8000 pid=...` 与
    `exit begin ... reason=admin_shutdown` 行。Rust worker/ingress 不写 listening
    行，因此拆分形态下退出证据恒为“无证据”，watch 只按进程/端口状态判定；
    这是已知未覆盖范围，不是“Rust 进程没有退出”。
  - watch 的进程查询只认 worker（`--mode worker`），listener 检查与退出证据使用
    `WorkerPortFile`；self-test 固定 worker 端口并覆盖绝对路径端口文件、陈旧端口
    记录和 tailnet 监听。
  - 本节的 Rust 接口不改变 8001 控制台和云端 8002/8003 的单进程形态。
- 验证（#133 A，2026-11-21）：`cargo test -p um-gateway --test ingress`
  18 passed；其中真实 TCP 用例使用真实 `GatewayServer` + `ChatStub` 作 worker，
  其余用假 worker 覆盖恢复、超时、取消、队列上限、已转发断开、SSE、keep-alive、
  `/health`/`/ready`、`/admin/*` 不排队与 `/admin/shutdown`（含错误令牌）。
- 验证（#133 A2 返修，2026-11-21）：
  - 工作目录：`D:/git/github/Chat2API-GatewayConsole/.worktrees/133-ps`。
  - `pwsh -NoProfile -ExecutionPolicy Bypass -File tests/gateway-runtime.Tests.ps1`：
    582 passed，0 failed（含 `Get-GatewayServiceInspection` 的 worker 缺失 + ingress
    在监听、双组件在监听但未健康、worker `Conflict` + ingress `Absent`，控制台入口
    `Degraded` 分支，`Select-GatewayStopScope -Identities` 收窄、重复停止、worker
    端口上的 tailnet 监听、陈旧 `WorkerPortFile` 与含空格路径的观察器启动）。
  - `pwsh -NoProfile -ExecutionPolicy Bypass -File lib/watch-chat2api.ps1 -SelfTest`：
    PASS（含陈旧 worker 端口记录与 tailnet 监听在 worker 端口）。
  - `pwsh -NoProfile -ExecutionPolicy Bypass -File tests/console-runtime.Tests.ps1`：
    101 passed，0 failed（既有控制台入口回归；本轮 `ALL PASS`）。
  - 未验证范围：真实 Rust worker/ingress 的崩溃与端口复用、真实 Edge 登录、
    云端 8002/8003、桌面 App 壳；测试用替换的进程表、监听快照、HTTP 与
    `Start-Process` 边界，不启动真实服务。

## 3. 云端 idle RSS 基线

### 3.1 测量方法

- 工作目录：`D:/git/github/Chat2API-GatewayConsole/.worktrees/37`（本地 worktree）。
- SSH 命令（ticket 指定）：

```bash
ssh -F C:/Users/dacint/.ssh/aliyun-um-ecs.conf -o BatchMode=yes -o ConnectTimeout=15 um-share-ecs \
  'ps -eo pid,ppid,user,rss,etime,comm,args --sort=-rss | grep -Ei "chat2api|usagesrv|Xvfb|x11vnc|chrom" | grep -v grep'
```

- 补充只读读取：

```bash
ssh -F C:/Users/dacint/.ssh/aliyun-um-ecs.conf um-share-ecs \
  'systemctl is-active um-share-broker.service um-share-console.service um-share-display.service; \
   cat /proc/<pid>/status | grep -E "^(Name|VmRSS|VmHWM|Threads):"; free -m'
```

- 云端版本：Ubuntu，内核 `7.0.0-38-generic` x86_64；Python `3.14.4`；发布版本 `s2-20261113b`（manifest `generated_at=2026-11-13T04:01:52Z`）。
- 测量时间：2026-11-14 19:48（UTC+08:00）；系统 uptime 2 天 19 小时；`um-share-broker.service`、`um-share-console.service`、`um-share-display.service` 均为 `active (running)`。
- 两次采样相隔约 2 秒；旧部署 8100/Xvfb/x11vnc/broker 的 RSS 不变，旧部署 18096 从 111048 KiB 变为 111044 KiB（±4 KiB）。下表取第二次值。
- `idle` 判据：没有登录窗口、没有在途分享请求；`display-state` 读面为 `state=valid`，`operator.owner_present=false`；`/health` 的 watch 线程存活。读取本身只发只读 GET，不触发登录。
- RSS 是 `ps` 的 `rss`（KiB）与 `/proc/<pid>/status` 的 `VmRSS` 交叉核对值；RSS 包含共享页，不是 PSS，不能直接当作 Rust 进程的“净内存”。

| 目标 | PID | RSS KiB | RSS MiB | VmHWM MiB | 状态 | 说明 |
|---|---:|---:|---:|---:|---|---|
| 8100 chat2api.py（旧部署） | 95112 | 190004 | 185.6 | 210.8 | idle | 合并分享网关 |
| 18096 usagesrv.py | 103253 | 111044 | 108.4 | 132.5 | idle | 云端控制台宿主 |
| Xvfb :99 | 1580 | 24944 | 24.4 | 85.1 | idle | display 虚拟屏 |
| x11vnc | 1608 | 21836 | 21.3 | 24.2 | idle | 仅 loopback VNC |
| Chromium | — | — | — | — | 未运行 | 无浏览器进程；登录中/登录后未验证 |
| broker_ops.py（参考） | 92120 | 25460 | 24.9 | — | idle | 非票面五项，模块参考 |

系统内存（`free -m`）：total 1735 MiB，used 948 MiB，free 223 MiB，buff/cache 723 MiB，available 787 MiB；swap 2047 MiB 未使用。

未验证范围：

- **登录中/登录后 RSS 未在本契约的新代码上测量**：`docs/headless-login-acceptance.md` 记录了旧 release `s2-20261113b` 的一次真实登录中 Chromium RSS（约 852,228 KiB），不能当作本契约 headless 代码的读数。
- **Chromium idle RSS 不存在**：`pgrep` 未发现 `chrom`/`playwright`/`headless_shell` 进程；不能把“未运行”写成 0 MiB。
- **8002/8003/8097 未测量**：本契约的 idle 基线在旧部署 18096/8100/5901 上测得；目标端口未在云端复测。
- 本基线不是性能目标，也不能证明 Rust 一定更省内存；Rust 切片验收必须用同一命令、同一 idle 判据重新测量。

## 4. 契约覆盖矩阵

| 面 | 端点组 | 鉴权 | 请求/响应 | 错误码 | 流式 | 超时/取消 | 幂等 | 主要副作用 |
|---|---|---|---|---|---|---|---|---|
| 8003 | `/health`、`/ready` | 无 | JSON | 503/502 | 无 | 请求总预算 | 只读 | 读投影、UM 上游 |
| 8003 | `/v1/models` | 分享模式需分享 Key；非分享模式按本地 Key 规则 | JSON；目录可带 `limit`/`beta=true` | 401/404/503/502 | 无 | 请求总预算、认证等待预算 | 只读 | 读投影、UM 上游 |
| 8003 | `/v1/version` | 非分享模式按本地 Key 规则；分享模式 404 | JSON | 401/404/502/503 | 无 | 请求总预算 | 只读 | UM 上游 |
| 8003 | `/v1/chat/completions` | 分享模式 Bearer Key | JSON 进，JSON 或 SSE 出 | 400/401/413/429/502/503 | SSE 透传 | 首字节/静默/总预算 | 重试只在未提交前 | 追加账本、UM 上游 |
| 8003 | `/v1/messages` | 分享模式 Bearer 或 `X-Api-Key` | Anthropic JSON/SSE 字节透传 | 400/401/413/429/502/503 | SSE 透传 | 同上 | 重试只在未提交前 | 追加账本、UM 上游 |
| 8003 | `/v1/responses` | 分享模式 Bearer | Responses 请求，JSON 或 Responses SSE | 400/401/413/429/502/503 | Responses 事件流 | 同上 | 同上 | 追加账本、UM 上游 |
| 8003 | `/api/chat/completions`、`/api/v1/chat/completions`、`/api/v1/messages`、`/api/v1/responses` | 非分享模式本地 Key 规则；分享模式不经分享 Key 门，公网不反代 | JSON/SSE | 400/401/413/502/503 | 同对应 `/v1` 路由 | 同上 | 同上 | 追加账本、UM 上游 |
| 8003 | `/v1/embeddings` | 非分享模式 Bearer；分享模式 404 `route_not_shared` | JSON 透传 | 400/401/413/404/502/503 | 无 | 请求总预算 | 无账本行 | UM 上游 |
| 8003 | `/api/v1/embeddings` | 非分享模式 Bearer；分享模式不经分享 Key 门，公网不反代 | JSON 透传 | 400/401/413/502/503 | 无 | 请求总预算 | 无账本行 | UM 上游 |
| 8003 | `/admin/auth/*`、`/admin/share/revoke`、`/admin/shutdown` | 控制令牌 | JSON | 403 | 无 | 登录/撤销有界 | 浏览器、token/profile、撤销在途、停止服务 |
| 8002 | `/health`、`/api/summary`、`/api/console/*` 只读 | loopback Host/Origin；写面按路由：facade 需同源标记/控制令牌，operations 只受同源门 | JSON no-store | 400/401/403/404/409/410/413/502/503 | 无 SSE | display/操作/TTL | 读无；写按 Key/操作 | SQLite、账本、日志、broker |
| 8002 | `/api/console/share-management/*` 写面 | 同源标记或控制令牌 | JSON no-store | 400/403/404/409/410/413/503 | 无 | 写请求前端 20s 预算 | idempotency_key + row_version | SQLite、投影发布、安全日志 |
| 8002 | `/display`、`/display/` | 同源 + display 授权（display session、控制令牌或 Tailscale Serve 身份） | HTML/JS | 400/403/404/409/413/415/426/502/503 | 无 | grant 60s、session 300s、idle 900s | 单次 grant | operator 状态 |
| 8002 | `/display/launcher.js`、`/display/launcher-state.js`、`/display/<asset>`、`/display/session`、GET `/display/grant` | 同源 | JS/JSON | 400/403/404/409/413/415/426/502/503 | 无 | session 300s、idle 900s | 无 | noVNC 静态树 |
| 8002 | POST `/display/grant` | 同源 + 有效单次 grant | JSON | 400/403/404/409/413/415/426/502/503 | 无 | grant 60s | 单次 grant | grant 存储 |
| 8002 | `/display/websockify` | display session + operator generation | RFB WebSocket | 400/403/409/426/503 | RFB WebSocket | idle 900s、session 14400s | 单次 operator | x11vnc 网络、operator 状态 |
| broker | AF_UNIX 管理通道 | HMAC-SHA256 + 时间窗 + replay | 一行 JSON，最大 1 MiB | 固定结果码表 | 无 | 10s/90s/30s/130s | request_id 一次性 | 投影、pid、信号、Popen |
| Key 管理 | SQLite + `/api/share/*` | 管理面鉴权 | JSON | 400/401/404/409/410/503 | 无 | 发布 fail-closed | idempotency_key + row_version | SQLite、投影、安全日志 |
| 用量账本 | 只读查询 | 随宿主面 | JSON | 400/502/503 | 无 | 有界读取 | 只读 | 无写入 |
| Cloudflare | 配置校验 + 公网 DNS/TLS 检查 | 无凭据 | JSON 结果 | 检查器退出码 | 无 | 15s/60s/8s | 只读 | 不发配置变更 |
| 部署备份 | CLI | 文件权限/密钥 | JSON/归档 | 见 §12 | 无 | 排空 900s | 锁 + 幂等安装 | 文件、systemd、符号链接 |

## 5. 8003 分享网关

### 5.1 端点与鉴权

8003 是合并后的分享网关（`chat2api/chat2api.py`）。分享模式由 `--share-config` 显式开启；`ShareGate` 在进程内鉴权，不再有第二个监听或跨进程 HMAC 标记（`chat2api/share_auth.py:1245`）。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/`、`/health` | 无 | 健康快照 |
| GET | `/ready` | 无 | 凭据有效且上游可达才 200，否则 503 |
| GET | `/v1/models` | 分享模式：分享 Key；非分享模式：按本地 Key 规则 | 目录；支持 `limit`/`beta=true` 的受限查询 |
| POST | `/v1/chat/completions` | 分享模式：`Authorization: Bearer` | OpenAI Chat；`stream=true` 返回 SSE |
| POST | `/v1/messages` | 分享模式：Bearer 或 `X-Api-Key` | Anthropic Messages；字节透传，支持 SSE |
| POST | `/v1/responses` | 分享模式：Bearer | Responses 子集，JSON 或事件流 |
| POST | `/v1/embeddings` | 非分享模式：Bearer；分享模式 404 `route_not_shared` | OpenAI 兼容透传，不记账 |
| GET | `/v1/version` | 非分享模式：本地 Key（非 loopback 绑定时）；分享模式 404 `route_not_shared` | 版本与重试策略 |
| GET | `/api/models`、`/api/v1/models` | 非分享模式：本地 Key 规则；分享模式不经分享 Key 门，公网不反代 | 本机模型目录别名 |
| POST | `/api/chat/completions`、`/api/v1/chat/completions`、`/api/v1/messages`、`/api/v1/responses`、`/api/v1/embeddings` | 非分享模式：本地 Key 规则；分享模式不经分享 Key 门，公网不反代 | 本机兼容别名 |
| GET/POST | 未列入别名的 `/api/*` | 本地控制令牌；仅 local 角色装配，cloud 角色 501 `local_api_surface_s1a` | 上游 `api/*` 透传 |
| GET | `/admin/auth/status`、`/admin/auth/verify` | 本地控制令牌 | 认证状态观察；单次只读凭据核验 |
| POST | `/admin/auth/login`、`/admin/auth/cancel` | 本地控制令牌 | 启动/加入一轮登录；取消本轮登录（服务继续运行） |
| POST | `/admin/share/revoke` | 本地控制令牌 | 关闭某分享 Key 的在途请求 |
| POST | `/admin/shutdown` | 本地控制令牌 | 异步停止进程 |
| OPTIONS | 任意路径 | 无 | 204，CORS 允许方法与头 |

鉴权细节：

- 分享凭据只接受 `Authorization: Bearer <key>`；Messages 与模型目录还接受 `X-Api-Key: <key>`（`chat2api/share_auth.py:1192-1243`）。
- 同名凭据头重复、空值、超过 256 字符、Bearer 前缀错误、两种头值不同，一律 fail closed 为 401 `invalid_api_key`（`chat2api/chat2api.py:6196-6236`）。
- 分享 Key 的权威来源是投影文件；每次请求重新读取并校验 `enabled`、`secret_hash`、`secret_version`、`config_generation`（`chat2api/share_auth.py:1245-1320`）。
- 每 Key 并发、全局并发和每 Key 20 次/60 秒的滑动限速在进程内完成（`chat2api/share_auth.py:80-82,1300-1395`）。拒绝时 429 `sharing_limit_reached`，`reason` 区分 `per_key_concurrency`、`share_concurrency`、`key_rate_limit`、`state_not_accepting`。
- 非分享白名单的 `/v1/*` 路径在分享模式下返回 404 `route_not_shared`；分享模式只允许 `/v1/models`、`/v1/chat/completions`、`/v1/messages`、`/v1/responses`，因此 `/v1/version` 与 `/v1/embeddings` 只在非分享模式可用（`chat2api/chat2api.py:6196-6236`）。
- 目录只允许规范 `/v1/models`、`beta=true` 和单个 `limit`（1..1000，规范十进制）查询；其他查询 404 `route_not_shared`（`chat2api/share_auth.py:32-72`）。
- 目录默认附加 `--effort-model` 注册的 virtual effort 模型（`<base>-Fast/-Low/-Medium/-High`）；base 不在上游快照时不附加，已存在的 variant id 不重复。`X-Chat2Api-Models: raw-fresh` 跳过 virtual 模型、要求 fresh 上游结果并在响应带 `X-Chat2Api-Raw-Models: 1`（`chat2api/chat2api.py:6172-6235,8259-8291`）。
- `/api/*` 兼容别名是本机/非分享面，不在 Cloudflare 白名单内，也不经分享 Key 门；分享模式只保证四条 `/v1` 路径的公网语义（`chat2api/chat2api.py:6196-6236`、`um-cloud-deploy/cloudflared/check-ingress.py:26-142`）。
- 未列入别名的 `/api/*` 透传只在 local 角色装配：控制令牌 `X-Admin-Control`、loopback peer/Host、无 Origin；GET/POST 都转发上游 `api/*`，POST body 必须是 JSON object（空 body 视为 `{}`）。cloud 角色保持 501 `local_api_surface_s1a`，不转发（`chat2api/chat2api.py:7892-7930,8292-8326,8447-8482`）。

### 5.2 请求与响应

- `_read_body` 以 `Content-Length` 为界：非法长度 400 `invalid_request`；超过上限 413 `payload_too_large`；非法 JSON 400 `invalid_json`；非对象 400 `invalid_body`（`chat2api/chat2api.py:6127-6157`）。
- 分享模式默认请求体上限 32 MiB，可由 `CHAT2API_SHARE_MAX_REQUEST_BYTES` 显式调整到 1 GiB（`chat2api/chat2api.py:422-434`）。
- 分享模式的 Chat 请求只接受白名单字段；`store` 必须为 `false`；`messages` 必须为非空列表（`chat2api/share_auth.py:75-78,1145-1158`）。
- 分享模式的 Messages 请求只接受 `MESSAGES_FIELDS`；`max_tokens` 必须是正整数，`stream` 必须是 bool；未知字段拒绝，绝不补默认值（`chat2api/share_auth.py:1159-1190`）。
- 成功响应：Chat 非流式为 JSON；Chat 流式、Messages 流式为上游 SSE 字节透传；Responses 为翻译后的 JSON 或 Responses 事件流。
- 错误体统一使用协议感知的信封（`docs/adr/0003-protocol-aware-error-contract.md`）：Chat/Responses/models/version 与本地 `/api/*` 使用 OpenAI 形 `{"error": {"message", "type", "code", "param", "detail"}}`；`/v1/messages` 的本地错误使用 Anthropic 形 `{"type": "error", "error": {"type", "message", "code", "param", "detail"}}`。`type` 是错误类别（`invalid_request_error`、`authentication_error`、`permission_error`、`not_found_error`、`rate_limit_error`、`api_error`、`overloaded_error`、`upstream_error`），`code` 是稳定机器码，`param` 是出错字段名或 `null`，`detail` 是结构化诊断扩展；`reason` 与 `upstream_status` 继续作为附加诊断保留。8001 控制台的 `{"error": "机器码"}` 形状不变。
- 所有响应不发 CORS 头，除非 Origin 在 `CHAT2API_ALLOWED_ORIGINS` 白名单内；错误响应主动关闭连接（`chat2api/chat2api.py:5945-5960`）。

### 5.3 流式、超时、取消与重试

- 首字节预算 120 秒；上游单次 socket 静默预算 180 秒；请求总预算默认 540 秒（认证等待计入 540 秒总预算），环境变量可调但夹在 10..3600 秒（`chat2api/chat2api.py:377-385,440-444`）。
- 认证等待预算：本机 8000 默认 30 秒，云端 8002/8003 保持 180 秒；显式 CLI/env 值覆盖默认并 clamp 到 1..180 秒，非法值保持角色默认，不会关闭预算（`chat2api/chat2api.py:12097-12115`）。
- 200 头只在首个“实质数据”事件出现后提交；提交前的元数据/思考事件可短暂暂存，以便早期重复循环或零数据断流仍可隐藏重试（`chat2api/chat2api.py:8043-8080,8417-8431`）。
- 提交前失败返回 502；首字节、响应头等待、socket 静默或总预算超时返回 504 `upstream_timeout` / `request_deadline_exceeded`；提交后失败写 in-band `error` 事件并结束流，绝不把部分完成当成功（`chat2api/chat2api.py:599-613,8060-8077`）。
- 预提交暂存上限 64 KiB；未成行字节缓冲上限 1 MiB；生产者/消费者队列上限 256；并发流 reader 上限已取消（ADR 0004，`too_many_concurrent_streams` retired）（`chat2api/chat2api.py:388-403`）。
- 重试：快速失败最多 5 次，慢失败（耗时达到 30 秒）最多 2 次，物理尝试上限 12；408/429/500/502/503/504/529/520-524、传输失败与网关等待使用完整预算；402 与 403 权限文字只探测一次；400/422 只在文字表示瞬态时探测一次；404/405/409/413/415 不重试；401 与 403 认证文字触发共享重登录（见 5.4）；只在客户端尚未收到任何 SSE 事件前重试。`Retry-After` 不超过 8 秒时等待后重试，超过 8 秒时保留原状态与响应头返回客户端；`X-Should-Retry: false` 停止重试。可重试状态为 408/429/500/502/503/504/520/521/522/523/524/529（`chat2api/chat2api.py:522-548,7015-7030`）。
- 客户端断开在每个字节检查；断开后关闭上游、取消 reader 生产线程，账本记 `client_disconnected`（`chat2api/chat2api.py:6160-6175,8080-8003`）。
- Messages 流要求官方事件序列；`message_stop` 前 EOF 不算成功；`error` 事件立即终止（`tests/test_chat2api_endpoints.py:501-610`）。
- Responses 流事件为 `response.created`、`response.output_text.delta`、`response.function_call_arguments.delta`、`response.completed`、`response.incomplete`、`response.failed`（`chat2api/chat2api.py:6720-6760,6955-6985`）。
- 非流式 Chat/Messages 可由 `--nonstream-upstream-aggregate` 选择上游 SSE 聚合，但只对已验证的 (key, model, protocol) 三元组；默认仍走上游非流式（`chat2api/chat2api.py:7490-7535`）。
- Embeddings 只透传，不写账本行（`chat2api/chat2api.py:7006-7030`）。

### 5.4 错误码

所有 8003 自产错误使用统一信封：Chat/Responses/models/version 与本地 `/api/*` 为 OpenAI 形
`{"error": {"message", "type", "code", "param", "detail"}}`；`/v1/messages` 为 Anthropic 形
`{"type": "error", "error": {"type", "message", "code", "param", "detail"}}`。`type` 是类别
（`invalid_request_error`、`authentication_error`、`permission_error`、`not_found_error`、
`rate_limit_error`、`api_error`、`overloaded_error`、`upstream_error`），`code` 是稳定机器码，
`param` 是字段名或 `null`，`detail` 是结构化诊断扩展。8001 控制台的
`{"error": "机器码"}` 形状不变。

| HTTP | 错误 | 触发 |
|---|---|---|
| 400 | `invalid_request` / `invalid_json` / `invalid_body` / `unsupported_chat_request` / `unsupported_messages_request` / `unsupported_responses_request` | 请求形状不合法 |
| 401 | `invalid_api_key` / `local_auth_error` | 分享 Key 无效；非 loopback 绑定缺少本地 Key |
| 402 | `upstream_quota_exhausted` | 上游额度耗尽 |
| 403 | `origin_denied` / `upstream_forbidden` / `reauthentication_failed` / `control_denied_endpoint` / `control_denied_token` | 浏览器来源、上游权限、重登录失败、控制令牌、本机管理边界 |
| 404 | `route_not_shared` / `unknown_path` / `upstream_request_rejected` | 未知路径；分享模式下非白名单路由；上游 404（保留原状态） |
| 405 | `method_not_allowed` | API 路径的方法未实现 |
| 408 | `request_timeout` | 请求体读取超时 |
| 409 | `upstream_request_rejected` | 上游 409（保留原状态） |
| 413 | `payload_too_large` / `upstream_request_rejected` | 请求体超过上限；上游 413（保留原状态） |
| 415 | `unsupported_content_encoding` / `upstream_request_rejected` | 请求体压缩编码不支持；上游 415（保留原状态） |
| 429 | `sharing_limit_reached` / `upstream_rate_limited` | 并发、限速或停止接受；上游 429（保留原状态） |
| 500 | `upstream_unavailable` | 上游 500（保留原状态） |
| 501 | `not_implemented` | 尚未装配的本机管理面 |
| 502 | `upstream_unavailable` / `upstream_request_rejected` / `upstream_stream_error` / `request_deadline_exceeded` / `upstream_request_failed` / `chat2api_error` | 上游失败、超时、流错误；连接/DNS/TLS/代理失败 |
| 503 | `reauthentication_required` / `share_config_unavailable` / `upstream_unavailable` | 需要交互登录；分享配置不可读；上游 503（保留原状态）；`too_many_concurrent_streams` 已由 ADR 0004 标记 retired |
| 504 | `upstream_timeout` / `request_deadline_exceeded` | 网关等待上游超时；上游 504 保留原状态 |

上游状态映射（#60）：上游 4xx/5xx 保留原 HTTP 状态码，错误体按客户端协议重建，不透传上游 body。Chat/Responses 使用 OpenAI 形，`/v1/messages` 使用 Anthropic 形；`detail` 带 `upstream_status` 与有界的 `upstream_message`（单行，最多 200 字符），`upstream_status` 也作为顶层附加诊断保留。401 仍转 503 `reauthentication_required`，reason 为 `upstream_rejected`，上游存在 `Retry-After` 时保留该响应头。402 → `upstream_quota_exhausted`，403 → `upstream_forbidden`，429 → `upstream_rate_limited`，504 → `upstream_timeout`；其余 4xx → `upstream_request_rejected`，其余 5xx → `upstream_unavailable`。

连接、DNS、TLS 或代理失败返回 502 `upstream_unavailable`；首字节、响应头等待、socket 静默或总预算超时返回 504 `upstream_timeout` / `request_deadline_exceeded`；上游自己的 504 保留 504。重试耗尽后返回最后一次上游状态；最后一次是传输错误则返回 502；等待重试撞到总预算则返回 504 `request_deadline_exceeded` 并在 `detail.last_upstream_status` 记录最后一次状态。

重登录触发与预算（#63）：上游 401，或 403 且 `upstream_message` 命中 `Not authenticated`/`sign in`/`session expired`/`authentication required`/`please log in`（大小写不敏感）时，每个逻辑请求最多启动一次共享重登录会话；会话恢复后重发一次。重发后仍 401 → 503 `reauthentication_required`；403 认证文字 → 403 `reauthentication_failed`，`detail.reauthentication_attempted` 为 `true`；403 权限、Cloudflare challenge、402、404 不触发重登录。认证等待每个逻辑请求累计 30 秒、最多 128 个并发等待者、每秒复查一次，并计入 540 秒总预算。等待超时 → 503 `reauthentication_required`，`reason` 为 `auth_wait_timeout`；等待槽满 → `reason` 为 `auth_wait_capacity`；滚动 900 秒内 3 次全局启动上限 → `reason` 为 `global_attempt_cap`，有确定下次窗口时带 `Retry-After`；`--no-browser` → `reason` 为 `BrowserDisabled`。认证等待类的 503 仍以 `detail: upstream_rejected` 保留原 401 映射，`reason` 是 #63 的附加诊断字段；403 `reauthentication_failed` 的 `detail` 带 `reauthentication_attempted`、`upstream_status` 与有界 `upstream_message`。

Rust 8003 的登录执行器（#69）：默认 headless-first，不要求 `DISPLAY`/`WAYLAND_DISPLAY`；只有显式 `/admin/auth/login` 的 `{"visible": true}`，或按需 manual display 标记文件 `CHAT2API_LOGIN_DISPLAY_FILE`（默认 `/run/um-share/login-display.env`）完整有效时，才打开可见窗口。没有可用显示时的显式 visible 返回 `visible_login_unavailable`，不静默降级。同一 profile 目录由 `login-browser.lock` 的 OS 锁（POSIX `flock`/Windows `LockFileEx`）保证单实例；锁被占用时本轮以 `login_already_running` 结束且不打开第二个浏览器。登录成功按 Python 相同格式原子写 `CHAT2API_TOKEN_FILE`（默认 `/var/lib/um-share-console/share-backend/token.json`），并更新进程内 bearer、清空模型缓存。上游请求与凭据探测优先使用该文件的 `api_key`，没有或已被上游 401/403 拒绝时回退 JWT bearer；登录成功保存新 token 时保留已有的 `api_key`，不覆盖。可重试的 `timeout`/`attempts_exhausted` 终态按 `CHAT2API_RECOVERY_RETRY_INTERVAL_S`（默认 1800，60..86400）与 `CHAT2API_RECOVERY_RETRY_MAX_PER_DAY`（默认 3，0..24）自动重开轮次；人工接管、拒绝、取消和 `BrowserDisabled` 不重开。

`/admin/auth/status` 返回 `state`（`valid`/`needs_login`/`logging_in`/`failed`/`cancelled`）、`label`（认证有效/需要登录/登录中/登录失败/已取消登录）、`error`、`primary`、`evidence`、`login`、`gate`、`watch`、`recovery`、`login_window_deadline_epoch` 与 `login_window_s`。`login` 记录当前轮次的 `phase`/`origin`/`manual_reason`/`detail`/`form` 与自动提交计数 `submit_count`；`recovery` 额外记录上一轮结束时的 `last_form`/`last_submit_count`，以及 `retry_scheduled`/`retry_count`/`next_attempt_epoch`；`CHAT2API_AUTH_STATE_FILE` 的顶层 `upstream_401_summary` 是脱敏后的 401/403 body 文本；`/admin/auth/status` 的 `evidence` 不包含该摘要。UMPASS 自动提交只在可信 HTTPS 页面、账号与密码栏就绪且提交按钮唯一时发生一次；受保护预填保留可见窗口并返回操作提示，字段未就绪、提交按钮不唯一、不可信页面和上游明确拒绝分别记录且不再自动提交旧凭据。`/admin/auth/verify` 是 GET-only 单次 `/api/models` 核验，不改认证状态与模型缓存，返回 `verify`/`reason`/`upstream_status`/`checked_epoch`。`/admin/auth/login` 接受可选 `{"visible": bool}` 并返回 202 状态快照；`/admin/auth/cancel` 返回 200 最终状态，取消本轮但服务继续运行，普通请求不会自动重开。可见窗口关闭按操作员取消记为 `cancelled`（`error` 为 `window_closed`）；headless 窗口关闭是可重开的 transient。人工接管原因保留 `manual_takeover_required:<reason>`（`umpass_mfa_required`、`umpass_prefill_missing`、`unrecognized_page`），Duo 推送等待不误判为人工接管。`--no-browser` 的显式登录与 401 恢复都返回 `BrowserDisabled`。认证等待仍按 #63 的 30 秒预算、128 并发等待者与每秒复查；滚动 900 秒内 3 次全局登录启动上限由登录执行器统一执行，显式登录同样受限。测试通过 `GatewayServer::start_with_login` 注入 fake browser factory 与 fake credential probe，不打开真实浏览器；真实 UM 登录与 MFA 由维护者验收。

### 5.5 副作用

- 读投影文件；每次鉴权重新读取，不写投影。
- 追加用量账本 `usage-share-backend.jsonl`；写失败则本进程禁用记账，但绝不阻断代理（`chat2api/chat2api.py:2502-2512`）。
- 追加服务日志 `share-backend.log`；日志不含凭据、请求体或响应体。
- 登录/刷新时写 token/API Key 文件与浏览器 profile；只有显式登录或 401 恢复才打开浏览器，默认 headless，显式 visible 或有效 manual display 才 headful。Rust 8003 读写与 Python 相同的 `CHAT2API_TOKEN_FILE` 与 profile 路径，不迁移数据；登录浏览器按 profile 单实例，进程退出后 OS 锁自动释放。
- 写脱敏认证证据 `CHAT2API_AUTH_STATE_FILE`（本机默认 `data/auth-state.json`，云端 `/var/lib/um-share/auth-state.json`）：原子替换、0600，只含轮次、结果、表单计数、`saved_at`、`iat`/`exp`、`jti`/`id` 哈希前缀与脱敏 401 摘要，不含 token、API key 或完整 `jti`；写失败只记日志，不影响请求或登录轮次。
- 写 UM 登录轮次历史 `CHAT2API_LOGIN_HISTORY_FILE`（本机默认 `data/login-history.jsonl`；云端 8003 默认 `/var/lib/um-share/login-history.jsonl`）：JSONL 追加，单文件 1 MiB 轮转、最多 3 个轮转文件、合计 4 MiB、保留 7 天；每轮记录 `run_id`、轮次、状态、触发来源、可见模式、提交次数、结果、有界原因、上游 HTTP 状态与证据结论；8001/8002 `/api/console/um/login-rounds` 只读，不探测、不登录、不追加。云端 8002 只放行这一条 `um/*` 读面，其余 `um/*` 仍返回 503 `capability_not_assembled`。
- 撤销状态文件 `share-revoke.state` 与投影快照，原子写（`chat2api/share_auth.py:475-526`）。
- 注册实例控制 AF_UNIX 通道；只应答 `instance-status`、`load`、`drain`、`emergency`、`resume`，绝不调用管理动作（`chat2api/chat2api.py:9023-9100`）。
- `/admin/shutdown` 只停止本进程；不停止上游 UM。

## 6. 云端控制台（默认 8002）

### 6.1 端点与角色边界

云端控制台宿主（`usagesrv.py`）目标监听 8002；旧 18096 不保留兼容别名。本节端点行以 8002 标注该服务面。云端角色只装配 UM 分享；非 UM 读面明确返回 503 `capability_not_assembled`，不探测也不读取（`usagesrv.py:3043-3062`）。云端唯一放行的 `um/*` 读面是 `GET /api/console/um/login-rounds`（#116）；其余 `um/*` 仍按 `um` 能力返回 503。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/`、`/index.html` | loopback Host/Origin | 托管 `web/dist`；构建身份 role/line/sourceHash 不匹配返回 500 |
| GET | `/health` | 无 | `{status, service, port}` |
| GET | `/api/summary` | 无 | 云端汇总；角色 `cloud`，不读私人账本 |
| POST | `/api/session` | 引导码 | 兑换 HttpOnly 会话 Cookie，TTL 12 小时 |
| POST | `/api/session/reissue` | `X-Admin-Control` | 重新签发引导码 |
| GET | `/display` | 同源 + display 授权 | 重定向到 `/display/` |
| GET | `/display/` | 同源 + display 授权 | noVNC launcher 页面 |
| GET | `/display/launcher.js`、`/display/launcher-state.js`、`/display/<asset>` | 同源 | launcher 模块与只读 noVNC 静态树，防穿越 |
| GET | `/display/session` | 同源 | `{authorized, expires_at}` |
| GET | `/display/grant` | 同源 | launcher 页面，fragment 兑换 |
| POST | `/display/grant` | 同源 + 有效单次 grant | 单次 grant 换 HttpOnly Cookie |
| GET | `/display/websockify` | display session 或控制令牌或 Tailscale Serve 身份 | WebSocket RFB 中继 |
| GET | `/api/console/cloud-ops` | 无（只读） | 安全状态只读面 |
| GET | `/api/console/share-management/display-session` | 无（只读） | launcher 可用性、operator 状态 |
| GET | `/api/console/share-management/display-state` | 无（只读） | 认证观察 + display 事实 |
| POST | `/api/console/share-management/display-operator` | 同源标记或控制令牌 | `acquire`/`takeover` |
| POST | `/api/console/share-management/backend-login-cancel` | 同源标记或控制令牌 | 代理网关 `POST /admin/auth/cancel`；200/202 归一为 200 |
| POST | `/api/console/share-management/display-grant` | 管理身份 | 一次性 display grant |
| GET | `/api/console/share-management/control-status` | 无（只读） | broker 控制通道是否响应 |
| GET | `/api/console/share-management/status` | 无（只读） | 分享状态、代次、容量、broker |
| GET | `/api/console/share-management/keys` | 无（只读） | Key 列表；不含明文/哈希 |
| GET | `/api/console/share-management/keys/{id}/secret` | 无（只读） | 单 Key 明文 readback，no-store |
| POST | `/api/console/share-management/keys` | 同源标记或控制令牌 | 创建 Key，明文只回一次 |
| POST | `/api/console/share-management/keys/{id}/test` | 同源标记或控制令牌 | 公网 Key 自检 |
| POST | `/api/console/share-management/keys/{id}/test-private` | 同源标记或控制令牌 | 本机 Key 自检 |
| POST | `/api/console/share-management/keys/{id}/rotate` | 同源标记或控制令牌 | 轮换，明文只回一次 |
| POST | `/api/console/share-management/keys/{id}/revoke-and-terminate` | 同源标记或控制令牌 | 撤销并终止该 Key 在途请求 |
| PATCH | `/api/console/share-management/keys/{id}` | 同源标记或控制令牌 | 改名、改每 Key 并发 |
| PATCH | `/api/console/share-management/settings` | 同源标记或控制令牌 | 改全局并发 1..64 |
| DELETE | `/api/console/share-management/keys/{id}` | 同源标记或控制令牌 | 删除 Key |
| GET | `/api/console/share-usage`、`/api/console/share-usage/requests` | 无（只读） | 分享用量汇总/逐请求 |
| GET | `/api/console/um/login-rounds` | 无（只读） | 云端 8002 与本地 8001 共用的 UM 登录轮次历史；只读，不触发探测/登录/追加 |
| GET | `/api/console/um/today`、`trend`、`requests`、`logs`、`request-logs`、`maintenance` | 无（只读） | 本机 UM 主链读面；云端角色 503 |
| GET | `/api/console/usage-tokens`、`/api/console/usage-rates` | 无（只读） | 本机 Token/速率读面；云端角色 503 |
| GET | `/api/console/gateway-connection` | 无（只读） | 连接信息；云端角色 503 |
| GET | `/api/console/services`、`/api/console/health` | 无（只读） | 服务与健康 |
| GET | `/api/console/logs`、`/log-sources`、`/log-entries`、`/log-overview` | 无（只读） | 日志读面；云端角色 503 |
| GET | `/api/console/stats` | 无（只读） | 有界统计；云端角色 503 |
| GET | `/api/console/actions` | 无（只读） | 动作目录 |
| GET | `/api/console/operations`、`/operations/{id}` | 无（只读） | 操作记录 |
| POST | `/api/console/operations` | 无（同源写边界） | 提交动作；201/200 |
| POST | `/api/console/operations/{id}/cancel` | 无（同源写边界） | 协作取消 |
| GET | `/api/share/usage`、`/usage/requests` | 会话或控制令牌 | 旧管理面只读 |
| GET | `/api/share/status`、`/keys`、`/keys/{id}`、`/keys/{id}/secret`、`/operations/{id}` | 会话或控制令牌 | 旧管理面只读 |
| POST | `/api/share/limits/normalize` | 控制令牌或会话+CSRF | 显式归一化 |
| POST | `/api/share/keys`、`/keys/{id}/test`、`/keys/{id}/rotate`、`/keys/{id}/revoke-and-terminate` | 控制令牌或会话+CSRF | 旧管理面写 |
| PATCH | `/api/share/settings`、`/keys/{id}` | 控制令牌或会话+CSRF | 旧管理面写 |
| DELETE | `/api/share/keys/{id}` | 控制令牌或会话+CSRF | 旧管理面写 |
| POST | `/api/share/start`、`/stop`、`/emergency-stop` | 控制令牌或会话+CSRF | 202 受理 |
| ANY | `/gw/usagesrv/...` | 同实际路径 | 开发/兼容前缀，剥离后走同一路由 |

补充：`/api/share/*` 是云端控制台上的旧管理面（默认 8002），走会话/控制令牌鉴权；它不在 Cloudflare 公网白名单内，公网入口仍只有 8003 的四条 `/v1` 路径。

补充：#106 起 `POST /api/console/share-management/backend-login-cancel` 代理网关控制令牌门 `POST /admin/auth/cancel`（复用 `account.um_status` 的同一个控制面客户端），把网关的 200/202 `{cancelled, state, detail}` 归一为 200；网关不可达、控制令牌缺失/不可读、非 2xx 或响应不符合契约时返回 503 `{cancelled:false, state:null, error, detail}`，不伪造成功。控制令牌只在本机发往网关，不进日志或响应。旧 `/api/share/backend-login-cancel` 仍是 404。

### 6.2 鉴权与请求边界

- 路由层先校验 Host 必须是 loopback 名，Origin 必须是同 scheme、同 loopback、同端口的本机页面；Tailscale Serve 元数据出现时，必须通过 `TailnetPolicy` 的精确 origin、owner 和 peer 设备校验（`usagesrv.py:1120-1175`、`console/tailnet.py:98-130`）。
- `_console_api` 的读请求免凭据；写请求由各子路由自己加门（`usagesrv.py:3022-3042`）。
- `share-management` 写必须带 `X-Requested-With: XMLHttpRequest` 或 `X-Admin-Control`（`usagesrv.py:2639-2648`）。
- `/api/share/*` 读需要会话 Cookie 或控制令牌；写需要控制令牌，或会话 + `X-Requested-With`（`usagesrv.py:1682-1706`）。
- 敏感头重复（Content-Type、Content-Length、Authorization、Cookie、X-Admin-Control、X-Requested-With、Transfer-Encoding、Content-Encoding）一律 400，并且只发一个响应后关闭连接（`usagesrv.py:1708-1719`）。
- 请求体：`Transfer-Encoding`/`Content-Encoding` 拒绝；Content-Length 必须唯一、0..65536；有 body 或 Content-Type 时必须 `application/json`；非法 JSON/非对象 400；超限 413（`usagesrv.py:1720-1750`）。
- 未知字段一律 400，绝不静默忽略（`usagesrv.py:1752-1758`）。

### 6.3 请求/响应与错误码

| HTTP | 错误/结果 | 触发 |
|---|---|---|
| 200 | 正常 JSON | 读面、写面成功 |
| 201 | 新建 Key / 新操作 | 创建成功 |
| 202 | `{operation_id, status}` | start/stop/emergency-stop 受理 |
| 400 | `invalid_*`、`unknown field(s)`、`unsupported_body_encoding`、`invalid_content_length`、`content_type_required`、`incomplete_body`、`invalid_json` | 参数、请求体、查询串 |
| 401 | `share management credentials required` | `/api/share/*` 缺少会话/令牌 |
| 403 | `local-only service`、`same-origin marker or control token required`、`display_authorization_required` | 来源、写标记、display 授权 |
| 404 | `unknown path`、`unknown operation`、`key identity not found` | 未知路径/操作/Key |
| 405 | `method not allowed` | `control-status` 非 GET |
| 409 | 版本冲突、重复名、`display_operator_exists`、`display_session_required`、操作已结束、幂等键冲突 | 乐观并发、display operator、操作状态 |
| 410 | `key_deleted` | 已删除 Key 的明文 readback |
| 413 | `request_too_large` / `display grant` body 超限 | 请求体超限 |
| 415 | `display grant` Content-Type 非 JSON | display grant |
| 426 | `unsupported websocket version` | display WebSocket |
| 500 | `share management error` / `console_error` / `summary unavailable` | 内部错误 |
| 502 | `share_usage_unavailable` / `log_reader_failed` / `um_query_failed` / `stats_query_failed` / `runtime status unavailable` | 读面读取失败 |
| 503 | `console subsystem unavailable`、`share_broker_unavailable`、`display_proxy_unavailable`、`capability_not_assembled` | 子系统/ broker / display 未装配 |

补充：

- `share-management/status` 的 `gateway_running` 是三态：只有 broker 明确确认 True/False 才出现，未确认时省略，不折成 false（`usagesrv.py:2730-2748`）。
- `share-management/status` 的 `capacity.in_flight` 来自 broker maintenance 视图；未接线/读不到时为 `null` 并带原因，不写 0（`usagesrv.py:2267-2330`）。
- `share-management/keys/{id}/secret` 的四种结果：200 明文；200 `secret_unavailable` + `reason=legacy|undecryptable|version_conflict`；410 `key_deleted`；404 未知（`usagesrv.py:2205-2239`）。
- `revoke-and-terminate` 返回 `revoked=true`，并区分 `terminated`/`released`/`aborted`/`upstream_closed`；缺失计数保持 `null`，不伪造 0（`usagesrv.py` 的 `revoke-and-terminate` 分支）。
- 操作提交：`idempotency_key` 1..128；同 action/target/params 复用同一操作，不同则 409 `idempotency_key_conflict`；新建 201，复用 200（`usagesrv.py:3510-3538`、`console/operations.py:433-534`）。
- 操作取消：已结束 409 `operation_already_finished`；未知 404 `operation_not_found`；排队中取消立即变 `cancelled`，执行中置 `cancel_requested`（`console/operations.py:544-570`）。

### 6.4 超时、取消与幂等

- 云端控制台没有 SSE；所有响应都是 `Cache-Control: no-store` 的 JSON。
- display grant TTL 60 秒；display session TTL 300 秒；最多 64 个待用 grant、64 个会话；最多 2 个并发 relay；idle 900 秒；单会话最长 14400 秒；握手 10 秒；关闭宽限 5 秒（`console/display_proxy.py:102-107,218-245`）。
- 分享写面前端预算 20 秒，display 写面 30 秒（`web/src/cloud/cloud-writes.ts`）；超时是“未确认”，不是成功。
- 操作取消是协作式：`cancel_requested` 只是请求，执行器在下一个检查点退出；不会强杀子进程。
- 幂等键按操作类型绑定；同一 Key 的 create/rotate 重放不返回明文。
- 部署脚本的 `flock` 锁与 900 秒排空预算见 §12。

### 6.5 副作用

- 写 `data/console-state.sqlite3`：操作记录、跨进程资源锁、幂等键；未完成操作在启动时标为 interrupted，不重放（`console/operations.py:268-370`）。
- 写分享 Key SQLite：`share-store.db`；schema 3，含 `keys`、`ops`、`configgen`、`security`；投影 schema 仍为 2（`share/store.py:54-60,398-441`）。
- 写安全日志：每次撤销/轮换/降低并发写一行 `security`，同一事务内先写意图；发布未确认时 fail closed（`share/store.py:415-440`、`share/management.py:1153-1200`）。
- 写 `configgen`：desired generation 与 applied generation 分离；发布成功才推进 applied。
- 写会话引导码文件；`/api/session` 只写 HttpOnly Cookie，不返回 token。
- 读账本、日志、broker 状态、pid 文件、display 状态；不写 8003 账本、不启动 8003。
- 发布投影：原子写临时文件 + `os.replace`；generation 单调；同代内容不同拒绝（`share/runtime.py:24-80`）。
- 写 broker 操作记录：`data/console-state.sqlite3` 的 `operations`；明文 Key 绝不进 `result_json`（`usagesrv.py:3248`）。
- 不自动备份、不做预算保护；备份只走显式 CLI。

### 6.6 读面响应形状

以下只冻结顶层形状；字段级语义与缺失值规则以对应构造点为准，Rust 不得自行补零或改写。

| 端点 | 顶层响应字段 |
|---|---|
| `GET /health` | `status`、`service`、`port` |
| `GET /api/summary` | `summary`、`checkedAt`；角色 `cloud` 不读私人账本 |
| `GET /api/console/share-management/status` | `generation`、`keys`、`capacity`、`gateway_running`；缺失保持 `null`/省略 |
| `GET /api/console/share-management/keys` | `keys[]`；每项 `id`、`name`、`max_concurrency`、`row_version`、状态字段；不含明文/哈希 |
| `GET /api/console/share-usage` | `summary`、`filters`、`coverage`、`checkedAt`；费用/价格字段已退役 |
| `GET /api/console/share-usage/requests` | `rows[]`、`nextCursor`、`truncated`、`filters`、`coverage`、`checkedAt` |
| `GET /api/console/um/today` | `today`、`coverage`、`timezone`、`checked_epoch` |
| `GET /api/console/um/trend` | `range`、`days`/点序列、`coverage`、`checked_epoch` |
| `GET /api/console/um/requests` | `rows[]`、`nextCursor`、`truncated`、`coverage` |
| `GET /api/console/um/logs`、`request-logs` | `rows[]`、`nextCursor`、`truncated`、`coverage` |
| `GET /api/console/um/login-rounds` | `rounds[]`、`scope`、`truncated`、`coverage`；只读，不触发探测/登录/追加 |
| `GET /api/console/stats` | `requests`、`input_tokens`、`output_tokens`、`cache`、`coverage`；云端角色 503 |
| `GET /api/console/logs` | `rows[]`、`coverage`；云端角色 503 |
| `GET /api/console/operations` | `operations[]`；每项 `id`、`action`、`status`、`result` |
| `GET /api/console/operations/{id}` | 单个 operation；未知 404 |
| `GET /api/console/actions` | `actions[]`；动作目录 |
| `GET /api/console/gateway-connection` | `service`、`connection`、`snapshot`；云端角色 503 |

## 7. broker 契约

### 7.1 传输与帧

- POSIX 生产传输：AF_UNIX `SOCK_STREAM`，管理 socket `/run/um-share/broker.sock`，目录 0700、socket 0600；对端身份用 `SO_PEERCRED` + `/proc/<pid>` 证据（`share/broker_ops.py:270-360`）。
- 一连接一 cycle；单行 JSON；`\n` 结尾；最大 1 MiB；先检查上限再接受完整帧；空帧 `empty_request`；超时 `deadline_exceeded`（`share/broker_protocol.py:29-40,215-300`）。
- 管理请求字段：`v`、`action`、`request_id`、`ts`、`fields`、`mac`；实例请求额外 `instance_id`。HMAC-SHA256 对固定字段排序；`ts` 窗口 ±300 秒；`request_id` 一次性消费防重放（`share/broker_protocol.py:206-280`）。
- 结果码固定集合：`ok`、`not_installed`、`stale_generation`、`fingerprint_mismatch`、`instance_mismatch`、`replay`、`stale_ts`、`overlimit`、`deadline_exceeded`、`unconfirmed`、`blocked`、`bad_request`、`forbidden`、`internal_error`、`empty_request`、`unknown_instance`、`capability_missing`、`access_denied`（`share/broker_protocol.py:160-180`）。

### 7.2 动作白名单

| 通道 | action | 字段 | 说明 |
|---|---|---|---|
| 管理 | `status` | 无 | 状态与诊断 |
| 管理 | `publish` | `generation`、`fingerprint`、`projection`、可选 `terminate_keys` | 原子写投影 + 实例 load/ACK |
| 管理 | `start` | 可选 `explicit` | 受限启动；`explicit` 清停止意图 |
| 管理 | `drain` | 无 | 有界排空 + 最终停止 + 关闭验证 |
| 管理 | `emergency-stop` | 无 | 清在途 + 停止 + 关闭验证 |
| 管理 | `ts-service-control` | `action: start|stop` | Windows 专用；Linux 不装配，未知动词拒绝 |
| 管理 | `host-verify` | `check: running|closed` | 三证据 |
| 管理 | `host-stop` | 无 | 任务门 |
| 管理 | `host-repair-limits` | 无 | 限额修复 |
| 管理 | `maintenance-begin` | `component`、`lease_s` | 维护租约，默认 300 秒、上限 600 秒 |
| 管理 | `maintenance-end` | `component`、`lease_id` | 恢复接受 |
| 管理 | `maintenance-status` | 无 | 只读视图 |
| 管理 | `gateway-restart` | `component`、`budget_s` | 默认 120 秒、上限 180 秒 |
| 管理 | `host-evidence` | `check: limits|faults|watchdog` | 只读证据 |
| 实例 | `instance-status` | 无 | 身份与在途数 |
| 实例 | `load` | `generation`、`fingerprint`、可选 `terminate_keys` | 加载投影并 ACK |
| 实例 | `drain` | 无 | 暂停接受 |
| 实例 | `emergency` | 无 | 清在途并停止 |
| 实例 | `resume` | 无 | 有界排空后恢复接受 |

### 7.2.1 Rust 8003 的实例通道

- Rust 8003 由 `um-share-gateway.service` 以 `--instance-key-file /etc/um-share/instance.key`、`--instance-pipe-prefix /run/um-share/instance-`、`--registration-file /var/lib/um-share/run/instance-registration.json` 启动；socket 父目录 0700、socket 0600，注册文件原子写入，broker 只读。
- `load` 先重新读取投影文件再 ACK：ACK 的 `generation`/`fingerprint` 来自本次 load，`security_generation` 来自最新投影的 `config_generation`；旧内存投影会让安全日志代次无法确认。
- `load` 只对一次性 `terminate_keys` 调用 gate 的按 Key 终止；`drain`/`resume` 切换准入状态，`emergency` 清空在途并把状态置为 `stopped`；`instance-status` 返回在途计数与最近代次。
- 实例通道不可用时 8003 仍提供 HTTP 服务；broker 的 `publish` 会以 `unconfirmed` 失败并由管理面走紧急停止，绝不在无实例通道时伪造在线发布。

### 7.3 生命周期、超时与副作用

- 默认 wire deadline 10 秒；管理 dispatch deadline 130 秒；排空窗口 60 秒；最终停止预算 20 秒；客户端排空 deadline 90 秒；紧急停止 deadline 30 秒（`share/broker_protocol.py:29-58`）。
- 启动后轮询三证据，`start_ready_timeout=6.0` 秒、轮询 0.25 秒；超时或证据不可读都回答 `unconfirmed`，绝不伪造 `started`（`share/broker_host.py:257-325,581-630`）。
- 排空先通知实例 `drain`，再轮询 `active_count` 到 0；无论通知是否失败，都要执行最终 host stop + `verify_closed`；`timed_out` 与 `stopped` 分开报告（`share/broker_host.py:631-705`）。
- 紧急停止同样以 VERIFIED host closure 为权威；实例通知失败不跳过 host stop（`share/broker_host.py:706-758`）。
- 停止优先级：VERIFIED closure 清维护租约并 bump closure epoch；停止后不能被维护租约重新打开（`share/broker_host.py:748-770`）。
- 维护租约绑定 `(component, instance_id, round, expires_at)`；过期时只在实例/轮次仍为当前时才 resume，替换后的实例绝不 resume（`share/broker_host.py:834-1030`）。
- gateway restart 三证据门通过才执行；先写 `restarting` 维护记录，再 stop、验证 closed、explicit start、验证 running；未确认则 `unconfirmed` 并带实际 flags，不重试强杀（`share/broker_host.py:1052-1135`）。
- 持久记录：`lifecycle.json`、`maintenance.json`、`broker-status.json`、`share-host-desired.json`、`share-gateway.pid`。原子写 tmp + replace；写失败时生命周期转换回答 `unconfirmed`，不让内存记录声称已持久化（`share/broker_server.py:586-610,1156-1250`；`share/broker_ops.py:321-380,579-700`）。
- broker 是 8003 的唯一 Popen 者；控制台 unit 被 `NoExecPaths` 围栏挡住，不能成为第二个启动者（`um-cloud-deploy/systemd/um-share-console.service`、`um-cloud-deploy/systemd/um-share-broker.service`）。
- 运维重启：`systemctl restart um-share-gateway.service` 的 `ExecStop` 会先写持久停止意图（`/run/um-share/share-host-desired.json`），随后 `ExecStartPost` 的受限 `start` 被该意图拒绝，journal 只看到 `start did not confirm the host task`。重启后必须用 `sudo -u um-share /opt/um-share/bin/um-share-gatewayctl.py start-explicit` 清意图并启动，或直接走 `update.sh`（已自动调用 `start-explicit`）。`Restart=on-failure` 的崩溃重启不经过 `ExecStop`，不受此影响；`systemctl stop` 是操作员的持久停止，普通 start 必须继续拒绝。

## 8. Key 管理

### 8.1 数据模型

`share-store.db` schema 3（投影 schema 2）：

| 表 | 字段 | 约束 |
|---|---|---|
| `meta` | `key`、`value` | `schema_version`、`share_max_concurrency` |
| `keys` | `key_id`、`name`、`secret_hash`、`secret_version`、`masked_key`、`secret_blob`、`secret_algo`、`enabled`、`max_concurrency`、`created_at`、`updated_at`、`deleted_at`、`row_version` | `name` 在未删除行中唯一；`row_version` 每次变更 +1 |
| `ops` | `id`、`kind`、`idempotency_key`、`status`、`payload`、`created_at` | `idempotency_key` 唯一 |
| `configgen` | `schema_version`、`config_generation`、`applied_generation` | desired/applied 分离 |
| `security` | `record_id`、`key_id`、`generation`、`action`、`secret_version`、`status`、`recorded_at`、`applied_at`、`applied_generation`、`instance_id`、`remote_confirmed_at`、`remote_receipt` | 跨机安全日志 |

（`share/store.py:398-441`）

### 8.2 语义

- 新建 Key：默认每 Key 并发 16，允许 1..48；明文只回一次；idempotency replay 不再返回明文（`share/store.py:85-89,1010-1050`）。
- 全局分享并发：1..64；新部署默认 64；旧部署保持 2；超出旧契约上限（每 Key 64、全局 128）在解析时夹到新上限，超过旧上限拒绝（`share/store.py:75-90`、`share/broker_protocol.py:557-595`）。
- 更新：`enabled` 只读；只接受 `False`（撤销方向），`True` 明确拒绝；改名与每 Key 并发可原子更新；降低并发是撤销方向，必须发布确认，否则 503（`share/store.py:1191-1275`）。
- 轮换：`secret_version+1`，写新 hash、mask、ciphertext，记录安全日志，发布确认；idempotency key 绑定目标 Key，重放返回元数据但不返回明文（`share/store.py:1277-1325`）。
- 删除/撤销：写 `deleted_at`、清 ciphertext、发布确认；已删除 Key 的明文 readback 410；重复删除返回 `already_deleted`（`share/store.py:1326-1348`）。
- 明文保护：Windows 用 DPAPI user scope；POSIX 用 AES-256-GCM + 文件密钥；缺失/损坏/权限过宽一律 fail closed，不降级为 hash-only（`share/store.py:160-300`）。
- 删除 Key 清 ciphertext；旧 hash-only 行保持可用，直到人工轮换（`share/store.py:1074-1130`）。

### 8.3 发布链与失败关闭

- 每次变更先写本地 SQLite（单一写者），再通过 broker 发布投影；投影写入是原子替换，generation 单调，同代内容不同拒绝（`share/management.py:1-20`、`share/runtime.py:24-80`）。
- 撤销方向（disable/rotate/delete/降低并发）必须等实例 ACK 新代次；未确认则 503，且记录可查询的发布失败，绝不返回假 200（`share/management.py:1-20,800-840,1153-1200`）。
- 安全日志与 Key 变更同一事务写入，先于发布；远端确认失败保持 pending，不声称跨机保护完成（`share/store.py:415-440`、`um-cloud-deploy/backup_restore/security.py:1-80`）。
- 提高并发是安全方向，best-effort 发布；降低并发是撤销方向，fail closed（`share/store.py:1191-1275`）。

## 9. 用量账本

### 9.1 写入行

8003 每个请求追加一行 JSONL（`chat2api/chat2api.py:2427-2512`）：

```json
{
  "time": "ISO-8601 本地时间",
  "service": "chat2api",
  "model": "string",
  "stream": true,
  "status": "ok | cancelled | upstream_failed | client_disconnected | ...",
  "input_tokens": 0,
  "output_tokens": 0,
  "cached_input_tokens": 0,
  "cache_report_status": "reported | unreported",
  "reasoning_tokens": 0,
  "total_tokens": 0,
  "estimated": false,
  "duration_ms": 0,
  "usage_source": "upstream | partial | estimated | none",
  "protocol": "chat | messages | responses | null",
  "key_id": "string | null",
  "request_id": "string | null",
  "attribution": "verified | absent | legacy"
}
```

- 只有 `status="ok"` 是完整成功；取消、失败、断流、重试都是已知下界，不补零（`share/metering.py:30-40,233-290`）。
- `usage_source` 四态：`upstream`、`partial`、`estimated`、`none`；未知不当 0。
- `cache_report_status` 区分上游是否报告 cache；`cached_input_tokens` 的兼容 0 不能当真实 cache 命中。
- 归因：只有鉴权后的分享请求写 `key_id`/`request_id`/`attribution=verified`；直连 loopback 请求写 `absent`，不猜 Key。
- 写失败则本进程禁用记账并打印一行错误，不阻断代理。

### 9.2 读面

- `GET /api/share/usage`、`GET /api/console/share-usage`：同一账本、同一口径；可选 `window`、`key`、`model`、`result`；默认完整历史。未知/缺失不当 0。
- `GET /api/share/usage/requests`、`GET /api/console/share-usage/requests`：逐请求分页；`limit` 1..200，默认 50；cursor 是行号；未知时间/未知用量保持 `null`；`coverage.olderRecordsUnread` 与 `truncated` 区分“窗口到头”与“账本到头”（`share/metering.py:478-600`）。
- 拒绝读私人账本 `usage-chat2api.jsonl`，只读分享账本 `usage-share-backend.jsonl`（`share/metering.py:32,292-298`）。
- 只读：不写、不创建、不截断；最多 200000 行；截断在 coverage 中披露（`share/metering.py:1-20,30,344-477`）。

### 9.3 控制台读面

- `/api/console/share-usage` 与 `/api/share/usage` 共用 `_share_usage_response`；`/api/console/share-usage/requests` 与 `/api/share/usage/requests` 共用逐请求页。
- `/api/console/usage-tokens` 近 N 个北京自然日（默认 14，上限 31）；`/api/console/usage-rates` 近 N 小时（默认 24，上限 168）；缺分量单列，不折零。
- `/api/console/stats` 日期范围 `YYYY-MM-DD`，半开区间 `[from 00:00, to+1d 00:00)`，跨度上限 92 天；来源/模型筛选；每端点最多 5000 行。
- `/api/console/um/today|trend|requests|logs|request-logs` 读 UM 自己的账本/日志；`today` 是北京自然日；请求数按逻辑请求，物理重试不重复计；Token 总量包含重试行的真实消耗。
- `/api/console/um/maintenance` 读原始操作记录，不读分享账本。
- `/api/console/um/upstream-faults` 读 8000 的持久上游故障记录：`active`、
  `history`、`last_check`、`next_check_epoch`；缺失文件是合法空记录，损坏文件是
  明确读失败，不产生探测或新条目。

### 9.4 本机 UM 上游故障记录（#103）

- 8000 是本机故障状态的唯一所有者；记录写在 `data/upstream-faults.json`
  （或 `CHAT2API_UPSTREAM_FAULT_LOG`），每次变化原子替换，重启后保留活动
  故障与已结束历史。
- 打开条件：第一次观察到 5xx、连接/DNS/TLS/协议错误或超时。分类固定为
  `upstream_5xx`、`timeout`、`network_error`、`stream_error`；`upstream_status`
  只在拿到 HTTP 响应时记录。同一上游没有恢复证据时连续失败更新同一条记录，
  `failures` 每条最终观察只加 1；一条业务请求的重试不重复计。
- 来源：`background_probe`（auth watch）、`active_probe`（`/ready`、
  `/admin/auth/verify`）、`business_request`（业务请求最终结果）。
- 摘要：可识别的 `no healthy upstream` 原样保留；其他错误文本清理控制字符、
  标记、Bearer 值并截断到 200 字符，不存账号、凭据、请求正文或整页响应。
- 恢复条件：后台核验、主动核验或实际业务请求得到契约有效成功（模型目录
  含 `data` 数组、成功完成的 Chat/Messages/Responses/Embeddings 业务响应）。
  HTTP 200 空响应、错误形状、未完成流、本机 `/health` 200 都不结束故障。
  认证拒绝与缺少凭据走 #98/#99 的认证契约，不打开上游故障记录；可用性与
  认证状态分别展示。
- 后台复查沿用 auth watch 的基准间隔与 2 倍退避（上限 30 分钟）；记录中的
  `next_check_epoch` 是下一次复查时间。`GET /health` 与 8001 读面只读，
  不探测、不写记录。

## 10. display

### 10.1 路由

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/display` | 同源 + display 授权 | 重定向到 `/display/` |
| GET | `/display/` | 同源 + display 授权 | noVNC launcher 页面 |
| GET | `/display/launcher.js` | 同源 | launcher 模块脚本 |
| GET | `/display/launcher-state.js` | 同源 | launcher 状态模块 |
| GET | `/display/<asset>` | 同源 | 只读 noVNC 静态树，防穿越 |
| GET | `/display/session` | 同源 | `{authorized, expires_at}` |
| GET | `/display/grant` | 同源 | launcher 页面，fragment 兑换 |
| POST | `/display/grant` | 同源 + 有效单次 grant | 单次 grant 换 HttpOnly Cookie |
| GET | `/display/websockify` | display session + operator generation | WebSocket RFB 中继 |

### 10.2 语义

- grant 只存 SHA-256 digest，单次使用、绑定精确 origin；错误 origin 不消耗；TTL 60 秒；最多 64 个待用（`console/display_proxy.py:366-470`）。
- session 可重连、绑定 origin、TTL 300 秒；最多 64 个；Cookie `HttpOnly; SameSite=Strict; Path=/`，HTTPS 时加 `Secure`（`console/display_proxy.py:470-540,1400-1410`）。
- WebSocket 只接受 RFC 6455 v13；`Sec-WebSocket-Key` 必须是 16 字节 base64；子协议只接受 `binary`；不合法 400/426（`console/display_proxy.py:1044-1071`）。
- VNC 认证由 console 代理完成：通过 display service 的窄 helper socket 做 RFB challenge-response；密码不进入 console 进程、HTML、JS、URL、浏览器存储或日志；x11vnc 保持 `-localhost` 与 `-rfbauth`（`console/display_proxy.py:1-40`）。
- operator 单页：acquire 幂等；另一活跃页 409 `display_operator_exists`；stale owner 可 takeover；generation 单调；旧 generation 的 KeyEvent/PointerEvent 丢弃；观看不受影响（`console/display_proxy.py:470-560`）。
- 连接上限 2、idle 900 秒、单会话 14400 秒、握手 10 秒、关闭宽限 5 秒（`console/display_proxy.py:218-245`）。

### 10.3 副作用

- 启动 `Xvfb :99` 与 `x11vnc`；x11vnc 只监听 loopback。
- 读 `/run/um-share/vnc-auth.sock` 或 root-only VNC 凭据文件；不把密码写入响应。
- 写 `/run/um-share/display.auth` 等运行目录；不写 release。
- 不记录请求头、RFB 帧、凭据或密码。

## 11. Cloudflare 检查

### 11.1 入口配置校验

- `python um-cloud-deploy/cloudflared/check-ingress.py --config um-cloud-deploy/config/cloudflared.yml.example --expect-host <配置公网主机>`。
- 只读解析 YAML：公网 host 只允许 4 条分享路径 `/v1/models`、`/v1/chat/completions`、`/v1/messages`、`/v1/responses`；service 必须是 8003 loopback；最后一条必须是 `http_status:404`；禁止出现 8002/8097 与旧 8100/8096/18096/5901；重复路径、缺路径、错 host 都报 problem（`um-cloud-deploy/cloudflared/check-ingress.py:26-142`）。
- 不写、不 reload、不 restart tunnel；只返回 `{ok, problems, rules}`，退出码 0/1。

### 11.2 公网 DNS/TLS 检查

- `console/public_probe.py:609-684`：一次有界 DNS/TLS 往返，不发 HTTP 请求；默认 15 秒，最大 60 秒；每个地址 8 秒；最多 4 个地址。
- DNS 两个独立 DoH 源：Cloudflare 与 NextDNS（Google 源仍兼容读取旧状态），并行查询、共用整轮预算；每个源 8 秒；响应最大 64 KiB；NXDOMAIN、NOERROR 无 A、SERVFAIL、DoH HTTP/格式错误、超时分别呈现；TTL 缺失显示未知，不硬编码 300 秒（`console/public_probe.py:118-136,451-520`）。
- 整体状态：`ok`、`degraded`、`incomplete`、`all_failed`、`not_checked`；地址截断不改写成“全部失败”；DNS 分歧与 TLS 可达分别表达。
- Key 自检（Rust `console/src/broker_write.rs`）：版本绑定 `expected_row_version`；先确认 broker `host_verify("running")`（20 秒预算），再 reveal 明文。公网 probe 把 public origin 解析为 global IPv4，读 `/v1/models` 后发一次最多 1 输出 Token 的非流式推理；私有 probe 只读 loopback `/v1/models`。结果字段为 `connected`/`reason`/`stage`/`http_status`/`model`/`public_ip`/`dns`/`elapsed_ms`；成功 `reason=ok`、`stage=inference`（公网）或 `models`（私有）。模型 id 为空、超长、含控制字符或含 Key 时不采用；推理响应必须有 `choices[0].message.role=assistant` 与非空 `content`。失败原因区分 `timeout`、`public_dns_failed`、`http_error`、`invalid_response`、`no_models`、`gateway_unreachable`，不压平成 `connection_failed`。公网请求带浏览器 UA（绕开 Cloudflare 1010，UA 不是凭据）；Key 明文不写日志/响应。结果保留在页面内存，绑定 Key 版本与 API 地址，5 分钟后标陈旧；同一 Key 同时只允许 1 个测试，全局最多 2 个；不自动发推理请求。

### 11.3 副作用

- 只读：不写 Cloudflare 配置、不 reload、不重启 tunnel、不发 DNS 变更。
- Key 自检会产生一次真实模型用量；这是显式动作，不是页面刷新副作用。

## 12. 部署与备份

### 12.1 安装、更新、回滚

- `um-cloud-deploy/install.sh`：只安装冻结发布包，不构建产品代码；已存在的 release 若 manifest 相同则复用；配置、密钥、数据不覆盖；`--apt-locked` 安装锁定版本。
- `um-cloud-deploy/update.sh`：先停 cloudflared，再 `um-share-gatewayctl.py --drain-budget 900 --poll-interval 5 stop` 排空；未排空返回 75，不切换版本；然后 host-stop、停控制台 unit、原子切换 `current`；健康门失败自动切回 `previous`。
- 健康门：`wait_port_open` 等待云端控制台与 8003；部署默认等待 8002/8003；再 `health_gate`；失败返回 68 并回滚，回滚失败 69。
- `um-cloud-deploy/rollback.sh`：只切符号链接并重启 unit；不删除 release、数据、密钥或备份。
- 部署锁：`/run/um-share/deploy.lock` 的非阻塞 `flock`；另一个部署操作返回 75。
- 显式恢复：`rollback.sh --recover-backup <包> --recover-security-state <状态>`；备份与恢复都走显式 CLI。

### 12.2 备份/恢复

- `python um-cloud-deploy/backup_restore.py --check-deployment-assembly <role> <unit> <env>`：只读检查 unit/env 安全围栏。
- `python um-cloud-deploy/backup_restore.py --store ... --ledger ... --key-file ... --remote-dir ... --state ... --spool ...`：一致 SQLite 快照 + 账本最后完整行 + AES-256-GCM 封装 + 原子写入。
- `python um-cloud-deploy/backup_restore.py --restore-backup <包> --key-file ... --restore-target <新目录>`：只恢复到新目录，重复恢复拒绝；先校验 magic、key fingerprint、GCM tag、SHA-256、`PRAGMA integrity_check`、embedded config generation，再写目标。
- `python um-cloud-deploy/backup_restore.py --deployment-restore --restore-backup <包>`：部署路径恢复，先排空，带独立安全状态 floor；安全状态比备份新时拒绝。
- `python um-cloud-deploy/backup_restore.py --print-freshness --state ...`、`--retry-pending ...`：只读新鲜度、重试 pending 上传。
- 预算保护与自动备份已从默认装配移除；不新增自动备份、不自动恢复。
- 包格式：`UMCLDBK1` + `um-share-backup-v1`；AES-256-GCM；密钥文件 32 字节、模式 0600；恢复 kit 用 scrypt；不记录明文。
- 退出码：0 成功；1 明确失败；64 用法；65 校验；66 输入缺失；67 目标不可用；68 健康门失败但已回滚；69 回滚/补偿失败；70 软件/装配；71 命令/OS 缺失；72 文件/OS；73 无法创建；74 I/O；75 排空超时/锁忙；77 权限（`um-cloud-deploy/README.md`）。

### 12.3 副作用

- 安装/更新/回滚会写 release 目录、systemd unit、符号链接、运行数据目录；不覆盖配置、密钥、数据。
- 备份读 SQLite、账本、密钥文件、安全状态；写加密包、state、spool；不删除源数据。
- 恢复写新目标目录；不覆盖已有目标；不改源数据。
- 所有脚本默认 fail closed；未确认的关闭不写成 stopped。

## 13. Parity 门禁（仓库实际命令）

以下命令是本仓库实际存在的命令；本票只写文档，**未运行全量门禁**，不能把下表当作本票的通过证据。

| 门禁 | 工作目录 | 命令 | 覆盖 |
|---|---|---|---|
| 完整检查 | 仓库根 | `bash scripts/check.sh --full` | compile、ruff、名字/退役检查、契约夹具、diff check、tsc、前端 contracts/logs/um、全部 Python 测试 |
| 快速检查 | 仓库根 | `bash scripts/check.sh` | 不跑全量 Python 测试 |
| Python 回归 | 仓库根 | `python -m unittest discover -s tests -p "test_*.py"` | 云端分享/控制台契约 |
| 云端控制台前端 | `web/` | `npm run check:cloud-console` | 云端页面/写面 |
| UM 视图 | `web/` | `npm run check:um` | UM 读面 |
| 契约夹具新鲜度 | 仓库根 | `python tests/generate_frontend_contract_fixture.py` 后 `node web/scripts/backend-contract/check-generated.mjs` | 改契约代码后必须重生成 |
| 分享链名字 | 仓库根 | `python scripts/check_share_names.py` | 旧/新名字混用 |
| 退役功能 | 仓库根 | `python scripts/check_retired_features.py` | 退役功能不复活 |
| 端口审计 | 仓库根（Windows 生产机） | `python check_port_bindings.py` | 治理段未登记监听 |
| Linux 分享链 | 仓库根（WSL/Linux） | `bash um-cloud-deploy/run-linux-checks.sh --mode full` | 真实 8003 进程、长请求、云运维演练 |
| Cloudflare 入口 | 仓库根 | `python um-cloud-deploy/cloudflared/check-ingress.py --config um-cloud-deploy/config/cloudflared.yml.example --expect-host <配置主机>` | 4 条白名单 + 404 |
| 部署装配 | 仓库根 | `python um-cloud-deploy/backup_restore.py --check-deployment-assembly console <unit> <env>` | unit/env 安全围栏 |
| 发布包一致性 | 仓库根 | `python um-cloud-deploy/make-manifest.py --check um-cloud-deploy/RELEASE-MANIFEST.json` | manifest 与当前字节 |

切片验收至少跑：该切片对应的定向测试 + `bash scripts/check.sh --full`；改前端再跑 `web/` 的两个检查；改分享链名字再跑 `check_share_names.py`。

## 14. 模块边界与副作用总表

| 模块 | 读 | 写 | 外部边界 | 不做什么 |
|---|---|---|---|---|
| 8003 分享网关 | 投影文件、token/API Key、账本 | 账本 JSONL、服务日志、撤销状态、token/profile | UM 上游 HTTP、Edge/Playwright、实例 AF_UNIX | 不写 SQLite、不发布投影、不管理 Key |
| 8002 控制台 | 账本、日志、pid、broker 状态、安全状态 | console SQLite、share SQLite、会话码、投影（经 broker） | broker AF_UNIX、x11vnc、公网 DNS/TLS（显式检查） | 不启动 8003、不直接执行 chat2api.py、不自动备份 |
| broker | pid、/proc、投影、注册文件 | 投影、lifecycle、maintenance、broker-status、pid、停止意图 | AF_UNIX 管理/实例通道、信号、Popen | 不接受任意命令/路径/服务名 |
| Key 管理 | SQLite、安全状态 | SQLite、投影、安全日志 | broker 发布 | 不在日志/列表返回明文 |
| 用量账本 | JSONL | 无 | 无 | 不读私人账本、不写 |
| display | noVNC 静态、VNC helper | 运行目录、grant/session 内存 | x11vnc、浏览器 WebSocket | 不把密码写入响应/日志 |
| Cloudflare | 配置、DNS/TLS | 无 | DoH、公网 TLS | 不改配置、不 reload |
| 部署备份 | release、配置、数据、密钥 | release/unit/符号链接、备份包、恢复目标 | systemd、broker CLI、对象存储（显式） | 不自动备份、不覆盖已有目标 |

## 15. 未验证与待确认

- 运行 `bash scripts/check.sh --quick`（工作目录 `.worktrees/integration-rust-contract`，提交 `cb29da4`）：退出码 0。
- 运行 `bash scripts/check.sh --full`（同上）：退出码 0；`Ran 2310 tests` / `OK (skipped=63)`，`local/tests` `Ran 54 tests` / `OK`（`local/tests` 已于后续删除）。
- 运行 `python -m unittest tests.test_chat2api_linux_platform tests.test_cloud_display_on_demand tests.test_console_display_proxy`（同上）：退出码 0。
- 未运行 Rust 编译/测试：本票只产出测量、契约与切片计划，未创建 Cargo 工程。
- 登录中/登录后 RSS 未在本契约的新代码上验证；旧 release 的登录中 Chromium 读数见 `docs/headless-login-acceptance.md`。
- Chromium 未运行，没有 idle RSS；登录相关 display 行为以 spec #8 为准。
- #40 已合入本分支；8002/8097 未在云端复测。
- 未执行真实 Cloudflare Tunnel 变更、UM 上游登录、broker 重启或生产备份/恢复。
