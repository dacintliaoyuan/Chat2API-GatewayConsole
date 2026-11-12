# AI 网关控制台 (AIGatewayConsole)

AI 网关控制台及其管辖的本机服务的领域词汇。

## Language

**费用统计 (Usage Billing)**:
控制台中按实际账本来源聚合用量、积分与可估算费用的页面；账户实时额度快照与请求历史消耗分开表达。
_Avoid_: 账单、消耗统计

**服务 (Service)**:
控制台可管理或观察的本机实例：当前包括 UM chat2api (8000) 和费用/管理宿主 (8001)。Qoder 国内版、Qoder 国际版、Gemini CLIProxy 与 M365 已退役，其历史服务记录与当前活动来源分开判断。
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
为恢复 chat2api 上游认证而打开的一次可见 Edge 登录交互；同一时刻所有触发方共享唯一会话。
_Avoid_: 登录窗口、刷新 token

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
提供权威模型目录的独立服务；当前同步来源仅 UM，退役来源（Qoder 国内版、Qoder 国际版、Gemini CLIProxy、M365）不再同步或进入默认读链。同一个模型 ID 在不同来源中是不同模型。
_Avoid_: provider、消费方

**UM**:
由 chat2api 暴露给本机消费方的澳门大学 Open WebUI 模型来源；面向用户统一显示为「UM」。
_Avoid_: OpenWebUI、openwebui、chat2api 模型

**已退役模型来源 (Retired model sources)**:
Qoder 国内版、Qoder 国际版与 Gemini CLIProxy 的源码、启动器与动作已于 2026-11-01 删除，不再同步、测速或出现在控制台读面；历史账本、日志与显示名后缀（「（qoder_CN）」「（qoder_Global）」）只作追溯，不再产生新记录。
_Avoid_: 把退役来源当活动 provider、删除历史账本、复用旧凭据

**受管消费方 (Managed consumer)**:
由模型目录镜像维护可选模型配置的本机应用，当前包括 DSH 和 ZCode。
_Avoid_: 模型来源、客户端进程

**模型目录镜像 (Model catalog mirroring)**:
受管消费方配置跟随模型来源的单次成功目录快照：新出现即发布，快照中缺失即退役，重新出现则再次发布。
_Avoid_: 模型确认退役、单向新增

**模型退役 (Model retirement)**:
把当前模型来源目录中已缺失的模型从受管消费方配置移除；只改变本机可选模型，不删除模型来源中的模型。
_Avoid_: 删除上游模型、卸载模型

**受管模型 (Managed model)**:
已登记为由模型目录镜像负责维护的消费方模型；来源重新出现时可再次发布，来源缺失时可自动退役，手工维护的条目不属于受管模型。
_Avoid_: 全部已配置模型、上游模型、固定模型

**能力探测 (Capability probe)**:
通过真实、有界且单独确认的推理请求，验证模型实际呈现的思考档位或可处理的上下文下界；目录字段或参数未报错本身不构成能力证据，同步从不附带探测。
_Avoid_: 可用性探测、档位猜测、应用并探测

**实测上下文下界 (Measured context lower bound)**:
能力探测在真实请求中验证通过的上下文 Token 数；只证明模型至少能处理这么多，既不是精确最大容量，也不是上游声明值。
_Avoid_: 实测上下文、最大上下文（指实测值时）

**模型设置 (Model settings)**:
本机为一个受管模型维护的显示别名、上下文取值来源和默认思考档位；只改变消费方看到的呈现，不改变模型的真实身份与路由。
_Avoid_: 高级覆盖、上下文覆盖

**设置草稿 (Settings draft)**:
已保存、尚未经预览和显式应用发布到消费方的模型设置；定时同步不发布草稿。
_Avoid_: 保存即生效、待生效配置

**设置预览绑定 (Settings preview binding)**:
真实预览生成的选择版本及 token，绑定待应用设置、能力证据、目录与消费方配置快照。任何绑定输入变化都要重新预览；读面中的草稿版本本身不是预览结果。整组延期保留旧已应用设置，不把延期写成新配置已发布。

**已应用设置 (Applied settings)**:
经显式应用成功写入受管消费方配置的那一版模型设置；定时同步只沿用这一版。
_Avoid_: 已生效（未经客户端读回时）

**运行中模型同步 (Live model synchronization)**:
受管消费方保持运行时完成模型配置更新，并确认其模型选择器或后续请求已采用相应版本；仅写入配置文件不构成运行中生效证据。ZCode 侧按文件锁、配置格式与变更监听协议判定兼容，版本仅作信息；未准入或不兼容时整体 pending，不自动重启。文件已写 ≠ 客户端已加载 ≠ 会话已采用。
_Avoid_: 强制热写、保存即生效

**客户端读回 (Client readback)**:
从每个消费方配置实际读出的别名、上下文、默认思考档位与只读输出上限；与草稿和已应用设置分别比较。它证明配置文件内容，不单凭此声称运行中的客户端已加载；缺席或不可读分别标明，不能填成零。
_Avoid_: 已同步（仅凭写入成功）

**同步范围 (Sync scope)**:
一次同步明确作用的来源、消费方与模型集合，分「全部」与「选中」两种；选中为空即拒绝，不回退为全部。
_Avoid_: 默认全量、隐式范围

**能力档案 (Capability profile)**:
受管模型已经验证并可在消费方中公开的思考档位集合；模型退役后仍保留，重新发布时直接沿用。
_Avoid_: 模型目录元数据、每次上线重测

**积分 (Credits)**:
Qoder 国内版上游按请求真实扣除、由国内网关记账的积分；国际版未提供可归因积分时保持未知，不与国内版合并。两个来源均已退役，历史积分只作追溯、不再产生新记录。
_Avoid_: 配额（quota 是账户余额快照，不是历史费用）

**额度快照 (Quota snapshot)**:
上游账户在某次检查时自报的剩余、已用、总量及到期信息；它不是本机请求账本，读取失败或过期时不把上次值当现值。
_Avoid_: 累计消耗、账单

**Token 消耗量 (Token consumption)**:
有完整输入与输出记录的请求在指定时段内的输入加输出 Token；缺失任一分量的请求单独标为部分可见，不混入完整总量。
_Avoid_: 请求数、仅输出 Token

**模型整次平均输出速率 (Request-average output rate)**:
一次请求的输出 Token 除以从发起到结束的总耗时；包含首字等待，不等于模型实际生成阶段的速度。
_Avoid_: 纯生成速度、瞬时 Token/s

**估算费用 (Estimated cost)**:
拿不到真实积分的服务（当前为 chat2api；M365 仅历史账本）按 token 用量 × 本地价目表算出的费用，展示时始终标注「估算，以平台账单为准」。
_Avoid_: 结算价

**价目表 (Price table)**:
本目录 `data/prices.json` 自持的价格数据（模型 × 输入缓存命中/未命中/输出 三档，含峰谷双价），用户查官网后手工维护（`checkedAt` 过期提醒）；计费语义仍移植自 dsh-cost-meter，但运行时不再读取该插件。2026-09-11 起与插件解耦。
_Avoid_: models.dev（已弃用的外部价格来源）；插件账本价（解耦前的主源）

**记账 (Accounting record)**:
各参与计量的服务按请求追加到本地 JSONL 的一条记录（模型、token、时间、来源）；与诊断用的日志是两回事。
_Avoid_: 日志、usage log、调用日志

**运行事件 (Runtime event)**:
服务运行日志中的一条诊断事件（时间、模型、阶段、结果、错误、耗时），日志页只呈现这一种；按请求计量的记账归费用页。
流式结束日志的上游请求 ID 必须是有界、无空白的单个字段；HTTP 解析可能把折叠头部的换行转换为空格，因此含控制字符或空白的 ID 显式标为 unavailable，不把它清洗成看似可查询的 ID。服务器名称等其他响应头中的正常空格保留。
_Avoid_: 调用日志、账本行

**DSH 快照 (DSH ledger snapshot)**:
DSH 2026-09-02 账本日的冻结快照（`data/dsh-ledger-20260902.json`，从账本存档生成），是 DSH 块展示与顶部累计并入的唯一数据源；现行插件账本已丢失该日，快照按用户 2026-09-04 决定保留该日统计。DSH 之后的流量经 chat2api 网关由 `usage-chat2api.jsonl` 逐条记账。
_Avoid_: DSH 账本实时读取（已解耦，ledger.json 不再被 8001 读取）

**来源 (Origin)**:
请求由哪个服务经手并记账；当前活动来源仅 chat2api（M365 与 chat2api 的端口物理分离为历史事实，M365 不再转发 chat2api），同一请求只会被一个服务记账，无需标记去重；DSH 流量走 chat2api（provider "openwebui"）由网关侧 JSONL 记账。
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
本机链路、Funnel 映射、公网 DNS 和真实模型调用分别显示。本机就绪要求入口进程、8110 进程、专用账号认证和发布同步均有肯定证据；Funnel 开启不能覆盖后端停止或缺失的读数。公网 DNS/TLS 来自每轮 Cloudflare/Google 双源检查（逐源状态、rcode/分类、TTL/负缓存与检查时间），模型回复来自手动密钥测试，均标出检查时间；缺少字段保持未确认，不推断为关闭或已连通。

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

**分享公网恢复轮次 (Sharing public recovery cycle)**:
API 服务运行意图仍为运行时，围绕同一次公网链路故障组织的有限一组恢复动作（映射重发、Tailscale 停止/启动、Tailscale 服务启动）；同一故障的重复触发归入同一轮次，人工关闭终止该轮次。Tailscale 服务启动使用独立的滚动 30 分钟最多 3 次预算，不占用映射或重启槽位。
_Avoid_: 后端进程自动拉起、UM 自动认证恢复、无限重启

**分享公网服务看门狗 (Sharing public service watchdog)**:
应用轮次在读取公网地址与探测前，先通过只读状态查询观察 Tailscale 服务。仅在确认服务停止时，才用受限原语启动并确认运行，然后继续本轮探测；服务已在运行或状态不可读时不做动作。它只启动、从不停止服务，并遵守安全、维护、取消与运行意图门禁；启动失败如实上报，不把未确认写成成功。
_Avoid_: 看门狗停止服务、把未知状态当作停止、绕过安全门禁的自动启动

**分享看门狗诊断 (Sharing watchdog diagnostics)**:
“读取分层状态”通过受限 SYSTEM broker 的固定只读查询返回 `umshare-watchdog` 的存在性、当前任务状态、执行程序与脚本路径是否匹配、上次任务返回值、运行时间和可得的保护日志更新时间。浏览器不传任务名、路径或命令。旧版后台、访问失败和缺失任务分别呈现；正在运行的任务不因历史非零返回值直接判为离线，保护日志更新时间也不能单独证明进程活跃。受限启动被拒绝时，操作错误保留安全的失败码和启动阶段。

**分享安全条件 (Sharing security conditions)**:
管理员后台读取 WFP 隔离规则及出站防火墙的实际状态；规则缺失或范围不符为条件不满足，查询或程序故障为未确认，只有两项均通过才为正常。页面通过“重新检查”执行一次既有分层只读诊断，通过“查看原因”解释检查结果；不由维护者手工勾选通过。

“修复隔离规则”通过已认证的受限管理员后台执行固定维护动作：先撤下公网、记录停止意图并停止入口，再重建和核验 WFP 及专用出站防火墙规则。入口关闭后临时暂停已核对身份的看门狗，重建完成或失败时重新启动，避免其把重建空档误判为隔离失效并删除分享任务。浏览器不能传入命令、路径或端口。修复成功仍保持分享关闭，由维护者选择“开启分享”。WFP 规则持久化保存，看门狗以开机触发方式重新核验；规则失效时即使入口已停止，也会撤下公网映射。查询失败或修复失败均不展示为通过。
_Avoid_: 浏览器安全设置、手动确认即安全

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

**API 展示费用（人民币）(API display cost in CNY)**:
API 用量中供维护者查看的人民币费用读数；按每条记录当时有效价表版本与汇率换算后聚合，已知、估算与合计各自带 partial 标记，原币 USD 记录保留可追溯；缺时间、缺汇率或无价记录显示未知或下界，不按当前汇率补算历史，也不改变用量归属。
_Avoid_: 分享展示费用（旧称）、原始账本金额、实际扣款、把未知费用记为零、用一个当前汇率重算全部历史

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
_Avoid_: 分享并发（旧称）、连接数、每分钟请求数

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
_Avoid_: 整链重启、share.stop→share.start

**白面档案 (Document Face)**:
控制台原型详情态中承载档案正文的空白文书表面——档案抽出后展示内容的纸面；三维磁带物件与其内部卷轴在详情态不展示。
_Avoid_: 空面白、磁带、档案物件

**网关下线 (Gateway offline)**:
网关服务链在控制台中被摘除展示与服务卡的状态；只表示该链不再出现在控制台与费用口径中，不表示其进程已停止或历史账本被删除。
_Avoid_: 停进程、删除账本

**管理会话 (Management session)**:
以一次性引导码兑换的本机 HttpOnly 会话，供控制台网页以外的本机管理通道使用；控制台网页不使用它，也不读取、存储或代管任何凭据。
_Avoid_: 控制台登录、控制令牌 (X-Admin-Control)、上游登录凭据

**控制台访问边界 (Console access boundary)**:
控制台网页读写管理接口时唯一的信任条件：页面必须在运行该控制台的机器上以本机回环地址同源打开，满足即视为维护者本人操作；控制台本身不设登录。
_Avoid_: 管理会话、私网可达即可信

**本地控制台 (Local console)**:
维护者个人电脑上管理 UM 自用实例及其他本机自用服务的控制台入口；API Key 与 API 服务归云端控制台管理。入口为 `Open-GatewayConsole.bat`（必要时启动 8001 宿主），生产页是 8001 托管的 `web/dist`（local 角色）；`?console=workbench` 保留旧工作台作为兼容入口。
_Avoid_: 主控制台、统一控制台

**云端控制台 (Cloud console)**:
云机上只管理云端 API 服务的控制台入口；与本地控制台共用前端代码，数据与操作目标各自归属，不经对方转发。入口为 `Open-CloudConsole.bat`（经专用 SSH 配置建 loopback 隧道到 18096），生产页是发布包内的 `web/dist`（cloud 角色，构建自 `web/dist-cloud`）。
_Avoid_: 远程管理本地、跨机统一控制台

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
由 `UM_SHARE_ROLE`/`UM_SHARE_DEPLOYMENT` 显式选择的 `local`（Windows 本机）或 `cloud`（Linux 云机）装配身份；未设置即 `local`，冲突或非法值在导入时拒绝。`cloud` 只装配 UM 分享动作与 `share-gateway` 服务控制，model_sync/zcode 模块惰性导入、不进入云端进程（Qoder/CLIProxy 模块已删除）；管理端口 `local` 默认 8001、`cloud` 默认 18096（`USAGESRV_PORT` 严格校验）。云端 `schema_version=2` 的 `unix_socket` 管理传输与 Windows `schema_version=1` 的 Named Pipe 配置分别有效，不得把 Unix 路径塞进 `pipe_name`。
_Avoid_: 用前端隐藏卡片代替后端隔离、云端照搬整个私人 data 目录

**Linux 平台适配 (Linux platform adaptation)**:
分享链在 POSIX 上的等价实现：重认证锁用 `fcntl.flock`（Windows 保留 `msvcrt`），Key 秘密保护用 AES-256-GCM 文件密钥（`CHAT2API_SHARE_SECRET_KEY_FILE`，0600/当前 uid，Windows 保留 DPAPI），broker 与实例通道用 AF_UNIX＋`SO_PEERCRED`（父目录 0700、socket 0600），受限服务管理用 pid 文件＋`/proc` 身份与信号（Windows 保留计划任务/PowerShell）。秘密文件读取统一走 `read_private_secret_path`；POSIX 上保护失败一律 fail-closed，不静默降级成 hash-only。
_Avoid_: 把 Unix 路径当 Named Pipe、只改前端、用 root 掩盖权限问题

**云端发送调度 (Cloud dispatch)**:
8110 在请求带 `verified` 归属事实时，于真实发送前按 Key 轮转并保持同 Key FIFO，累计等待默认 10 秒（`CHAT2API_SHARE_DISPATCH_MAX_WAIT_S`）；到期仍放行发送，请求级期限到期则释放且不发送。等待期间仍占用 8100 已分配的 API 并发名额；8000 主链与未验证归属不装配调度器。
_Avoid_: 云端分享发送调度（旧称）、入口外额外排队、把调度当上游限流、到期即拒绝

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
Linux 进程归属只认解释器的入口脚本操作数（`linux_entry_script`：跳过解释器选项，`-c`/`-m`/`-` 返回 `None`），脚本名仅作为参数出现时绝不匹配，`/proc/<pid>/cmdline` 为空或不可读时 fail-closed；`--port` 配对与 uid 检查仍保留。云端控制台的 `share.start/stop/estop/retry_stop` 以 `enabled=False` 注册并带「Tailscale/Funnel 编排未装配」原因，提交在创建操作行前以 `action_disabled` 拒绝（HTTP 403），本地注册表四项仍启用。`share_dispatch.revoke_key` 关闭 PENDING 与 GRANTED、保留 COMMITTED，重复撤销返回 0。`chat2api.py` 按父目录文件加载 `share_secret_protection`（不改 `sys.path`）；`--instance-pipe-prefix` 缺省按平台解析，实例模块不可用时返回 `None` 并报未就绪而不是猜测。`um-cloud-deploy/run-linux-checks.sh` 是 Linux-only 入口（Windows Git Bash 下 `/mnt/d` 路径解析失败），`tests/test_cloud_ops.py` 按平台写独立报告文件，`tests/fake_um/fake_um_server.py` 在 bind 前设置 `request_queue_size`。
_Avoid_: 用 argv 末位或参数中的脚本名判定归属、用前端隐藏代替后端 disabled、把 PENDING 撤销当成已撤销在途发送、在 Windows 上运行 Linux 检查脚本
