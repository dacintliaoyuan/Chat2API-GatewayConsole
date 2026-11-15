# Rust S2b 验收记录（#47）

- 状态：完成。候选进程只监听临时 loopback 端口；**没有切换 8003**，没有启动
  `chat2api.py`、broker 或 display relay。
- 输入：`docs/rust-contract.md` §5、`docs/rust-plan.md` S2、ticket #47。
- 范围：`rust/crates/gateway/src/responses.rs`、`nonstream.rs`、`embeddings.rs`、
  `config.rs`、`server.rs`，以及 `rust/crates/gateway/tests/responses_*.rs`、
  `embeddings_*.rs`。

## 交付

| 交付 | 位置 | 说明 |
|---|---|---|
| Responses 请求/响应翻译 | `rust/crates/gateway/src/responses.rs` | 冻结的 17 字段白名单、chat 翻译、工具、usage |
| Responses 事件流 | `rust/crates/gateway/src/responses.rs` | chat 与 Anthropic Messages 两种上游事件翻译 |
| 非流式聚合 | `rust/crates/gateway/src/nonstream.rs` | `ChatCompletionAggregator` 的 Rust port |
| 聚合三元组门 | `rust/crates/gateway/src/config.rs`、`server.rs` | 启动开关、严格 scope、已验证 key |
| Embeddings 透传 | `rust/crates/gateway/src/embeddings.rs` | `/api/v1/embeddings` 透传，不写账本 |
| 路由接入 | `rust/crates/gateway/src/server.rs` | `/v1/responses`、`/api/v1/responses`、`/v1/embeddings`、`/api/v1/embeddings` |
| 门禁脚本 | `scripts/check-rust-s2b.sh` | Rust 全量测试、fmt、clippy、夹具新鲜度 |

## 验收对照

| # | 验收条目 | 定向测试 |
|---|---|---|
| 1 | Responses 非流式 JSON 与事件流形状与 Python 一致 | `responses_forwarding::non_stream_responses_translates_chat_json_tools_and_usage`；`responses_stream::the_stream_starts_with_created_and_a_json_response_has_the_same_shape` |
| 2 | 事件顺序覆盖 `response.created`、`response.output_text.delta`、`response.function_call_arguments.delta`、`response.completed`、`response.incomplete`、`response.failed` | `responses_stream::chat_stream_events_follow_the_python_shape_and_order`；`responses_stream::messages_stream_events_follow_the_python_shape_and_order`；`responses_stream::a_length_finish_reason_is_incomplete`；`responses_stream::an_upstream_error_chunk_yields_a_failed_response`；`responses_stream::an_error_event_yields_a_failed_response` |
| 3 | 工具调用参数截断与 Python 一致 | `responses_tool_truncation::a_truncated_tool_call_is_forwarded_verbatim_and_never_completed`；`responses_tool_truncation::an_unnamed_argument_fragment_is_held_until_the_name_arrives`；`responses_tool_truncation::a_complete_tool_call_stream_is_completed_with_its_arguments` |
| 4 | `stream=false` 聚合只对已验证的 (key, model, protocol) 三元组生效 | `responses_aggregate::a_selected_triple_aggregates_the_upstream_sse_into_one_json_response`；`responses_aggregate::an_unselected_model_stays_on_the_native_non_stream_transport`；`responses_aggregate::a_local_alias_has_no_verified_key_and_stays_native`；`responses_aggregate::the_scope_parser_is_strict_and_fails_closed`；`responses_aggregate::an_aggregate_failure_is_a_502_and_never_a_partial_success`；`responses_aggregate::an_explicit_error_chunk_is_an_upstream_explicit_error` |
| 5 | Embeddings 只透传，不写账本行 | `embeddings_forwarding::embeddings_are_relayed_unchanged_and_write_no_ledger_row`；`embeddings_forwarding::a_non_200_embedding_response_is_passed_through_without_a_ledger_row` |
| 6 | 分享模式 `/v1/embeddings` 返回 404 `route_not_shared`；`/api/v1/embeddings` 不经分享 Key 门，公网不反代 | `embeddings_aliases::the_shared_embeddings_route_is_not_shared`；`embeddings_aliases::the_local_alias_bypasses_the_share_key_gate`；`embeddings_aliases::the_embeddings_paths_are_not_in_the_public_whitelist` |
| 7 | `/api/v1/responses` 是本机兼容别名，不进入公网白名单 | `responses_aliases::the_alias_bypasses_the_share_key_gate`；`responses_aliases::the_shared_v1_responses_route_still_requires_its_key`；`responses_aliases::the_alias_is_not_in_the_public_whitelist` |

### 行为边界

- 分享模式只接受冻结的 17 个 `RESPONSES_FIELDS`；`model` 非空、`input` 为非空
  string/list、`stream` 必须是 bool、`store` 只能是 `false`、`max_output_tokens` 必须
  是正整数；未知字段拒绝且不发上游请求。
- 非流式 Responses 请求翻译到 `POST /api/chat/completions`；带图片的
  `function_call_output` 分支翻译到 `POST /api/v1/messages`，与 Python 的
  `responses_requires_messages` 一致。
- 非流式 200 的 `chat_to_responses_response` 保留文本、工具调用、`finish_reason`
  的 `length`/`content_filter` 状态、usage 的 cache/reasoning 明细；响应形状与
  `tests/test_r19_shared_responses.py:380-457` 相同。
- 事件流：chat 上游产出 `response.created`、`output_item.added/done`、
  `output_text.delta`、`function_call_arguments.delta`、`response.completed`/
  `response.incomplete`/`response.failed`；Anthropic Messages 上游按官方事件序列翻译。
  没有 `[DONE]`/`message_stop` 的 EOF 产出 `response.failed`，绝不补造完成事件。
- 工具调用参数截断：已提交的工具调用只转发已收到的 name 与参数片段；EOF 后以
  `response.failed` + `upstream_eof_without_done` 结束，不合成完整参数。参数名未到达
  前的片段先保留，name 到达后一次性转发。
- `stream=false` 聚合只在该进程启动开关打开、scope 合法且非空、请求已由分享 Key
  验证、且 `(key_id, model, protocol="responses")` 三者都在 scope 内时启用；否则
  走原生非流式。聚合上游必须同时出现 `[DONE]` 和非空 `finish_reason`；否则 502，
  不返回部分成功。
- Embeddings 原样透传上游状态、`Content-Type`/`Content-Length`/`Cache-Control`
  与 body 字节；不解析 JSON、不写账本行。
- 分享模式 `/v1/embeddings` 在分享 Key 门前返回 404 `route_not_shared`；
  `/api/v1/embeddings` 与 `/api/v1/responses` 不经分享 Key 门，也不在 Cloudflare
  公网白名单内。

## 门禁结果

以下命令在本次实施中实际运行，工作目录 `.worktrees/rust-47`
（Windows，`cargo 1.100.0`，`rustc 1.100.0`，Python 3.14）。

| 命令 | 实际结果 |
|---|---|
| `cargo test --manifest-path rust/Cargo.toml --offline` | 退出码 0；全部 Rust workspace 测试通过 |
| `cargo test --manifest-path rust/Cargo.toml --offline -p um-gateway` | 退出码 0；`um-gateway` 135 个测试通过 |
| `cargo fmt --manifest-path rust/Cargo.toml -- --check` | 退出码 0 |
| `cargo clippy --manifest-path rust/Cargo.toml --all-targets --offline` | 退出码 0；无 warning |
| `bash scripts/check-rust-s2b.sh` | 退出码 0 |
| `python -m unittest tests.test_rust_gateway_fixture tests.test_rust_contract_fixture tests.test_rust_broker_fixture tests.test_rust_store_fixture` | `Ran 8 tests`；`OK` |
| `python -m unittest tests.test_chat2api_endpoints tests.test_chat2api_stream tests.test_usage_boundary tests.test_r19_shared_responses` | `Ran 126 tests in 134.288s`；`OK` |
| `python -m unittest discover -s tests -p "test_*.py"` | 退出码 0；`Ran 2369 tests in 758.558s`；`OK (skipped=63)` |
| `bash scripts/check.sh --full` | 退出码 0；Python `Ran 2369 tests in 825.027s` / `OK (skipped=63)`；`local/tests` `Ran 54 tests in 20.521s` / `OK` |

## 未验证范围

- 未启动真实 Rust 8003 生产进程，未切换 8002/8003/8097。
- 未连接真实 UM 上游；HTTPS 上游与真实登录不在 S2b。
- 未运行 `bash um-cloud-deploy/run-linux-checks.sh`。
- 未执行 Cloudflare、备份/恢复或生产 systemd 操作。
