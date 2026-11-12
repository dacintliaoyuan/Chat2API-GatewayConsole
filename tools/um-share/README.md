# um-share · 分享 Key 计量闭环的只读核对工具

本目录装两个**只读**工具，用于把「分享 Key 计量闭环」的授权窗口压到最短：
现场核对（快照 + 比对）与三事实验收。它们服务于
`output/ai-gw-round6-20260927/metering-action-gate.md` 的精确动作门
（① 载入前检查 → ② 生成密钥 → ③ 受控重启 → ④ 三事实验收 → ⑤ 回退）。

**工具只读、不替代维护者授权。** 两个工具都只做三件事：读现场、算判定、打印。
它们**不重启任何服务、不生成密钥、不发模型请求、不写 `ai-gateway/data/`、
不改 `web/`、绝不打印任何密钥内容**（只给路径、原因码与 `strip()` 后的长度）。
「工具说 PASS」不等于「维护者已授权执行下一步」——②/③/④ 的授权仍由维护者
按动作门 §⑥ 逐项给出。

**「只读」的精确边界（2026-09-28 复审执行轮 9/10 后收紧：两个验收工具的委托观察已改为零写）。**
本目录两个工具自身唯一的写动作是 `share_chain_snapshot.py --out` 指定的那份快照
JSON（先临时文件再受控替换）；`metering_acceptance.py` 自己不写任何文件。
**`metering_acceptance.py` 与 `post_restart_acceptance.py` 两个验收工具**的 8110 观察
一律走**只读开关**，**不删任何文件、不写任何文件**（都不在 `ai-gateway/data/`
下、不杀活进程、不重启、不发模型请求）：

1. `production_backend_observe(forget_stale=False)`（`console_share_production.py:1495-1546`）
   把 `verify_running()`（`:1328-1346`）里唯一那次 `_forget()`（`:1408-1412`）关掉——
   此前它会在 `backend.pid` 存在但 PID 不匹配／进程已不在时 `os.remove` 那份**陈旧**
   pid 文件（`%LOCALAPPDATA%\SelfUseAI\ai-gateway\share-backend\backend.pid`，
   当前存在、5 字节）；现在陈旧文件照实报出并原样留在盘上；
2. `production_backend_pid_state()`（`:1549-1557`）读同一份 pid 文件与身份证据，
   全分支无写、无删：陈旧即 `state="pid_stale"`（进程已不在或身份不匹配，两者不猜）；
3. `_backend_auth_state()`（`:1560-1583`）会读 `control-token` 并以
   `X-Admin-Control` 头发只读 `GET /admin/auth/status`——本工具自身的 HTTP 请求不带
   凭据，这一条是委托的只读观察（只读 GET、非模型请求、不打印令牌内容）。

**仍带条件写的路径（本线未改，如实记在此处）**：`share_chain_snapshot.py`
自己的采集 `collect_snapshot`（def `:1037`，委托点 `:1062`）仍用默认 `forget_stale=True`
——`--out` 档拍快照时那份**陈旧** `backend.pid` 仍可能被 `os.remove`。它是本工具自己的
委托副作用，**没有被消掉**；要把它也改成零写会改变 `--out` 档行为，需单独决策（不在执行轮
10 的 L1 授权内，L1 只收紧两个验收工具）。

## 文件与角色

| 文件 | 角色 | 退出码 |
| --- | --- | --- |
| `share_chain_snapshot.py` | 只读现场快照（`--out`）+ 8096 重启前后逐项比对（`--compare`）；执行轮 9 起 schema v2 增窗口七类信号与 5 项窗口判定 | `--out`：0=已写出，2=失败；`--compare`：0=全 PASS，2=任一 FAIL/不可判定 |
| `metering_acceptance.py` | 三事实验收只读打印（动作门 §④） | 0=三件事实都成立且私人账本未变；2=任一不成立/不可判定 |
| `watchdog_forensics.py` | 看门狗机制取证（纯函数 + 夹具；执行轮 9）：输入信号快照 → 输出「会/不会/取决于 X」与命中规则 | `--print-rules`：0=已打印；`--from-fixture`：0=「不会」、3=「会」、2=「取决于 X」 |
| `post_restart_acceptance.py` | 8096 重启后八层验收（不能只验「8096 已重启」；执行轮 9） | dry-run 0；`--check`：0=八层全 PASS，2=任一层 FAIL/UNDECIDABLE 或采集失败（打印「未完成核对」） |
| `request_budget_estimate.py` | ④ 的授权声明与费用上限预估（执行轮 9）：价目表缺该模型即拒发 | 0=已算出上限并打印四行声明；2=模型不在价目表/请求数非正 |

两个验收工具自身**不含**任何写动作，除 `share_chain_snapshot.py --out` 指定的那份快照
JSON（先写临时文件再受控替换）；**两个验收工具**的 8110 观察走上文 `forget_stale=False`
的只读档，**不删任何文件、不写任何文件**。`share_chain_snapshot.py` 自己的 `collect_snapshot`
不在此列：它仍用默认档，上文已如实记出。

## 授权窗口内的执行顺序

动作门的顺序是 ①→②→③→④（每一步**先判据再进下一步**，§⑥ 给出未授权时的
停止点）。两个工具只覆盖其中的只读核对与验收，不执行任何动作：

```powershell
# 窗口之前：看门狗会不会因这次重启停分享链（纯离线，读夹具即可）
python ai-gateway/tools/um-share/watchdog_forensics.py --from-fixture output/<轮目录>/fixture-restart-8096-orphan-8110.json

# ① 之前/之内：现场基线（动作门 §① 的只读检查清单）
python ai-gateway/tools/um-share/share_chain_snapshot.py --out output/<轮目录>/snapshot-before.json

# ② 生成密钥 + ACL（须提权，照 ai-gateway/SHARING.md；本目录工具不参与）

# ③.1 受控重启 8096 之前：再取一次「前」快照
#     （与 ① 的快照可同一份，但重启窗口前重取一次更稳）
python ai-gateway/tools/um-share/share_chain_snapshot.py --out output/<轮目录>/snapshot-before.json

# ③.1 重启 8096（本目录工具不重启任何东西；命令见动作门 §③.1）

# ③.1b：重启后取「后」快照并逐项比对（核对表八行 + 门内不变式）
python ai-gateway/tools/um-share/share_chain_snapshot.py --out output/<轮目录>/snapshot-after.json
python ai-gateway/tools/um-share/share_chain_snapshot.py --compare output/<轮目录>/snapshot-before.json output/<轮目录>/snapshot-after.json

# ③.3 健康后验：8096 /health 200、PID 已变、读面键集 = 7 键（--compare 已含）

# ③.1 之后：8096 重启后八层验收（不能只验「8096 已重启」；任一层不过即退出码 2）
python ai-gateway/tools/um-share/post_restart_acceptance.py --check --before output/<轮目录>/snapshot-before.json

# ④ 之前：先算出这条请求的费用上限与四行声明（只读）
python ai-gateway/tools/um-share/request_budget_estimate.py --model <模型 id> --requests <N>

# ④ 三事实验收（须先有 ② 的密钥与 ③.2 的 8100 重启 + 维护者单独授权的一条真实分享请求）
python ai-gateway/tools/um-share/metering_acceptance.py

# ⑤ 回退：本目录工具不执行回退动作，只在回退后再跑一次快照/验收核对现场
```

- ③.2 的 8100 重启（提权，`Prep → Watchdog → Start -Mode real`）不在本工具内；
  本工具只观察 8100 的监听 PID、镜像名与启动时间。
- ④ 的真实分享请求（请求数、模型、费用上限、完成后入口状态）须维护者按
  动作门 §⑥.3 单独写明授权；本工具只读账本与读面，绝不发请求。

## share_chain_snapshot.py

### `--out <path>`

采集一份只读现场并写成 JSON 快照，字段：

| 字段 | 内容 |
| --- | --- |
| `listeners` | 8000/8096/8100/8110/8320 的 LISTENING PID（`netstat -ano -p TCP`） |
| `health_8096` | `GET http://127.0.0.1:8096/health` 的状态与 body |
| `chain_parent` | 8096↔8110 的父子关系（CIM `ParentProcessId`） |
| `gateway_8100` | 8100 监听者的镜像名与启动时间（CIM） |
| `share_status` | 控制台分享状态：`funnel_on`、归因读面键集与形状 |
| `gateway_attribution` | 同身份的只读直调：`file_ready`/`file_reason`/`key_chars`/`instance_loaded` |
| `backend_observe` | 8110 的 `running`/`auth_state`/`auth_label` 与账本路径 |
| `share_entry_probe` | 未带凭据的 `GET /`、`GET /health`（期望 404 `route_not_shared`）与 `GET /v1/models`（期望 401 `invalid_api_key`） |
| `operations` | 控制台在途操作的 `total` 与 `live`（queued/running 数） |
| `share_ledger` | 8110 账本行数、末行 `time/model/key_id/attribution`、已归属行、attribution 直方图 |
| `private_ledger` | 私人总账行数与 `key_id` 非空行数（门内不变式 3：必须为 0） |
| `window_signals` | 窗口七类信号，逐项标 `ok / absent / undecidable`（见下节） |
| `watchdog_task_query` | watchdog 任务查询原文：`elevated` / `identity` / `found` / `error`（审计用） |
| `not_covered` | 本快照**不含**的项：8100 启动任务日志、limits（须提权）、密钥内容 |

`schema_version`：v2 = v1 之上**只增** `window_signals` / `watchdog_task_query`（旧字段与判定语义未改）。
比对旧 v1 快照时，本工具会提示「窗口判定未纳入」并照常跑 round7 的 10 项。

退出码：0 = 快照已写出（即使现场本身是「未接线」也照写）；2 = 采集或写出失败。
读不到一律如实记为 `error`/`reason`（读不到 ≠ 没在听 ≠ 通过）。

### `window_signals`（窗口七类信号；执行轮 9 新增，只增不删）

| 信号 | 内容 | `ok` / `absent` / `undecidable` |
| --- | --- | --- |
| `read_surface_keys` | 8096 读面键集（4 键旧 / 7 键新） | 读到键集 = `ok`；读面可读但无 attribution = `absent`；读面读不到 = `undecidable` |
| `gateway_8100_image_start` | 8100 监听者的镜像名与启动时间 | 镜像名与启动时间都可读 = `ok`；8100 不在听 = `absent`；进程行/启动时间读不到 = `undecidable` |
| `backend_8110_pid_parent` | 8110 的 PID 与其父 PID；标出父 PID 是否等于 8096 的 PID | 三者都可读 = `ok`；8110 不在听 = `absent`；进程查询失败/行缺失/父 PID 或 8096 PID 读不到 = `undecidable` |
| `watchdog_task` | `umshare-watchdog` 计划任务的存在与 `State` | 提升身份下读到 = `ok`（权威「未注册」= `absent`）；**非提升身份查不到 = `undecidable`，绝不记成「未注册」**（动作门 §1.6） |
| `share_entry_gates` | 分享入口的 404/401 两种门 | 三条探测都命中期望 = `ok`；可读但某条门不符期望 = `absent`；任一条探测失败 = `undecidable` |
| `share_ledger_tail` | 8110 账本行数与末行 attribution | 可读且有末行 = `ok`；可读但 0 行 = `absent`；账本读不到/末行读不到 = `undecidable` |
| `funnel` | Funnel 状态 | `funnel_on` 可读 = `ok`；读面可读但无该字段 = `absent`；读面读不到 = `undecidable` |

### `--compare before.json after.json`

按动作门 **§③.1b** 的核对表逐项比对两份快照，逐项打印
`PASS`/`FAIL`/`UNDECIDABLE`：

| # | 比对项 | 通过判据 |
| --- | --- | --- |
| 1 | 8096 健康（③.1b 行1） | `/health` 200 `ok/usagesrv/8096` **且** 8096 PID 已变（PID 未变 = 新代码未载入） |
| 2 | 8100 监听（③.1b 行2） | 仍在监听且 PID 与重启前一致（8100 不随 8096 重启） |
| 3 | 8100 归因载入（③.1b 行3） | 密钥已就绪时 `instance_loaded=yes`；密钥未生成时两端一致报未启用（合法状态） |
| 4 | 8110 监听（③.1b 行4） | 仍在监听且 PID 与重启前一致 |
| 5 | 8110 认证（③.1b 行5） | `running=true` 且 `auth_state` 与重启前一致 |
| 6 | Funnel（③.1b 行6） | `funnel_on` 与重启前一致（由开变关 = FAIL） |
| 7 | 分享入口（③.1b 行7） | 未带凭据的 `GET /`、`GET /health` 仍 404 `route_not_shared`，白名单路由仍 401 `invalid_api_key` |
| 8 | 8110 账本（③.1b 行8） | 行数只增不减；已归属行逐行保留（`index`/`key_id`/`attribution` 不变） |
| 9 | 私人总账（门内不变式 3） | `key_id` 非空 = 0（且无 `attribution=verified` 行） |
| 10 | 8096 读面形状（③.3） | 归因键集 = 7 键（仍是 4 键旧形状 = 新代码未载入） |
| 11 | 8096 读面键集迁移（③.3，窗口专用） | 窗口前 4 键 → 窗口后 7 键（窗口前已是 7 键也 PASS，记「本窗口无必要」）；窗口后仍 4 键 = FAIL |
| 12 | 8100 镜像名与启动时间（③.1b 行2/行3 补强） | 镜像名 ∈ `{um-share-python.exe}`；PID 与 `started_at` 与窗口前一致 |
| 13 | 8110 父 PID 关系（③.1b 行4 补强） | 8110 PID 不变；父 PID = **窗口前的 8096 PID**（孤儿签名，符合 detached 预期） |
| 14 | watchdog 任务（③.1b 补强） | 窗口前后都读到且 `State` 不变（期望 `Running`）；**读不到 = UNDECIDABLE，绝不记成「未注册」** |
| 15 | 8110 账本末行归因（③.1b 行8 补强） | 末行 `index` 不回退、同 index 字段不被改写；窗口期间新增行如实打印 |

- 第 11–15 项**只在任一侧快照含 `window_signals` 时纳入**；两侧都是 round7 旧快照时不纳入
  （旧快照没有任何 watchdog 来源，强行判定会把「本快照没查过」误报成「未注册」），
  输出会打印一行说明。混用（一侧旧、一侧新）时纳入，缺失侧按 `UNDECIDABLE` 记。
- 第 11 项在自比对（同一份快照、8096 未重启）时必然 FAIL、第 13/14 项必然 `UNDECIDABLE`——
  这是**负向对照**，不是故障：窗口自比对预期退出码就是 2。

- 第 3 项以「密钥文件就绪度 + 8100 启动时间 vs 密钥 mtime 的推断」代替
  8100 启动行（启动行需提权读取，见 `not_covered`）；密钥未生成时两端一致报
  「未启用」也是**合法状态**，不是故障。
- 退出码：**全 PASS = 0；任一项 FAIL 或 UNDECIDABLE = 2**。读不到 ≠ 通过；
  不可判定按非零报，绝不用「看起来没问题」补齐。
- 判定逻辑（`compare_snapshots` 及逐项函数）是**纯函数**（字典进、字典出），
  与 IO 分离；`ai-gateway/tests/test_um_share_tools.py` 直接喂夹具字典验同一判定。

## metering_acceptance.py

只读打印三件事实（动作门 §④）与两个旁证：

1. **文件就绪** `file_ready` / `file_reason` / `key_chars`（只给长度，不给内容）；
2. **8100 实例已载入** `instance_loaded`（`value` / `basis` / `reason`；
   这是推断不是证明，查不到进程即 `unknown`，绝不默认 `yes`）；
3. **请求归因被观察到** `request_attribution_observed` 与 8110 账本末 3 行的
   `time/model/key_id/attribution`（运行期事实 = 某行 `attribution="verified"`）；
4. 控制台 `GET /api/console/share-usage` 的 `requests` / `keys[]` / `unattributed`；
5. 私人总账 `key_id` 非空行数（门内不变式 3：必须为 0）。

- 事实 1/2 优先取 8096 读面（新 7 键形状）；读面仍是旧 4 键形状时回落到
  同身份的只读直调（动作门 §4.1 的替代观察），输出里写明来源。
- 退出码：**0 = 三件事实都成立且私人账本未变；2 = 任一不成立或不可判定**。
  「未接线」是**合法状态**，但门必须报非零——绝不把未接线报成成功。

## 只读边界（两个工具共同）

- 两个工具**自身**的 HTTP 只发**不带凭据**的 `GET http://127.0.0.1:<本机端口>`；
  委托的 8110 观察（`production_backend_observe`）会带 `X-Admin-Control` 头发只读
  `GET /admin/auth/status`（见上文「只读」的精确边界）。进程查询只跑
  `netstat -ano -p TCP` 与 `Get-CimInstance Win32_Process`；账本只读打开。
- `share_chain_snapshot.py --out` 另跑一次 `Get-ScheduledTask -TaskName 'umshare-watchdog'`
  （只读查询，不写任何东西）：**非提升身份读不到 → `undecidable`，绝不记成「未注册」**；
  提升身份下的读数才是权威。
- 绝不打印密钥内容或分享 Key 明文：账本里本来只有 `key_id`，读面只给路径、
  原因码与 `strip()` 后的长度。
- 工具不做任何动作（②生成密钥、③重启、④真实请求、⑤回退）；这些动作的
  授权、判据与停止点见动作门 §②–§⑥。
