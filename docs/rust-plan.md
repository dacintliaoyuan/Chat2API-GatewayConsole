# Rust 垂直切片计划（#37）

- 状态：计划冻结（#37 交付；随父 spec #36 评审后生效）。
- 输入：`docs/rust-contract.md`（同票契约冻结）、`docs/share-chain-map.md`、`GLOSSARY.md`、ADR 0001。
- 基线：`integration/rust-contract` @ `d131333`（2026-11-14）。
- 本计划**不写 Rust 实现**，只定义切片、依赖、顺序、验收门禁与回滚点。

## 1. 目标与非目标

### 目标

1. 以服务为切分单位，把云端 8100 与 8096 逐个替换为 Rust；每个切片都能独立验收、独立停下。
2. 每片先冻结外部行为（HTTP 请求/响应、SSE 事件、AF_UNIX 协议、文件副作用），再写实现。
3. 任何切片在 parity 门禁未通过前都不切生产端口；Python 始终是可回滚的权威。
4. Rust 只 port `docs/rust-contract.md` 列出的最终功能集，不把退役功能再 port 一遍。

### 非目标

- 不做一次性全量重写。
- 不改 headless 登录行为（spec #8）。
- 端口重排已由 #40 完成；本轨道不重做端口迁移。
- 不重写前端。
- 不在本轨道做真实云端 systemd/UM 登录验收。
- 不把本机 8001 的 Windows 专属服务（PowerShell 运行时、model_sync、local/）纳入第一批 Rust 切片。

## 2. 切分原则

1. **切片的交付单元是服务，不是端点。** 8100 与 8096 各自整体切换；服务内部可以按读面/写面分阶段实现，但生产端口只在整服务 parity 通过后切换。
2. **先只读/契约，再 8100 转发，再 8096 读面，最后写面。**
3. **每个切片都是垂直切片**：包含 HTTP 路由、鉴权、错误映射、超时/取消、幂等与副作用边界，不只写一个库函数。
4. **候选进程只监听临时 loopback 端口**，不进入 `AGENTS.md` 的治理段，不与生产端口双监听；测试通过后由部署脚本一次性切换。
5. **Python 保留为回滚权威**：Rust 切片失败时，不修改数据、不删除 release、不迁移 SQLite；切回 Python 即可。
6. **端口重排不混入切片**：#40 已把默认值改为 8096/8097，旧 8096/8097 只在一个兼容窗口内作为显式配置值；Rust 切片不硬编码端口，也不同时监听新旧两个端口。

## 3. 依赖图与顺序

```text
S0 契约夹具与 Rust 骨架
 ├── S1 8100 只读 + Chat 转发
 │    └── S2 8100 Messages / Responses / embeddings
 ├── S3 broker + Key 管理 + 投影发布
 │    ├── S4 8100 服务切换（依赖 S1/S2/S3）
 │    └── S5 8096 只读面
 │         ├── S6 8096 写面
 │         └── S7 display relay（依赖 S5/S6）
 └── S8 账本/Cloudflare/部署备份（只读 parity；工具保留 Python）
```

顺序约束：

1. S0 必须先完成，否则没有冻结夹具与 Rust 工程骨架。
2. S1 在 S0 后；S2 在 S1 后。S1/S2 可在候选端口运行，不切生产。
3. S3 可在 S0 后与 S1/S2 并行开发，但**必须在 S4 之前**完成 broker 的 8100 启动身份扩展，否则 Rust 8100 无法被 broker 拉起。
4. S4 在 S1/S2/S3 之后；S4 是 8100 的唯一切换点。
5. S5 在 S3 之后（分享状态/Key 读面依赖 broker）；S5 可在 S4 前或后，但按 spec 顺序排在 8100 转发之后。
6. S6 在 S5 之后；S6 是 8096 的唯一切换点。
7. S7 在 S5/S6 之后；display 的 grant/operator 依赖 8096 写面。
8. S8 可并行，不阻塞 S4/S6；Cloudflare/备份/部署工具默认保留 Python。

## 4. 切片明细

### S0：契约夹具与 Rust 骨架

**交付**

- 把 `docs/rust-contract.md` 的端点、错误码、SSE 事件、超时/取消/幂等/副作用固化为夹具。
- 建立 Rust workspace、HTTP 测试 harness、临时 loopback 端口、测试配置与临时数据目录；不连生产数据。
- 夹具生成仍走 `tests/generate_frontend_contract_fixture.py`，不手改生成物。

**依赖**：无。

**Parity 门禁**

```bash
node web/scripts/backend-contract/check-generated.mjs
python -m unittest tests.test_retired_features tests.test_check_gate
git -c core.whitespace=cr-at-eol diff --check
```

**切换/回滚**：无生产切换；只增加测试与骨架，失败即删除。

**风险**：夹具与 Python 行为漂移。对策：夹具由 Python 生成器产出；Rust 测试只读夹具，不在 Rust 里手写期望值。

### S1：8100 只读面 + Chat 转发

**交付**

- `GET /health`、`GET /ready`、`GET /v1/version`、`GET /v1/models`。
- `POST /v1/chat/completions` 与 `/api/chat/completions`、`/api/v1/chat/completions`，含非流式与 SSE；后两者是本机兼容别名，不在公网白名单内。
- `/v1` 分享路由的 Key 鉴权、每 Key/全局并发、20 次/分钟限速、撤销在途关闭。
- 请求体上限、错误形状、CORS/Origin 门、控制令牌门。
- 追加分享账本行（与 Python 同字段、同状态语义）。

**依赖**：S0。

**Parity 门禁**

```bash
python -m unittest tests.test_chat2api_endpoints tests.test_chat2api_stream \
  tests.test_share_gate_merged tests.test_chat2api_request_rejection \
  tests.test_share_gate_citations tests.test_chat2api_http_isolation
python -m unittest discover -s tests -p "test_*.py"
bash scripts/check.sh --full
```

**切换/回滚**：候选进程只监听临时 loopback 端口；不切 8100。失败直接杀候选进程。

**风险**：SSE 首字节/静默/预提交暂存/隐藏重试最容易出现行为差异；Rust 必须复现“提交前才发 200”和 in-band error。对策：以 `tests/test_chat2api_stream.py` 为 seam，先过流测试再写转发。

### S2：8100 Messages / Responses / embeddings

**交付**

- `POST /v1/messages` 与 `/api/v1/messages`：Anthropic 字节透传、SSE 事件序列、usage 记账。
- `POST /v1/responses` 与 `/api/v1/responses`：Responses 请求翻译、JSON/事件流、工具调用。
- `POST /v1/embeddings`：透传，不写账本。
- Messages/Responses 的 `stream=false` 聚合只对已验证 (key, model, protocol) 三元组生效。

**依赖**：S1。

**Parity 门禁**

```bash
python -m unittest tests.test_chat2api_endpoints tests.test_chat2api_stream \
  tests.test_share_gate_citations tests.test_share_gate_merged \
  tests.test_usage_boundary tests.test_r19_shared_responses
python -m unittest discover -s tests -p "test_*.py"
bash scripts/check.sh --full
```

**切换/回滚**：仍不切生产；S1+S2 都通过后才允许 S4。

**风险**：Anthropic SSE 字节保真、usage 累计、tool call 参数截断、Responses 事件顺序。对策：逐事件夹具比对；任何字节改写都要有 Python 端已知行为作依据。

### S3：broker + Key 管理 + 投影发布

**交付**

- AF_UNIX 管理 socket、HMAC/时间窗/重放、固定 action 白名单、实例通道。
- `publish` 的 writer→load→ACK、generation/fingerprint 单调、`terminate_keys` 一次性。
- `start`/`drain`/`emergency-stop`/`gateway-restart`/`maintenance-*`/`host-*` 的有界预算与证据语义。
- SQLite schema 3、Key 生命周期、安全日志、投影原子写。
- 扩展 broker 的 8100 启动身份：当前 `--expect-instance-image /opt/um-share/venv/bin/python`、`--expect-instance-cmdline-marker chat2api.py` 与 `--host-command ... chat2api.py` 必须改为可配置的 Rust 入口，同时保留旧 Python 入口的回滚路径。

**依赖**：S0。S3 的 8100 身份扩展是 S4 的前置。

**Parity 门禁**

```bash
python -m unittest tests.test_share_broker tests.test_share_broker_drain \
  tests.test_share_broker_server tests.test_share_broker_unix_socket \
  tests.test_share_store tests.test_share_store_key_reveal \
  tests.test_share_management tests.test_share_management_wiring \
  tests.test_share_security_generation tests.test_share_revoke_state \
  tests.test_share_secret_protection tests.test_share_limits_contract
bash um-cloud-deploy/run-linux-checks.sh --mode core
bash scripts/check.sh --full
```

**切换/回滚**：S3 可先切 broker 进程（Python broker → Rust broker），先用 Python 8100 验证 wire 兼容，再切 Rust broker；保留旧 unit 与旧 release；失败切回旧 broker unit。Key 数据格式不变，回滚不迁移数据。

**风险**：broker 是最高权限边界；Rust 若放宽 action/路径/服务名白名单、SO_PEERCRED 校验或 socket 权限，会把控制面暴露给错误进程。对策：先逐条对照 `share/broker_protocol.py` 的字段校验与 `share/broker_ops.py` 的 OS 边界；测试必须覆盖 forged MAC、stale ts、replay、超限、capability_missing、权限不足。

### S4：8100 服务切换

**交付**

- 用 S1+S2 的 Rust 8100 替换 Python 8100；broker 以 Rust 入口启动它。
- 更新 systemd/unit 与 `--host-command`、`--expect-instance-cmdline-marker`、`--expect-instance-image` 等部署事实；不双端口监听。
- 修订 `CODING_STANDARDS.md` 的分享链规则：生产 8100 入口改为 Rust，旧 Python 入口只保留为回滚路径；同步 `AGENTS.md` 端口表与部署文档。
- 账本、日志、token/profile 路径保持不变；Rust 不新增第二套数据目录。

**依赖**：S1、S2、S3。

**Parity 门禁**

```bash
bash scripts/check.sh --full
bash um-cloud-deploy/run-linux-checks.sh --mode full
```

另需在候选环境跑一次真实 8100 进程的 `tests/linux_share_chain.py --scenario core`（由 `run-linux-checks.sh` 调用）。

**切换**：停旧 8100 → 切换 `current` 符号链接/部署配置 → broker 启动 Rust 8100 → 验证 `/ready`、`/v1/models`、一次短请求、账本行。失败则切回上一 release 与旧 broker 配置。

**回滚点**：上一 release 符号链接 + 旧 broker unit/`--host-command`。回滚不删数据、不迁移 SQLite、不改账本。

**风险**：8100 的 SSE/重试/账本最容易出现“看起来成功”的差异；broker 身份标记与 systemd 围栏必须同步改，否则 broker 拒绝启动 Rust 入口。对策：先在候选端口跑完整流测试与账本对照；切换前用 broker `status`/`host-verify` 确认旧进程已停、Rust 身份被接受。

### S5：8096 只读面

**交付**

- `GET /health`、`/api/summary`、`/api/console/services`、`/api/console/health`。
- `/api/console/share-management/status`、`/keys`、`/keys/{id}/secret` 的只读面（secret 只在 200/410/404 语义内）。
- `/api/console/share-usage`、`/share-usage/requests`、`/um/*`、`/usage-tokens`、`/usage-rates`、`/stats`、`/logs`、`/log-sources`、`/log-entries`、`/log-overview`、`/gateway-connection`、`/actions`、`/operations` 只读。
- `/api/console/cloud-ops`、display-session、display-state。
- `/api/share/*` 只读面。
- 云端角色对非 UM 读面返回 503 `capability_not_assembled`。

**依赖**：S3（分享状态/Key 读面）、S0。

**Parity 门禁**

```bash
python -m unittest tests.test_console_api tests.test_console_share_facade \
  tests.test_console_stats tests.test_console_log_reader \
  tests.test_console_share_production tests.test_cloud_ops \
  tests.test_cloud_ops_wiring tests.test_gateway_connection_info \
  tests.test_um_stats_read_surface tests.test_console_public_probe \
  tests.test_console_public_url
cd web && npm run check:cloud-console
cd .. && bash scripts/check.sh --full
```

**切换/回滚**：候选 8096 只监听临时 loopback 端口；不切生产。读面失败杀候选。

**风险**：读面字段缺失时必须保持 `null`/省略，不能折 0 或 false；日志/账本路径不得进入响应；`share-management/status` 的三态 `gateway_running` 不能压成 bool。对策：逐字段对照 `docs/rust-contract.md` §6；用 Python 生成夹具。

### S6：8096 写面

**交付**

- `POST /api/console/operations`、`POST /operations/{id}/cancel`、操作列表/详情。
- `POST/PATCH/DELETE /api/console/share-management/keys*`、`PATCH /api/console/share-management/settings`。
- 旧 `/api/share/*` 写面。
- `POST /api/session`、`/api/session/reissue`。
- `POST /api/console/share-management/display-grant`、`display-operator`。
- 明文一次性、row_version 乐观并发、idempotency_key、fail-closed 发布。
- 不 port `POST /api/console/share-management/backend-login-cancel`：当前 8096 没有该路由（404），它是 #34 已退役的专用后端登录动作；登录/display 的呈现由 spec #8 决定。

**依赖**：S5、S3。

**Parity 门禁**

```bash
python -m unittest tests.test_console_operations tests.test_console_share_facade \
  tests.test_console_share_actions tests.test_console_display_route \
  tests.test_console_share_secret_redaction tests.test_console_share_production \
  tests.test_share_management tests.test_share_limits_contract
cd web && npm run check:cloud-console
cd .. && bash scripts/check.sh --full
```

**切换**：S6 是 8096 的唯一切换点。先完成 S5 读面，再一次性切 8096；不按端点双写。

**回滚点**：上一 release 符号链接 + 旧 unit；写面失败时切回 Python 8096。数据格式不变，回滚不迁移 SQLite。

**风险**：写面最易出现“假成功”：撤销未发布确认却返回 200、明文进入操作记录、row_version 冲突被吞、display grant 泄漏到日志。对策：逐条对照 `share/management.py` 的 `_require_applied`、`usagesrv.py` 的 `_console_share_api`、`console/display_proxy.py` 的 grant/session；测试必须覆盖 409/410/503、明文只在 no-store 响应、日志不含明文。

### S7：display relay

**交付**

- `/display/` launcher、`/display/launcher.js`、`/display/launcher-state.js`、`/display/<asset>`。
- `/display/session`、`/display/grant`（GET/POST）、`/display/websockify`。
- RFB 3.8/3.7/3.3 握手、VNC challenge-response helper、operator generation、输入帧过滤。
- noVNC 静态资源与安全头。

**依赖**：S5、S6。

**Parity 门禁**

```bash
python -m unittest tests.test_console_display_proxy tests.test_console_display_route
node --test tests/test_console_display_launcher_state.mjs
cd web && npm run check:cloud-console
cd .. && bash scripts/check.sh --full
```

**切换**：与 S6 同一次 8096 切换；display 是 8096 的模块，不单独监听端口。

**回滚点**：同 S6。

**风险**：RFB 字节流、WebSocket 分片、send 超时、operator generation、VNC 密码边界。对策：用假 x11vnc/RFB server 做夹具；不在测试里连真实 display；日志与响应不得出现密码、请求头或帧内容。

### S8：账本、Cloudflare、部署备份

**交付**

- 账本读面 parity：`share/metering.py` 的汇总/逐请求/筛选/游标/coverage。
- Cloudflare 入口检查与公网 DNS/TLS 检查 parity。
- 部署装配检查、备份/恢复 CLI、更新/回滚脚本的 parity。
- 这些工具**默认继续用 Python**；Rust 只做只读 parity 或后续独立切片。

**依赖**：S5（账本读面）；S0。

**Parity 门禁**

```bash
python -m unittest tests.test_share_metering tests.test_share_usage_results \
  tests.test_cloud_ops tests.test_cloud_edge_ingress \
  tests.test_cloud_deploy_assembly tests.test_manifest_stability
python um-cloud-deploy/cloudflared/check-ingress.py \
  --config um-cloud-deploy/config/cloudflared.yml.example \
  --expect-host <配置公网主机>
python um-cloud-deploy/backup_restore.py \
  --check-deployment-assembly console /etc/systemd/system/um-share-console.service \
  /etc/um-share/console.env
bash um-cloud-deploy/run-linux-checks.sh --mode full
```

**切换/回滚**：不切生产工具；Cloudflare/备份/部署脚本保持 Python。Rust 只消费同一 CLI/JSON 契约，不新增自动备份或 tunnel 变更。

**风险**：公网 DNS/TLS 检查会访问网络；测试必须注入夹具，不在 CI 打真实公网。备份/恢复涉及密钥与数据，默认不自动执行。

## 5. 每片的验收标准

每个切片必须同时满足：

1. **定向测试**：上表对应测试全部通过，且是外部行为 seam，不是内部函数。
2. **完整门禁**：`bash scripts/check.sh --full` 通过。
3. **前端门禁**：涉及 8096 页面/写面时 `cd web && npm run check:cloud-console` 通过；涉及 UM 读面时 `cd web && npm run check:um` 通过。
4. **契约夹具**：改 `usagesrv.py`/`console/adapters.py`/`console/model_actions.py`/`share/metering.py`/`console/stats.py`/`console/log_reader.py` 后重新运行 `python tests/generate_frontend_contract_fixture.py`，并跑 `node web/scripts/backend-contract/check-generated.mjs`。
5. **Linux 专属**：涉及 broker/AF_UNIX/真实 8100 进程时，在 WSL/Linux 跑 `bash um-cloud-deploy/run-linux-checks.sh --mode full`。
6. **无敏感信息**：提交前 grep 内网地址、凭据值、token 值；日志/错误只记录存储类别。
7. **可回滚**：切片失败时保留旧进程、旧数据、旧 unit；不删除 release、不迁移 SQLite、不改账本。

## 6. 风险清单与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| SSE 字节/事件顺序不一致 | 客户端解析失败、重复计费 | 逐事件夹具 + 真实 Python 对照；首字节/静默/预提交/隐藏重试全测 |
| 流式取消与账本状态不一致 | 取消后仍写成功行 | 取消在每个字节检查；账本写 `client_disconnected`；夹具对照 |
| 分享并发/限速语义偏差 | 超卖或误拒 | 每 Key/全局并发与 20/min 限速逐项对照；429 reason 区分 |
| 撤销未确认却返回成功 | Key 仍可访问、假成功 | 撤销方向 fail closed；409/503 与 blocked_publications 对照 |
| 明文进入日志/操作记录 | 凭据泄漏 | 明文只走一次性 no-store 响应；测试 grep 日志/操作记录 |
| SQLite schema/加密格式不一致 | 回滚后数据不可读 | schema 3 与投影 schema 2 分开；DPAPI/AES-GCM 逐平台测试；不迁移数据格式 |
| broker 权限放宽 | 任意命令/路径/服务名注入 | 固定 action 白名单、HMAC、replay、SO_PEERCRED、无 shell |
| display RFB/WebSocket 差异 | 登录窗口不可用或凭据泄漏 | 假 x11vnc/RFB server；grant/session/operator 夹具；不记录帧与密码 |
| 端口 #40 混入 | 与 Rust 变更互相污染 | Rust 只读配置端口；#40 单独分支；兼容窗口只接受显式旧值 |
| 内存收益不成立 | 重写没有收益 | 同命令、同 idle 判据复测；RSS 含共享页，需同时看 PSS/工作集 |
| 测试夹具与 Python 漂移 | Rust 测试自证 | 夹具由 Python 生成；`check-generated` 失败即停 |
| 部署/备份工具重写风险 | 运维入口回归 | 默认保留 Python；Rust 只做只读 parity，不切生产 |

## 7. 回滚策略

1. **任意切片可停**：S0/S1/S2 只在候选端口运行，删除候选进程即可；S8 不切生产。
2. **S3 broker 回滚**：切回旧 broker unit 与旧 Python broker；Key SQLite 不迁移、不删除；旧投影继续被 8100 读取。
3. **S4 8100 回滚**：切回上一 release 符号链接；恢复旧 broker `--host-command` 与 `--expect-instance-cmdline-marker`；旧 8100 由 broker 重新拉起；账本/日志/token 路径不变。
4. **S6 8096 回滚**：切回上一 release 符号链接；旧 unit 重新启动；share-store.db 不迁移、不删除；display 与操作记录保留。
5. **数据回滚**：不在切片切换中自动恢复备份；只有显式 `rollback.sh --recover-backup` 或 `backup_restore.py --deployment-restore` 才恢复，且先排空、再校验安全状态 floor。
6. **禁止**：不双端口监听、不双写账本、不双 broker、不把 Rust 与 Python 同时作为生产权威。

## 8. 与 #40 的边界

- Rust 切片不实现端口重排；新端口由 #40 的部署模板决定，Rust 只读环境变量。
- #40 完成后，Rust 服务必须接受 `USAGESRV_PORT` 与 `CONSOLE_DISPLAY_VNC_PORT` 的显式配置；默认值来自部署模板，不来自 Rust 代码。
- 兼容窗口内，旧端口值作为显式配置仍可启动；窗口结束后移除旧别名。Rust 不得同时监听新旧两个端口。
- 非法值继续 fail closed；Rust 不静默回退默认端口。
- 本计划不修改 `AGENTS.md` 端口表；#40 已同步端口表、部署模板、测试与文档。
- `CODING_STANDARDS.md` 的「生产 8100 只运行 `chat2api/chat2api.py`」由 S4 在切换时修订；#40 不修改这条规则。

## 9. 未验证范围

- 本票未运行任何 parity 门禁；上表命令是仓库实际命令，不是本票的通过证据。
- 未创建 Rust 工程、未写 Rust 代码、未跑 Rust 测试。
- 未在云端验证 #40 的新端口；#40 已合入本分支。
- 未在本计划的新代码上触发登录；旧 release 的登录中 Chromium 读数见 `docs/headless-login-acceptance.md`，不能当作本计划的读数。
- 未执行真实 Cloudflare Tunnel 变更、broker 重启、生产备份/恢复或 UM 登录验收。
