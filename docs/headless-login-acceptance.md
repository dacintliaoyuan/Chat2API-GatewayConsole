# headless 登录行为验收与 RSS 基线（ticket #16）

- 对应 spec：#8
- 记录日期：2026-11-14
- 被测行为代码：`integration/headless-login` @ `aec8b3d`（#9–#15 已合入）
- 本机测试环境：Windows 10.0.26300 x64，Python 3.14.10，Git Bash
- 云端基线环境：`um-share-ecs`（SSH 别名），Ubuntu 26.04.1 LTS，kernel 7.0.0-38-generic x86_64，Python 3.14.4，release `/opt/um-share/releases/s2-20261113b`，`chat2api_build=e2d60349+reviewed`

## 行为验收

工作目录：`D:\git\github\Chat2API-GatewayConsole\.worktrees\16`（Git Bash）

命令：

```bash
python -m unittest -v \
  tests.test_chat2api_browser_login \
  tests.test_chat2api_reauth_recovery \
  tests.test_chat2api_auth \
  tests.test_console_um_login \
  tests.test_console_um_adapter \
  tests.test_cloud_display_on_demand \
  tests.test_domain_docs_headless_login \
  tests.test_console_display_route
```

结果：`Ran 236 tests in 53.924s`，`OK (skipped=1)`。跳过项是
`tests.test_chat2api_browser_login.RealDomLoginPageTests`：本机没有安装
Playwright 的 `chromium_headless_shell-1248`，`setUpClass` 报
`Executable doesn't exist at C:\Users\dacint\AppData\Local\ms-playwright\chromium_headless_shell-1248\chrome-headless-shell-win64\chrome-headless-shell.exe`。
其余 235 项通过，包括全部 fake page 驱动的主 seam、控制台契约 seam 与部署静态 seam。

| ticket | 验收事实 | 覆盖的测试 | 结果 |
| --- | --- | --- | --- |
| #9 | 术语与 ADR：重新认证会话不再绑定可见窗口；新增登录呈现方式、人工接管、登录浏览器单实例；ADR 记录 headless-first、按需 display、同 profile、纯 HTTP 出局理由 | `tests.test_domain_docs_headless_login`（5 tests） | 通过 |
| #10 | 默认 headless、复用同一 profile、POSIX 无 `DISPLAY` 不报 `display_missing`、fake page 下两段 UM 自动流程通过 | `tests.test_chat2api_browser_login.SessionLifecycleTests`、`tests.test_chat2api_browser_login.TwoStepFlowTests` | 通过 |
| #11 | 同 profile 单实例锁、进程死亡后锁自动释放、打开前只清理同 profile 旧 Chromium、直接调用与多实例都受约束 | `tests.test_chat2api_browser_login.ProfileSingleInstanceLockTests`、`tests.test_chat2api_browser_login.ProfileBrowserCleanupTests` | 通过 |
| #12 | 成功、超时、取消、失败都关闭 context 并释放 slot；5 分钟无进展关闭；30 分钟总预算仍生效；关闭失败显式上报 | `tests.test_chat2api_browser_login.SessionLifecycleTests`、`tests.test_chat2api_reauth_recovery` | 通过 |
| #13 | 三个 manual 原因各自返回 `manual_takeover_required:<reason>`；manual 阶段不保留浏览器；Duo 推送等待继续；重试开新轮 | `tests.test_chat2api_browser_login.TwoStepFlowTests`、`tests.test_chat2api_reauth_recovery.RecoveryScheduleContractTests`、`tests.test_chat2api_auth` | 通过 |
| #14 | display unit 安装但默认不 enable；按需 `manual-takeover.sh start`；默认路径不依赖 display；真实 systemd 留目标机 follow-up | `tests.test_cloud_display_on_demand` | 通过 |
| #15 | 重复 `POST /admin/auth/login` 加入当前轮；health/status 显示 manual 原因和人工接管窗口截止时间；成功、失败、取消状态正确；控制台不显示 token | `tests.test_console_um_login`、`tests.test_console_um_adapter`、`tests.test_console_display_route` | 通过 |

本次只跑了上表的定向行为验收；`scripts/check.sh --full` 由 workflow 的最终验证阶段执行，不在本记录中冒充已跑。

## #41 medium 修复验证记录

- 工作目录：`D:\git\github\Chat2API-GatewayConsole\.worktrees\integration-headless-fixes`
- 被测状态：`integration/headless-fixes` @ `67345d6`（修复提交 `61909fa`）
- 命令与结果（来自 #41 实现报告，本次记录只归档，不冒充本票重跑）：
  - `python -m unittest tests.test_chat2api_browser_login tests.test_chat2api_reauth_recovery tests.test_console_um_login tests.test_console_um_adapter tests.test_console_display_route`：`Ran 150 tests in 39.768s`，`OK (skipped=1)`，EXIT=0。
  - `python -m unittest tests.test_chat2api_auth`：`Ran 89 tests in 20.572s`，`OK`，EXIT=0。
  - `bash scripts/check.sh --full`：Python 套件 `Ran 2310 tests in 704.794s`，`OK (skipped=63)`；local 套件 `Ran 54 tests in 20.947s`，`OK`；其余编译、ruff、share-names、retired-features、check-generated、tsc、契约、日志与 UM 检查全部通过，EXIT=0。
- 未验证范围：上述记录来自 #41 的 Windows 本机运行，不是 #42 的回归证据；真实云端 systemd、真实 UM 登录/MFA/Duo 与部署后 headless 行为仍未在目标机复验。

## #42 low findings 验证记录

- 工作目录：`.worktrees/integration-final`
- 被测状态：`integration/feature-retirement-cleanup` @ `8063e97`（#42 实现 `74a4c8e`；评审修复 `b2dbb00`/`a1c4f9c`/`4e01712`/`8063e97`）
- 命令与结果：
  - `python -m unittest` 定向 576 项（11 个 headless/console 模块 + broker、端口、文档）: `Ran 576 tests`，`OK (skipped=37)`。
  - `python -m unittest tests.test_console_um_login tests.test_console_display_route tests.test_console_um_adapter tests.test_share_broker_server tests.test_share_broker_unix_socket tests.test_cloud_role_assembly`：`Ran 183 tests`，`OK (skipped=28)`。
  - `python -m unittest tests.test_fake_um_server` 连续 3 次：`Ran 50 tests` / `OK`，三次 EXIT=0；barrier teardown 的 `ConnectionAbortedError` 未复现。
  - `python -m unittest discover -s tests -p "test_*.py"` 连续 3 次：三次均 `Ran 2346 tests` / `OK (skipped=63)`，EXIT=0；`test_fake_um_server` 的 close 缺失与 `test_console_api` 的 `checkedAt` 跨秒失败已修复后未复现。
  - `bash scripts/check.sh --quick`：EXIT=0；前端 80 + 4 + 12 + 69 项通过，关键 Python 回归 135 项 `OK (skipped=5)`。
  - `bash scripts/check.sh --full`：`Ran 2346 tests / OK (skipped=63)`，`local/tests Ran 54 / OK`，EXIT=0。
- 未验证范围：真实云端 systemd、真实 UM 登录/MFA/Duo、部署后 headless 行为仍未在目标机复验；上述记录不是目标机证据。

## RSS 基线

基线目的：在改动部署到目标云机之前，记录当前生产 release `s2-20261113b`
（`chat2api_build=e2d60349+reviewed`，仍是可见登录旧代码，`headless=False`）的进程内存，
作为后续 headless 版本对比与数值目标的起点。本次没有部署 `integration/headless-login`，
所以下表**不是** headless 代码的基线。

工作目录：云端 `/`（SSH 会话 `um-share-ecs`）。RSS 单位：KiB（`ps -o rss=`）。
环境版本：Ubuntu 26.04.1 LTS，kernel 7.0.0-38-generic x86_64，Python 3.14.4。

采样命令（token 只从云端文件读取，不打印）：

```bash
ssh -F "C:/Users/dacint/.ssh/aliyun-um-ecs.conf" -o BatchMode=yes um-share-ecs
cd /
TOKEN=$(cat /etc/um-share/admin-control-token)
PID8100=$(pgrep -f "^/opt/um-share/venv/bin/python /opt/um-share/current/chat2api/chat2api.py --port 8100" | head -1)
PID18096=$(pgrep -f "^/opt/um-share/venv/bin/python /opt/um-share/current/usagesrv.py" | head -1)
PIDXVFB=$(pgrep -x Xvfb | head -1)
PIDVNC=$(pgrep -x x11vnc | head -1)
rss() { ps -o rss= -p "$1" 2>/dev/null | tr -d " "; }
chromium_rss() { ps -eo rss,args | awk '$2 ~ /chrome-linux64\/chrome$/ {s+=$1} END {print s+0}'; }
chromium_count() { ps -eo args | awk '$1 ~ /chrome-linux64\/chrome$/ {n++} END {print n+0}'; }
ps -o pid=,rss=,comm= -p "$PID8100" "$PID18096" "$PIDXVFB" "$PIDVNC"
echo "chromium_procs=$(chromium_count) chromium_rss_kb=$(chromium_rss)"
curl -s http://127.0.0.1:8100/health
curl -s -X POST -H "X-Admin-Control: $TOKEN" http://127.0.0.1:8100/admin/auth/login
curl -s -X POST -H "X-Admin-Control: $TOKEN" http://127.0.0.1:8100/admin/auth/cancel
```

采样时间：idle 取第一轮触发前 `2026-11-14T07:51:22Z` 的快照；登录中取第二轮
`POST /admin/auth/login` 后逐秒采样的峰值；登录后取第二轮浏览器完全退出后的
`2026-11-14T07:52:53Z`。第二轮触发前 `07:52:32Z` 的 8100 已是 179,532 KiB（第一轮登录后
尚未回落），因此 idle 以第一轮触发前为准。

| 进程（pid） | idle RSS KiB | 登录中 RSS 峰值 KiB | 登录后 RSS KiB |
| --- | --- | --- | --- |
| 8100 服务进程（95112） | 177,496 | 179,868 | 179,868 |
| 8100 broker（92120） | 26,164 | 26,244 | 24,988 |
| 18096 控制台宿主（103253） | 113,220 | 111,572 | 110,544 |
| Xvfb（1580） | 24,924 | 25,088 | 24,704 |
| x11vnc（1608） | 20,120 | 20,096 | 19,900 |
| Chromium 浏览器进程合计 | 0（无进程） | 852,228（9 个 `chrome-linux64/chrome` 进程） | 0（无进程） |

登录中逐秒采样（第二轮，`2026-11-14T07:52:33Z` 触发）：

```text
07:52:34  8100=179,804  18096=111,572  Xvfb=24,896  x11vnc=19,944  Chromium=19,156（1 进程）
07:52:35  8100=179,688  18096=111,520  Xvfb=25,088  x11vnc=19,920  Chromium=769,184（7 进程）
07:52:37  8100=179,228  18096=111,484  Xvfb=24,828  x11vnc=19,916  Chromium=852,228（9 进程）
07:52:39  8100=178,140  18096=110,896  Xvfb=24,876  x11vnc=19,916  Chromium=709,968（8 进程）
07:52:41  8100=177,556  18096=110,544  Xvfb=24,644  x11vnc=19,900  Chromium=810,260（9 进程）
07:52:42  8100=179,804  18096=110,544  Xvfb=24,704  x11vnc=19,900  Chromium=0（进程已退出）
```

基线采集说明：

- 为了得到“登录中”样本，显式触发了一次 `POST /admin/auth/login`。第一轮触发时健康读面为
  `authentication.state=failed`、manual 原因 `umpass_prefill_missing`、`recovery.state=exhausted`；
  上游 401 后可见 Chromium 完成登录，健康读面变为 `authentication.state=valid`。
- 第一轮用宽过滤 `chrome|chromium` 采到 Chromium 合计峰值 941,528 KiB（12 个匹配进程，含
  crashpad/辅助进程）；第二轮在凭据已有效时再次显式登录，用精确过滤
  `$2 ~ /chrome-linux64\/chrome$/` 采到浏览器进程合计峰值 852,228 KiB（9 个进程）。上表取精确过滤值。
- 第一轮 8100 采样误取了 broker 进程（92120），实际 8100 服务进程（95112）的登录中 RSS 来自第二轮；
  表中的 8100 broker 行单独列出，避免把 broker 的 25 MB 当成 8100 服务。
- 两轮登录都在浏览器退出后结束；第二轮 `POST /admin/auth/cancel` 返回
  `{"cancelled": false, "state": "no_login_in_progress"}`，说明登录已在调用前成功结束。
- 基线采集后，云端 `authentication.state` 从 `failed` 变为 `valid`。这是本次显式登录的真实结果，记录在此，
  不当作部署后 headless 行为的验证。该真实登录发生在旧 release、非本票验收（`s2-20261113b`）；
  本票只保留记录，不再执行新的真实云端登录（spec #8 把真实云端登录列为目标机 follow-up）。
- 采集时 `um-share-display.service` 仍是 `enabled`/`active`（旧安装遗留），`um-share-broker.service`
  与 `um-share-console.service` 均为 `enabled`/`active`；`8100`、`18096`、`5901` 都只监听 `127.0.0.1`。

## 数值目标

数值目标不在本 ticket 设定：数值目标留到基线后单独定，等 headless 版本部署到目标云机后，
用上面的同一命令复测 idle、登录中、登录后 RSS，与本次基线比较后再定；基线本身不构成目标，也不证明
改进幅度。

## 未验证范围

- 基线测的是 `s2-20261113b` 的可见登录旧代码（`headless=False`），不是 `integration/headless-login`
  的 headless 代码；新代码的 RSS 与内存收益未测量。
- 真实 headless 浏览器登录、真实 UM 登录/MFA/Duo 批准、真实 systemd 部署后行为都未在目标云机复验；
  本次真实登录是在旧 release 上完成的，不能代替新代码验收。
- 本机 `RealDomLoginPageTests` 被跳过：Playwright `chromium_headless_shell-1248` 不存在
  （`Executable doesn't exist at ...chrome-headless-shell.exe`）。真实 DOM 选择器契约未在本机执行；
  fake page 覆盖不能替代它。
- 云端 `um-share-display.service` 当前仍是 `enabled`/`active`。新 `install.sh` 的
  “默认安装不 enable” 只由静态 shell/systemd 检查覆盖；目标机现有 enablement 未改变，也未在真实
  systemd 上验证按需 `manual-takeover.sh start`。
- 部署后 headless 版本的登录中 RSS 峰值、成功/超时/取消/失败后的残留进程与锁释放都未在目标机复验。
- 端口 18096/5901 的治理段重编号不在 spec #8 与 ticket #16 范围内。

## 本机登录驱动切回条件（2026-11-19）

本机 8000 继续用 Playwright 驱动浏览器（2026-11-19 决定，暂不切回原生 Rust）。云端 8003 的原生 Rust 自动重登录验收通过并观察满 24 小时后，才可将本机登录驱动切回原生 Rust。按 [#112 的云端验收记录](https://github.com/dacintliaoyuan/Chat2API-GatewayConsole/issues/112)，2026-11-19 16:38（北京时间）记录了实际自动恢复，最早复核时间为 2026-11-20 16:38。切回前核对云端观察结果和实际运行版本。

2026-11-19 17:01（北京时间），在仓库根目录恢复本机 Playwright 配置，通过 `invoke-gateway-service.ps1 -Key chat2api` 停止并启动 8000，使用原有 Playwright 构建。上游明确拒绝旧凭据后，通过 `POST /admin/auth/login` 触发一次不可见的登录。Python 子进程由 8000 拉起，第一轮提交一次 UMPASS，最终 `last_outcome=restored`、`evidence.verdict=valid`，`GET /ready` 返回 HTTP 200。本次实测覆盖显式触发的后台登录；2026-11-19 决定本机继续用 Playwright，云端继续验证原生 Rust。云端 24 小时观察和本机原生 Rust 自动重登仍待验收。

## 驱动替换决定（2026-11-20）

云端 8003 的当前原生 Rust 驱动已完成 24 小时观察并实际自动重登录，说明当前手写 CDP 驱动可以工作。`chromiumoxide` 只替换启动、连接和鼠标事件，不提供 Playwright 的 stable、hit-target、enabled、重试和 `fill`；当前驱动在 #112 已补上这些语义。引入它还要增加 tokio runtime 和 MSRV 1.85，收益不足。决定不替换当前 Rust 驱动，#113 以 `wontfix` 关闭。

本机继续使用 Playwright opt-in。去掉本机 Python 依赖由 #120 跟踪：先把 `%LOCALAPPDATA%\Chat2API\credentials.json` 的 UM 账号密码注入 8000，再切回当前原生 Rust 驱动，不引入 `chromiumoxide`；验收是本机真实自动重登录和 Playwright 文件/opt-in 删除，失败就保留 Playwright。

实现状态（2026-11-20）：`lib/gateway-runtime.ps1` 已在本机 8000 启动时读取该凭据文件并注入 `CHAT2API_UM_ACCOUNT` / `CHAT2API_UM_PASSWORD`；显式环境变量优先，文件 ACL 不是仅当前用户可读时拒绝注入并只记录脱敏状态。真实本机自动重登录与 Duo 待维护者验收，Playwright 尚未删除。
