# Rust S3b 验收记录（#49）

- 状态：实现完成；生产端口未切换，Python 仍是回滚权威。
- 输入：`docs/rust-contract.md` §7–§8、`docs/rust-plan.md` S3、ticket #49。
- 范围：`rust/crates/store/`、`rust/crates/broker/`（publish/host）、
  `share/broker_ops.py` 的 `--host-entry-kind`、`tests/generate_rust_store_fixture.py`、
  `tests/fixtures/rust_store_rev1.json`、`tests/test_broker_start_identity.py`、
  `tests/test_rust_s3b_boundaries.py`、`tests/test_rust_store_fixture.py`、
  `scripts/check-rust-s3b.sh`。

## 交付

| 交付 | 位置 | 说明 |
|---|---|---|
| SQLite schema 3 | `rust/crates/store/src/schema.rs` | 与 `share/store.py` 逐列同 DDL；v1→v2→v3 单事务迁移 |
| Key 生命周期 | `rust/crates/store/src/store.rs` | create/rename/update/rotate/delete/verify/reveal、row_version、idempotency |
| 安全日志 | `store.rs` | 与 Key 变更同事务的 revoke/rotate/delete/limit_lower 意图、ACK 水位 |
| 投影原子写 | `rust/crates/store/src/projection.rs` | 共享解析器预检、tmp+replace、回读校验、generation 单调 |
| POSIX 密文 | `rust/crates/store/src/secret.rs` | `aesgcm-file-key-v1`，与 Python 同 blob 布局；DPAPI 行按不可解密失败关闭 |
| publish | `rust/crates/broker/src/publish.rs` | writer→load→ACK；实例/注册版本绑定；`terminate_keys` 一次性 |
| 有界生命周期 | `rust/crates/broker/src/host.rs` | start/drain/emergency/gateway-restart/maintenance-*/host-* 预算与证据 |
| 8003 启动身份 | `share/broker_ops.py` | `--host-entry-kind {python,native}`；默认 python 保留旧 unit 回滚 |
| 夹具 | `tests/generate_rust_store_fixture.py` | 由真实 Python store/协调器/解析器/AES-GCM 生成 |

## 验收标准对应

| # | 验收条目 | 实现 | 定向测试 |
|---|---|---|---|
| 1 | SQLite schema 3 与 Python 一致，不迁移数据格式 | `schema.rs`、`store.rs` | `rust/crates/store/tests/schema_parity.rs`：`a_fresh_store_creates_the_python_schema_3`、`a_schema_1_store_migrates_like_python_without_touching_key_data`、`an_unsupported_schema_version_is_refused_instead_of_rewritten` |
| 2 | Key 生命周期、安全日志、投影原子写与 Python 一致 | `store.rs`、`projection.rs`、`secret.rs` | `rust/crates/store/tests/lifecycle_parity.rs`：`the_key_lifecycle_matches_the_python_store`、`the_plaintext_readback_is_narrow_and_fail_closed`；`rust/crates/store/tests/projection_parity.rs` |
| 3 | `publish` writer→load→ACK；generation/fingerprint 单调；`terminate_keys` 一次性 | `publish.rs` | `rust/crates/broker/tests/publish_parity.rs`：`the_publish_chain_matches_python`、`terminate_keys_is_forwarded_only_when_present`；`projection_parity.rs` 的 writer 单调测试 |
| 4 | start/drain/emergency-stop/gateway-restart/maintenance-*/host-* 有界预算与证据 | `host.rs` | `rust/crates/broker/tests/host_parity.rs`：`the_bounded_lifecycle_matches_python`、`a_verified_closure_clears_the_lease_and_bumps_the_epoch`、`host_verify_and_stop_require_verified_evidence`、`limits_repair_and_host_evidence_never_fabricate` |
| 5 | broker 8003 启动身份可配置，保留旧 Python 入口 | `share/broker_ops.py` | `tests/test_broker_start_identity.py`（6 条） |
| 6 | 不新增第二套 SQLite、第二个 broker、第二个 8003 启动者 | `store/schema.rs`、`broker_ops.py` | `tests/test_rust_s3b_boundaries.py`（4 条）、`rust/crates/store/tests/single_database.rs`（2 条） |
| 7 | 回滚切回旧 broker unit 与旧 Python 入口；不迁移数据 | `um-cloud-deploy/systemd/um-share-broker.service` 未改；`--host-entry-kind` 默认 python | `tests/test_rust_s3b_boundaries.py`：`test_the_rollback_path_is_the_old_python_entry`；`single_database.rs` |

## 门禁结果

工作目录 `.worktrees/rust-49`（Windows，`cargo 1.100.0`、`rustc 1.100.0`）。

| 命令 | 实际结果 |
|---|---|
| `python -m unittest tests.test_share_store tests.test_share_store_key_reveal tests.test_share_management tests.test_share_management_wiring tests.test_share_security_generation tests.test_share_revoke_state tests.test_share_secret_protection tests.test_share_limits_contract` | `Ran 259 tests`；`OK (skipped=14)` |
| `bash um-cloud-deploy/run-linux-checks.sh --mode core D:/tmp/umlinux` | 退出码 0；`scenario core: PASS` |
| `bash scripts/check.sh --full` | 退出码 0；`tests` `Ran 2367 tests in 736.795s` / `OK (skipped=63)`，`local/tests` `Ran 54 tests in 20.128s` / `OK`；新增 S3b 测试另经 discover 运行：`test_rust_s3b_boundaries` 4 条、`test_broker_start_identity` 6 条均 `OK` |
| `bash scripts/check-rust-s3b.sh` | 退出码 0；fmt、clippy、`cargo test`、Android `cfg(unix)` 类型检查、夹具新鲜度与 diff check 全通过 |
| `cargo test --manifest-path rust/Cargo.toml` | 39 个 test result 行全部 `ok`；S3b 的 store/broker 定向测试均在其中 |

## 未验证范围

- 未在真实云端切换 8003/8002/8097；`--host-entry-kind native` 只是 broker 配置，
  Rust 8003 生产切换属于 S4（#50）。
- 未在 Linux 上执行真实 AF_UNIX/SO_PEERCRED 测试；Windows 上相关测试由
  `#![cfg(unix)]` 收集为 0。
- 未连接真实 UM 上游、未执行真实登录；未运行生产 systemd/Cloudflare/备份恢复。
- `bash um-cloud-deploy/run-linux-checks.sh --mode core` 在 Windows 上需要显式
  传入可写的 evidence 路径（默认 `/mnt/d/...` 会被 Git Bash 解析到
  `C:\Program Files\Git\mnt\d`）；本记录使用 `D:/tmp/umlinux`。
