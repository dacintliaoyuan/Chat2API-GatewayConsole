# Rust S1b 验收记录（#45）

- 状态：完成。候选进程只监听临时 loopback 端口；**没有切换 8003**，没有启动
  `chat2api.py`、broker 或 display relay。
- 输入：`docs/rust-contract.md` §5、`docs/rust-plan.md` S1、ticket #45。
- 范围：`rust/crates/gateway/src/chat.rs`、`ledger.rs`、`upstream.rs`，
  `config.rs`、`http.rs`、`server.rs` 的接入，以及
  `rust/crates/gateway/tests/chat_*.rs`。

## 交付

| 交付 | 位置 | 说明 |
|---|---|---|
| Chat 转发 | `rust/crates/gateway/src/chat.rs` | 非流式 JSON、SSE 字节透传、提交门、错误映射、超时、重试、断开 |
| 上游客户端 | `rust/crates/gateway/src/upstream.rs` | `POST /api/chat/completions`、逐字节读取、chunked 解码、可取消 SSE 生产者 |
| 分享账本 | `rust/crates/gateway/src/ledger.rs` | Python `record_usage` 字段、usage 归一化、写失败降级 |
| 路由接入 | `rust/crates/gateway/src/server.rs` | `/v1/chat/completions` 与两条本机别名 |
| 配置 | `rust/crates/gateway/src/config.rs` | 账本路径、首字节/静默/总预算、重试与 reader 上限 |

## 验收对照

| # | 验收条目 | 定向测试 |
|---|---|---|
| 1 | 非流式与 SSE 的请求/响应形状与 Python 一致 | `chat_forwarding::non_stream_json_is_relayed_unchanged`；`chat_forwarding::sse_is_forwarded_with_python_event_bytes_and_headers`；`chat_forwarding::a_chunked_upstream_sse_body_is_decoded_before_forwarding` |
| 2 | 200 头只在首个实质数据事件后提交；元数据/思考事件可暂存 | `chat_commit_gate::metadata_is_staged_until_the_first_substantive_event`；`chat_commit_gate::thinking_is_staged_until_the_first_visible_event`；`chat_commit_gate::a_first_content_event_commits_without_staging`；`chat_commit_gate::an_oversized_prelude_fails_before_the_200` |
| 3 | 提交前失败 502；提交后 in-band `error` 并结束 | `chat_stream_errors::a_zero_data_eof_before_the_200_is_502`；`chat_stream_errors::an_explicit_error_event_before_the_200_is_502`；`chat_stream_errors::a_post_commit_eof_writes_one_in_band_error_and_no_done`；`chat_stream_errors::a_post_commit_explicit_error_keeps_its_sanitized_message`；`chat_stream_errors::a_done_without_finish_reason_is_an_incomplete_stream` |
| 4 | 首字节 120 秒、静默 180 秒、总预算 360 秒且环境变量夹在 10..3600 | `chat_budgets::the_timeout_defaults_match_the_contract_fixture`；`chat_budgets::a_silent_upstream_hits_the_first_byte_budget`；`chat_budgets::a_post_commit_silence_hits_the_socket_silence_budget`；`chat_budgets::the_total_budget_stops_retrying_before_any_upstream_data`；`chat_budgets::a_committed_stream_stops_at_the_total_budget` |
| 5 | 快速失败 5 次、慢失败 2 次、物理尝试 12 次；只在未收到 SSE 事件前重试 | `chat_retry::fast_failures_retry_five_times`；`chat_retry::slow_failures_retry_twice`；`chat_retry::physical_attempts_stop_at_twelve`；`chat_retry::a_pre_commit_eof_is_retried_before_any_sse_event`；`chat_retry::a_committed_stream_is_never_retried` |
| 6 | 客户端断开逐字节检查；关闭上游、释放 reader 槽、账本记 `client_disconnected` | `chat_disconnect::a_post_commit_disconnect_closes_the_upstream_and_frees_the_slot`；`chat_disconnect::a_pre_commit_disconnect_records_client_disconnected_without_a_200` |
| 7 | 账本行字段与 Python 一致；写失败不阻断代理 | `chat_ledger::a_non_stream_row_matches_the_python_field_set_and_values`；`chat_ledger::an_unreported_usage_row_keeps_the_estimate_and_coverage`；`chat_ledger::a_stream_row_uses_the_upstream_usage_and_the_stream_status`；`chat_ledger::a_ledger_write_failure_never_blocks_the_proxy` |
| 8 | 两条 `/api` 别名是本机兼容面，不进公网白名单 | `chat_aliases::the_aliases_bypass_the_share_key_gate_and_record_no_attribution`；`chat_aliases::the_share_v1_route_still_requires_its_key`；`chat_aliases::the_aliases_are_not_in_the_public_share_whitelist` |

### 行为边界

- 上游请求固定为 `POST /api/chat/completions`，使用存储的 bearer；`stream=true`
  时补 `stream_options.include_usage=true`，客户端 `stream=false` 时仍走上游非流式。
- SSE 转发保留上游事件字节；生产者在每个字节检查取消，在自己的 `finally`
  关闭上游并释放 reader 槽。
- 提交门：只把可见 `content`、`tool_calls`/`function_call` 或 `finish_reason`
  当实质事件；role/metadata、reasoning 与注释先暂存，暂存上限 64 KiB。
- 预提交失败返回 502 `upstream_stream_error`；提交后失败只写一个
  `data: {"error": ...}` 事件并结束，不补 `[DONE]`，不把部分完成当成功。
- 重试只发生在提交前；已提交的流不再重试，避免拼接两个完成。
- 账本写失败只关闭本进程记账并打印一行错误，响应仍照常返回。
- `/api/chat/completions` 与 `/api/v1/chat/completions` 不走分享 Key 门，
  公网白名单仍只有四条 `/v1` 路径（契约夹具 `cloudflare.sharePaths`）。

## 门禁结果

以下命令在本次实施中实际运行，工作目录 `.worktrees/rust-45`
（Windows，`cargo 1.100.0`，`rustc 1.100.0`）。

| 命令 | 实际结果 |
|---|---|
| `cargo test --manifest-path rust/Cargo.toml --offline -j 1` | 全部 Rust workspace 测试通过；新增 S1b 测试 31 个 |
| `python -m unittest tests.test_chat2api_endpoints tests.test_chat2api_stream tests.test_chat2api_request_rejection` | `Ran 80 tests`；`OK` |
| `python -m unittest discover -s tests -p "test_*.py"` | 见下方复跑结果 |
| `bash scripts/check.sh --full` | 见下方复跑结果 |

## 未验证范围

- 未启动真实 Rust 8003 生产进程，未切换 8002/8003/8097。
- 未连接真实 UM 上游；HTTPS 上游客户端与真实登录仍不在 S1b。
- 未运行 `bash um-cloud-deploy/run-linux-checks.sh`。
- 未执行 Cloudflare、备份/恢复或生产 systemd 操作。
