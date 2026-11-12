# AI 网关控制台 (AIGatewayConsole)

网页控制台（本地与云端两个入口）及其管辖的本机服务：当前只管理 UM chat2api 与费用/管理宿主，负责受管服务的启停与健康观察，并按来源展示用量、额度和费用；云端只承载 UM 的 API 服务。Qoder 国内/国际、Gemini CLIProxy 与 M365 已退役，历史账本与日志保留。命令行恢复入口是 `invoke-gateway-service.ps1` 与 `Recover-ShareControl.bat`。

## 入口

| 角色 | 真实入口 | 生产产物 |
|------|----------|----------|
| 本机 | `Open-GatewayConsole.bat` → 8001；`invoke-gateway-service.ps1` 启停；`Recover-ShareControl.bat` 分享恢复 | `web/dist`（local 角色，8001 托管） |
| 云端 | `Open-CloudConsole.bat` → 专用 SSH 隧道 → 18096；`um-cloud-deploy/install.sh`／`update.sh`／`rollback.sh` | `web/dist-cloud`（cloud 角色，打包为 `web/dist`） |
| 共用 | `usagesrv.py` 的 `UM_SHARE_ROLE` 装配；`web/src/console-entry.ts` 按构建期角色装入 | `web/src/shell/`、`web/src/console/backend/`、`share_*.py`、`chat2api/` |

候选目录、打包目的地与历史材料归属见 [docs/gateway-entrypoints.md](docs/gateway-entrypoints.md)。各脚本与目录的职责清单见 [README.md 的文件一节](README.md#文件)。

## 产物与构建角色

候选：`--mode candidate-local` → `web/release/candidate/local/`；`--mode candidate-cloud` → `web/release/candidate/cloud/`；旧默认 `--mode candidate` → `web/release/candidate/`（8099 只读预览）。生产构建必须显式 `CONSOLE_PRODUCTION_BUILD=1` 与 `CONSOLE_ROLE`，本机角色另需 `CONSOLE_LOCAL_SHARE_RETIRED=1`；每次构建都写 `candidate-build.json`，8001／云端 18096 只托管与自身角色一致的树。发布包默认写到 `output/um-share-release/`，本机不写生产 `dist/`。

### 控制台候选预览（8099，只读）

`ai-gateway/web/release/candidate` 的候选页在 `http://127.0.0.1:8099/` 上只读预览（`vite preview`，见 `PORTS.md`）；8001 已恢复为 local 角色的 UM 生产页，8099 只用于候选预览与门禁，不再替代生产入口：

- **只答 GET/HEAD，对生产 0 转发**：`vite.config.ts` 的 `candidateReadOnlyGuard` 在代理之前把**一切非 GET/HEAD 在本机拒成 403**（JSON 响应带可读原因；含 OPTIONS 预检——`preview.cors` 已关闭，否则 vite 自带的 CORS 中间件会抢先以 204 应答），一个字节都不转发给生产网关。原先「后端声明为只读的动作 POST 可以转发」的例外已撤销：那些 POST 同样只得到本地 403（它们会在生产操作账本落行）。要执行任何操作请用 8001 生产页面。改动对**运行中的预览进程**生效需要重启它——vite 只在启动时读配置，`scripts/ensure-candidate-preview.ps1` 只在 8099 未监听时才启动新进程。
- **候选构建输出固定**：`--mode candidate-local` → `release/candidate/local/`，`--mode candidate-cloud` → `release/candidate/cloud/`；旧默认 `--mode candidate` → `release/candidate/`（本机角色，8099 预览与日志门禁读取；重建不清空根目录，保留 `local/`、`cloud/` 子目录，根目录旧文件可能残留）。每个模式拒绝其它 outDir。
- **禁止直接写 `dist/`**：`dist/` 是 8001 托管的 local 生产页，`dist-cloud/` 是 cloud 生产页；构建守卫默认拒绝覆盖，生产构建必须单独授权并显式声明 `CONSOLE_PRODUCTION_BUILD=1` 与 `CONSOLE_ROLE=local|cloud`，本机角色另需 `CONSOLE_LOCAL_SHARE_RETIRED=1`。`prebuild` 仅执行 rolling-number 兼容补丁，旧档案导出、字体准备和托管平台发布脚本已移除。
- **产物与源码的绑定可核对**：每次构建都在产物根写 `candidate-build.json`，记录 `line`／`role`／`mode`、每个构建输入（`index.html`、`package.json`、`tsconfig.json`、`vite.config.ts`、`src/`、`public/`）的 sha256、聚合 `sourceHash`、`gitHead` 与入口资产名；重算源码哈希与 `sourceHash` 比对，即可判断 8099 或生产目录上的产物是否由当前源码构建。`usagesrv.py` 按该文件的 `role` 拒绝托管另一角色的树。

## 本机控制台私网访问

8001 后端仍只监听 `127.0.0.1`。手机连接本机现有 Tailscale 私网后，经本机 **HTTPS HTTP Serve** 入口访问；本机 `http://127.0.0.1:8001/` 保留。实际入口和授权设备由私有配置 `data/console-tailnet-access.json` 管理，不写入 Git。

配置版本为 `schema_version: 1`，包含 `enabled`、精确的 HTTPS `origin`（含端口）、`allowed_login` 与 `allowed_peer_ips`。仅同时匹配登录身份及设备地址的 PC／手机能进入；同账号的其他节点不会自动获权。配置缺失时关闭私网访问，读取失败或格式损坏时拒绝私网请求，本机访问继续可用；修改策略后只需重载 8001。

Serve 必须代理到 `http://127.0.0.1:8001`，不能改用原始 TCP 转发或 Funnel。后端检查 Serve 重写的来源、身份与 HTTPS 元数据；发现代理头时不回退到本机信任，写操作保留跨源与 CSRF 检查，已有管理令牌／会话校验继续执行。私网访问依赖电脑开机、8001 和 Tailscale 正常运行，不等于公网发布。

服务识别按监听地址和端口共同判断：Tailscale 在私网地址使用 8000／8001，与 UM 或控制台的 `127.0.0.1` 监听可共存；通配地址占用或归属无法核实仍拒绝启停。停止控制台仅终止已核实的本实例进程，不终止同端口的 Tailscale 进程。8096 已退役，迁移时先验证新入口，再单独撤销旧 8096 Serve 映射，保留 8000 映射。

## 模型设置与前端接口

模型设置按 `source|真实 model_id` 标识。`model.settings_save` 只保存草稿；别名、上下文来源、上下文 Token 和默认思考档位可显式传 `null` 恢复目录默认，省略字段则保留原草稿或已应用值。默认思考档位须有目录或已验证能力证据；采用实测上下文须有同模型、同测量代次的可信 Token 下界，字符长度不能充当 Token。

发布顺序为 `model.settings_preview(selection)` → `model.settings_apply(selection, preview_token)`。预览基于所选草稿生成真实同步计划，绑定版本、目录、证据和客户端配置快照；非空应用必须携带相同选择与 token，空选不会扩大为全部。客户端快照变化返回 `version_conflict`。ZCode 运行中无法通过兼容准入，或规则库不可用时，整组设置延期，原已应用值和所有客户端配置保留，新草稿仍待同步；正常发布在一次事务中写入设置及客户端配置。取消在落盘前生效，落盘后的客户端实际读回是另一项证据。

`model.context_overrides` 分别返回草稿、已应用值与 `client_readbacks`（按消费方的别名、上下文、默认档位、只读输出上限及比对结果）；没有配置或无法读取时保持 `absent`/`unavailable`，不填零。`model.context_probe_preview.records[].history` 是有界历史证据。日志 `GET /api/console/log-entries?request_id=…` 按结构化请求 ID 精确筛选，范围仍是当前读取窗口，可沿 `next_cursor` 查询更早窗口；不是全文请求链追踪。

分享用量的 Key、模型、日期及无日期分组各自带 `coverage`、`tokenUsage`、`priceVersions`。`tokenUsage.totalTokens` 只含完整可信输入＋输出；中断/单侧用量列为下界，估算和未知另列。`coverage.costComplete=false` 时金额只是有价格覆盖的部分，不能解释为完整费用；真实零仍为零。旧金额与 Token 字段保留原兼容口径。私人模型排行使用 `token_total` 和 `token_coverage`，不能用旧的单侧输入/输出累加推算完整总量。

前端数据模块在 [`web/src/console/backend/`](web/src/console/backend/)，接线、夹具和验证入口见 [`web/scripts/backend-contract/README.md`](web/scripts/backend-contract/README.md)。它没有 DOM 或样式依赖；生成夹具通过真实后端处理器和私有文件取得，明确为 Demo 数据，不访问生产服务。本机生产页已装入 UM 三页，并由 `web/src/local/um-data-controller.ts` 读取真实接口；`?console=workbench` 保留旧工作台作为兼容入口。代码验证完成不等于生产已载入，真实 UM 读取与动作仍须按运行验收记录判断。

Rev9（2026-10-03，A9-1～A9-8）新增四条单层动作并发布其参数 schema：`share.diagnostics`（八层只读诊断，无副作用、不触发 SSO/模型请求）、`share.backend_reauthenticate`（仅重新认证 8110，不冷启动、不重启进程）、`share.backend_restart`（单层重启 8110：暂停 8100 接收→有界排空→重启→恢复接收，超时不强停）、`share.gateway_restart`（单层重启 8100，只经受限 broker 宿主协议，绝不以 `share.stop→share.start` 代替）。`GET /api/console/actions` 加性发布有界 `param_schema`（类型/上下限白名单，无函数对象）；两个 restart 动作成功后取消同资源排队工作。夹具升级为 [`tests/fixtures/frontend_contract_rev24.json`](tests/fixtures/frontend_contract_rev24.json)（`rev23.json` 保留）：新增 `share_maintenance` 段（动作目录、九场景：正常/未知/过期/部分失败/认证中/维护中/超时/取消/预算耗尽、监督决策），由真实处理器经真实 registry 与 OperationManager 加注入的私有夹具生成，监督决策来自真实 `share_backend_supervision.run_once`（临时监督目录、假开机标识与假 broker 视图）；诊断与维护事件不写 Token/费用账本，分享账本仍与私人账本隔离。

8110 的维护入口区分“启动 / 按需登录 8110”和“重启运行中的 8110”。前者经 `share.backend_login` 启动已停止的专用实例，认证有效时无需重复登录；认证状态未知时入口仍可见。后者经 `share.backend_restart` 核实已有实例身份后排空并重启，不冷启动，不重启私人 8000 或分享入口 8100。

## 分享后台响应与本机恢复

分享状态、密钥列表、用量及单项密钥读取覆盖响应体解析的 45 秒截止；重叠状态刷新合并为同一次在途读取，失败后可重新刷新，不以空表或旧成功冒充新结果。九层诊断逐层报告进度；8100 重启在执行副作用前再次检查取消，8110 的前后比较只读取后端与认证两层。开启失败仅回滚已确认由本次开启的层，未知前态和原有映射均保留归属。

“受限后台未响应”表示当前控制连接未获确认，不能据此断定任务已停止或账号已失效。`share.start` 先做只读后台预检，未确认时不启动或登录 8110，避免启动后再回滚。后台存活查询只读取任务状态（上限 6 秒），不在单条管理管道中执行耗时的 WFP／防火墙检查，安全条件字段保持未知；显式分层诊断与真正开启时的安全门继续完整核验。三证及宿主证据的只读请求使用 35 秒总预算；其中 WFP 与防火墙复合检查有独立的 25 秒执行上限，超时、检查程序缺失和权限拒绝分别报告；状态只读 RPC 也采用 35 秒预算，允许排在 25 秒安全检查之后；管道繁忙后遇到切换空隙时继续沿用同一请求的剩余期限，从未观察到服务的缺失管道仍按短时限拒绝，启停期限保持原契约，不重发已提交操作。 原生管道空闲接入与连接后读取各自有界计时，避免在空闲窗口末尾接入时丢弃正常请求；显式调用期限仍是不可续期的总截止，静默客户端到期后断开并释放。 同步管道 I/O 超时后先通过 `CancelSynchronousIo` 取消本进程拥有的工作线程 I/O，再关闭句柄并有界确认线程退出；不能把 `DisconnectNamedPipe`／`CloseHandle` 自身当作可靠的超时机制。已开始的管理处理不以取消管道 I/O 打断其文件事务。[Windows 接口契约](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelsynchronousio)。

网页出现该提示时，在本目录运行 [Recover-ShareControl.bat](Recover-ShareControl.bat)，入口先由原用户权限重载 8001，再通过 Windows 管理员确认恢复固定后台任务；管理员子进程不会启动提升权限的网页控制台。默认只读检查为 `pwsh -NoProfile -File Recover-ShareControl.ps1 -Action Check`。恢复入口核对已有 `umshare-broker`、`umshare-watchdog` 的 SYSTEM 身份、完整动作及进程归属；优先仅重载 8001，仍未确认时才停止并恢复已有 broker。旧任务与旧进程均须确认停止后才再启动，不重建任务、清除凭据或更改 ACL，不操作私人 8000、分享 8100/8110 或公网映射。

恢复使用固定本机只读口 `GET /api/console/share-management/control-status`，只确认后台响应，不装配分享存储、不探测账号、不调用模型，也不表示整条分享链已开放。恢复后刷新网页、读取分层状态，再按需启动专用后端和开启分享；账号有效时跳过登录。任务缺失、身份不符、权限不足和响应未确认分别报告，失败不冒充成功。

### 分享页与公网巡检

分享页顶部常驻紧凑概况：状态、本机就绪、公网 TLS 与手动模型测试分别表达，下面是实际分享地址、普通启停与独立紧急停止。内容分为「密钥／诊断与恢复／用量」三页签，默认密钥；页签随页面正常滚动，不新增吸顶层。异常原因和对应处理入口直接在顶部显示，人工关闭与巡检取消不被画成需要自动重开的故障。

密钥区是操作表格：名称、掩码、状态（有效／已停用）、并发和常用显示/复制/测试在行内，低频的改名、调并发、轮换、撤销与删除直接展开为行内动作网格；表格固定列宽、确认标签简短，点击后按钮不位移。Key 长期有效，页面不提供停用/启用；撤销会终止在途请求并显示 terminated/released/aborted/upstream_closed 计数（未上报的保持未知）。窄窗口转为紧凑条目，关键信息与动作不横向溢出。诊断页承载分层状态、公网巡检、历史与维护入口；用量页以每密钥请求数、Token 与人民币费用汇总表为主，失败汇总按成功/失败/已取消/未判定分列并可打开带 `result=` 的逐请求明细，趋势与模型/日期明细按需展开，未归属与已删除身份单列。

分享用量的费用按每条记录当时有效价表版本与汇率换算成人民币，已知、估算与合计各自标记是否为下界；原始 USD 字段保留可追溯。旧响应没有人民币字段时显示「人民币读数未提供」，缺时间、缺汇率或无价记录显示未知或下界，不按当前汇率补算历史，也不把未知折成 ¥0。公网 DNS/TLS 检查与手动模型测试分别表达。人工关闭、运行意图代次或检查目标变化会使旧轮次失效，过期结果不能写成正常或发出恢复通知；恢复仍遵守 60 秒巡检、10 秒复核及滚动 30 分钟 2 次映射 / 1 次 Tailscale 重启 / 3 次 Tailscale 服务启动上限。

应用轮次在读取公网地址前先只读观察 Tailscale 服务：确认服务停止时，先用受限原语启动并确认运行，再继续本轮 DNS/TLS 探测，因此「公网地址暂不可得」的未检查轮次和全部入口失败轮次都能恢复。服务看门狗只启动、从不停止服务，遵守安全、维护、取消与运行意图门禁；状态不可读按未知处理，不当作停止。Tailscale 服务启动使用独立的滚动 30 分钟最多 3 次预算，不占用映射或重启槽位；旧预算文件缺少服务启动键时按空历史读取，不判为预算不可读。Tailscale 重启在发送停止前先确认安全与连通性，条件不满足时不发送停止；停止确认后只复核不依赖服务运行的安全/维护/取消/意图条件，通过则启动，任一条件不满足、启动被拒绝或未确认时如实上报（exit 2/1），不把已发送的启动写成无动作或成功；临时失败或未确认时有界重试一次，永久拒绝不重试并如实上报。

公网巡检每 60 秒同时查询 Cloudflare 与 NextDNS DoH（Google 源仍兼容读取旧状态），并分别记录两源状态、DNS 状态码/分类、有效公网 IPv4、TTL/负缓存剩余、检查时间与实际下次复查时间。NXDOMAIN、无 A、SERVFAIL、DoH HTTP/格式错误和超时分别呈现；缺少 SOA/TTL 显示未知，不硬编码 300 秒。DNS 解析分歧只做页面提示，不发系统通知、不触发恢复；两个源返回不同的有效地址集合本身不判故障。

## 费用统计口径

- **价格引擎**：计费语义移植自 dsh-cost-meter v1.7.7（模型匹配/峰谷判定/选档），但价目表**自持**（2026-09-11 起与插件解耦，不再读取插件账本或插件包内任何文件）。读取链：本目录 `data/prices.json`（主源，用户查官网后手工维护——改价格、更新文件顶部 `checkedAt`；维护方法见文件内 `_说明` 键）→ `data/price.override.json`（补缺，手工编辑，最高优先级）→ 未定价标注（费用不计入）。`checkedAt` 超过 30 天页面黄条提醒核对官网。
- **峰谷价**：与 cost-meter 口径一致——北京时间 09:00-12:00 / 14:00-18:00 高峰 2 倍价，周六/周日全天谷价，逐条按请求时刻判定；峰谷开关/生效日/时段与汇率均取 `prices.json`，全站 CNY 展示。

## 使用约定与直连

- 全启只纳入 chat2api 与后台宿主；Qoder 国内/国际、Gemini CLIProxy 与 M365 均为退役来源，不再有活动菜单项、启停或健康检查目标。每天 06:00 目录镜像不唤醒电脑、错过不补跑。ZCode 运行中按文件锁、配置格式与变更监听协议判定兼容，版本仅作信息；兼容时协作锁内发布 provider_config.json，不兼容或事实不完整则待应用、不自动重启。人工模型与用户排序保持不动。私人 8000 守护受本次开机的启动意图约束，见下方守护章节。
- **直连约定**（2026-09-02）：由控制台拉起的服务一律清空代理环境变量并设 `NO_PROXY=*`，网关间转发、上游访问和模型体检都不走 VPN/系统代理；`start_chat2api.bat` 单独双击运行时同样生效。
- 网络策略由本机配置维护，清理不修改代理或 TUN 规则。当前网关的网络要求见 `chat2api/` 的说明；退役来源的域名规则不再作为部署说明。
- chat2api 把“服务运行状态”和“上游认证状态”分开：认证值为「认证有效」「需要登录」「登录中」「登录失败」。启动验证或真实业务请求首次收到 401 时自动弹出唯一的 Edge 登录窗口；失败后保持「登录失败」，不会被后续请求反复弹窗。网页控制台 8000 卡片的「账号登录」先经受限端点核验当前凭据：有效时提示「认证有效，无需重新登录」，缺失或被拒后才打开专用 Edge；核验未知或失败时如实提示，稍后可重试。认证恢复后原请求重试一次；登录失败或超时返回 `503 reauthentication_required`。隐藏服务的脱敏日志见 `chat2api/chat2api.log`。
- **UM 自动认证恢复轮次**（2026-10-23，A1—A8）：启动验证、业务 401、周期巡检和手动入口加入同一实例的同一恢复轮次，不再各开一次窗口；旧探测结果按凭据/会话代次丢弃，不覆盖新会话。首次失败立即重试，之后依次等待 30/60/300/600 秒（从实际失败结束起算），首次加 5 次重试共最多 6 次会话，最后一次失败暂停，普通请求和巡检不会自动重置预算。两个实例仍共享滚动 15 分钟最多 3 次会话启动的硬上限：互斥/限频拒绝不消耗重试，调度等待最早允许时刻，成功只重置本轮进度并保留窗口内记录。租约覆盖启动、导航、凭据探测、人工等待和清理的显式预算（`--login-timeout` 再加 135 秒），停止/失败释放，崩溃后有界恢复。仅明确的账号/密码拒绝或可见错误区域才判为凭据拒绝并暂停；无法识别的认证页面、MFA/验证码留给人工，窗口关闭按预算重开。`/admin/auth/status` 加性提供 `recovery` 摘要（轮次、已启动会话数、下次允许时刻、暂停原因），`/health` 只给粗粒度进度；`/health`、`/ready`、`/admin/auth/verify` 仍不触发登录。管理端等待超时只是等待预算，不表示后台轮次已经结束。8110 的专用实例同样使用该轮次，但旧分享实例可能清空共享窗口记录，严格跨实例硬上限需要两侧都采用新语义。
- **UM 认证等待与自动接续**（2026-10-27）：等待其他实例的活跃登录租约时，对方会话提前释放后，本实例最多 2 秒内自行接续，不睡到旧到期时间，也不清空滚动窗口内的启动记录。业务请求等待本实例恢复认证时，每个请求有累计 30 秒的认证等待预算，计入进入恢复、协调锁等待、加入已有轮次和等待所耗时间；同一次请求的内部 401 重发、兼容重试和隐藏重试共享剩余额度，并受请求整体剩余期限约束。普通上游处理、重试间隔和请求体处理不计入该预算。已能确定无法在预算内恢复时提前返回 `503 reauthentication_required` 并说明具体原因；只有存在确定的下一允许时刻时才附整数秒 `Retry-After`（不超过 900 秒），活跃租约的旧到期时间不作为重试时间。认证等待占用每实例 128 个认证等待名额，与 128 个流式读取名额分别计量；流式请求在认证等待期间释放读取名额，恢复后真正重发前重新取得，满额时不打开无界上游连接或读取器，超限快速返回带原因的 503，不排无界队列。共享租约按恢复轮次绑定：旧轮次收尾不得释放或覆盖新轮次租约。分享接口保持 `share_gateway_error / um_unavailable` 包装，只转发通过校验的整数秒 `Retry-After`，非法或超界值丢弃，不透传认证正文或凭据。
- 消费方保留本机已配置的 DSH 与 ZCode；缺席的 DSH 配置不会由 ZCode-only 同步重建。受管来源仅 UM 沿用既有 provider ID；退役 provider 的手工配置不在此次清理中删除。

## chat2api 契约

### 图片预算（2026-09-18）

UM 每次 prompt 最多 16 张图片（上游 400 正文 `At most 16 image(s) may be provided in one prompt`），长寿命代理会话累积历史截图会反复撞限。chat2api 在发送前按**显式 `type` 键**识别图片部件（`image_url`/`input_image`/`image`；碰巧带 url 字段的 dict 永不误删），每个下游请求只 normalize 一次（兼容重试不递进裁剪），copy-on-write 不改写调用方原 body：

- **默认行为**：受保护段（最后一条 user 消息到结尾，含其后的 agent/tool 图片）绝不静默删；历史图片 oldest-first 裁到预算内，被裁空的 image-only 历史消息留一条占位文本；当前轮图片超限时本地返回 400 `too_many_images_current_turn`，不发上游。
- **环境变量**：`CHAT2API_MAX_IMAGES`（默认 16，钳制 1–256）；`CHAT2API_AUTO_TRIM_HISTORICAL_IMAGES`（默认 1；=0 关闭一切自动改写，是总开关）；`CHAT2API_AUTO_TRIM_CURRENT_TURN_IMAGES`（默认 0；=1 时当前轮超限改为保留最新 16 张、本地删最旧并记 WARNING，历史预算随之清零全部裁掉——无人值守自愈用，默认不静默丢用户本轮提交的图）。
- **观测**：日志行 `image budget normalized`（涉及当前轮裁剪时升 WARNING）与各路径 `request summary` 的 `images_before/after/trimmed`；`GET /v1/version` 的 `retry_policy` 含 `max_images` 与两个开关当前值；启动 banner 打印 `image policy` 一行。
- env 仅在进程启动时读入：改动对生产生效需要重启 chat2api（重启属单独授权动作）。

### 重复止损与带内错误（2026-09-18）

- 模型重复退化（locklock 循环）由重复止损器掐流：输出可见前的循环可透明重试（`CHAT2API_REPETITION_AUTO_RETRY` 开关，独立于 transport 的 `CHAT2API_UPSTREAM_AUTO_RETRY`；两开关混合时 repetition 开关=0 即彻底关闭隐藏重试）；输出/工具调用已可见后一律终态掐流，不做二次补全。
- 终态观测：chat 路径搜 `stage=degenerate_repetition`（WARNING，含 phase/downstream_body_sent/content_seen/tool_action_seen 等结构化字段）；compat（Anthropic）路径前缀 `compat chat failure` 同字段。
- 带内 SSE 错误帧契约（OpenAI 流）：`{"error":{"message":...,"type":"upstream_stream_error","code":<status>,"detail":{...}}}`；`degenerate_repetition` 的 message 固定以 `[chat2api:degenerate_repetition]` 开头。消费方注意（ZCode 3.12.3 实测）：`@ai-sdk/openai-compatible` 只透传 `error.message`，`code`/`detail` 会被丢弃——**按 message 内标记检索**。
- **长单元行循环只观测不掐流（2026-09-20）**：思维链存在整行循环（如 `Hmm — OK.` / `Hmm — hmm.` 交替，单元 ~28 字符），周期超过 1-8 字符窗口，止损器结构性看不见。观测器 `_line_cycle_repetition` 按行周期（≤6 行）检测尾部循环：`CHAT2API_LINE_LOOP_OBSERVE=0` 关闭；命中打 WARNING `line repetition pattern`（含 request/site/cycle_lines/repeats/tail_chars/sample），每个流每个签名只打一次；`request summary` 增记 `line_loop_hits`。阈值：≥10 次重复且重复尾 ≥600 字符、单行 ≤120 字符。**不掐流、不重试**——攒生产证据后再定掐流阈值。

### 浏览器来源白名单（2026-10-14）

- **门**：POST 端点拒绝一切**携带** `Origin` 头的请求，除非该来源列入 `CHAT2API_ALLOWED_ORIGINS`（逗号分隔，进程启动时读入一次；`/health`、`/ready` 等只读 GET 不受影响）。来源命中白名单时响应带 `Access-Control-Allow-Origin`；未命中返回 403 `origin_denied`，且**不写访问日志**（只出现在响应正文）。非浏览器程序（curl、ZCode、SDK）不带 `Origin`，不受影响。
- **当前配置**：`start_chat2api.bat` 已列入 KISS Translator 扩展的来源 `chrome-extension://bdiifdefkgmcblbcghdlonllpjhhjgof`。扩展的翻译请求从扩展服务工作线程发出，浏览器会带上扩展自己的 `chrome-extension://` 来源；该 ID 由商店签名密钥派生，Chrome 与 Edge 同 ID，换用其它浏览器扩展时按同一机制追加。
- **验证（2026-10-14 实测）**：扩展「点击测试」按钮经网关返回 `测试成功`；`https://example.com` 页面翻译经网关逐段返回 200；陌生来源 `https://evil.example` 仍 403（负控）。env 仅在进程启动时读入，改动需重启 chat2api；测试断言见 `tests/gateway-runtime.Tests.ps1`。

### 私人 chat2api（8000）进程守护与退出记录（2026-09-30 上线）

维护者 2026-09-30 批准上线（起因：09-29 19:51–20:11 与 09-30 00:30 两次无声消失，历史原文保留于 Git；历史收尾记录见[Git 原文](../handoffs/archive/README.md#git-ba293ae40c25940694f4467319123c52b308cf72)）。

- **守护**：计划任务 `SelfUseAI AI Gateway Chat2API Watch`（当前用户、每分钟、隐藏运行，`lib/register-chat2api-watch.ps1 -Action Register|Unregister`）执行 `lib/watch-chat2api.ps1`。只在同时满足以下条件时经 `Restart-GatewayService` 拉起：期望状态为「运行」且写于**本次开机**；`/ready` 连续两次失败；按私人实例身份（排除 8110 等其它 chat2api）确认进程不在且 8000 无人监听；上一个实例不是在期望状态之后被优雅关闭的；本小时启动未满 3 次。拉起前取与控制台 / CLI 相同的 `service:chat2api` 资源锁并复核。上游 401、5xx、网络失败**不会**触发重启（重认证是 chat2api 自己的事）。
- **期望状态**：`%LOCALAPPDATA%\SelfUseAI\ai-gateway\watch\chat2api-desired.json`，由控制台（`invoke-console-runtime-json.ps1`）、CLI（`invoke-gateway-service.ps1`）与守护自身的启动 / 停止写入；**经控制台或 CLI 停止后守护不会拉起**；重启电脑后在人工启动一次之前也不会自动拉起。测试与 `invoke-model-sync.ps1` 的临时借用不写它。
- **守护日志**：同目录 `chat2api-watch.log`（正常时不写，状态变化 / 判死 / 拉起才写，1 MB 轮转一代）。
- **退出记录**：`chat2api/chat2api.log` 的启动行带 `role / port / pid / ppid / launched_by`（`console` / `cli` / `watch` / `unspecified`），绑定端口后写 `listening`；经控制令牌 `/admin/shutdown` 的优雅关闭写 `exit begin … reason=admin_shutdown` → `exit complete`（`serve_forever` 只可能因该路由正常返回）。外部强杀（`taskkill /F`、`Stop-Process -Force`、所在进程树被结束）不留 exit 行——缺 exit 行只表示「未知」。隐藏启动时启动器不再 `pause`，本次运行的控制台输出与 Python 退出码写入 `chat2api/chat2api.console.log`（上一次保留为 `.prev`）；连 `python exited` 行都没有，说明整棵进程树被外部结束。
- 8110 专用分享后端的父侧启停记录写 `%LOCALAPPDATA%\SelfUseAI\ai-gateway\share-backend\backend-lifecycle.log`（代码随 8001 下次重启载入）；8110 不在守护范围内，其启停权威仍是控制台动作。

### 分享开启确认与费用只读保证（2026-10-08）

- **受限 start 的就绪确认（C1）**：broker 侧 `share_broker_host.BrokerHostCoordinator._start` 只 Run 任务一次，随后在**注入时钟**上按 `start_ready_timeout`（默认 6s）有界轮询同一个三证检查（任务 Running + share_gateway 进程 + 8100 监听）；只有三证为真才算就绪，`None`（读取失败）绝不算成功。就绪预算严格小于协议交换时限（`share_broker_protocol.DEFAULT_DEADLINE_SECONDS` = 10s）与控制台 backend 层预算（`console_share_actions._BACKEND_DEFAULT_S` = 30s）——调用超时之后不会再有「晚到的成功」留下无主实例。轮询期间并发的 drain / emergency-stop 一旦确认关闭，本次 start 绝不报 `started`（stop 优先）。
- **「该层已运行」也要受管确认（C2）**：`console_share_actions._open_backend` 发现 8100 已在运行时，仍要经受限 start 做一次受管确认——只有成功的受管开启动作才会把受保护 lifecycle（`lifecycle.json` / `broker-status.json`）从先前 emergency-stop 的 `stopped` 刷成 `running`，监督判据（`share_backend_supervision` 的 `managed_stop_in_force`）才随之一致。端口在场只是读面观察、不是权威状态；未经本次有效开启动作，人工停止 / 紧急停止仍然禁止自动恢复，本次不归属、回滚绝不停止开启前已在运行的层。受管确认失败时如实失败（`share_layer_failed`），不冒充成功。
- **费用读面只读保证（C3）**：`GET /api/console/share-usage` 的 Key 名称投影只经 `share_store.read_key_labels`（SQLite `mode=ro` + `PRAGMA query_only` + `BEGIN`，只 SELECT `key_id/name/deleted_at`）读取，绝不装配 `ShareManager`、绝不读部署 loader、绝不开 broker 管道；名称读不到时如实为 `None`（`coverage.keyNames` 同时如实），费用与名称都不丢、不折成零。带撤销意图的已删除 Key 仍返回名称与费用，且不恢复、不停止分享。
- **验证命令**：控制台日志页（分类 / 真实 0 与未知 / 跨日日期与小时刻度 / 局部滚动 / 刷新间复制 / 筛选切换 / 重复事件 / 剪贴板拒绝 / 原文折叠 / 两主题与窄屏）由 `ai-gateway/tests/console-log-gate.Tests.ps1` 覆盖（已接入项目根 `verify.ps1` 的统一门禁：按最终源码重建候选 + 离线夹具页门禁，不写生产 `dist/`）。后端回归仍走 `verify.ps1` 的 Python regression。
- **验收边界**：私人 8000 守护改动是否被运行进程载入、ZCode GUI 会话采用及其真实业务闭环，仍须分别验收。8001 与 8100 分享链的部署、真实开启和公网模型回复以本轮运行验收记录为准，不能用离线通过代替；不同能力的结论不互相替代。

## UM 熟人分享试验

独立入口 `share_gateway.py` 固定本机监听 8100；云端公网入口是 Cloudflare Tunnel 只连接这个经过鉴权与接口限制的入口（`https://dacintapi.de5.net/v1` → `127.0.0.1:8100`；旧 Tailscale Funnel `.ts.net` 域名已退役，Caddy 443 仅保留为历史回退样例）。先用 `--probe --upstream-port 8110` 测试连通性，再以同一端口接入**专用分享后端**（上游端口没有默认值，缺省会拒绝启动——分享入口绝不静默转发进私人 8000）；不加入控制台一键全启，不自动启动原 UM。密钥权威来源是本机管理服务的 SQLite（`data/share-store.db`；projection 模式下 Prep 校验派生投影并跳过旧配置复制，旧 legacy `data/um-share.json` 仅显式 legacy 模式使用）；明文只在受限交付文件 `data/um-share-friend*.txt`。

**运维入口按链归属**：本机 Windows 分享链的部署/恢复脚本在 `ai-gateway/um-share-deploy/`（README 内有完整启停/验证/密钥命令；旧 `tmp/p0-umshare/` 已清理，需要时从 Git 历史查阅）；云端正式分享链以 `um-cloud-deploy/` 的 broker/console unit 为准，两者不共用运行目录。**安全依赖**：`um_share` 受限身份 + 专用映像 + WFP（仅放行专用分享后端 127.0.0.1:8110；生产 chat2api 8000 不被分享链使用）+ 防火墙程序出站阻断 + Job（单进程）+ SYSTEM Watchdog（`umshare-watchdog`，10s 有界延迟停止）。启动前置：限制强校验通过且 Watchdog=Running，否则拒绝开分享。**归因密钥（C2 已验证归因）**：8100 与 8110 必须指向**同一个**密钥文件（默认 `C:\ProgramData\UMShareRun\run\share-attribution.key`，两侧解析函数与部署脚本由 `tests/test_console_share_production.py` 的跨端用例锁住）。生成用系统密码学随机源（32 字节 → 64 hex 字符，**不用 `Get-Random`**），写后读回长度与内容哈希、再断言 `um_share` 与 `dacint` 都有 Allow 读 ACE。生成、ACL、启用核对与轮换流程见 [归因密钥](SHARING.md)：轮换**只重启 8100**，不重启 8110；只换文件不重启 8100 会得到全量 `bad_signature`（记为未归属且不报错）。密钥缺失或过短是明确的**文件未就绪**状态——分享照常服务、每条请求记为未归属，部署日志与 `production_backend_observe()` 的 `attribution` 会如实报出；`GET /api/console/share-management/status` 的 `attribution` 字段（加性，2026-09-26 owner 裁定）本机同源可读，网页渲染仍是另一步。

### Anthropic Messages 客户端用法与限制（2026-09-30）

公网入口只有上面这一个 Cloudflare Tunnel 域名（→ 8100 分享入口）；本机直连 `127.0.0.1:8100` 同契约。`POST /v1/messages` 是 Anthropic Messages 的原样入口（另有 `GET /v1/models` 与 `POST /v1/chat/completions`）：请求体按 Messages 契约校验后原样转发、不增不删字段（入口按解析结果重新序列化 JSON，空白与浮点表示可能变化，字节层面不保证；响应侧才逐字节回传），白名单为 `model`、`messages`、`max_tokens`、`system`、`stream`、`temperature`、`top_p`、`top_k`、`stop_sequences`、`metadata`、`tools`、`tool_choice`、`thinking`；未知字段一律 400 `unsupported_messages_request`，`max_tokens` 必填且必须为正整数（入口不替调用方补默认值），`stream` 只接受布尔。

鉴权两种形状收敛到同一份分享 Key（同一摘要、同一并发预算与账本入口）：`Authorization: Bearer <分享密钥>`（Claude Code 的 `ANTHROPIC_AUTH_TOKEN`、OpenAI 兼容客户端）与 `X-Api-Key: <分享密钥>`（Anthropic SDK `api_key` 的默认形状，仅 `POST /v1/messages` 接受）。两头同时在场仅当值相同才接受；同名重复头、空凭据、凭据头超过 256 字节（`Bearer` 形状把 `Bearer ` 前缀一起计入）一律 401 `invalid_api_key`。归因标记由分享入口在认证之后自建（服务端 HMAC），客户端伪造的 `X-Share-*` 头一律被丢弃。

边界：请求体上限 1 MiB（声明超限在读请求体之前即 413 `request_too_large`，图片同受此限）；每 Key 并发与全局并发按投影条目执行（新 Key 默认 16、单 Key 最高 48、全局 64），另每 Key 每分钟最多 20 个已接受请求；协议头只转发 `anthropic-version` 与 `anthropic-beta`（单行 ≤200 字符），其余客户端头（凭据、Cookie 等）一律不透传。流式与非流式响应都原样回传（Anthropic SSE 事件含 `thinking_delta`/`signature_delta` 不改写成 OpenAI 形状）；流式在读静默 180 秒时中断、非流式 600 秒读上限（与专用后端预算对齐，持续有数据则不限总时长）；上游非 200 折叠为 502/503 `um_unavailable`，错误响应统一为 `{"error": {"type": "share_gateway_error", "code": ...}}`（429 另带可读的 `reason`/`detail`）。

模型 ID 使用上游真实 id：分享入口不发明本地别名或思考档位，可用模型以 `GET /v1/models` 的实时目录为准（raw-fresh 契约，无虚拟别名）。生产读面出现过的示例：`GLM-5.3-Flash`、`DeepSeek-V4.1-Flash`、`gemma-4-31B-it`、`gpt-oss-120b`——可用性随时变化，不承诺静态清单。

费用参考口径：本机 `GET /api/console/share-usage`（或管理面 `GET /api/share/usage`）按 Key 汇总——已知参考费用（`knownCostUsd`，上游报告了用量的请求）、估算费用（`estimatedCostUsd`，只有本地估算侧）、用量未知（`unknownRequests`）与未定价（`unpricedRequests`/`unpricedModels`）分列，未归属请求单独在 `unattributed` 桶；费用按每条记录自己的时刻价目版本派生（不重算历史）。这些是**参考费用，不是供应商账单**。

仍不开放：Anthropic 的 `files`、`batches` 与 `POST /v1/messages/count_tokens`（一律 404 `route_not_shared`；专用后端没有 count_tokens 路由，分享入口也不自行计数、不伪造 token 计数）。运维启停、密钥管理与验收步骤见 [分享说明](chat2api/SHARING.md) 与 [um-share-deploy/README.md](um-share-deploy/README.md)。

**已知限制**：无开机自启；重启后需按 routine 恢复；流式请求在每次读静默超过 180 秒时中断（持续有数据则不限总时长，与专用后端预算对齐）；经本机 Clash TUN 自连公网域名会失败（外部直连不受影响）。见 [分享说明](chat2api/SHARING.md) 与 [um-share-deploy/README.md](um-share-deploy/README.md)。

### 控制台日志与用量展示（2026-10-07）

- 日志总览只统计各运行日志本次有界尾读窗口，分类为错误／异常、警告、通过、信息和未分类；通过须有明确成功结果，INFO 不自动算通过。可分类筛选并进入来源详情；失败或未读取的来源不算零事件，调用账本仍在费用页。 日志页采用左侧来源列表与右侧事件表，置顶全部日志；来源可搜索、拖拽或Alt+↑／↓排序，独立保存在当前浏览器，取消不保存。切换来源保留分类；原始内容按事件展开／复制，复制拒绝显示失败。操作记录保持独立。
- Token、速率与费用曲线在零值或缺统计时保留图框：底部实线短标代表已观察到 0，虚线短标代表缺失，缺失不连成零值曲线。无构成数据的环图保留空环，不计算占比。 时间轴逐日／逐小时保留全部刻度，小时显示跨日日期；窄图框横向滚动，不省略日期或让整页溢出。
- 分享每 Key 明细按名称／完整 Token／已知与估算费用／数据质量分列；模型与日期分组展开，开合状态按稳定 Key 身份保存。未归属记录独立列出，无价与未知不折算为零费用。

### 分享费用名称读取的只读边界

费用接口的 Key 名称与删除状态通过 `share_store.read_key_labels` 读取，只取稳定ID、名称、删除时间。该连接使用SQLite只读模式，不创建或迁移数据库、不创建 `ShareManager`，因此不会因管理库内的待发布撤销意图触发broker恢复或分享停止。库缺失、损坏或读不到时，费用记录保留，名称/删除状态标未知。测试默认数据库与部署配置均需临时重定向，真实命名管道内核必须在调用前拒绝并在结束时核对尝试次数，不能仅靠PASS或后置运行状态证明隔离。

### 分享关闭的时间预算

优雅关闭先用最多60秒通知实例并等待在途请求，再留20秒停止阶段；管理调用最多90秒。紧急关闭直接使用20秒停止阶段，管理调用最多30秒。实例通知最多使用停止阶段的一半，给宿主停止及关闭核验保留时间；通知失败不跳过宿主停止。

实例管道连接、身份核验、单次重试与收发沿用同一截止点。到期后不再发送请求或重新起算超时；阻塞原生调用沿用有界放弃与工作线程额度，可能多出既有的短收尾观察窗口。任务、进程或端口核验失败时结果为未确认，不能把已发停止命令当作已关闭。上述预算修复在载入运行实例后生效。

## ZCode 运行中同步的兼容性

运行中同步按文件锁、配置格式及变更监听的已验证协议判断，版本号仅展示，整包或整份bundle摘要不限制升级。无关代码变化和依赖chunk改名可兼容；关键协议实现变化但未核验时仍显示待应用，不自动重启客户端。安装来源无法确认、配置损坏或并发冲突也不强行写入。保存与发布仍遵守受管范围和文件锁，文件已写、客户端已加载、当前会话已采用分别报告。证据及限制见[协议取证](docs/zcode-hot-sync-3.14.5-evidence.md)。

## 编码注意

`start_chat2api.bat`、`Open-GatewayConsole.bat`、`Open-CloudConsole.bat` 刻意保持纯 ASCII，避免 cmd/PowerShell 编码差异问题；`start_usage_service.bat` 含中文注释，由 `start_usage_service.vbs` 隐藏调用。中文交互界面与快捷方式由 UTF-8 with BOM 的 `Open-GatewayConsole.ps1`／`Open-CloudConsole.ps1` 负责，不再受 GBK 编码约束。

## DSH 桌面 App（web 已退役，2026-09-13）

- **web 退役终态**：全局 npm 包 `@deepseek-ai/dsh` 已卸载（removed 517 packages），3080 不再监听；旧桌面快捷方式 `DeepSeek DSH.lnk`、`Start-DeepSeekDSH(.ps1/.vbs)` 启动链与 web 日志已删除。`~/.dsh/profiles/web` 仅作 bundles 历史记录保留（package.json、lockfile 等，不装载）；当时已装的第三方插件归档在仓库 `deepseek-harness/web-plugins/`（归档副本，非运行安装）。
- **现用入口**：桌面快捷方式 `DeepSeek Harness.lnk` → 构建产物 `deepseek-harness/apps/desktop/.desktop-build/targets/win-x64/artifacts/win-unpacked/DeepSeek Harness.exe`（未签名 exe，SmartScreen 首次拦截需「更多信息 → 仍要运行」）。桌面 App 与已退役 web 版共享 `~/.dsh`（会话/设置/凭据/storages），使用独立 `profiles/desktop`（未装载第三方插件），运行时零监听端口；模型路由仍走本机网关（消费关系见上方「消费方」）。
- **控制台边界**：控制台不再管理 DSH——菜单无 DSH 项（2026-09-13 重编号后为 [1] M365 / [2] UM / [3] Qoder 国内 / [4] 全启 / [5] 全停 / [6] 费用 / [7] Qoder 国际 / [U] 同步 / [0] 退出），上述菜单编号为退役当时的历史记录；2026-10-03 起 M365 项也已归档，2026-11-01 起 Qoder 国内/国际与 CLIProxy 也随源码退役，运行时全启/全停只纳入 chat2api 与后台宿主，模型同步不联动重启 DSH。
