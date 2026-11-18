# AI 网关控制台 (AIGatewayConsole)

AI 网关控制台及其管辖的本机服务的领域词汇。

## Language


**服务 (Service)**:
控制台可管理或观察的本机实例：当前包括 UM chat2api (8000) 和用量/管理宿主 (8001)。
_Avoid_: 网关（网关只是其中一类服务）、端口

**服务状态 (Service state)**:
受管服务的本地进程是否可访问；它与上游凭据是否仍然有效相互独立。
_Avoid_: 登录状态、可用状态

**本机控制台私网入口 (Local console tailnet ingress)**:
8001 保持 loopback 监听，经显式配置的 Tailscale HTTPS HTTP Serve 代理向已选 PC／手机开放。后端同时核对精确来源、登录身份和设备地址；代理元数据出现后必须通过私网校验，不能退回本机路径。私有策略缺失或损坏时拒绝远程，本机入口保留；不使用 Funnel 或原始 TCP 转发。服务归属按实际 loopback 监听识别，同端口的 Tailscale 私网监听不构成冲突，也不进入停止范围。
_Avoid_: 公网管理入口、同账号全部设备自动授权

**进程意外退出 (Unexpected service exit)**:
受管服务进程消失的状态；监督自动拉起以它为唯一触发条件，上游故障或网络抖动不在此列。
_Avoid_: 服务崩溃（混淆误杀）、恢复（指动作不是状态）

**上游不可达 (Upstream unreachable)**:
本机到远程模型来源的连接失败；不等于上游认证失效，也与本机服务状态相互独立，对本机重启无响应。
_Avoid_: 上游宕机（含本机归因）、登录失效、服务状态

**上游认证状态 (Upstream authentication state)**:
chat2api 访问 UM 时所持凭据的状态，规范值为「认证有效」「需要登录」「登录中」「登录失败」。
_Avoid_: 服务状态、token 状态

**重新认证会话 (Reauthentication session)**:
为恢复 chat2api 上游认证而进行的一次登录交互（可见或 headless）；同一时刻所有触发方共享唯一会话。
_Avoid_: 登录窗口、刷新 token、可见窗口

**UM 登录态复用 (UM sign-in session reuse)**:
首次登录后保留上游认可的会话信息，供后续请求及服务重启继续使用；保存成功不代表会话不会被上游撤销或要求本人再次验证。
_Avoid_: 永久登录、另存账号密码、绕过 Duo

**登录呈现方式 (Login presentation mode)**:
一次登录交互是否创建可见浏览器窗口的呈现选择；默认 headless-first，正常路径不创建可见窗口，不要求 `DISPLAY` 或 `WAYLAND_DISPLAY`，并复用同一 profile。
_Avoid_: 默认可见窗口、每次新建专用 profile

**按需登录 (On-demand login)**:
控制台先核验当前 UM 凭据，仅在凭据缺失或被拒时才触发登录轮次；当前凭据有效时跳过登录，不打开窗口。它与强制重新登录、自动重登录分别计量。
_Avoid_: 每次点击都登录、把有效凭据当成掉线

**强制重新登录 (Forced re-login)**:
维护者在本地控制台显式请求的一次重新认证；跳过“当前凭据有效”的按需核验，直接触发登录轮次，并可选是否显示可见登录窗口。自动重登录仍保持 headless。
_Avoid_: 自动重登录、按需登录、重启服务

**可见登录窗口 (Visible login window)**:
一次显式强制重新登录选择在本机桌面显示浏览器窗口，供本人完成 UM SSO；自动重登录与按需登录默认 headless。没有可用显示时明确返回不可用，不静默降级成 headless。
_Avoid_: 自动弹窗、云端专用实例窗口、把 headless 伪装成可见

**Duo 推送等待 (Duo push wait)**:
自动登录在 UMPASS 之后等待本人批准 Duo 推送的阶段；它不是失败，也不是终端人工接管，自动轮次会在此期间打开可见窗口。控制台显示“等待本人确认 Duo 推送”。
_Avoid_: 自动重登录失败、登录超时、人工接管已结束

**人工接管 (Manual takeover)**:
自动流程遇到只能由本人完成的步骤时立即结束当前轮并释放浏览器，默认返回 `manual_takeover_required:<reason>`；操作员显式选择才按需拉起可见 display 并开新轮，不复用旧轮。`umpass_mfa_required`、`umpass_prefill_missing`、`unrecognized_page` 属于人工接管；`duo_prompt_waiting` 继续等待，不误判为人工接管。
_Avoid_: 自动重试人工步骤、把 Duo 推送等待当人工接管

**人工接管等待 (Manual takeover wait)**:
自动流程遇到只能本人完成的步骤时，等待操作员在可见窗口完成的阶段；控制台返回 `login_manual_takeover_required` 并提供「打开可见窗口」，不把等待当成失败。
_Avoid_: 登录失败、自动重试、后台线程已结束

**candidate 模式 (Candidate mode)**:
Rust 二进制只绑定临时 loopback 端口、供 parity 测试使用的运行模式；它不能绑定生产端口，也不作为生产服务被 systemd 拉起。
_Avoid_: 生产模式、临时端口即生产、candidate 即回滚点

**Rust 生产入口 (Rust production entry)**:
Rust 二进制绑定 8000/8002/8003 loopback 的生产运行模式；它与 candidate 模式共用同一套代码，但允许生产端口。云端由 systemd 与部署脚本安装和切换，本机 8000 由现有 Windows 启动器拉起、回滚只改启动命令。
_Avoid_: candidate 模式、临时端口、端口映射

**切流 (Cutover)**:
停掉 Python 服务、启动 Rust 服务、验证并保留回滚点的动作；顺序是 8002 → 本机 8000 → 本机 8001，每一步独立验收，失败立即回滚。
_Avoid_: 重启服务、灰度发布、一次性全量切换

**回滚 release (Rollback release)**:
切流失败时恢复的 Python 发布包；它必须能启动原 Python 服务并保留运行数据，不能是期望旧入口脚本的上一 release。
_Avoid_: 上一 release、备份目录、Rust 二进制回退

**本机回滚 (Local rollback)**:
本机切流失败时恢复 Python 实现的动作；本机没有发布包，回滚点是未删除的 `chat2api.py`／`usagesrv.py` 与原启动命令，不是目录级 release。ADR 0010 计划在满足前置条件后退役这份回滚实现，届时恢复改用回滚包或云端旧 release。
_Avoid_: 回滚 release（云端发布包）、重新安装

**Python 回滚实现 (Python rollback implementation)**:
只作为回滚保留的 Python 入口集合：`usagesrv.py`、`chat2api/chat2api.py`、`start_chat2api.bat`、`start_usage_service.bat` 与 `um-share-console-python.service`；它不是常驻服务，也不是 sidecar。ADR 0010 定义它的退役条件。
_Avoid_: 所有 Python 文件、生产实现

**回滚包 (Rollback bundle)**:
退役 Python 回滚实现前打出的离线包（`tmp/python-rollback-<date>.tar.gz`，gitignored），包含回滚入口及其闭包；它替代“未删除的 Python 文件”成为本机恢复路径。
_Avoid_: 回滚 release（云端发布包）、源码备份

**一次性 sidecar (One-shot sidecar)**:
由 Rust 本机控制台按动作启动、执行一次即退出的 Python 子进程；当前承载 8001 的 12 个本机动作与 `model_sync` 调用，不是常驻服务。退役 Python 回滚前必须先把这些动作迁到 Rust。
_Avoid_: 常驻 Python、后台服务

**常驻服务 (Resident service)**:
由 systemd、Windows 启动器或登录项拉起、持续监听端口的进程；Rust 迁移范围只包含常驻服务，编排脚本、display 辅助与按需调用的库不在范围。
_Avoid_: 自研程序、全部 Python 文件、后台脚本

**登录浏览器单实例 (Single login-browser instance per profile)**:
同一 profile 目录在同一时刻最多只有一个登录浏览器；按 profile 目录持有 OS 级文件锁（POSIX `flock` / Windows `msvcrt.locking`），锁文件放在 profile 目录并附带 pid 与进程启动时间用于诊断，进程退出后锁自动释放。打开前只清理同 profile 的旧 Chromium（先 SIGTERM，等待，再 SIGKILL），不碰其他浏览器。
_Avoid_: 全局单浏览器、同 profile 多开

**人工接管 (Manual takeover)**:
自动流程遇到只能由本人完成的步骤时立即结束当前轮并释放浏览器，默认返回 `manual_takeover_required:<reason>`；操作员显式选择才按需拉起可见 display 并开新轮，不复用旧轮。`umpass_mfa_required`、`umpass_prefill_missing`、`unrecognized_page` 属于人工接管；`duo_prompt_waiting` 继续等待，不误判为人工接管。
_Avoid_: 自动重试人工步骤、把 Duo 推送等待当人工接管

**UM 自动认证恢复轮次 (UM automatic authentication recovery cycle)**:
一个实例围绕同一次 UM 上游认证恢复需求组织的有限一组重新认证会话；同一实例的并发触发归入同一轮次，两个实例分别确认各自认证结果。
_Avoid_: 单次业务请求、无限重登录、服务重启

**UM 跨实例登录租约 (UM cross-instance sign-in lease)**:
同一 UM 账号的多个本机实例之间，一次重新认证会话持有的临时独占权；会话结束可以提前释放，异常退出后的占用有到期边界。
_Avoid_: 永久登录锁、登录次数额度

**UM 全局登录启动限频 (UM global sign-in start limit)**:
同一 UM 账号的多个本机实例共享的滚动窗口会话启动数量限制；登录成功或释放登录租约不删除仍在窗口内的启动记录。
_Avoid_: 单实例重试次数、仅失败登录计次

**UM 请求认证等待预算 (UM request authentication wait budget)**:
单个业务请求等待本实例恢复认证所能消耗的累计时长；与后台自动认证恢复轮次、一次交互登录的等待预算分别计量。
_Avoid_: 登录窗口超时、整个恢复轮次的时长

**UM 首段业务输出 (First substantive UM output)**:
一次模型调用首次实际交付客户端的文本、思考或工具调用内容，是判断中断后能否重发原请求的边界；HTTP 响应头、心跳与不含业务内容的协议控制事件本身不构成业务输出，已提交的不可逆协议终态仍须保留。
_Avoid_: HTTP 首字节、首个心跳、登录窗口打开

**UM 按需账号登录 (UM on-demand account login)**:
维护者请求检查并恢复 UM 上游认证的动作。当前凭据有效时无需重新认证；凭据缺失或被拒时进入重新认证会话，无法确认时保留未知结论。
_Avoid_: 强制重新登录、重启服务

**模型发布 (Model publication)**:
把模型来源当前目录中的新模型加入受管消费方配置，使其可被选择；它不向模型来源上传任何内容。
_Avoid_: 上传模型、安装模型

**模型来源 (Model source)**:
提供权威模型目录的独立服务；当前同步来源仅 UM，同一模型 ID 在不同来源中是不同模型。
_Avoid_: provider、消费方

**UM**:
由 chat2api 暴露给本机消费方的澳门大学 Open WebUI 模型来源；面向用户统一显示为「UM」。
_Avoid_: OpenWebUI、openwebui、chat2api 模型





**能力探测 (Capability probe)**:
通过真实、有界且单独确认的推理请求，验证模型实际呈现的思考档位或可处理的上下文下界；目录字段或参数未报错本身不构成能力证据，同步从不附带探测。
_Avoid_: 可用性探测、档位猜测、应用并探测

**实测上下文下界 (Measured context lower bound)**:
能力探测在真实请求中验证通过的上下文 Token 数；只证明模型至少能处理这么多，既不是精确最大容量，也不是上游声明值。
_Avoid_: 实测上下文、最大上下文（指实测值时）









**Token 消耗量 (Token consumption)**:
有完整输入与输出记录的请求在指定时段内的输入加输出 Token；缺失任一分量的请求单独标为部分可见，不混入完整总量。
_Avoid_: 请求数、仅输出 Token

**模型整次平均输出速率 (Request-average output rate)**:
一次请求的输出 Token 除以从发起到结束的总耗时；包含首字等待，不等于模型实际生成阶段的速度。
_Avoid_: 纯生成速度、瞬时 Token/s



**记账 (Accounting record)**:
各参与计量的服务按请求追加到本地 JSONL 的一条记录（模型、token、时间、来源）；与诊断用的日志是两回事。
_Avoid_: 日志、usage log、调用日志

**运行事件 (Runtime event)**:
服务运行日志中的一条诊断事件（时间、模型、阶段、结果、错误、耗时），日志页只呈现这一种；按请求计量的记账归用量页。
流式结束日志的上游请求 ID 必须是有界、无空白的单个字段；HTTP 解析可能把折叠头部的换行转换为空格，因此含控制字符或空白的 ID 显式标为 unavailable，不把它清洗成看似可查询的 ID。服务器名称等其他响应头中的正常空格保留。
_Avoid_: 调用日志、账本行

**DSH 快照 (DSH ledger snapshot)**:
DSH 2026-09-02 账本日的冻结快照（`data/dsh-ledger-20260902.json`，从账本存档生成），是 DSH 块展示与顶部累计并入的唯一数据源；现行插件账本已丢失该日，快照按用户 2026-09-04 决定保留该日统计。DSH 之后的流量经 chat2api 网关由 `usage-chat2api.jsonl` 逐条记账。
_Avoid_: DSH 账本实时读取（已解耦，ledger.json 不再被 8001 读取）

**来源 (Origin)**:
请求由哪个服务经手并记账；当前活动来源仅 chat2api，同一请求只会被一个服务记账，无需标记去重；DSH 流量走 chat2api（provider "openwebui"）由网关侧 JSONL 记账。
_Avoid_: 客户端（指绕过统一入口直连某个服务的程序，只是来源的一种）

**API 入口 (API endpoint)**:
供获授权朋友调用指定模型接口的访问边界，负责识别调用方并限制可用操作；与管理入口及模型来源的认证相互独立。
_Avoid_: 分享入口（旧称）、UM 账号共享、控制台公网化

**API 服务本机独立性 (API service PC independence)**:
API 调用及维持服务所必需的认证恢复，不依赖维护者个人电脑开机、联网或承担中转的能力。
_Avoid_: 分享本机独立性（旧称）、固定公网地址、仅迁移公网入口

**API 服务无人值守能力 (Unattended API service)**:
API 服务在日常运行及上游凭据到期后的恢复中，无需维护者人工登录或操作即可继续提供获授权模型接口的能力。
_Avoid_: 分享无人值守能力（旧称）、进程常驻、自动重启、定期人工续期

**API 连接自检 (API connection self-check)**:
在 Key 操作列手动点“测试”，管理后台查询 API 域名的公网 DNS，经公网 HTTPS 读取模型目录，再发送一次最多 32 个输出 Token 的短请求；会产生少量模型用量。成功表示这一次公网往返和模型回复通过，不证明另一设备的网络体验。测试结果仅保留在当前页面并绑定 Key 版本与 API 地址，轮换、停用或删除后失效，超过 5 分钟标为陈旧。页面刷新和只读分层诊断不自动发送推理请求。
_Avoid_: 分享连接自检（旧称）、公网全面可用、永久可用

**分享可用性证据 (Sharing availability evidence)**:
本机链路、公网 DNS 和真实模型调用分别显示。本机就绪要求入口进程和发布同步均有肯定证据；后端停止或缺失时不能以公网读数覆盖。公网 DNS/TLS 来自每轮 Cloudflare/Google 双源检查（逐源状态、rcode/分类、TTL/负缓存与检查时间），模型回复来自手动密钥测试，均标出检查时间；缺少字段保持未确认，不推断为关闭或已连通。

**分享公网链路检查 (Sharing public connection check)**:
确认 API 入口的公网域名解析与 TLS 连通性的动作，不包含模型推理；其结果与本机服务状态及最近一次模型返回结果分别表达。
_Avoid_: 模型可用性测试、公网全面可用

**分享公网解析分歧 (Sharing public DNS disagreement)**:
同一分享域名在同轮检查中，不同公网解析服务对是否存在有效公网地址给出冲突结论；单纯返回的公网 IP 集合不同不构成这一结论。它与入口 TLS 是否可达分别表达。
_Avoid_: 全部公网入口失效、模型不可用、地址集合不同即故障

**分享公网解析读数 (Sharing public DNS reading)**:
每轮分别记录 Cloudflare DoH 与 NextDNS DoH（Google 源仍兼容读取旧状态）的解析状态、DNS 状态码/分类、有效公网 IPv4、可得 TTL 或负缓存 TTL、检查时间与实际下次复查时间；两源并行查询并共用整轮有界预算。NXDOMAIN、NOERROR 无 A、SERVFAIL、DoH HTTP/格式错误和超时分别呈现；缺少 SOA/TTL 显示未知，不硬编码 300 秒。TTL 只表示本次响应有效期，不保证到点恢复，也不延长默认 60 秒巡检。
_Avoid_: 公网断线、TTL 到期即恢复、所有解析错误同一结论

**分享公网 DNS 提示 (Sharing public DNS notice)**:
DNS 异常及其恢复只在分享页和现有本地状态/事件记录中呈现，不发系统通知，不重发映射、不重启 Tailscale、不消耗恢复次数；真实 TLS/入口故障及恢复通知保持原样，混合故障不能被 DNS 静默规则吞掉。
_Avoid_: DNS 恢复通知、DNS 自动修复、DNS 异常触发恢复轮次





**网关调用凭据 (Gateway invocation credential)**:
客户端访问选定本地模型网关API所用的凭据；是否需要凭据由该网关的调用鉴权状态决定，不代表上游账号登录状态，也不等同于控制台管理凭据或云端的 API Key。本地网关页按客户端配置字段名把它显示为「API Key」。
_Avoid_: 上游登录Token、管理密钥、所有网关通用Key

**API Key**:
授予一位调用方使用 API 入口的凭据，可以单独轮换或撤销；不代表模型来源账号的登录凭据，也不是本地的网关调用凭据。单用户机器上一把 Key 通常绑定一个调用方客户端，使账本能按客户端归属定位流量。
_Avoid_: 分享密钥（旧称）、UM token、上游 API key、多客户端共用一把 Key

**密钥身份 (Key identity)**:
一份 API 授权的稳定归属，独立于可更换的密钥秘密与可修改的显示名称；授权删除后，其历史用量仍归于该身份并标记已删除。
_Avoid_: 明文 API Key、朋友姓名即唯一身份

**API 用量 (API usage)**:
经 API 入口接受的调用所产生、可归属到密钥身份的用量；是 UM 总用量的一部分，不是额外重复累加的一份消费。
_Avoid_: 分享用量（旧称）、双重记账、独立上游扣款


**分享管理页签 (Share management tabs)**:
分享页顶部的 keys／diagnostics／usage 三页签；概况与地址在页签外常驻并随页面正常滚动，异常原因与处理入口直接在顶部显示。页签只组织呈现，不改变读口、权限、确认或动作语义。
_Avoid_: 把页签当权限门、把隐藏内容当未读取、新增吸顶层

**正常关闭 (Graceful shutdown)**:
API 入口停止接受新请求，并允许已接受的请求在期限内结束，再完成关闭。
_Avoid_: 立即杀进程

**紧急停止 (Emergency stop)**:
立即撤下公网入口并中断在途 API 连接的操作；不等同于证明 UM 上游已经停止推理或不再计量。
_Avoid_: 上游消费立即归零


**API 并发 (API concurrency)**:
API 入口已接受且尚未释放资源的请求数量；流式请求持续期间占用名额，受每把 API Key 及 API 入口整体两个上限约束，与请求频率不同。
_Avoid_: 分享并发（旧称）、连接数、每分钟请求数、网关流式读取并发

**网关流式读取并发 (Gateway streaming reader concurrency)**:
网关在转发一个流式或聚合响应期间占用的上游读取名额；与分享准入的每 Key/全局并发、认证等待并发和 HTTP 监听 backlog 分别计量，不决定客户端 Key 的并发额度。
_Avoid_: API 并发、分享并发、认证等待、监听队列

**API 排队等待 (API dispatch wait)**:
API 入口已接受但尚未提交给 UM 的请求，因发送资源或速率约束而处于有界等待的阶段；等待期间占用相应 API 并发名额，并计入原请求总预算。
_Avoid_: 分享提交等待（旧称）、入口外额外排队、认证恢复等待、模型生成等待

**UM 自用实例 (Private UM instance)**:
供维护者本机客户端调用 UM 的自用服务实例，具有独立的运行状态、会话资料及用量归属；与 UM 后端共用实现，但不占用 API 并发名额。
_Avoid_: 全部本地网关、UM 后端、API 入口

**UM 后端 (UM backend)**:
专供 API 入口的 UM 模型转发实例，与 UM 自用实例不共用进程内并发名额；不意味着上游账号额度、上游限流或机器资源独立。
_Avoid_: 独立分享后端（旧称）、独立上游账号、无限 API 容量、UM 自用实例

**分层维护 (Layered maintenance)**:
针对 API 入口或 UM 后端分别进行的维护，保留整条链的 API 服务运行意图；与整链开启、正常关闭和紧急停止分别表达。恢复动作由维护者选择执行，以动作后重新核验的证据判断结果。
_Avoid_: 整链重启、重新登录全部网关

**API 服务运行意图 (API service desired state)**:
维护者希望 API 服务链保持运行或停止的目标状态，与各层当下是否正常运行相互独立；单层维护不代表维护者关闭 API 服务。
_Avoid_: 分享运行意图（旧称）、进程运行状态、入口可达状态

**异常退出恢复 (Unexpected-exit recovery)**:
API 服务运行意图仍为运行时，恢复已确认意外退出的 UM 后端的操作；不把上游错误或状态未知当作进程退出。
_Avoid_: 上游请求重试、整链恢复

**单层重启 (Single-layer restart)**:
只重启 API 入口或 UM 后端中的一侧，并经临时维护租约暂停另一侧接收、有界排空后再恢复；不重启另一侧实例，也不以整链关闭再开启代替。
_Avoid_: 整链重启、旧的本机停止→启动动作



**管理会话 (Management session)**:
以一次性引导码兑换的本机 HttpOnly 会话，供控制台网页以外的本机管理通道使用；控制台网页不使用它，也不读取、存储或代管任何凭据。
_Avoid_: 控制台登录、控制令牌 (X-Admin-Control)、上游登录凭据

**控制台访问边界 (Console access boundary)**:
控制台网页读写管理接口时唯一的信任条件：页面必须在运行该控制台的机器上以本机回环地址同源打开，满足即视为维护者本人操作；控制台本身不设登录。
_Avoid_: 管理会话、私网可达即可信

**本地控制台 (Local console)**:
维护者个人电脑上管理 UM 自用实例及其他本机自用服务的控制台入口；API Key 与 API 服务归云端控制台管理。入口为根目录的 `一键部署本机网关.bat`（部署并启动 8000/8001 宿主），生产页是 8001 托管的 `web/dist`（local 角色）。
_Avoid_: 主控制台、统一控制台

**云端控制台 (Cloud console)**:
云机上只管理云端 API 服务的控制台入口；与本地控制台共用前端代码，数据与操作目标各自归属，不经对方转发。端口是 8002（2026-11-17 已切 Rust；Python 回滚单元 `um-share-console-python.service` 监听 8096），入口为 `cloud/Open-CloudConsole.bat`（经专用 SSH 配置建 loopback 隧道），生产页是发布包内的 `web/dist`（cloud 角色，构建自 `web/dist-cloud`）。
_Avoid_: 远程管理本地、跨机统一控制台

**云端网关 (Cloud gateway)**:
云机上唯一的分享 API 合并进程（Rust `um-gateway`；Python `chat2api.py` 是回滚实现），负责分享 Key 鉴权、每 Key 并发、撤销在途请求、用量账本与 SSE 转发；端口是 8003（Rust 已切流；Python 回滚 release 仍用 8100），由 `um-share-gateway.service` 拉起。
_Avoid_: 合并网关（旧称）、云端反代（它不是独立反代层）、broker（broker 是拉起者，不是网关本身）

**网关/控制台配对段 (Gateway/console pair segment)**:
`8000–8009`：`8000/8001` 是本机网关/控制台对，`8002/8003` 是云端控制台/网关对，`8004–8009` 预留。
_Avoid_: 会话适配段（旧段名）

**控制台角色 (Console role)**:
一份控制台前端构建所呈现的身份（本地或云端），在构建时选定；只有与后端的云端部署角色一致时才允许写操作，访问地址不作为判断依据。
_Avoid_: 按访问地址推断角色、前端角色即权限

**UM 非流式内部聚合 (UM nonstream aggregation)**:
客户端请求非流式结果时，本机内部接收上游分段输出，核验真实完整结束后聚合为一个 JSON 返回；不改变客户端的非流式接口，也不代表可恢复已中断的生成。
_Avoid_: 模拟流式输出、断点续传、已修复长请求

**缓存用量报告状态 (Cache usage reporting status)**:
一次请求的缓存Token数是否得到上游有效报告的状态；有效报告包含明确的零，缺失或无效报告属于未知，与该请求整体用量是否已知分别判定。
_Avoid_: 缓存命中率、缓存能力开关、未报告即零命中

**云端 API 服务 (Cloud API service)**:
部署在云主机上、独立持有运行状态、UM 会话与账本的反代服务实例；API 调用及必要的会话恢复不经过维护者个人电脑。
_Avoid_: 云端分享实例（旧称）、仅部署云端入口、私人UM自用实例、电脑远程转发

**云端分享采购候选 (Cloud sharing procurement candidate)**:
为完整云端分享用途比较、尚待采购判断的主机配置及配套资源组合；配置报价、已购机与生产验收是不同状态。
_Avoid_: 免费生产机、已获准采购、已上线实例

**云端分享采购准备 (Cloud sharing procurement preparation)**:
为判断采购候选能否满足既定分享目标而进行的调查、验证和决策阶段；准备完成不等于获得采购或上线授权。
_Avoid_: 配置询价、产品实现完成、生产验收

**独立注册域名 (Individually registered domain)**:
以维护者本人或其主体登记、可续费和转移的域名；与第三方主域名下获授的子域名使用权不同。独立注册本身不表示已完成备案或接入验收。
_Avoid_: 免费DNS托管、获授子域名、已备案入口

**免费子域名 (Free delegated subdomain)**:
免费使用第三方登记的主域名下的名称，可在授予范围内管理解析；使用权、主域名注册权与持续服务承诺分别判断。
_Avoid_: 本人独立注册域名、永久稳定承诺、免费即免备案

**API 预算保护 (API budget protection, 已不在默认装配)**:
为维持既定年度支出约束而暂停接收、并在必要时中断在途 API 调用的运行控制；与费用提醒、普通维护及备份退化分别表达，恢复须满足原预算边界。D18-11 起云端默认装配不再安装预算库与 `um-share-backup.timer`，该术语只描述历史配置与代码内语义。
_Avoid_: 分享预算保护（旧称）、预算告警即自动停费、日常更新强断、允许自动加预算

**云端部署角色 (Cloud deployment role)**:
由 `UM_SHARE_ROLE`/`UM_SHARE_DEPLOYMENT` 显式选择的 `local`（Windows 本机）或 `cloud`（Linux 云机）装配身份；未设置即 `local`，冲突或非法值在导入时拒绝。`cloud` 只装配 UM 分享动作与 `share-gateway` 服务控制；管理端口 `local` 默认 8001、`cloud` 默认 8096（`USAGESRV_PORT` 严格校验；一个发布周期内旧 18096 仍是显式兼容值）。云端 `schema_version=2` 的 `unix_socket` 管理传输与 Windows `schema_version=1` 的 Named Pipe 配置分别有效，不得把 Unix 路径塞进 `pipe_name`。
_Avoid_: 用前端隐藏卡片代替后端隔离、云端照搬整个私人 data 目录

**Linux 平台适配 (Linux platform adaptation)**:
分享链在 POSIX 上的等价实现：重认证锁用 `fcntl.flock`（Windows 保留 `msvcrt`），Key 秘密保护用 AES-256-GCM 文件密钥（`CHAT2API_SHARE_SECRET_KEY_FILE`，0600/当前 uid，Windows 保留 DPAPI），broker 与实例通道用 AF_UNIX＋`SO_PEERCRED`（父目录 0700、socket 0600），受限服务管理用 pid 文件＋`/proc` 身份与信号（Windows 保留计划任务/PowerShell）。秘密文件读取统一走 `read_private_secret_path`；POSIX 上保护失败一律 fail-closed，不静默降级成 hash-only。
_Avoid_: 把 Unix 路径当 Named Pipe、只改前端、用 root 掩盖权限问题

**云端运维隔离件 (Cloud operations isolation)**:
`um-cloud-deploy/` 的预算保护与自动备份已从默认装配移除（D18-11）：`install.sh` 不再传 `--budget-db/--budget-config`、不再安装 `um-share-backup.timer`，`console.env` 不再写 `UM_CLOUD_BUDGET_DB` 等预算读面变量，`update.sh`/`rollback.sh` 不再执行切换前备份检查点。`backup_restore.py` 只保留显式备份/恢复（默认显式备份参数、`--restore-backup`、`--deployment-restore`），由运维单独授权；现有备份文件与 `backup.key` 不删除。真实云机上的 `um-share-backup.timer` 停用是逐对象的运维动作，本目录不代为实现；未获授权前不得声称已停用或已闭环。
_Avoid_: 隔离演练通过即生产可用、把本地目录当异机备份

**云端发布资源 (Cloud release assets)**:
`web/dist` 与 `web/dist-cloud` 都被 `web/.gitignore` 忽略且未跟踪：`web/dist` 是本机 8001 的 local 生产页，`web/dist-cloud` 是 cloud 生产页；云端发布包必须单独构建并携带整棵云端前端资源（打包为包内 `web/dist`，否则 `usagesrv` 的 `INDEX_FILE` 直接 500）。`ai-gateway` 无 `requirements*.txt`/`pyproject.toml`，云端依赖版本以 `um-cloud-deploy/requirements.lock` 与 `dependencies.lock.json` 为准；`bootstrap-wsl.sh` 只是 Windows/WSL 引导，不是云机安装器。
_Avoid_: 只拷源码即发布、依赖版本靠环境现状、把 dist-cloud 当已部署

**Linux 检查入口 (Linux check entry)**:
`um-cloud-deploy/run-linux-checks.sh` 支持 `core`（默认，`tests/linux_share_chain.py --scenario core`）、`full`（core + long 场景 + `tests.test_cloud_ops`）与 `resource`（`tests/linux_resource_envelope.py`）三种模式，各场景保留独立退出码；`scripts/check.sh` 的 `compileall` 覆盖 `chat2api/`、`share/`、`console/` 等全部 Python 包。这些入口不是完整云端验收，也不能据单一 core 通过声称 Linux 全套通过。
_Avoid_: 单项 core 绿即全套通过、跳过 Windows 全量

**云端 P1 收口约束 (Cloud P1 closure constraints)**:
Linux 进程归属只认解释器的入口脚本操作数（`linux_entry_script`：跳过解释器选项，`-c`/`-m`/`-` 返回 `None`），脚本名仅作为参数出现时绝不匹配，`/proc/<pid>/cmdline` 为空或不可读时 fail-closed；`--port` 配对与 uid 检查仍保留。云端控制台的分享动作只装配 8100 的 `share.gateway_restart`；本机注册表保留服务、账号与模型目录动作。`chat2api.py` 按父目录文件加载 `share_secret_protection`（不改 `sys.path`）；`--instance-pipe-prefix` 缺省按平台解析，实例模块不可用时返回 `None` 并报未就绪而不是猜测。`um-cloud-deploy/run-linux-checks.sh` 是 Linux-only 入口（Windows Git Bash 下 `/mnt/d` 路径解析失败），`tests/test_cloud_ops.py` 按平台写独立报告文件，`tests/fake_um/fake_um_server.py` 在 bind 前设置 `request_queue_size`。
_Avoid_: 用 argv 末位或参数中的脚本名判定归属、用前端隐藏代替后端 disabled、把 PENDING 撤销当成已撤销在途发送、在 Windows 上运行 Linux 检查脚本

**错误信封 (Error envelope)**:
网关返回错误时使用的 JSON 外壳。OpenAI 形包含 `message`、`type`、`code`、`param`，`detail` 是结构化诊断扩展；`/v1/messages` 使用 Anthropic 形。`type` 表示错误类别，`code` 是稳定的机器码，`param` 指出出错字段或为 `null`。
_Avoid_: 把 HTTP 状态码当机器码、把 detail 当标准字段、所有路径共用一种协议形状

**上游错误保留 (Upstream error preservation)**:
上游返回 4xx/5xx 时，网关保留原状态码，按客户端协议重建错误体，并在 `detail` 中记录 `upstream_status` 与有界的 `upstream_message`；只有 401 转成网关自己的 503 重新认证状态。连接失败是 502，网关自己等超时是 504。
_Avoid_: 一律折叠成 502、原样透传上游 body、把 401 当客户端 Key 错误

**可重试错误 (Retryable error)**:
网关判断值得再次发送同一上游请求的错误。当前包括 408、429、5xx、529、520-524、传输失败和网关超时；402/403 只做一次探测，400/422 仅在文字表示瞬态时做一次探测，404/405/409/413/415 不重试。重试只在客户端收到首个 UM 首段业务输出前发生，且对客户端隐藏。
_Avoid_: 所有非 200 都重试、重试 401/403/404、提交后重试

**分享准入拒绝 (Share admission rejection)**:
分享准入闸门在请求到达 UM 之前产生的 401/429；401 表示分享 Key 无效，429 用 `reason` 区分每 Key 并发、全局并发、每 Key 频率和停止接受，并带 `Retry-After` 或 `X-Should-Retry: false`。它不是上游状态，也不触发 UM 重登录。
_Avoid_: 把分享 429 当上游 429、把分享 401 当 UM 认证失败、分享 Key 鉴权触发重登录
