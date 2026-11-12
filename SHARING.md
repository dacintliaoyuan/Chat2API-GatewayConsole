# AI 网关：分享归因密钥（生成与轮换）

本文件只讲**归因密钥**这一份部署秘密。它不是分享 Key（每人一把，见
[um-share-deploy/README.md](um-share-deploy/README.md)），也不是控制台秘密：8100 分享入口给每条转发的请求附一个
HMAC 标记（`X-Share-Key-Id` 等头），8110 专用后端校验标记后把请求记到对应分享 Key 名下。
密钥缺失或过短**不是故障**，而是明确的「未接线」状态：分享照常服务，只是每条请求都记为**未归属**。

## 两端接线（必须指向同一个文件）

| 端 | 读取方式 | 注入通道 |
| --- | --- | --- |
| 8100 分享入口（`chat2api/share_gateway.py`） | 启动时**读一次** | 环境变量 `SHARE_ATTRIBUTION_KEY_FILE`：由 `um-share-deploy/deploy-elevated.ps1` 的 Start 阶段写进 live 包装脚本；也可用 `--attribution-key-file` 显式指定（优先级更高） |
| 8110 专用后端（`chat2api/chat2api.py`） | 每请求按 `stat` **重读** | 环境变量 `CHAT2API_SHARE_ATTRIBUTION_KEY_FILE`：由 `console_share_production._child_env()` 注入，路径由同一模块的 `share_attribution_key_path()` 解析 |

两侧默认解析到**同一个文件**：`<RunRoot>\run\share-attribution.key`，其中 `RunRoot` 默认
`C:\ProgramData\UMShareRun`（`deploy-elevated.ps1 -RunRoot` 的参数默认值；
`console_share_production.DEFAULT_SHARE_RUN_ROOT`）。`tests/test_console_share_production.py` 的跨端用例会解析部署
脚本，一旦两侧的密钥文件名或默认 `RunRoot` 漂移就会失败——**不要只改一侧的字面量**。

**硬约束（都实测过）**：

- **不要放 `ai-gateway/data`**：该目录对 `DAICNT\um_share` 是**显式拒绝 ACE**，8100 读不到 →
  静默退化成未启用（分享照常跑，但每条请求都未归属）。
- **不要放 `RunRoot\run\live`**：每次部署都会删除并重建该目录（`deploy-elevated.ps1` 的 Start 阶段），
  密钥会随之消失。
- 文件必须**同时**给 `DAICNT\um_share`（8100，启动时读一次）与 `DAICNT\dacint`（8110，每请求 stat
  重读）可读；`RunRoot\run` 目录本身是 `um_share:(OI)(CI)RW` + `SYSTEM:(OI)(CI)F` +
  `BUILTIN\Administrators:(OI)(CI)F`（`deploy-elevated.ps1:478`），所以**必须**给文件自己设 ACL（见下）。
- 注入通道是**环境变量**，不是 `UM_SHARE_ARGS`：D4 实验路径会把该串与配置派生串**逐字符比对**，
  多一个参数就会拒绝启动；环境变量不碰该契约。

## 生成（维护者本机执行；内容不提交 Git、不外传）

内容要求：一个秘密文本，`strip()` 后 **≥32 字符**（`ATTRIBUTION_KEY_MIN_BYTES = 32`，两端同值：
`chat2api/share_gateway.py:139`、`chat2api/chat2api.py:1601`）；ASCII、**无 BOM**、单行，
结尾换行可有可无（两端都用 `read_text(encoding="utf-8").strip()` 读，BOM 会留在秘密里并计入长度）。

**生成源必须是系统密码学随机数**：`Get-Random` **不保证密码学安全随机性**（微软文档明说，SDL 也把它列为
不适用于密码学的函数），不能用来生成密钥。下面用 .NET 的 `RandomNumberGenerator`（Windows 上由 OS CSPRNG
实现，本机 PS 5.1 实测 `Create()` 返回 `RNGCryptoServiceProvider`）取 **32 字节**再转十六进制：

- **32 字节 → 64 个 hex 字符**（每字节 2 个 hex 字符；Base64 则为 44 字符）；两种写法都 **≥32**，
  满足两端的最小长度；
- hex/Base64 都是 ASCII、无空白、单行 → `strip()` 不改动内容，运行时读到的长度就是 64（或 44）。

**写入步骤要在提升的 PowerShell 里跑**：`RunRoot\run` 目录只给 `um_share`/SYSTEM/Administrators 写权限
（`um-share-deploy/deploy-elevated.ps1:478`），本机实测非提升的 `dacint` 对 `RunRoot` 与 `RunRoot\run`
都是「拒绝访问」（`Get-ChildItem 'C:\ProgramData\UMShareRun'`、`icacls 'C:\ProgramData\UMShareRun\run'` 均被拒）。
读回、ACL 断言与运行身份自读可以在非提升提示符里跑（文件自身的显式 `DAICNT\dacint:R` 就是为此设的）。

```powershell
$keyPath = 'C:\ProgramData\UMShareRun\run\share-attribution.key'

# ① 生成：32 字节系统密码学随机数 → 64 个 hex 字符
$rng   = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)      # PS 5.1（.NET Framework）没有静态 GetBytes(int)，只能用实例方法
$rng.Dispose()
$key = -join ($bytes | ForEach-Object { $_.ToString('x2') })
if ($key.Length -lt 32) { throw '密钥短于 32 字符：两端都会退化成未接线' }

# ② 写入：无 BOM、单行、UTF-8（不追加结尾换行）
[IO.File]::WriteAllText($keyPath, $key, (New-Object Text.UTF8Encoding($false)))

# ③ 写后读回：字节数 + 内容哈希 + 无 BOM + strip 后长度；任一不符就 throw，不要继续
$keyBytes  = [Text.Encoding]::UTF8.GetBytes($key)     # 期望的逐字节内容（GetBytes 不加 BOM）
$readBytes = [IO.File]::ReadAllBytes($keyPath)
$readText  = (New-Object Text.UTF8Encoding($false)).GetString($readBytes)
$wantHash  = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($keyBytes) |
                    ForEach-Object { $_.ToString('x2') })
$gotHash  = (Get-FileHash -LiteralPath $keyPath -Algorithm SHA256).Hash
"bytes=$($readBytes.Length) chars=$($readText.Trim().Length) hash_equal=$($wantHash -eq $gotHash)"
if ($readBytes.Length -ne $keyBytes.Length -or $wantHash -ne $gotHash -or $readText -ne $key) {
    throw '写后读回不一致：字节数/哈希/内容与写入值不符（多写 BOM 或换行都会在这里被抓住）'
}
if ($readText.Trim().Length -lt 32) { throw '读回长度 <32：两端都会退化成未接线' }

# ④ ACL：只给两个读取方读、SYSTEM/Administrators 完全控制。必须 /inheritance:r——
# run/ 目录给 um_share 继承的 RW 若不收紧，um_share 能改写密钥（`deploy-elevated.ps1:478`）。
icacls $keyPath /inheritance:r `
    /grant:r 'DAICNT\um_share:R' /grant:r 'DAICNT\dacint:R' `
    /grant:r 'SYSTEM:F' /grant:r 'BUILTIN\Administrators:F'

# ⑤ ACL 核对：不是「icacls 有输出」，而是断言两个身份都有 Allow 读 ACE，再留原始输出
$acl = Get-Acl -LiteralPath $keyPath
foreach ($who in 'DAICNT\um_share', 'DAICNT\dacint') {
    $sid = (New-Object System.Security.Principal.NTAccount($who)).Translate(
        [System.Security.Principal.SecurityIdentifier])
    $allow = @($acl.Access | Where-Object {
        $_.AccessControlType -eq 'Allow' -and
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]) -eq $sid -and
        ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Read) -ne 0 })
    if ($allow.Count -eq 0) { throw "$who 没有 Allow 读 ACE：该端读不到密钥（8100→未归属，8110→no_key）" }
}
icacls $keyPath   # 留档：应看到 DAICNT\dacint:(R)、DAICNT\um_share:(R)、SYSTEM:(F)、BUILTIN\Administrators:(F)

# ⑥ 以运行身份自读一次（只打印长度，绝不打印内容）
# 8110 身份 = 当前 dacint 的非提升提示符：靠文件自身的显式 dacint:R ACE 读到
python -c "import sys;print(len(open(sys.argv[1],encoding='utf-8').read().strip()))" $keyPath
```

`DAICNT\dacint` 的读权限**显式给出**，不要只靠继承的 Administrators ACE：8110 由 8001 拉起，
本机实测当前非提升的 `dacint` 令牌里**没有** `BUILTIN\Administrators` 组，只靠继承 ACE 可能在运行时读不到。

**存成脚本再跑时的编码**：整段含中文，PS 5.1 对**无 BOM** 的 `.ps1` 按 ANSI 解码，本机实测会在第 ③ 步
解析失败（`意外的标记`）；存成 **UTF-8 with BOM** 或直接粘进控制台都正常（本机实测 rc=0，
`bytes=64 chars=64 hash_equal=True`）。与 README「编码注意」同一口径。

**8100 身份（`DAICNT\um_share`）怎么核对**：不要为了核对去 `net user um_share <新口令>` 或重新注册任务——
那会重置口令并让已注册的分享任务失效（`deploy-elevated.ps1:496-504` 的 LSA 配方是口令的唯一持有者）。
用两条证据代替：第 ⑤ 步的 Allow 读 ACE（静态事实）＋ 重启 8100 后的启动行 `请求归因：已启用`
（运行期事实，见下节）；若启动行仍报 `未启用（共享密钥不可读，后端记为未归属）`，就是 um_share 实际读不到。

**轮换时注意**：文件被收紧成 R 之后，非提升的 `dacint` **覆盖写会被拒绝**（本机实测
`WriteAllText` → 「对路径的访问被拒绝」），所以轮换要先取回写权限，写完再收紧并重跑 ④/⑤——步骤见「轮换」第 1 步。

## 启用与核对（接线后）

1. 重启分享入口 **8100**（它只在启动时读一次密钥）：按 **`-Stage Prep` → `-Stage Watchdog` →
   `-Stage Start -Mode real`** 三步执行（`run-deploy-guarded.ps1`，管理员窗口；与
   `um-share-deploy/README.md:22-29` 的 Prep/Watchdog/Start 序列一致）。**不要重启 8110 专用后端**：
   它每请求按 `stat` 重读，会自己用上新密钥。

   **不得用 `-Stage Stop` → `-Stage Start` 做常规更新**：`Stop` 会撤掉启动所需的**安全限制**
   （WFP app-id 过滤 + 防火墙出站阻断）并**注销 `umshare-watchdog` 任务**
   （`deploy-elevated.ps1:994-1003`：`Unregister-ScheduledTask -TaskName 'umshare-watchdog'` + `Remove-Limits`，
   后者 = `Invoke-WfpRemove` + `Remove-UmFirewallBlockRule`，`:285-288`），而 `Start` 的前置判据正是
   这两样（`:791-802`）——撤完再启按设计会 **fail-closed 拒绝**，不是「重启成功」。8100 的常规更新一律
   重跑 **Prep → Watchdog → Start real**；`Stop` 只用于应急/拆除（撤销动作本身是静默的：撤除失败时
   Start 可能侥幸通过，所以这条路径两头都不可依赖）。

   **失败时不开放入口（三条判据，都可只读核对）**：

   | # | 判据 | 代码依据（`deploy-elevated.ps1`） |
   | --- | --- | --- |
   | 1 | **Prep 失败 → 不注册 watchdog 任务、不启动分享进程** | `Prep` 只有三步：复制运行副本 → 装限制 → 强校验（`:654-658`），任一步失败即抛错、以退出码 1 结束（`:974-981`）；它**从不**注册 watchdog 任务、**从不**启动分享进程（Prep 自己会起辅助子进程：`wfp_helper.ps1` `:279`/`:288`/`:297`、`python.exe` `:345`，所以准确说法是「不启动分享进程」，不是「不启动任何进程」）。限制可能已被部分装上，但没有任务、没有分享进程去用它。 |
   | 2 | **Watchdog 未 `Running` → Start 拒绝** | `Start` 先要求限制强校验通过（`:775-778`），再要求 `umshare-watchdog` 任务存在且 `State=Running`（`:782-786`），否则 `throw`——没有武装的监视者就不开分享。 |
   | 3 | **Start 失败 → 入口不得处于可服务状态** | `Start` 在「8100 30 秒内无监听」（`:873`）或「监听者镜像/属主不符」（`:886`）时抛错；失败后必须核对：公网 443 **无** Funnel 映射 **且** 8100 **无**监听。这是**失败后必须成立的状态**、不是代码保证（进程可能已经起来），核对不通过就先关掉入口再继续。 |

   只读核对命令（都不改任何状态）：

   ```powershell
   # 公网：应看不到 443 → 127.0.0.1:8100 的映射
   & 'C:\Program Files\Tailscale\tailscale.exe' funnel status
   & 'C:\Program Files\Tailscale\tailscale.exe' serve status

   # 8100：应没有任何 Listen 行（无输出 = 没在服务）
   Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue
   # 等价只读写法：netstat -ano -p TCP | findstr ":8100"
   ```

   **「测试后关闭入口」的次序（终态对了、次序错了会让驱动判 FAIL）**：终态是「8100 停止 + Funnel
   关闭」，但关的顺序必须是 ①请求在 **Funnel 开启时**发出（否则那条请求不是公网请求）→ ②**先关 Funnel**
   → ③8100 **保持监听**，让驱动 `metering_request_probe.py` 的运行后门判据核对完成（`/` 与
   `/v1/models` 仍答 404/401、`funnel_on=False`，见 `:92-95`、`:243-262`）→ ④**再停 8100**。
   反过来先停 8100：运行后两门判据读不到（`metering_acceptance.py:85-86` 把连接被拒记成
   `status=None`）⇒ 驱动 `:640-641` 的 `ok` 判定为未通过。**声明值仍写终态「8100 停止、Funnel 关闭」，
   上面四步只是次序。**

2. 部署日志（`um-share-deploy/logs/deploy-*.log`，每个 Stage 各一条：Prep / Watchdog / Start 各写一条）：
   **Prep 与 Start 的日志各应出现** `attribution key: FILE READY`（**Canary 走同一条 `Copy-ShareSource`，
   也会写这一行**）；**Watchdog 的日志不会有这一行**——`Write-AttributionKeyState` 的调用点只有
   `Copy-ShareSource` 内的 `:482`（Prep `:655`、Canary `:661` 走到）与 Start 的 `:854`，打印只在函数体
   `:602-621`，Watchdog 阶段（`:894-930`）不调用它。文件未就绪时是
   `WARN attribution key: NOT FILE READY (reason=missing|unreadable|too_short)`。**注意这只是「部署时本进程能读到长度合格的文件」**（2026-09-26 复核 F-18：原先叫 WIRED 的名字强于它证明的事实）。
3. 8100 启动行（`C:\ProgramData\UMShareRun\run\live\raw.txt`）应出现 `请求归因：已启用`；未接线时是
   `未启用（共享密钥不可读，后端记为未归属）`。
4. 状态读面：`console_share_production.production_backend_observe()` 的 `attribution` 键给出**三件互不相同的事实**
   （2026-09-26 复核后拆分，**不再有 `wired` 这个含义模糊的字段**）：
   - `file_ready` / `file_reason` / `key_file` / `key_chars` —— **文件就绪**：本进程此刻能读到且长度合格；
     `file_reason` 是文件级原因码（`ok` / `missing` / `unreadable` / `too_short`），四者不混同；
   - `instance_loaded` —— **运行实例是否已载入**：`{value: yes|no|unknown, basis: inference, rule, reason, gateway_pid, gateway_started_at, key_file_mtime}`；8100 **只在启动时读一次**密钥，所以文件就绪 ≠ 8100 已用上新值；
     读不到进程或监听者身份不符时 `value=unknown`，**绝不默认 yes**；
   - `request_attribution_observed`（布尔）与 `request_attribution_detail` —— **请求是否已被验证归属**：本读面
     不观察该事实，固定 `false` + `verdict=not_observed_here`，真正的来源是 8110 账本里 `attribution=verified` 的行。
   判读时必须三件分别成立，**不得**用 `file_ready=true` 或任何单一字段宣布「接线成功」。
5. 端点字段（owner 裁定 2026-09-26，加性）：`GET /api/console/share-management/status` 的
   `attribution` 字段就是上面第 4 条那个子字典（控制台状态面 `share.status` 动作的
   `backend_auth.attribution` 同形状、同口径），本机同源页面免凭据可读；文件未就绪时如实报
   `file_ready=false` + `file_reason`，绝不折成已启用；消费面**必须判 `request_attribution_observed`**，
   不要用 `request_attribution_detail` 的真值判断（未观察时它是非空字典）。
6. 归因分布读面：`GET /api/console/share-usage`（控制台面）或 `GET /api/share/usage`（管理面）的
   `unattributed.attributionReasons` 给出**原因分布**，用来区分「未接线」与「接线了但都未归属」：
   - `absent`：8100 侧没有密钥 → 请求根本没带标记（8100 未接线）；
   - `no_key`：8110 侧读不到密钥 → 标记带了但校验不了（8110 未接线）；
   - `bad_signature`：两端**不是同一个文件**（接线了但密钥不一致）；
   - `verified`：校验通过，`keys[]` 里按分享 Key 分开计数。

**注意（两步分开验收）**：归因启用状态已在**端点**可见（上面第 5 条，加性字段），把它**渲染到网页**
仍是**另一步**（前端接入，按 cross-line-delivery.md 交前端线），两者分别验收——「字段可见」不等于
「前端已展示」。当前 1634 条历史分享记录全部未归属，接通归因链也不会回填它们。

## 轮换

1. 把**同一个路径**的文件内容换成新值：文件已被收紧成 R，先取回写权限，再重复生成段 ①–③（生成、写入、写后读回），
   最后跑 ④ 的 `icacls` 收紧与 ⑤ 的 ACL 断言：

   ```powershell
   icacls $keyPath /grant:r 'DAICNT\dacint:F'   # owner 有隐式 WRITE_DAC，非提升也能取回（本机实测可写）
   # ... 生成段 ①–③：生成新密钥、写入、写后读回 ...
   # ... 生成段 ④ 收紧 ACL、⑤ 断言两个身份都有 Allow 读 ACE ...
   ```

2. **只重启 8100**（分享入口；按上面「启用与核对」的 **Prep → Watchdog → Start real** 三步，不得用
   `Stop` → `Start`），**不重启 8110**（8110 每请求 `stat` 重读，会自己用上新密钥）。
3. 核对：部署日志 `WIRED`；`GET /api/console/share-usage` 的**新**请求开始出现 `keys[]` 归属
   （旧记录不回填，仍显示未归属）；`unattributed.attributionReasons` 不再增长 `bad_signature`。

写后读回（上面 ③）只证明**文件内容**已经是新值，不证明运行中的 8100 已载入新值——它只在启动时读一次，
所以第 2 步的重启不能省。

**只换文件不重启 8100**：8100 用内存里的旧密钥签名、8110 用新密钥校验 → 之后每条请求都记
`bad_signature`（**全量未归属，且不报错、不拒绝服务**）；重启 8100 后即恢复。反过来只重启 8110（或
8110 读不到文件）会得到 `no_key`。

## 未接线/失败语义（都不是故障）

- 文件缺失、不可读或 `strip()` 后 <32 字符：两侧都不附/不认标记 → **每条请求记为未归属**，
  分享照常服务，不报错、不拒绝服务、不降级其他功能。
- 归因不了的历史请求与绕过入口的请求一律记为**未归属**，绝不均摊或猜测归属。
- 密钥内容绝不出现在日志、状态读面或错误信息里：读面只给路径、原因码与 `strip()` 后长度。

## 凭据读面的单一 reader 规则

凭据文件不设共享的 Python 读面。每个文件格式只有一个 reader，且 reader 归它的消费者所有：

- `chat2api/token.json`（UM 会话）由 `chat2api/chat2api.py` 自己读；
- `data/admin-control-token` 由 `lib/gateway-runtime.ps1` 的 `Get-LocalControlHeader` 读（PowerShell 不能 import Python）；
- 分享归因密钥由 `share_secret_protection.py` 读，接线规则见上面 §1。

只有**两个以上 Python 消费者必须对同一文件格式达成一致**时才新建共享 reader；一个消费者一个 reader，绝不新造第二套路径或第二种读取口径。原来的 `gateway_credentials.py` 就是这条规则的占位，它没有任何代码，已删除。
