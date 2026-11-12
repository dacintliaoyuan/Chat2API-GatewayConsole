# UM 熟人分享试验

当前方案：朋友客户端 → Tailscale Funnel HTTPS → 本机独立分享入口 → chat2api → UM。
不需要 vLLM、域名或云服务器。大陆直连与长时间 SSE 需要朋友的实际网络验收；本机验证不代表通过。

## 运行边界

- `share_gateway.py` 使用 Python 标准库，固定只监听 `127.0.0.1`，默认端口 8100；上游后端**必须显式指定**：`--upstream-port` 没有默认值，未提供即拒绝启动（fail-closed），生产值是本机 8110 专用分享后端（独立 profile，`deploy-elevated.ps1` 的 `ShareBackendPort` 显式传入）。私人 chat2api 的 8000 绝不被分享链隐式使用。不读取 UM 凭据。
- 唯一公开业务路由是 `GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/messages`（Anthropic Messages）与 `POST /v1/responses`（OpenAI Responses）。模型目录额外接受单个 `?limit=N`（十进制整数 1–1000，不接受前导零）与 SDK 的 `?beta=true`，仅截取本次新鲜目录，查询不转发上游；这是 Harness 的单页目录兼容，不实现 Anthropic 游标分页。Messages 同样只接受 SDK 的 `?beta=true`，转发上游时使用规范路径。重复、编码或未知参数及其他路由的查询串仍返回 404。管理、健康检查、上游 `/api/*` 与其他路径不开放；Anthropic 的 `files`、`batches`、`count_tokens` 同样不开放（专用后端本身没有 `count_tokens`，分享入口也不自行计数）。
- 每人独立分享密钥；配置仅存 SHA-256 摘要。每次请求重读配置，撤销影响后续请求；已接受的请求不会因撤销被强制取消。损坏配置拒绝访问。
- 每个身份最多一个在途请求、总共两个；每人每分钟最多 20 个已接受请求，模型列表同样计入。最多 16 个 HTTP 处理连接，请求体限制 1 MiB（Messages 的图片同样受这一上限约束）。
- 请求只转发常规 OpenAI 聊天字段；额外接受 Harness 默认发送的严格布尔 `store: false`，验证后移除该字段，不向上游请求存储语义；`store: true` 或其他类型仍返回 400。此兼容项不改变 UM 自身的数据保留政策。不开放 Open WebUI `tool_ids` 等服务端扩展。Messages 走自己的字段白名单（未知字段一律 400 `unsupported_messages_request`，`max_tokens` 必填且不替调用方补默认值）。Responses 走自己的字段白名单（未知字段一律 400 `unsupported_responses_request`），分享入口只做校验并把原始 Responses 请求转发到专用后端的 `/v1/responses`；翻译成聊天请求再翻译回 Responses 事件由后端统一完成，分享入口不再保留第二份翻译。覆盖 Harness 使用的文本与函数工具子集，接受 `reasoning` 与 `include`（后端只把 `reasoning.effort` 映射为聊天 `reasoning_effort`，`summary` 与加密推理内容不转发），不开放 Responses 的托管工具、会话存储或游标。函数工具调用按 Responses 事件返回，连续多个 `function_call` 合并进同一条 assistant 消息；`finish_reason: length`/`content_filter` 返回 `response.incomplete`，流式缺少 `[DONE]` 返回 `response.failed`，不把截断伪装成完整成功；`usage` 保留缓存与推理明细。工具结果含图片时，后端改走 Anthropic Messages（`tool_result` 内容块支持 image），其余请求仍走 Chat；客户端始终只看到 Responses 协议。朋友密钥、Cookie 及转发头均不发送给 chat2api。
- Messages 和模型目录接受两种凭据形状，并收敛到同一份 Key 摘要、启用状态、并发预算与账本入口：`Authorization: Bearer <分享密钥>`（Claude Code 的 `ANTHROPIC_AUTH_TOKEN`、OpenAI 兼容客户端）与 `X-Api-Key: <分享密钥>`（Anthropic SDK `api_key` 的默认形状）。同名重复头、空凭据、超长凭据（凭据头上限 256 字节）一律 401；两个头同时在场时仅当值相同才接受，值不同一律 401。Chat Completions 仍只认 Bearer。Messages 的 `anthropic-version`/`anthropic-beta` 原样转发，其余客户端头不转发。上游非 200 折叠为 502/503 `um_unavailable`（与聊天相同的显式失败契约），不伪造成功。
- SSE 按到达数据转发。上游静默上限与专用后端自身的预算对齐：流式每次读静默上限 180 秒（收到数据即重置，无总时长上限），非流式整答等待上限 600 秒。调用方断开在下次读写/超时后释放连接和并发槽，不保证即时取消 UM 推理。
- 默认不记录聊天正文或请求日志；原 chat2api 的用量记录仍按原规则执行，本次未增加逐人计费。
- 停止分享进程不停止原 UM。电脑关机、睡眠、断网或 UM 认证失效会导致不可用；不自动启动或重启原 UM 服务。

## 本机操作

以下命令在 `ai-gateway` 目录执行。密钥输出仅显示一次，应自行保存，不提交 Git。

```powershell
# 创建/轮换/撤销身份：统一经本机管理服务（share_admin.py，唯一写入者
# SQLite）。同名 create 返回 409；轮换是显式 rotate 操作。明文只此一次。
python share_admin.py create --name friend1

# 首先启动测试模式：不会调用 UM，模型名为 connectivity-test。
# 两条命令都必须显式给出上游端口（没有默认值；缺省会拒绝启动）。
# 手动入口要指向本机正在运行的**专用分享后端 8110**（正式运维链由
# deploy-elevated.ps1 自动传入同一端口）；`--probe` 也不接受省略端口。
.\start_um_share.bat --probe --upstream-port 8110

# 另一个终端：先检查，确保不会覆盖已有 Funnel/Serve 规则。
& 'C:\Program Files\Tailscale\tailscale.exe' funnel status
& 'C:\Program Files\Tailscale\tailscale.exe' serve status
# 仅当 443 未被占用、且确认目标是 8100 分享入口时启用。
& 'C:\Program Files\Tailscale\tailscale.exe' funnel --bg --https=443 http://127.0.0.1:8100
```

首次登录与首次启用 Funnel 会要求打开官方账号链接完成交互。初次连接使用 `--accept-dns=false --accept-routes=false`，不指定出口节点，避免接管本机 DNS 和其他网络路由。

朋友选择 OpenAI 兼容服务：Base URL 为 Funnel 输出的 HTTPS 地址后加 `/v1`，API Key 为个人密钥；试验阶段选择 `connectivity-test`。

DeepSeek Harness 0.2.0-rc.2：OpenAI Chat Completions 与 OpenAI Responses 都使用 `https://分享域名/v1`；Anthropic Messages 使用 `https://分享域名`（不加 `/v1`）。Harness 的 Anthropic 模型发现会请求 `/v1/models?limit=1000` 并发送 `x-api-key`，SDK 还会带 `?beta=true`；推理由 Anthropic SDK 自行追加 `/v1/messages`，若 Base URL 也带 `/v1` 会拼出不开放的 `/v1/v1/messages`。`?beta=true` 与 `limit` 都在分享入口本机消费，转发上游时使用规范路径。Responses 由分享入口翻译成聊天上游，返回 `response.created` / `response.output_text.delta` / `response.completed` 等事件；目录返回现有真实 `data[].id`，Harness 可直接读取；不伪造创建时间、模型别名或分页元数据。

实际网络测试通过后，在原终端 Ctrl+C 停止测试进程，再运行 `.\start_um_share.bat --upstream-port 8110` 接入已经运行的 UM，朋友刷新模型列表。不要同时启动两个 8100 进程。手动启动只用于试验；正式链仍按 `um-share-deploy/README.md` 的 guard/watchdog 流程启动。

```powershell
# 撤销身份（tombstone；202 后可用 `share_admin.py operation <id> --wait` 跟踪）。
python ai-gateway\share_admin.py delete <key_id> --expected-version <n>

# 关闭本任务的 Funnel 443 映射；先核对状态和归属。
& 'C:\Program Files\Tailscale\tailscale.exe' funnel --https=443 off
# 然后在分享入口终端 Ctrl+C。不要用 funnel reset 清除其他规则。
```

## 控制台 Key 的公网模型测试

分享页的「测试」保持原位置，点击后从当前 Tailscale 节点重新取得 HTTPS 分享地址，经 Cloudflare HTTPS DNS 取得公网 IPv4，连接该公网入口并保留原域名的 SNI、证书校验和 HTTP Host。它不使用本机 8100 或 Tailscale 内网 IP 作为成功回退；DNS 查询只发送公开域名，不携带 API Key。

测试先读取 `/v1/models`，再用目录中的第一个模型发送一次 `POST /v1/chat/completions`：固定短提示，非流式，最多 32 个输出 Token，会产生少量模型用量。只有取得非空 assistant 回复才显示「已连通，模型已回复 · 公网往返」；空目录、HTTP 200 错误体、TLS 握手/证书失败、鉴权拒绝、限流和超时均不算通过。结果显示模型、耗时和检查时间，不显示密钥或回复正文。

当公开 DNS 返回多个 IPv4 时，模型目录查询最多尝试其中 4 个；多地址时每个地址最多 8 秒，连接、TLS 或传输失败才切换，鉴权拒绝、HTTP 错误和无效业务响应直接报告。单地址仍使用原总预算。所有候选地址都保留同一域名的 SNI、证书校验和 HTTP Host；模型 POST 至多发送一次，推理阶段失败不会自动重放。

网络阶段总预算 60 秒，独立进程在 65 秒硬截止；同一 Key 不重复启动测试，全局最多两个。请求过程中 Key 被轮换、停用或删除时，结果作废。公网地址查询失败不会复用旧域名发送密钥。

这项测试验证本机经公网 ingress 往返到模型的当前路径；朋友的 Wi-Fi、移动数据或代理仍需各自验收。端口与 Funnel 配置全绿仅说明各层内部观察，不能代替公网测试。若公网测试显示 TLS 握手失败，应检查到该域名的网络/代理与 Funnel ingress；不要关闭证书校验，也不要改成 localhost 让测试变绿。

## 验收

1. 未带密钥、错误密钥返回 401；撤销后旧密钥返回 401；配置损坏返回 503。
2. `/admin/*`、`/api/*`、`/health` 不可访问；密钥和 Cookie 不转发上游。
3. 模型列表、普通聊天成功；首个 SSE 事件应在整个回答完成前到达。
4. 同人并发、总并发和速率超过限制时返回 429；完成或失败后槽位释放。
5. 上游断开得到明确错误；停止分享不影响本机原有 UM 调用。
6. Messages：带 `anthropic-version` 的 Anthropic 客户端可用（Anthropic SSE 事件原样到达，不改写成 OpenAI 形状）；SDK 默认的 `X-Api-Key` 与 Claude Code 的 Bearer 两种形状都通过且共用同一限额，同名重复头、空值与两值不同的双头均 401；`files`/`batches`/`count_tokens` 返回 404。
7. 朋友用大陆实际网络、关闭代理/Tailscale 客户端测试，覆盖晚高峰和数分钟回答。未通过前不把试验入口称为稳定服务。

离线自动化测试：在 `ai-gateway` 目录运行 `python -m unittest discover -s tests -p "test_share_gateway.py" -v`，仅使用隔离本地 HTTP 服务，不调用 UM。
该用例集在导入时需要同目录的 `share_broker_protocol.py`（投影/墓碑路径），所以工作目录必须是 `ai-gateway`；项目根的共享门禁跑全量
（`python -m unittest discover -s ai-gateway/tests -p "test_*.py"`）时会由其它测试模块先注入该路径，两种跑法都有效。

官方资料：[Funnel](https://tailscale.com/docs/features/tailscale-funnel)、[Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel)。

## 当前状态与运维（2026-09-06）

- 公网入口域名：`https://dacint.tailae8db5.ts.net`（节点 443 Funnel → `127.0.0.1:8100`；旧 `daicnt` 已废弃）。
- 正式运维脚本位于 `ai-gateway/um-share-deploy/`（README 列出启停/验证/密钥命令；`tmp/p0-umshare/` 仅作历史证据）。
- 安全依赖：`um_share` 受限身份 + 专用映像 + WFP（仅放行 127.0.0.1:8110 专用分享后端）+ 防火墙程序出站阻断 + Job 单进程 +
  SYSTEM Watchdog（`umshare-watchdog`，10 秒轮询 = **有界延迟停止，非即时**；公网兜底 `funnel --https=443 off`）。
- 启动前置：限制强校验通过 且 Watchdog=Running 才开 8100（fail-closed）。
- 模型列表契约：请求 chat2api 的“原始上游+新鲜度”（`X-Chat2Api-Models: raw-fresh` + 回执标记），
  不做后缀猜测过滤；上游失败明确报错，不用旧缓存。
- 新分享管理阶段（S1–S5 离线，2026-09-06）：`share_gateway.py` 已兼容 SQLite 管理 store 的
  `schema_version/config_generation/key_id` 投影，同时保留旧 `clients` 格式仅用于受控迁移期；
  `share_store.py`/`share_management.py`/`share_admin.py` 提供单一写入者命令层与保护 API/CLI。
  真实 Key 迁移、高权限 broker 安装与在线切换仍待用户显式授权，默认只保存在离线 store。

- 已知限制：无开机自启（重启后需按 um-share-deploy/README 恢复）；流式请求在每次读静默超过 180 秒时中断
  （持续有数据则不限总时长；非流式等待上限 600 秒）；调用方断开在下次读写/超时后释放并发槽；本机经 Clash TUN
  自连 Funnel 域名会失败（外部直连不受影响）。
