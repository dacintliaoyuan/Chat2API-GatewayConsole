# Rust S0 验收记录（#43）

- 状态：完成。S0 只建立 Rust workspace、HTTP 测试 harness、临时数据目录与
  由 Python 生成器产出的契约夹具；**没有切换任何生产端口**。
- 输入：`docs/rust-contract.md`、`docs/rust-plan.md` S0。
- 范围：`rust/`、`tests/generate_rust_contract_fixture.py`、
  `tests/fixtures/rust_contract_rev1.json`、`tests/test_rust_contract_fixture.py`、
  `scripts/check-rust-s0.sh`。

## 交付

| 交付 | 位置 | 说明 |
|---|---|---|
| Rust workspace | `rust/Cargo.toml` | `um-contract` 与 `um-test-harness` 两个 crate |
| HTTP 测试 harness | `rust/crates/test-harness` | 默认 `127.0.0.1:0`，内核分配临时端口；显式生产端口在 bind 前拒绝 |
| 临时数据目录 | `rust/crates/test-harness` | 只在 OS 临时目录创建；拒绝 `..`；`close()`/drop 删除 |
| 契约夹具 | `tests/fixtures/rust_contract_rev1.json` | 由 `tests/generate_rust_contract_fixture.py` 生成 |
| 夹具读取 | `rust/crates/contract` | 只反序列化夹具；端点、错误码、SSE 事件与预算不写在 Rust 里 |
| 隔离证明 | `rust/crates/test-harness/tests/isolation.rs` | 临时端口不在生产端口集合；临时目录不在生产数据根；失败候选不留下临时状态 |

夹具覆盖：

- 端点：8003 分享网关与 8002 云端控制台的端点表（含方法、路径、鉴权与来源）。
- 错误码：网关、控制台错误表与 broker 固定结果码。
- SSE：Chat 透传、Messages 终止/错误事件、Responses 事件序列。
- 超时/取消/重试：首字节、socket 静默、请求总预算、预提交/未成行缓冲、
  队列/reader 上限、broker 预算、display 预算与 deploy 排空预算。
- 幂等：操作幂等键、Key create/rotate 重放、broker `request_id`、
  display grant/operator。
- 副作用：账本/日志/撤销状态/实例通道、控制台 SQLite/安全日志/投影发布、
  broker 投影原子写、display 运行目录、Cloudflare 只读、部署 release 写入；
  禁止边界单独列出。

## 门禁结果

以下命令均在本次 S0 实施中实际运行；工作目录为仓库根
`.worktrees/rust-43`（Windows，`cargo 1.100.0`，`rustc 1.100.0`）。

| 命令 | 实际结果 |
|---|---|
| `python tests/generate_rust_contract_fixture.py` | 生成 `tests/fixtures/rust_contract_rev1.json` |
| `node web/scripts/backend-contract/check-generated.mjs` | `result: PASS`；`liveRequests: 0` |
| `python -m unittest tests.test_rust_contract_fixture` | `Ran 4 tests`；`OK` |
| `cargo test --manifest-path rust/Cargo.toml` | 契约 3 + harness 4 + 隔离 3，共 10 个测试通过 |
| `python -m unittest tests.test_retired_features tests.test_check_gate` | `Ran 11 tests`；`OK` |
| `bash scripts/check-rust-s0.sh` | 退出码 0 |
| `bash scripts/check.sh --quick` | 退出码 0 |
| `git -c core.whitespace=cr-at-eol diff --check` | 退出码 0 |

## 无生产端口切换

- Rust harness 只绑定 `127.0.0.1:0`；`HarnessConfig::forbidden_ports` 在
  bind 前拒绝生产端口，测试用夹具里的 `harness.productionPorts` 证明实际端口
  不在 8000/8001/8002/8003/8097/8099/5173 集合内。
- 本票没有修改 systemd unit、部署模板、端口默认值或 `current` 符号链接；
  没有启动 `chat2api.py`、broker 或 display relay。
- 没有读取或写入 `data/`、`profile/`、`tmp/`、`output/`、
  `chat2api/token.json`、`chat2api/.edge-profile`、`/var/lib/um-share`、
  `/var/lib/um-share-console`、`/etc/um-share`。

## 失败即删除候选

- `TestServer` 的 drop/shutdown 会停止接受并 join 线程；harness 测试验证
  端口在 shutdown 后释放。
- `TempDataDir` 在 `close()`/drop 时删除整棵临时树；测试验证写入的
  `candidate.json` 与目录一起消失。
- 生产端口启动在 bind 前失败（`HarnessError::ProductionPort`），不会留下
  监听或临时目录。

## 未验证范围

- 未运行 `bash scripts/check.sh --full`；本票 parity 门禁只要求
  `check-generated`、两个定向 unittest 与 diff check。
- 未在云端或 WSL/Linux 运行 `um-cloud-deploy/run-linux-checks.sh`。
- 未启动真实 Rust 候选进程、未切换 8002/8003/8097、未执行 UM 登录或
  Cloudflare/备份操作。
- 夹具冻结的是当前 Python 行为与冻结契约；后续 S1–S8 的 parity 测试仍需
  逐片运行各自的定向门禁。
