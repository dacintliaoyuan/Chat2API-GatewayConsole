# Rust S2a 验收记录（#46）

- 状态：完成。候选进程只监听临时 loopback 端口；**没有切换 8003**，没有启动
  `chat2api.py`、broker 或 display relay。
- 输入：`docs/rust-contract.md` §5、`docs/rust-plan.md` S2、ticket #46。
- 范围：`rust/crates/gateway/src/messages.rs`、`upstream.rs`、`ledger.rs`、
  `server.rs` 的接入，以及 `rust/crates/gateway/tests/messages_*.rs`。

## 交付

| 交付 | 位置 | 说明 |
|---|---|---|
| Messages 转发 | `rust/crates/gateway/src/messages.rs` | 非流式 JSON、Anthropic SSE 字节透传、事件终止语义、账本 |
| 上游客户端 | `rust/crates/gateway/src/upstream.rs` | `POST /api/v1/messages`，存储 bearer + Anthropic 头 |
| 账本翻译 | `rust/crates/gateway/src/ledger.rs` | Anthropic usage → 同一 `record_usage` 行形状 |
| 路由接入 | `rust/crates/gateway/src/server.rs` | `/v1/messages` 与 `/api/v1/messages` |
| 门禁脚本 | `scripts/check-rust-s2a.sh` | Rust 全量测试、fmt、clippy、夹具新鲜度 |

## 验收对照

| # | 验收条目 | 定向测试 |
|---|---|---|
| 1 | Bearer 或 `X-Api-Key` 鉴权与 Python 一致；两种头值不同时 401 | `messages_auth::bearer_and_api_key_are_the_same_share_credential`；`messages_auth::conflicting_credential_headers_are_401_before_any_upstream_request`；`messages_auth::malformed_or_repeated_credentials_fail_closed`；`messages_auth::the_api_key_shape_stays_messages_only` |
| 2 | `MESSAGES_FIELDS` 白名单；`max_tokens` 正整数；`stream` bool；未知字段拒绝 | `messages_validation::unknown_fields_are_rejected_without_an_upstream_request`；`messages_validation::max_tokens_is_required_and_must_be_a_positive_integer`；`messages_validation::stream_is_optional_but_must_be_a_bool_when_present`；`messages_validation::model_and_messages_keep_the_python_shape_rules`；`messages_validation::every_whitelisted_field_is_accepted` |
| 3 | SSE 事件序列与官方一致；`message_stop` 前 EOF 不算成功；`error` 事件立即终止 | `messages_stream::the_official_event_sequence_is_forwarded_byte_for_byte`；`messages_stream::eof_before_message_stop_is_not_a_success`；`messages_stream::an_error_event_terminates_immediately`；`messages_stream::a_message_delta_stop_reason_is_not_a_terminal_event`；`messages_ledger::a_stream_without_message_stop_is_recorded_as_incomplete`；`messages_ledger::an_error_event_is_recorded_as_an_upstream_stream_error` |
| 4 | 非流式 JSON 与流式字节透传均与 Python 一致 | `messages_forwarding::non_stream_json_is_relayed_unchanged_with_the_anthropic_headers`；`messages_forwarding::a_missing_stream_field_is_forwarded_unchanged`；`messages_forwarding::a_non_200_upstream_response_is_passed_through_unchanged`；`messages_stream::the_official_event_sequence_is_forwarded_byte_for_byte`；`messages_stream::a_chunked_upstream_sse_body_is_decoded_before_forwarding` |
| 5 | usage 账本行与 Python 同字段、同状态语义 | `messages_ledger::a_non_stream_row_translates_the_anthropic_usage_block`；`messages_ledger::a_non_stream_row_without_usage_keeps_the_estimate_and_coverage`；`messages_ledger::a_stream_row_translates_the_anthropic_usage_events`；`messages_ledger::a_stream_without_usage_keeps_the_local_estimate`；`messages_ledger::a_non_200_row_keeps_the_upstream_status_and_no_fabricated_usage`；`messages_ledger::a_ledger_write_failure_never_blocks_the_proxy` |
| 6 | `/api/v1/messages` 是本机兼容别名，不进入公网白名单 | `messages_aliases::the_alias_bypasses_the_share_key_gate_and_records_no_attribution`；`messages_aliases::the_share_v1_messages_route_still_requires_its_key`；`messages_aliases::the_alias_uses_the_local_key_when_the_local_key_is_required`；`messages_aliases::the_alias_is_not_in_the_public_share_whitelist` |

### 行为边界

- 上游请求固定为 `POST /api/v1/messages`，使用存储的 bearer；客户端
  `Anthropic-Version`/`Anthropic-Beta` 原样转发，客户端本地凭据不离开本进程。
- 分享模式接受 `Authorization: Bearer <key>` 与 `X-Api-Key: <key>`，两者解析到
  同一个 Key；同名头重复、空值、超 256 字符、Bearer 前缀错误、两值不同一律
  401 `invalid_api_key`。Chat 仍只接受 Bearer。
- 分享模式的 Messages body 只接受冻结的 13 个 `MESSAGES_FIELDS`；`max_tokens`
  必填且为正整数，`stream` 可选但必须是 bool；拒绝时不发上游请求。
- 非流式响应保留上游状态、`Content-Type`/`Content-Length`/`Cache-Control`
  与 body 字节；上游非 200 也原样透传。
- SSE 保留上游事件字节；提交门与 Chat 相同：`message_start`/`ping` 等元数据
  先暂存，首个文本 delta、tool action 或终止事件后提交 200。
- `message_stop` 是成功终止；`message_delta` 携带 `stop_reason` 不是终止信号；
  没有 `message_stop` 的 EOF 记为 `upstream_eof_without_event`，不补造事件。
- `error` 事件立即终止转发，记为 `upstream_stream_error`，不把部分完成当成功。
- 账本行 `protocol="messages"`，`stream` 取客户端请求；输入侧由
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` 翻译为
  `prompt_tokens`，cache read 进入 `cached_input_tokens`；输出侧由
  `message_delta.usage.output_tokens` 取累计最大值。
- 未报告 usage 的流保留本地估算与 `estimated`/`usage_source` 覆盖；账本写失败
  只关闭本进程记账并打印一行错误，不阻断代理。
- `/api/v1/messages` 不经分享 Key 门，非分享模式按本地 Key 规则；公网白名单
  仍只有四条 `/v1` 路径。

## 门禁结果

以下命令在本次实施中实际运行，工作目录 `.worktrees/rust-46`
（Windows，`cargo 1.100.0`，`rustc 1.100.0`）。

| 命令 | 实际结果 |
|---|---|
| `cargo test --manifest-path rust/Cargo.toml --offline -p um-gateway` | 退出码 0；26 个测试二进制共 104 个测试通过 |
| `cargo fmt --manifest-path rust/Cargo.toml -- --check` | 退出码 0 |
| `cargo clippy --manifest-path rust/Cargo.toml --all-targets --offline` | 退出码 0；无 warning |
| `python -m unittest tests.test_chat2api_endpoints tests.test_chat2api_stream tests.test_usage_boundary` | `Ran 90 tests in 137.681s`；`OK` |
| `python -m unittest discover -s tests -p "test_*.py"` | `Ran 2367 tests in 761.636s`；`OK (skipped=63)` |
| `bash scripts/check.sh --full` | 待运行 |

## 未验证范围

- 未启动真实 Rust 8003 生产进程，未切换 8002/8003/8097。
- 未连接真实 UM 上游；HTTPS 上游与真实登录不在 S2a。
- 未运行 `bash um-cloud-deploy/run-linux-checks.sh`。
- 未执行 Cloudflare、备份/恢复或生产 systemd 操作。
