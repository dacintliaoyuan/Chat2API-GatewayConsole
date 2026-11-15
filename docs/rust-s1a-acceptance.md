# Rust S1a 验收记录（#44）

- 状态：完成。候选进程只监听临时 loopback 端口；**没有切换 8003**，没有启动
  `chat2api.py`、broker 或 display relay。
- 输入：`docs/rust-contract.md` §5、`docs/rust-plan.md` S1、ticket #44。
- 范围：`rust/crates/gateway/`、`tests/generate_rust_gateway_fixture.py`、
  `tests/fixtures/rust_gateway_rev1.json`。

## 交付

| 交付 | 位置 | 说明 |
|---|---|---|
| 只读面 | `rust/crates/gateway/src/readonly.rs` | `/health`、`/ready`、`/v1/version`、`/v1/models` |
| 分享 Key 门 | `rust/crates/gateway/src/gate.rs` | Bearer / `X-Api-Key`、每 Key/全局并发、20 次/60 秒限速、撤销在途关闭 |
| 请求边界 | `rust/crates/gateway/src/body.rs` | 分享路由沿用 8003 `_read_body`；控制令牌路由沿用 8002 `_read_share_json_body` |
| 路由与门 | `rust/crates/gateway/src/server.rs` | 四条 `/v1` 分享路由、CORS/Origin、控制令牌、错误关闭连接 |
| 统一错误体 | `rust/crates/gateway/src/error.rs` | `{error:{message,type,detail}}` + 可选 `code`/`reason`/`upstream_status` |
| 候选入口 | `rust/crates/gateway/src/main.rs` | `um-gateway-candidate`，默认 `127.0.0.1:0` |
| 行为夹具 | `tests/fixtures/rust_gateway_rev1.json` | 由真实 Python `chat2api.Handler` + `ShareGate` 生成 |

## 验收对照

| # | 验收条目 | 定向测试 |
|---|---|---|
| 1 | 只读面形状与 Python 一致 | `readonly_surface::health_ready_version_and_models_match_the_python_shapes`；`version_success_requires_non_share_mode` |
| 2 | Bearer Key 的 401 族 | `share_key_gate::every_credential_shape_matches_the_python_gate`；`duplicate_and_overlong_credentials_never_reach_the_digest_lookup` |
| 3 | 并发/限速 429 四类 reason | `concurrency_limit::per_key_concurrency_is_refused_with_its_python_reason`；`global_share_concurrency_is_refused_with_its_python_reason`；`the_twenty_first_request_in_a_minute_hits_the_key_rate_limit`；`state_not_accepting_is_the_fourth_reason` |
| 4 | 分享模式四条 `/v1` 路由 | `share_route_whitelist::share_mode_route_cases_match_python`；`every_unlisted_v1_path_is_route_not_shared` |
| 5 | `/v1/version` 与 `/v1/embeddings` 404 | `share_version_embeddings::version_is_route_not_shared_in_share_mode`；`embeddings_is_route_not_shared_in_share_mode` |
| 6 | 请求体/字段拒绝 | `request_rejection::share_route_body_cases_match_python`；`console_body_cases_match_python`；`the_share_body_limit_is_32_mib_by_default` |
| 7 | CORS/Origin、控制令牌、错误关闭连接 | `cors_control::cors_options_and_origin_cases_match_python`；`control_token_gate_cases_match_python`；`every_error_response_actively_closes_the_connection` |
| 8 | 统一错误体 | `error_shape::every_recorded_error_body_uses_the_unified_shape`；`sharing_limit_carries_code_reason_and_detail`；`upstream_unavailable_carries_upstream_status` |
| 9 | 候选进程只监听临时 loopback 端口 | `candidate_process::the_candidate_process_binds_only_an_ephemeral_loopback_port`；`the_candidate_refuses_8003_and_non_loopback_before_bind` |

### 边界说明

- 分享路由（`/v1/*`）的请求体沿用 8003 `_read_body`：非法 Content-Length
  `invalid_request`、超限 `payload_too_large`、非法 JSON `invalid_json`、
  非对象 `invalid_body`、未知字段 `unsupported_chat_request` /
  `unsupported_messages_request` / `unsupported_responses_request`。
- 控制令牌路由（`/admin/share/revoke`）沿用 8002 的请求边界：重复敏感头
  `duplicate header: <name>`、`unsupported_body_encoding`、
  `invalid_content_length`、`content_type_required`、`incomplete_body`、
  `invalid_json`、`request_too_large` 与 `unknown field(s)`。
  这是 ticket 把 8002 的请求边界写进 S1a 验收后，按各自 Python 前例保留错误码；
  分享路由的重复 `Authorization` 仍按 401 `invalid_api_key` 处理。
- 分享模式的 `/v1/chat/completions`、`/v1/messages`、`/v1/responses` 只完成
  路由、Key、并发、请求体与字段门；转发与 SSE 由 S1b/S2 交付，当前合法请求返回
  501 `not_implemented`。
- 候选进程不实现 HTTPS 上游（S1a 的测试上游是 loopback HTTP stub）；生产
  `https://chat.genai.um.edu.mo` 的 TLS 客户端与真实 UM 登录留给后续切片。

## 夹具

```bash
python tests/generate_rust_gateway_fixture.py
```

夹具由真实 Python 代码生成：启动 `chat2api.Handler` 与 `ShareGate`，用 stub
上游记录 `/health`、`/ready`、`/v1/version`、`/v1/models` 的状态、响应头与
JSON 形状，以及 Key 门、路由白名单、429 四类 reason、请求体拒绝、CORS、
控制令牌与关闭连接。Rust 测试只读夹具，不手写 Python 期望值。

## 门禁结果

以下命令在本次实施中实际运行，工作目录 `.worktrees/rust-44`
（Windows，`cargo 1.100.0`，`rustc 1.100.0`）。

| 命令 | 实际结果 |
|---|---|
| `python tests/generate_rust_gateway_fixture.py` | 写出 `tests/fixtures/rust_gateway_rev1.json` |
| `python -m unittest tests.test_rust_gateway_fixture` | `Ran 2 tests`；`OK`（生成器与已提交夹具一致） |
| `cargo test --manifest-path rust/Cargo.toml` | S0 10 个 + S1a 41 个测试通过；0 failed |
| `python -m unittest tests.test_chat2api_request_rejection tests.test_chat2api_http_isolation tests.test_share_gate_merged tests.test_share_gate_citations` | `Ran 38 tests`；`OK (skipped=4)` |
| `bash scripts/check.sh --full` | 退出码 0；`tests` `Ran 2355 tests` / `OK (skipped=63)`，`local/tests` `Ran 54 tests` / `OK` |
| `git -c core.whitespace=cr-at-eol diff --check` | 退出码 0 |

全量门禁在实施过程中出现过一次 Windows 连接/时序抖动：`tests` 的
`test_rotate_is_fail_closed_until_runtime_catches_up`（`TimeoutError`）与
`test_explicit_5xx_is_an_error_not_sse`（`ConnectionAbortedError`）各报一次
error；两条单独重跑均 `OK`。最终复跑退出码 0，不把首次抖动写成通过证据。

## 无生产端口切换

- `GatewayConfig::validate_candidate` 在 bind 前拒绝 `8003`、治理段端口与非
  loopback host；候选进程默认 `127.0.0.1:0`，只打印内核分配的临时端口。
- 没有修改 systemd unit、部署模板、端口默认值或 `current` 符号链接。
- 没有读取或写入 `data/`、`profile/`、`tmp/`、`output/`、
  `chat2api/token.json`、`chat2api/.edge-profile`、`/var/lib/um-share`、
  `/var/lib/um-share-console`、`/etc/um-share`。

## 未验证范围

- 未启动真实 Rust 8003 生产进程，未切换 8002/8003/8097。
- 未连接真实 UM 上游；HTTPS 上游客户端与真实登录不在 S1a。
- 未运行 `bash um-cloud-deploy/run-linux-checks.sh`。
- 未执行 Cloudflare、备份/恢复或生产 systemd 操作。
