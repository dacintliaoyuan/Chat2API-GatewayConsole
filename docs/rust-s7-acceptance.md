# Rust S7 验收记录：display relay（#56）

- 状态：实现完成；候选进程只监听临时 loopback 端口，未切换生产 8002，也未连接真实 display。
- 分支：`rust/56`；基线：`integration/rust-rewrite` @ `c05f433`。
- 实现：`rust/crates/console`（crate `um-console`）新增 `src/display_relay.rs`，并扩展
  `display_grant.rs`（operator 代际）、`config.rs`（`CONSOLE_DISPLAY_*`）与
  `server.rs`（`/display/*` 路由与 WebSocket 升级）。
- 契约来源：[docs/rust-contract.md](rust-contract.md) §10；[docs/rust-plan.md](rust-plan.md) S7。

## 1. 交付范围

| 验收条目 | 实现 | 定向测试 |
|---|---|---|
| AC1 `/display/` launcher、静态资源防穿越、安全头与 Python 一致 | `display_relay.rs::DisplayConfig::load/serve_static`；`server.rs` 的 `/display` 路由 | `display_static.rs` |
| AC2 `/display/session` 返回 `{authorized, expires_at}`；GET grant 为 launcher 页面、fragment 兑换 | `serve_session`、GET `/display/grant` 分支 | `display_session_grant.rs` |
| AC3 POST grant 单次兑换 HttpOnly Cookie；grant 60s、session 300s、idle 900s | `serve_grant_exchange`、`DisplayConfig::load`、`DisplayGrants` 常量 | `display_grant_exchange.rs`、`display_grant.rs` 单测 |
| AC4 `/display/websockify` RFB WebSocket 中继；session/控制令牌/Tailscale 身份校验 | `handle_websocket`、`run_relay`、`ConnectionLimiter`、`server.rs` 的升级拦截 | `display_websocket.rs` |
| AC5 RFB 3.8/3.7/3.3 握手与 VNC challenge-response 与 Python 一致 | `perform_vnc_auth`、DES/`vnc_auth_response`、`PasswordFileVncAuth`、`UnixVncAuthClient` | `display_rfb.rs` |
| AC6 日志/响应不含密码、请求头或帧内容 | relay 无任何日志调用；错误只回固定码；grant 不回显 | `display_redaction.rs` |
| AC7 测试用假 x11vnc/RFB server，不连真实 display | `tests/support/display.rs::FakeDisplay`；`display_config` 只接受临时端口 | `display_fake_rfb.rs` |

## 2. 实现要点

- `DisplayConfig::load` 复刻 `display_proxy.py::load_config`：默认关闭；`CONSOLE_DISPLAY_VNC_HOST`
  必须是 loopback；端口 1..65535；noVNC root 与 launcher 必须是绝对路径；连接上限 1..4；
  idle 30..7200、max session 60..14400、handshake 1..30、关闭宽限 1..30；auth socket 与
  auth file 互斥且必须绝对路径；资产缺失分别报 `display_novnc_missing`、
  `display_launcher_missing`、`display_vnc_auth_missing`，其余非法值统一
  `display_config_invalid`。
- 静态面：`/display` 308 到 `/display/`；`/display/` 只读 launcher；`launcher.js` 与
  `launcher-state.js` 只从 launcher 文件所在目录取，绝不被 noVNC 树里的同名文件遮蔽；
  `/display/<asset>` 只允许 `STATIC_TYPES` 扩展名、拒绝 `\ % NUL :`、空段、`.`/`..`，
  规范化后必须仍在 noVNC root 内；所有响应带与 Python 相同的 7 个安全头。
- 查询字符串：除 `/display/websockify?operator=<canonical positive decimal>` 外一律 400
  `display query not allowed`；fragment 同样 400。
- 授权：`/display/` 与 `/display/websockify` 需要同源 + display session、控制令牌或
  usage session；`/display/grant` 页面与 `/display/session` 只做同源门；本候选不装配
  Tailscale Serve policy，任何 `Tailscale-*`/`X-Forwarded-*`/`Forwarded` 头 fail closed 403。
- `/display/session` 只返回 `{authorized, expires_at}`；`expires_at` 只在有活 display
  session 时是整数，控制令牌授权时为 `null`。
- grant 兑换：只存 SHA-256 digest、绑定精确 origin、单次使用；错误 origin 不消耗；TTL 60s、
  最多 64 个待用；session TTL 300s、最多 64 个；Cookie 为
  `um_display_session=...; HttpOnly; SameSite=Strict; Path=/; Max-Age=...`，HTTPS origin
  加 `Secure`；400/403/413/415 的响应体为空，只有成功 204 带 `Set-Cookie`。
- WebSocket：只接受 RFC 6455 v13；`Sec-WebSocket-Key` 必须是 16 字节 base64；子协议只接受
  `binary`；不合法 400/426。握手前先完成上游 RFB 安全握手，失败 502 且不升级；成功后浏览器
  只收到 RFB 3.8 None greeting。客户端 version/security/ClientInit 共 14 字节跨帧丢弃，之后
  的 RFB 字节原样转发。
- operator 单页：WebSocket 要求活 display session + 当前 generation；旧 generation 的
  KeyEvent/PointerEvent 被丢弃但观看仍可用；关闭只释放自己的 generation；连接上限 2、
  idle 900s、单会话 14400s、握手 10s、关闭宽限 5s（配置默认）。
- VNC 认证：`_des`/`d3des` 纯 Rust 移植，`x11vnc -storepasswd` 文件用固定 vnckey 解密，
  challenge 分两块 ECB；密码文件在 POSIX 上校验 0400/0440/0600/0640 与 uid（root 或本人）；
  helper socket 只传 16 字节 challenge 进、16 字节 response 出。密码不进 console 进程之外的
  任何响应、URL、日志或浏览器存储。
- relay 没有任何 `println!`/`eprintln!`/`log::`/`tracing::` 调用；所有错误体都是固定码。

## 3. 门禁结果（2026-11-16）

已运行：

```bash
python -m unittest tests.test_console_display_proxy tests.test_console_display_route
node --test tests/test_console_display_launcher_state.mjs
cd web && npm run check:cloud-console
bash scripts/check.sh --full
cargo test --manifest-path rust/Cargo.toml --offline
bash scripts/check-rust-s7.sh
```

实际结果：

- `python -m unittest tests.test_console_display_proxy tests.test_console_display_route`：
  Ran 130 tests，`OK (skipped=2)`。
- `node --test tests/test_console_display_launcher_state.mjs`：12 pass，0 fail。
- `cd web && npm run check:cloud-console`：71 pass，0 fail。
- `cargo test --manifest-path rust/Cargo.toml --offline`：98 个 test result 行，
  481 passed，0 failed。
- `bash scripts/check.sh --full`：退出码 0；其中主 Python suite
  `Ran 2385 tests`、`OK (skipped=64)`，`local/tests` `Ran 54 tests`、`OK`。
- `bash scripts/check-rust-s7.sh`：退出码 0（fmt/clippy/test + Python display 门禁 +
  node launcher-state + `web/check:cloud-console` + diff check）。

定向测试：

- `cargo test -p um-console --test display_static --offline`：6 passed。
- `cargo test -p um-console --test display_session_grant --offline`：3 passed。
- `cargo test -p um-console --test display_grant_exchange --offline`：5 passed。
- `cargo test -p um-console --test display_websocket --offline`：9 passed。
- `cargo test -p um-console --test display_rfb --offline`：7 passed。
- `cargo test -p um-console --test display_redaction --offline`：3 passed。
- `cargo test -p um-console --test display_fake_rfb --offline`：2 passed。
- `cargo test -p um-console --lib --offline`：新增 operator 匹配/释放单测通过。

## 4. TDD 与提交边界

- AC1 先写 config/静态面测试（首轮 404/403/503），再实现 `DisplayConfig::load`、静态树与安全头；
  顺带修正 dispatch 的 Origin 端口门使用实际监听端口（与 Python `PORT` 一致）。
- AC2/AC3 的 `serve_session`/`serve_grant_exchange` 在 AC1 的实现中已经落地，对应提交补上
  定向测试；AC3 还修正了 dispatch 对正确 ephemeral Origin 的拒绝，并让 grant 兑换的
  origin 失败返回 403（此前误为 404）。
- AC4 首轮红：缺少 `operator_matches`/`release_operator`、WebSocket 升级未接管原始流；
  实现后绿。
- AC5 首轮红：没有 RFB 握手与 DES；实现 `perform_vnc_auth`、纯 Rust DES 与两个 auth
  provider 后，用 Python 的 d3des 参考向量（`3fa40e8a984d4815`、`6e09a37726dd560c...`、
  `b866924125c8eebb9debc1db61c538e2`）验证一致。
- AC6 是回归钉子：首轮即绿（relay 无日志、错误只回固定码），新增 grant 403 固定体后保持绿。
- AC7 是边界钉子：`FakeDisplay` 只绑 `127.0.0.1:0`，测试文件扫描确认没有任何
  `vnc_port: 8097`。
- 7 个验收条目各自一个提交；文档、`scripts/check-rust-s7.sh` 与 README 单独提交。

## 5. 未验证范围

- 真实 x11vnc、`-rfbauth`、systemd display unit、Tailscale Serve 身份与真实 noVNC
  浏览器握手未在目标机验证；本票只覆盖候选 loopback 与假 RFB server。
- POSIX helper socket 与密码文件权限/uid 测试用 `#[cfg(unix)]` 编写；Windows 门禁不执行，
  需在 Linux/WSL 复跑 `cargo test -p um-console --test display_rfb`。
- 生产 8002/8097 切换与回滚不在本票；Rust 候选仍只绑临时 loopback 端口。
