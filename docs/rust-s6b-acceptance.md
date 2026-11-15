# Rust S6b 验收记录：8002 session、display grant 与旧 share 写面（#54）

- 状态：实现完成；候选进程只监听临时 loopback 端口，未切换生产 8002。
- 分支：`rust/54`；基线：`integration/rust-rewrite` @ `176e51c`。
- 实现：`rust/crates/console`（crate `um-console`）、`rust/crates/store`（`random_token_urlsafe` 导出）。
- 契约来源：[docs/rust-contract.md](rust-contract.md) §6、§10；[docs/rust-plan.md](rust-plan.md) S6。

## 1. 交付范围

| 验收条目 | 实现 | 定向测试 |
|---|---|---|
| AC1 `POST /api/session` 兑换 12h HttpOnly 会话 Cookie；reissue 需 `X-Admin-Control` | `session.rs`、`server.rs::handle_session_exchange/handle_session_reissue` | `session_routes.rs`、`session.rs` 单测 |
| AC2 display-grant 一次性；display-operator acquire/takeover；单次 grant 存储 | `display_grant.rs`、`server.rs::handle_display_grant/handle_display_operator` | `display_grant_operator.rs`、`display_grant.rs` 单测 |
| AC3 `/api/share/*` 写面走控制令牌或会话+CSRF；与 Python 一致 | `share_auth.rs::management_write_auth`、`share_write.rs`、`server.rs::handle_share_api` | `api_share_write.rs`、`api_share.rs` |
| AC4 不 port `backend-login-cancel`，8002 返回 404 | 写路由白名单不含该路径 | `backend_login_cancel.rs` |
| AC5 明文/密钥不进日志、操作记录、URL 或错误消息 | 明文只映射 create/rotate 与单 Key 读回；错误只回固定码 | `write_redaction.rs` |
| AC6 写面失败切回 Python；数据格式不变 | 同一 schema 3 / 投影 schema 2；引导码是明文 0600 文件 | `write_failure_fallback.rs` |

## 2. 实现要点

- `POST /api/session` 使用 Python 的宽松 body 解析（JSON 失败按空对象），引导码
  600 秒有效、成功兑换即原子消费、会话 12 小时且从兑换时起算；响应只写
  `usage_session` HttpOnly Cookie（`SameSite=Strict; Path=/; Max-Age=43200`），
  不回显 token。
- `POST /api/session/reissue` 必须携带有效 `X-Admin-Control`；新码立即作废旧码，
  并写入 `USAGE_SESSION_CODE_FILE` 的明文 0600 文件（POSIX）；文件写失败不影响
  接口，与 Python 一致。
- display grant 只存 SHA-256 digest，绑定精确 origin 与 subject；错误 origin 不消费，
  单次兑换，TTL 60 秒、最多 64 个待用；session TTL 300 秒、最多 64 个。
- operator 单页：acquire 幂等；另一活跃 session 409 `display_operator_exists`；
  stale owner 可被 acquire 替换；takeover 递增 generation；响应形状与
  `usagesrv.py::_console_display_operator_payload` 一致。
- display grant 的 subject 只来自控制令牌、usage 会话或既有 display 会话；本候选不装配
  Tailscale Serve 身份，任何 `Tailscale-*`/`X-Forwarded-*`/`Forwarded` 头一律
  403 `display_authorization_required`（fail closed，不把 loopback 当管理员）。
- `/api/share/*` 读面保持 S5b 的会话/控制令牌规则；写面要求控制令牌，或会话 +
  `X-Requested-With: XMLHttpRequest`（缺凭证 401，缺 CSRF 403
  `session CSRF marker required`）。
- 旧写面按 `usagesrv.py::_share_api` 分支逐条校验：create/rotate 返回 manager
  结果（无 facade 的 `key`/`plaintext`）；delete 返回 `key_id`/`deleted_at`/
  `published`；revoke-and-terminate 返回 `disabled` 结果 + `terminated`/`released`/
  `aborted`/`upstream_closed`（缺失保持 null）；`limits/normalize` 返回
  `{changed, global, keys, blocked}`；`start|stop|emergency-stop` 202。
- 生命周期与 key 自检是注入缝：未接线时 503，绝不伪造 operation_id 或探测成功；
  `test` 只把明文交给注入的 probe 缝，响应、错误和 URL 都不含明文。

## 3. 门禁结果（2026-11-16）

已运行：

```bash
cargo test --manifest-path rust/Cargo.toml --offline
python -m unittest tests.test_console_display_route tests.test_console_share_facade \
  tests.test_console_share_actions tests.test_console_share_secret_redaction \
  tests.test_console_share_production
cd web && npm run check:cloud-console
cd .. && bash scripts/check.sh --full
bash scripts/check-rust-s6b.sh
git -c core.whitespace=cr-at-eol diff --check
```

实际结果：

- `cargo test --offline`（整个 Rust workspace）：444 个测试全部 ok，0 failed。
- ticket 的 Python parity 门禁：Ran 141 tests，`OK`。
- `cd web && npm run check:cloud-console`：71 pass，0 fail。
- `bash scripts/check.sh --full`：退出码 0。
- 另跑 `python -m unittest discover -s tests -p "test_*.py"`：Ran 2385 tests，
  `OK (skipped=63)`；`local/tests` 在 `check.sh --full` 的捕获尾部为 Ran 54 tests、`OK`。
- `bash scripts/check-rust-s6b.sh`：首次退出码 0。
- `git -c core.whitespace=cr-at-eol diff --check`：退出码 0。

既有失败（与本票无关，单独重跑通过）：

- 最终再跑 `bash scripts/check-rust-s6b.sh` 时，`um-gateway --test chat_disconnect`
  的 `a_pre_commit_disconnect_records_client_disconnected_without_a_200` 在 Windows 上
  因 `ConnectionAborted (10053)` 失败一次。该测试属于 S1b 的 `crates/gateway`，
  本票未改该 crate；单独重跑 `cargo test -p um-gateway --test chat_disconnect
  --offline` 三次均为 2 passed、0 failed。

定向测试（S6b）：

- `cargo test -p um-console --test session_routes --offline`：3 passed。
- `cargo test -p um-console --test display_grant_operator --offline`：6 passed。
- `cargo test -p um-console --test api_share_write --offline`：7 passed。
- `cargo test -p um-console --test backend_login_cancel --offline`：1 passed。
- `cargo test -p um-console --test write_redaction --offline`：1 passed。
- `cargo test -p um-console --test write_failure_fallback --offline`：4 passed。
- `cargo test -p um-console --lib --offline`：session 7 + display_grant 5。

## 4. TDD 与提交边界

- AC1、AC2、AC3、AC6 先写失败测试（404/501 或编译缺字段）再实现；AC6 的
  tombstone 断言在首轮红后修正为 Python 的“先禁用再 fail closed”语义。
- AC4 与 AC5 是回归钉子：它们钉住 S5b/S6a/S6b 已实现的行为，首轮即绿，
  没有新增生产代码。AC4 的 `backend-login-cancel` 在 S6a 已钉过一次，本票补上旧
  `/api/share/*` 写面。
- 6 个验收条目各自一个提交；文档与 `scripts/check-rust-s6b.sh` 单独提交。
  其中 AC5 提交最初误包含 AC6 的测试文件，已用 `git reset --soft` 拆成两个提交
  （未 push，工作树内容不变）。

## 5. 数据格式与回退

- 旧写面与 S6a 写面写同一个 Python 创建的 `share-store.db` schema 3；投影
  schema 仍为 2；测试确认表集合仍是 `configgen/keys/meta/ops/security`，Python
  种子 Key 仍可读。
- 发布失败时安全方向仍返回 `published=false` + `publication_error`；撤销方向
  503，并先把行置为禁用/墓碑（与 Python `update_key(enabled=False)` 一致），
  之后切回 Python 8002 可直接读同一文件，不迁移数据。
- 引导码文件是 Python 的明文 token（不是 JSON 文档），POSIX 上 0600；reissue
  覆盖为当前码。
- 候选只绑定 `127.0.0.1:0`，不监听 8002/8003/8097 或任何治理段端口。

## 6. 边界与未验证范围

- 未装配 Tailscale Serve 身份：带 `Tailscale-*`/`X-Forwarded-*`/`Forwarded`
  的 display grant/operator 请求一律 403；真实 Serve 身份校验留给部署/后续切片。
- `/display/*` relay（launcher、`/display/grant` 兑换、`/display/websockify`）仍是
  S7；本票只实现 grant 存储与 operator 状态机，`/display/grant` 的 HTTP 兑换未 port。
- facade 的 `keys/{id}/test` 与 `test-private` 不在本票范围；候选对它们返回 404。
  本票只实现旧 `/api/share/keys/{id}/test`（公开探测）。
- 生命周期 `start/stop/emergency-stop` 与 key 自检的 broker/host 与公网探测都是
  注入缝；测试用 fake seam，未连真实 broker、真实 8003 进程或真实公网。
- 未在 Linux 上运行 `#[cfg(unix)]` 的 0600 文件断言；本机是 Windows，测试跳过
  这些分支。未运行 `bash um-cloud-deploy/run-linux-checks.sh --mode full`。
- 未新增 S6b 生成夹具：本票 Rust 测试的期望值逐条引用
  `usagesrv.py:702-743`（session）、`usagesrv.py:1767-1783`（写鉴权）、
  `usagesrv.py:2013-2278`（旧写面）、`usagesrv.py:2671-2743`（display）、
  `console/display_proxy.py:366-560`（grant/operator）；这是与 S6a 相同的已知
  夹具缺口，不是生成夹具的替代。
- 未 push、未切换生产端口、未重启 broker、未执行真实部署或 UM 登录。
