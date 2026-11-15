# Rust S8 验收记录：账本、Cloudflare 与部署备份 parity（#57）

- 状态：实现完成；生产工具未切换，Python 仍是回滚权威。
- 分支：`rust/57`；基线：`integration/rust-rewrite` @ `521a241`。
- 实现：`rust/crates/ops`（crate `um-ops`，二进制 `um-ops`）。
- 契约来源：[docs/rust-contract.md](rust-contract.md) §9、§11、§12；[docs/rust-plan.md](rust-plan.md) S8。

## 1. 交付范围

| 验收条目 | 实现 | 定向测试 |
|---|---|---|
| AC1 账本读面字段/筛选/游标/coverage 与 Python 一致；缺失不折 0 | `src/ledger.rs` | `tests/ledger_parity.rs`（8 条） |
| AC2 Cloudflare 配置校验与公网 DNS/TLS 检查与 Python 一致；测试注入夹具 | `src/ingress.rs`、`src/public_probe.rs` | `tests/cloud_parity.rs`（7 条） |
| AC3 部署装配检查与备份/恢复 CLI 的 JSON/归档/权限/退出码与 Python 一致 | `src/deploy.rs`、`src/main.rs` 的转发 | `tests/deploy_parity.rs`（6 条） |
| AC4 默认继续用 Python；Rust 只消费同一 CLI/JSON 契约 | `src/main.rs` 只读命令用 Rust，状态/网络命令转发 Python/bash | `tests/test_rust_s8_boundaries.py`、`the_public_probe_is_forwarded_to_the_python_worker` |
| AC5 不切生产工具；不自动执行备份/恢复 | 无 unit/script/manifest 改动；无 timer | `tests/test_rust_s8_boundaries.py`（10 条） |
| AC6 `bash um-cloud-deploy/run-linux-checks.sh --mode full` 通过 | 无生产切换 | 见 §3 |

## 2. 实现要点

- 账本读面完整移植 `share/metering.py` 的汇总/逐请求页：未知用量保持 `null`，
  不折 0；`coverage.ledgerPresent`/`ledgerReadable`/`truncated`/
  `olderRecordsUnread` 与 Python 同口径；私有 `usage-chat2api.jsonl` 直接拒绝；
  只读打开调用方给定的那一个账本，不写、不创建、不截断。
- Cloudflare ingress 校验完整移植 `check-ingress.py`：4 条白名单路径、8003 目标
  service（当前 Python 仍为 8100）、最后一条 `http_status:404`、禁止
  8096/8097/18096/5901、重复/缺失路径与 host 校验；只返回
  `{ok, problems, rules}`，不写、不 reload、不 restart。
- 公网 DNS/TLS 状态机移植 `console/public_probe.py` 的纯逻辑：origin 严格解析、
  两个 DoH 源聚合、`is_global` IPv4 过滤、TTL/negative TTL、`unchecked`、
  `dns_disagreement`/`dns_unconfirmed`/`addresses_truncated` 与退出码 0/1/2。
  `check_public_connection` 通过 `ProbeTransport` 注入 DNS/TLS 事实；测试不访问真实公网。
  生产公网探测仍走 Python worker（`um-ops cloudflare probe` 转发）。
- 部署装配检查完整移植 `backup_restore.deployment_assembly_problems` 与
  `deployment_paths`（含 console.env 的 share-secret 路径优先级）。
  `backup`/`restore`/`freshness`/`retry-pending`、`cloudflare probe`、
  `deploy assembly`、`deploy update`/`rollback` 都只转发现有 Python/bash CLI：
  参数、stdout/stderr、归档字节、文件权限与退出码原样透传；Rust 不实现第二套
  AES-GCM 归档，也不自动执行备份/恢复。
- 生产 unit、`install.sh`、`update.sh`、`rollback.sh` 与
  `RELEASE-MANIFEST.json` 未改；`um-ops` 不进入发布闭包。

## 3. 门禁结果（2026-11-15）

工作目录 `.worktrees/rust-57`（Windows，`cargo 1.100.0`、`rustc 1.100.0`；
Python 3.14.6；Git Bash 5.x）。

| 命令 | 实际结果 |
|---|---|
| `cargo fmt --manifest-path rust/Cargo.toml -- --check` | 退出码 0 |
| `cargo clippy --manifest-path rust/Cargo.toml --all-targets --offline` | 退出码 0，无 warning |
| `cargo test --manifest-path rust/Cargo.toml --offline` | 全部 `test result: ok`；`ledger_parity` 8 passed，`cloud_parity` 7 passed，`deploy_parity` 6 passed |
| `python -m unittest tests.test_rust_ops_fixture tests.test_rust_s8_boundaries tests.test_rust_gateway_fixture tests.test_rust_store_fixture tests.test_rust_broker_fixture tests.test_rust_contract_fixture` | 退出码 0 |
| `python -m unittest tests.test_share_metering tests.test_share_usage_results tests.test_cloud_ops tests.test_cloud_edge_ingress tests.test_cloud_deploy_assembly tests.test_manifest_stability` | `Ran 93 tests`；`OK (skipped=3)` |
| `python um-cloud-deploy/cloudflared/check-ingress.py --config um-cloud-deploy/config/cloudflared.yml.example --expect-host dacintapi.de5.net` | 退出码 0；`{"ok": true, "problems": [], "rules": [...5 条...]}` |
| `python um-cloud-deploy/backup_restore.py --check-deployment-assembly console um-cloud-deploy/systemd/um-share-console.service <临时 console.env + UM_SHARE_UID=1000>` | 退出码 0；`{"ok": true, "role": "console", ...}` |
| `bash scripts/check-rust-s8.sh` | 退出码 0；fmt/clippy/cargo test/夹具新鲜度/diff check 全通过 |
| `bash um-cloud-deploy/run-linux-checks.sh --mode full D:/tmp/umlinux` | 退出码 0；`core: exit 0`、`long: exit 0`、`ops: exit 0`、`overall: exit 0` |
| `bash scripts/check.sh --full` | 退出码 0；`local/tests` `Ran 54 tests ... OK`，其余 compile/ruff/名字/夹具/tsc/前端契约检查全通过 |

补充说明：本机 WSL `UM-Cloud-P1` 发行版的 VHDX 已不在
`D:/wsl/UM-Cloud-P1/ext4.vhdx`，WSL 无法启动该发行版；因此
`run-linux-checks.sh --mode full` 在 Windows Git Bash 中运行。`python3`
在 Git Bash 默认指向 WindowsApps 的 Python，缺 `cryptography`；本次用临时
PATH shim 让 `python3` 指向已安装 `cryptography` 的 Python 3.14.6。core/long/ops
三个场景均通过。该记录不是 WSL/Linux 发行版上的运行结果，未在真实 Linux
内核上复测 AF_UNIX/SO_PEERCRED。

## 4. 夹具

`tests/fixtures/rust_ops_rev1.json` 由
`tests/generate_rust_ops_fixture.py` 从真实 Python 实现生成：

- 账本：11 条有效记录 + 坏行/空行、两个 Key、未归属/legacy、未知用量、
  cache reported/unreported/unknown、中断/失败、无时间记录、limit 0/2/200000、
  `today`/`24h`/`unattributed`/`result` 筛选、逐请求游标页、缺失/空账本、
  200001 行截断、私有账本拒绝与 13 个非法筛选。
- Cloudflare：example 配置、缺/重复路径、错 host、缺/多个/带 host 的 catch-all、
  错 service、私有端口、非白名单路径、非 mapping 规则、缺/空 ingress、
  非 mapping 根、无效 YAML 与不可读文件；两个 DoH 源聚合、地址/negative TTL
  解析、`summarize` 7 种状态、退出码与 deadline 边界；注入 DNS/TLS 后的
  `check_public_connection` 8 种结果。
- 部署：console/broker 装配通过/缺失/未知角色、`deployment_paths` 三种
  share-secret 路径与不可读 console.env、真实 Python `--check-deployment-assembly`
  的 ok/missing/usage JSON 与退出码、`update.sh`/`rollback.sh` 的 help/usage 退出码。

Rust 测试只读该夹具；夹具新鲜度由 `tests/test_rust_ops_fixture.py` 调用生成器比对。

## 5. 边界与未验证范围

- 未切换生产 8002/8003/8097；未安装 `um-ops`，未改 systemd unit、部署脚本或
  发布 manifest。
- 未自动执行备份、恢复、更新或回滚；测试里的 Python/bash 都是显式调用。
- 未在真实 Linux/WSL 上运行 `run-linux-checks.sh --mode full`；本机 WSL 发行版
  无法启动，已在 Git Bash 中运行并通过，详见 §3 补充说明。
- 公网 DNS/TLS 的真实网络路径未在 Rust 中单独实现：`um-ops cloudflare probe`
  转发 Python worker；Rust 只测试注入事实的状态机。
- 未执行真实 Cloudflare Tunnel 变更、真实 DNS 变更、生产备份/恢复或 UM 登录。
