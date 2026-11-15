# Rust S5b 验收记录：8002 分享、用量、日志与 display 读面（#52）

- 状态：实现完成；候选进程只监听临时 loopback 端口，未切换生产 8002。
- 分支：`rust/52`；基线：`integration/rust-rewrite` @ `521a241`。
- 实现：`rust/crates/console`（crate `um-console`，二进制 `um-console-candidate`）。
- 契约来源：[docs/rust-contract.md](rust-contract.md) §6、§9、§10；[docs/rust-plan.md](rust-plan.md) S5。

## 1. 交付范围

| 验收条目 | 实现 | 定向测试 |
|---|---|---|
| AC1 `share-management/status` 的 `gateway_running` 三态 | `share_read.rs::status`、`ShareReadSeams` | `share_status.rs` |
| AC1b `share-management/control-status` 的 `{responsive,error_code}` 与 405 | `share_read.rs::control_status`、`server.rs` 路由 | `control_status.rs` |
| AC2 `keys/{id}/secret` 四种结果 | `share_read.rs::secret` | `share_secret.rs` |
| AC3 `share-usage` 汇总/逐请求/筛选/游标/coverage | `metering.rs`、`share_read.rs::read_key_labels` | `share_usage.rs` |
| AC4 `um/*`、`usage-tokens/rates`、`logs`、`log-*`、`gateway-connection`、`cloud-ops`、`display-session/state` | `cloud_ops.rs`、`display.rs`、云端 `capability_not_assembled` 门 | `console_reads.rs` |
| AC5 `/api/share/*` 只读面走会话/控制令牌；公网不反代 | `share_auth.rs`、`server.rs::handle_share_read` | `api_share.rs` |
| AC6 读面不写 SQLite/账本/日志；日志/账本路径不进入响应 | 全部读面只读打开；`metering.rs` 只给账本文件名 | `readonly_side_effects.rs` |
| AC7 候选 8002 只监听临时 loopback 端口 | `config.rs::validate_candidate`、`main.rs` | `candidate_process.rs` |

## 2. 实现要点

- `share_read.rs` 只读打开调用方给定的 `share-store.db`；库缺失时 `500 share management error`，
  不创建、不迁移。`status` 保留 `ShareManager.status()` 的字段，并叠加
  `share_url_source`/`share_url_reason`、`gateway_running`、`attribution`、`capacity`、
  `last_check_at`。
- `gateway_running` 只在 broker 读缝明确回答 `True`/`False` 时出现；未确认（`None`）省略，
  绝不折成 `false`。`capacity.in_flight` 未接线时为 `null` + `maintenance_unwired`，不写 `0`。
- `keys` 只列活动身份，映射为 `{id,name,masked,plaintext_viewable,enabled,max_concurrency,
  row_version,created_at}`；明文/哈希永不进入列表。
- `secret` 的四种结果：200 明文、200 `secret_unavailable` + `reason=legacy|undecryptable|
  version_conflict`、410 `key_deleted`、404 `key identity not found`；另保留 400
  `invalid expected_row_version`。明文只出现在该即时 no-store 响应。
- `metering.rs` 是 `share/metering.py` 的只读移植：同一账本、同一窗口/Key/模型/结果筛选、
  同一 `cursor` 行号分页、同一 `coverage`；未知用量/缓存/时刻保持 `null`，不折零。
  私有账本 `usage-chat2api.jsonl` 被拒绝；`coverage.ledger` 只给文件名。
- `/api/console/share-usage` 与 `/api/share/usage` 共用同一 reader 与同一 Key 名称投影。
- 云端角色对 `um/*`、`usage-tokens`、`usage-rates`、`logs`、`log-sources`、`log-entries`、
  `log-overview`、`gateway-connection`、`stats` 返回 `503 capability_not_assembled`，
  不探测、不读取非 UM 资源。
- `cloud_ops.rs` 只读安全状态文件：缺失配置 `configured=false`，不可读状态
  `available=false` + `security_unavailable`，绝不伪造“无撤销”成功。
- `display.rs` 在候选未装配 display relay 时返回 `display_disabled` 的诚实不可用；
  生产入口按 S7 的 `DisplayConfig::availability` 装配后，启用时 `display-session` 返回
  `available=true`/`url=/display/`，未启用时才返回 `display_disabled`；
  `display-state` 按 Python 的 `_console_display_state_of` 映射认证观察。
- `control-status` 只读 broker 快照：`state=ok` 且带 `broker` 对象时 200
  `{responsive:true}`；未接线为 `share_broker_unwired`，其余未确认为
  `share_broker_unavailable`；非 GET 返回 405 `method not allowed`。
- `share-management/status` 的 `last_publication_failure` 来自最新失败的 `publish`
  operation（`{kind,error,at}`）；后续发布成功不会清空它，与 Python `_last_failure`
  的保留语义一致。
- `/api/share/*` 读面先做敏感头重复检查，再要求会话 Cookie 或控制令牌；写方法在 S5b
  返回 `501 not_implemented`，不伪造写成功。公网入口仍只有 8003 的四条 `/v1` 路径。
- 候选继续只绑定 `127.0.0.1:0`；`USAGESRV_PORT` 只作为报告端口，8002/8003 与治理段
  端口在 bind 前拒绝。

## 3. 门禁结果（2026-11-15）

已运行：

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --offline
cargo test --manifest-path rust/Cargo.toml --offline
python -m unittest tests.test_rust_console_reads_fixture
python -m unittest tests.test_console_share_facade tests.test_console_share_production \
  tests.test_cloud_ops tests.test_cloud_ops_wiring tests.test_gateway_connection_info \
  tests.test_um_stats_read_surface tests.test_console_log_reader tests.test_console_stats
cd web && npm run check:cloud-console
cd .. && bash scripts/check.sh --full
bash scripts/check-rust-s5b.sh
git -c core.whitespace=cr-at-eol diff --check
```

实际结果：

- `cargo fmt --check`：退出码 0。
- `cargo clippy --all-targets --offline`：退出码 0，无 warning。
- `cargo test --offline`（整个 Rust workspace）：全部 `test result: ok`，无 FAILED。
- `um-console` 定向测试：`candidate_process` 4、`console_readonly` 18、`console_reads` 6、
  `readonly_side_effects` 6、`request_boundary` 5、`runtime_facts` 8、`share_secret` 4、
  `share_status` 4、`share_usage` 7、`api_share` 5。
- `python -m unittest tests.test_rust_console_reads_fixture`：Ran 2 tests，OK；夹具由
  `tests/generate_rust_console_reads_fixture.py` 从真实 Python 云端 `usagesrv.Handler` 生成。
- ticket 的 Python parity 门禁：Ran 253 tests，OK。
- `cd web && npm run check:cloud-console`：71 pass，0 fail。
- `bash scripts/check-rust-s5b.sh`：退出码 0。
- `bash scripts/check.sh --full`：退出码 0；`Ran 2371 tests` / `OK (skipped=63)`，
  `local/tests` `Ran 54 tests` / `OK`。
- `git -c core.whitespace=cr-at-eol diff --check`：退出码 0。

## 4. 夹具

`tests/fixtures/rust_console_reads_rev1.json` 记录：

- `shareStatus`：三态 `gateway_running`（true / false / 省略）、broker 映射、capacity、
  share_url、attribution 与 `last_check_at` 类型；以及 `share-store.db` 的 schema/rows/blob hex。
- `shareKeys` 与 `shareSecret`：活动/已删除/legacy/undecryptable/rotated 行的列表与
  readback 四种结果；密钥文件 hex 由夹具给出。
- `shareUsage`：固定读时刻的汇总与逐请求页（窗口/Key/模型/结果筛选、cursor、coverage），
  以及 HTTP 的状态码/时间无关 body。
- `ac4`：cloud-ops 的未配置/有效/损坏三种结果，display-session 与 display-state 的
  认证观察映射。
- `capabilityExplicit`：14 个显式非 UM 路径的 `503 capability_not_assembled`。
- `apiShare`：控制令牌与会话 Cookie 的读面、缺失/错误/重复凭据与跨源 Origin。

## 5. 边界与未验证范围

- S5b 不装配 broker 客户端、display relay、日志读取器或 UM 统计读取器；这些在云端角色
  返回 `503 capability_not_assembled`，与 Python 云端角色一致。`gateway_running` 与
  attribution 来自可注入读缝，生产接线留给后续切片。
- `display-session`/`display-state` 的可用性由 S7 的 `DisplayConfig` 经读缝提供；本切片的
  `display.rs` 只负责映射，display grant/session/WebSocket 由 S7 承担。
- `/api/share/*` 写面未实现（`501 not_implemented`），由 S6 承担；本切片不写
  `share-store.db`、账本或日志。
- 未在真实 Linux `/proc`、真实云端 `share-store.db` 或真实 8002 进程上验证；夹具使用
  临时目录与固定读时刻。
- 未切换云端 8002、未重启 broker、未执行真实部署或 UM 登录。

## 6. 审查修复（2026-11-16）

- Finding 6：新增 `GET /api/console/share-management/control-status`，按
  `usagesrv.py:2802-2819` 从 broker 快照返回 `{responsive}` 或
  `{responsive:false,error_code}`；非 GET 返回 405。
- Finding 8：`share-management/status` 与 `/api/share/status` 的
  `last_publication_failure` 从最新失败 `publish` operation 读取
  `{kind,error,at}`；发布成功不清空，保留语义与 Python `_last_failure` 一致。
- 定向测试：`cargo test -p um-console --test control_status --test share_status
  --test share_write_fail_closed --offline` 通过。
