# Rust S3a 验收记录（#48）

- 状态：实现完成；生产端口未切换，Python 仍是回滚权威。
- 输入：`docs/rust-contract.md` §7、`docs/rust-plan.md` S3a。
- 范围：`rust/crates/broker`、`tests/generate_rust_broker_fixture.py`、
  `tests/fixtures/rust_broker_wire_rev1.json`、`tests/test_rust_broker_fixture.py`、
  `scripts/check-rust-broker.sh`。

## 交付

| 交付 | 位置 | 说明 |
|---|---|---|
| 帧与结果码 | `rust/crates/broker/src/codec.rs`、`error.rs` | 单行 JSON、1 MiB 上限、18 个固定结果码 |
| Python 兼容序列化 | `codec.rs` | 与 `json.dumps(sort_keys=True)` 同字节（含 ensure_ascii 转义与浮点指数） |
| HMAC | `hmac.rs` | HMAC-SHA256、常量时间比较、signed keys 子集 |
| 请求校验 | `request.rs` | 版本、action、±300 秒、request_id/instance_id、字段白名单 |
| replay | `replay.rs` | `request_id` 一次性消费、过期回收、表满拒绝 |
| 管理分发 | `management.rs`、`server.rs` | 校验 -> replay -> handler，单连接一周期 |
| 实例通道 | `instance.rs` | 只应答 `instance-status`/`load`/`drain`/`emergency`/`resume` |
| 固定 action | `actions.rs` | 管理 14 个、实例 5 个类型化 action 与专用字段枚举 |
| 预算 | `budget.rs` | 10/130/60/20/90/30 秒与总截止读取 |
| AF_UNIX 策略 | `socket_policy.rs`、`peer.rs` | 绝对路径/107 字节、目录 0700、socket 0600、活动端点拒绝、SO_PEERCRED |
| AF_UNIX 适配器 | `transport/unix.rs` | `UnixListener`/`UnixConnection`、模式、回收、`FrameIo` |
| 生成夹具 | `tests/generate_rust_broker_fixture.py` | canonical JSON、帧与 HMAC 向量由 Python 生成 |

## 验收标准对应

| 验收条目 | 实现 | 定向测试 |
|---|---|---|
| AF_UNIX 目录/权限/peer 身份与 Python 一致 | `socket_policy.rs`、`peer.rs`、`transport/unix.rs` | `socket_policy.rs`、`peer_identity.rs`、`unix_socket.rs`（Linux） |
| HMAC、时间窗、replay、超限、capability_missing、权限不足 fail closed | `hmac.rs`、`request.rs`、`replay.rs`、`management.rs` | `request_validation.rs`、`replay_and_dispatch.rs` |
| 固定 action 白名单，无 shell/任意路径/服务名 | `actions.rs`、`request.rs` | `actions.rs` |
| 一行 JSON、1 MiB、固定结果码表 | `codec.rs`、`error.rs` | `codec.rs` |
| 实例通道只应答五个动作 | `instance.rs` | `instance_channel.rs` |
| `request_id` 一次性；预算与 Python 一致 | `replay.rs`、`budget.rs` | `budgets.rs`、`replay_and_dispatch.rs` |
| 覆盖 forged MAC/stale ts/replay/超限/capability_missing/权限不足 | `server.rs` | `fail_closed_matrix.rs` |

## 门禁结果

工作目录 `.worktrees/rust-48`（Windows，`cargo 1.100.0`、`rustc 1.100.0`）。

| 命令 | 实际结果 |
|---|---|
| `python -m unittest tests.test_share_broker tests.test_share_broker_server tests.test_share_broker_unix_socket tests.test_share_broker_drain` | `Ran 168 tests`；`OK (skipped=28)`（28 条 Linux-only AF_UNIX/SO_PEERCRED 测试在 Windows skip） |
| `bash scripts/check-rust-broker.sh` | 退出码 0；含 fmt、clippy、`cargo test`（19 个 test result 行）、Android `cfg(unix)` 编译检查、夹具新鲜度与 diff check |
| `cargo test --manifest-path rust/Cargo.toml` | broker 126 个测试通过；S0 的 contract/test-harness 10 个测试通过 |
| `cargo check --manifest-path rust/Cargo.toml -p um-broker --target aarch64-linux-android --tests` | 退出码 0（只做 `cfg(unix)` 类型检查，不执行） |
| `bash scripts/check.sh --full` | 退出码 0；`tests` `Ran 2354 tests in 1024.205s` / `OK`；`local/tests` `Ran 54 tests in 21.507s` / `OK` |

## 未验证范围

- 本机是 Windows，AF_UNIX/`SO_PEERCRED`/真实权限位测试由 `#![cfg(unix)]` 编译门控：
  `tests/unix_socket.rs` 在 Windows 上收集到 0 个测试。Android target 只完成类型检查，
  没有在 Linux 上执行真实 socket 测试；WSL 当前不可用，Docker daemon 未运行。
- 未启动真实 broker、未切换 `um-share-broker.service`/`um-share-gateway.service`，
  未读写 `/run/um-share`、`/var/lib/um-share` 或 `/etc/um-share`。
- 未连接真实 8003/8002 进程；S3a 只实现控制通道，投影发布、Key 管理与 8003 启动身份留给 S3b。
