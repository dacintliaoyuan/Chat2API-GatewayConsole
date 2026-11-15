# Rust S6a 验收记录：8002 operations 与分享 Key 写面（#53）

- 状态：实现完成；候选进程只监听临时 loopback 端口，未切换生产 8002。
- 分支：`rust/53`；基线：`integration/rust-rewrite` @ `8c45259`。
- 实现：`rust/crates/console`（crate `um-console`）、`rust/crates/store`（共享 schema 3 写入 API）。
- 契约来源：[docs/rust-contract.md](rust-contract.md) §6、§8；[docs/rust-plan.md](rust-plan.md) S6。

## 1. 交付范围

| 验收条目 | 实现 | 定向测试 |
|---|---|---|
| AC1 `POST /api/console/operations` 201/200、取消与状态 | `operation_manager.rs`、`server.rs::handle_console_write` | `operations_write.rs` |
| AC2 Key create/patch/delete/rotate/revoke-and-terminate | `share_write.rs`、`server.rs::handle_share_management_write` | `share_write.rs` |
| AC3 `PATCH settings` 1..64、row_version 409、idempotency 409 | `share_write.rs::set_share_limits`、`store.rs` | `share_write.rs` |
| AC4 明文只在 create/rotate 一次性 no-store 响应 | `share_write.rs` 的明文映射、`server.rs` 的 no-store 响应 | `share_write_redaction.rs` |
| AC5 撤销 fail closed、`key_deleted` 410、`secret_unavailable` | `share_write.rs::require_applied` / `fail_closed` | `share_write_fail_closed.rs` |
| AC6 不 port `backend-login-cancel`，返回 404 | 写路由白名单不含该路径 | `share_write_routes.rs` |
| AC7 写面失败切回 Python；数据格式不变、不迁移 | 写面只开调用方给定的 schema 3 文件；候选不切生产 | `share_write_data_format.rs` |

## 2. 实现要点

- `console-state.sqlite3` 仍是 schema 1：`console_meta` + `operations`，提交/取消/列表/详情字段与
  `console/operations.py` 的 `_view` 一致。资源锁是打开句柄/flock 本身；进程死亡自动释放。
- 操作管理器惰性装配：只读面不创建数据库、不把未完成行标 `interrupted`；首次写请求才按 Python
  `get_console_manager()` 的行为建库并标记。
- 动作目录固定为云端六个 UM 分享动作；执行器是注入缝。未接线的候选对特权动作回答
  `share_broker_unwired`，不伪造成功。
- 分享 Key 写面只打开调用方给定的 `share-store.db`（schema 3），不建第二套库、不迁移数据；
  投影 schema 仍是 2。
- 发布链：每次变更先写本地 SQLite 的 pending publish intent，再经注入的 broker 发布；ack 必须确认
  精确目标代次与安全水位。撤销方向（rotate/delete/revoke/降低并发）未确认即 503，并记录
  `blocked`/`emergency_stop`，绝不返回假 200。
- 明文只映射进 create/rotate 的即时响应；列表、状态、操作记录、错误和 URL 都不含明文。重放
  create/rotate 只返回元数据，`plaintext` 为 `null`。
- `backend-login-cancel` 没有写路由，POST/GET 都按未知路径 404，不 port #34 已退役动作。

## 3. 门禁结果（2026-11-16）

已运行：

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --offline
cargo test --manifest-path rust/Cargo.toml --offline
python -m unittest tests.test_console_operations tests.test_console_share_facade \
  tests.test_console_share_actions tests.test_console_share_secret_redaction \
  tests.test_console_share_production tests.test_share_management \
  tests.test_share_limits_contract
cd web && npm run check:cloud-console
cd .. && bash scripts/check.sh --full
bash scripts/check-rust-s6a.sh
git -c core.whitespace=cr-at-eol diff --check
```

实际结果：

- `cargo fmt --check`：退出码 0。
- `cargo clippy --all-targets --offline`：退出码 0，无 warning。
- `cargo test --offline`（整个 Rust workspace）：全部 `test result: ok`，无 FAILED。
- `um-console` 定向测试：`operations_write` 13、`share_write` 12、`share_write_redaction` 1、
  `share_write_fail_closed` 2、`share_write_routes` 1、`share_write_data_format` 2。
- ticket 的 Python parity 门禁：Ran 261 tests，`OK (skipped=1)`。
- `cd web && npm run check:cloud-console`：71 pass，0 fail。
- `bash scripts/check.sh --full`：退出码 0；`Ran 2385 tests` / `OK (skipped=64)`，`local/tests` `Ran 54 tests` / `OK`。
- `git -c core.whitespace=cr-at-eol diff --check`：退出码 0。

## 4. 数据格式与回退

- Rust 写入的是 Python 创建的同一种 `share-store.db` schema 3；`store.rs` 的 schema/投影 DDL 与
  `share/store.py` 逐列一致。AC7 用 S5b 夹具中由真实 Python `ShareStore` 生成的库做种子，
  Rust 写入后 `schema_version=3`、投影 schema 仍为 2，Python 种子 Key 仍可读。
- 写面不创建 `console-state.sqlite3`（分享 Key 路由不写操作记录），也不写账本或日志。
- 写面失败时切回 Python 8002：候选二进制只绑定 `127.0.0.1:0`，不切生产端口；旧 release、
  `share-store.db` 与投影文件都不迁移、不删除。
- 未接线发布时安全方向（create/提高并发）仍返回 `published=false` + `publication_error`，
  撤销方向 503；回退不需要改数据。

## 5. 边界与未验证范围

- 未装配真实 broker 客户端、真实 8003 进程或真实 display relay；候选写面的发布经注入缝，
  生产接线留给 S6 整体切换。
- 未在真实 Linux `/proc`、真实云端 `share-store.db` 或真实 8002 进程上验证；测试使用临时目录、
  临时 loopback 端口与固定夹具。
- `keys/{id}/test`、`test-private`、display grant/operator 与会话路由不在本票范围（S6b / S7）。
- 未切换生产 8002、未重启 broker、未执行真实部署或 UM 登录。
