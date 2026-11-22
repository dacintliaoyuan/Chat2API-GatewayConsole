//! S2b acceptance 2: the Responses event stream has the Python shape and
//! order for the chat and Anthropic Messages upstreams.
//!
//! Python source: `shared/responses_protocol.py::responses_stream_events` and
//! `messages_stream_events`, exercised by
//! `tests/test_r19_shared_responses.py:509-610`; the route behavior is
//! `chat2api/chat2api.py:7509-7756` and `7758-7995`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    parse_raw_response, sse_payloads, start_chat_gateway, wait_jsonl, ChatAttempt, ChatChunk,
    ChatStub, ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
};

fn event(name: &str, payload: Value) -> Vec<u8> {
    format!(
        "event: {name}\ndata: {}\n\n",
        serde_json::to_string(&payload).unwrap()
    )
    .into_bytes()
}

fn chat_events() -> Vec<Vec<u8>> {
    vec![
        b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":null}]}\n\n".to_vec(),
        b"data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"function\":{\"name\":\"lookup\",\"arguments\":\"{\\\"q\\\":\"}}]},\"finish_reason\":null}]}\n\n".to_vec(),
        b"data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"1}\"}}]},\"finish_reason\":null}]}\n\n".to_vec(),
        b"data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5,\"total_tokens\":15}}\n\n".to_vec(),
        b"data: [DONE]\n\n".to_vec(),
    ]
}

fn request() -> ClientRequest {
    ClientRequest::post(
        "/v1/responses",
        &json!({
            "model": "responses-model",
            "input": "hi",
            "stream": true,
            "tools": [{"type": "function", "name": "lookup",
                        "parameters": {"type": "object"}}],
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn start_stream(gateway: &support::Gateway) -> Vec<u8> {
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("streaming request");
    client.read_all(Duration::from_secs(5)).expect("read SSE")
}

fn event_names(body: &[u8]) -> Vec<String> {
    String::from_utf8_lossy(body)
        .split("\n\n")
        .filter_map(|chunk| {
            chunk
                .lines()
                .find_map(|line| line.strip_prefix("event: "))
                .map(str::to_string)
        })
        .collect()
}

fn event_payloads(body: &[u8]) -> Vec<(String, Value)> {
    let text = String::from_utf8_lossy(body);
    text.split("\n\n")
        .filter_map(|chunk| {
            let name = chunk
                .lines()
                .find_map(|line| line.strip_prefix("event: "))?
                .to_string();
            let data = chunk
                .lines()
                .filter(|line| line.starts_with("data:"))
                .map(|line| line.strip_prefix("data:").unwrap_or(line))
                .map(str::trim_start)
                .collect::<Vec<_>>()
                .join("\n");
            Some((name, serde_json::from_str::<Value>(&data).ok()?))
        })
        .collect()
}

fn payload_for<'a>(events: &'a [(String, Value)], name: &str) -> &'a Value {
    &events
        .iter()
        .find(|(event, _)| event == name)
        .unwrap_or_else(|| panic!("missing event {name}"))
        .1
}

#[test]
fn chat_stream_events_follow_the_python_shape_and_order() {
    let temp = TempDir::new("um-rust-responses-stream-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: chat_events().into_iter().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let raw = start_stream(&gateway);
    assert_eq!(
        event_names(&raw),
        [
            "response.created",
            "response.output_item.added",
            "response.output_text.delta",
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.delta",
            "response.output_item.done",
            "response.output_item.done",
            "response.completed",
        ]
    );
    let events = event_payloads(&raw);
    assert_eq!(
        payload_for(&events, "response.output_text.delta")["delta"],
        json!("hello")
    );
    let completed = &payload_for(&events, "response.completed")["response"];
    assert_eq!(completed["status"], json!("completed"));
    assert_eq!(
        completed["output"]
            .as_array()
            .expect("output array")
            .iter()
            .map(|item| item["type"].clone())
            .collect::<Vec<_>>(),
        [json!("message"), json!("function_call")]
    );
    assert_eq!(completed["output"][0]["content"][0]["text"], json!("hello"));
    assert_eq!(completed["output"][1]["call_id"], json!("call_1"));
    assert_eq!(completed["output"][1]["name"], json!("lookup"));
    assert_eq!(completed["output"][1]["arguments"], json!("{\"q\":1}"));
    assert_eq!(
        completed["usage"],
        json!({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    );

    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["protocol"], json!("responses"));
    assert_eq!(rows[0]["status"], json!("ok"));
    assert_eq!(rows[0]["stream"], json!(true));
    assert!(
        rows[0]["first_token_ms"].as_u64().is_some(),
        "a stream row records the first token time"
    );
}

#[test]
fn a_missing_done_yields_a_failed_response() {
    let temp = TempDir::new("um-rust-responses-no-done-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(
            b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":\"stop\"}]}\n\n"
                .to_vec(),
        )],
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let raw = start_stream(&gateway);
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let events = event_payloads(&body);
    let last = events.last().expect("a terminal event");
    assert_eq!(last.0, "response.failed");
    assert_eq!(
        last.1["response"]["error"]["code"],
        json!("upstream_eof_without_done")
    );
    assert_eq!(
        last.1["response"]["error"]["detail"]["reason"],
        json!("upstream_eof_without_done")
    );
    let header_id = headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("x-request-id"))
        .map(|(_, value)| value.as_str())
        .expect("X-Request-Id header");
    assert_eq!(
        last.1["response"]["error"]["detail"]["request_id"],
        json!(header_id)
    );
    assert!(!event_names(&body)
        .iter()
        .any(|name| name == "response.completed"));
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("upstream_eof_without_done"));
}

#[test]
fn response_created_is_staged_until_the_first_substantive_event() {
    let temp = TempDir::new("um-rust-responses-created-stage-");
    let stub = ChatStub::start();
    let content = chat_events()[0].clone();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::after(Duration::from_millis(300), content.clone()),
            ChatChunk::now(chat_events()[3].clone()),
            ChatChunk::now(chat_events()[4].clone()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    assert_eq!(
        client
            .read_some(Duration::from_millis(100))
            .expect("first read"),
        None,
        "the 200 must not be committed before substantive output"
    );
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let names = event_names(&body);
    assert_eq!(names.first().map(String::as_str), Some("response.created"));
    assert!(names
        .iter()
        .any(|name| name == "response.output_text.delta"));
    assert!(names.iter().any(|name| name == "response.completed"));
}

#[test]
fn a_pre_commit_eof_is_retried_before_the_200() {
    let temp = TempDir::new("um-rust-responses-precommit-retry-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: Vec::new(),
        hold: Duration::ZERO,
    });
    stub.push(ChatAttempt::Sse {
        chunks: chat_events().into_iter().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.retry_delay_base = Duration::ZERO;
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
    });
    let raw = start_stream(&gateway);
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(
        stub.attempts(),
        2,
        "the zero-event attempt was retried once"
    );
    let names = event_names(&body);
    assert_eq!(
        names
            .iter()
            .filter(|name| *name == "response.created")
            .count(),
        1,
        "the client sees exactly one response.created"
    );
    assert!(names.iter().any(|name| name == "response.completed"));
    assert!(!names.iter().any(|name| name == "response.failed"));
}

#[test]
fn a_pre_commit_metadata_eof_is_retried_before_the_200() {
    let temp = TempDir::new("um-rust-responses-precommit-metadata-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(
            b"data: {\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n"
                .to_vec(),
        )],
        hold: Duration::ZERO,
    });
    stub.push(ChatAttempt::Sse {
        chunks: chat_events().into_iter().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.retry_delay_base = Duration::ZERO;
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
    });
    let raw = start_stream(&gateway);
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(stub.attempts(), 2, "metadata alone never commits the 200");
    let names = event_names(&body);
    assert_eq!(
        names
            .iter()
            .filter(|name| *name == "response.created")
            .count(),
        1
    );
    assert!(names.iter().any(|name| name == "response.completed"));
    assert!(!names.iter().any(|name| name == "response.failed"));
}

#[test]
fn a_pre_commit_eof_without_retry_is_502() {
    let temp = TempDir::new("um-rust-responses-precommit-502-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: Vec::new(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
    });
    let response = support::client_request(gateway.addr, &request()).expect("pre-commit EOF");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("upstream_stream_error"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_eof_without_done")
    );
    assert!(response.header("x-request-id").is_some());
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn a_length_finish_reason_is_incomplete() {
    let temp = TempDir::new("um-rust-responses-incomplete-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":\"length\"}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let raw = start_stream(&gateway);
    let events = event_payloads(&raw);
    let last = events.last().expect("a terminal event");
    assert_eq!(last.0, "response.incomplete");
    assert_eq!(
        last.1["response"]["incomplete_details"],
        json!({"reason": "max_output_tokens"})
    );
    assert!(!event_names(&raw)
        .iter()
        .any(|name| name == "response.completed"));
}

#[test]
fn a_post_commit_error_chunk_yields_a_failed_response() {
    let temp = TempDir::new("um-rust-responses-error-chunk-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(chat_events()[0].clone()),
            ChatChunk::now(b"data: {\"error\":{\"message\":\"boom\"}}\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.retry_delay_base = Duration::ZERO;
        config.max_fast_retries = 5;
        config.max_slow_retries = 2;
        config.max_physical_attempts = 12;
    });
    let raw = start_stream(&gateway);
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let events = event_payloads(&body);
    let last = events.last().expect("a terminal event");
    assert_eq!(last.0, "response.failed");
    assert_eq!(
        last.1["response"]["error"]["code"],
        json!("upstream_stream_error")
    );
    assert_eq!(
        last.1["response"]["error"]["detail"]["reason"],
        json!("upstream_stream_error")
    );
    assert_eq!(
        last.1["response"]["error"]["detail"]["upstream_message"],
        json!("boom")
    );
    let header_id = headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("x-request-id"))
        .map(|(_, value)| value.as_str())
        .expect("X-Request-Id header");
    assert_eq!(
        last.1["response"]["error"]["detail"]["request_id"],
        json!(header_id)
    );
    assert!(!event_names(&body)
        .iter()
        .any(|name| name == "response.completed"));
    assert_eq!(stub.attempts(), 1, "a committed stream is never retried");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("upstream_stream_error"));
}

#[test]
fn an_error_chunk_before_the_200_is_502() {
    let temp = TempDir::new("um-rust-responses-error-chunk-precommit-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(
            b"data: {\"error\":{\"message\":\"boom\"}}\n\n".to_vec(),
        )],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
    });
    let response = support::client_request(gateway.addr, &request()).expect("pre-commit error");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("upstream_stream_error"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_stream_error")
    );
    assert_eq!(body["error"]["detail"]["upstream_message"], json!("boom"));
    assert!(response.header("x-request-id").is_some());
    assert_eq!(stub.attempts(), 1, "an explicit error is not retried");
}

fn anthropic_events() -> Vec<Vec<u8>> {
    vec![
        event(
            "message_start",
            json!({
                "type": "message_start",
                "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "m", "content": [], "stop_reason": null,
                    "stop_sequence": null,
                    "usage": {"input_tokens": 7, "output_tokens": 0},
                },
            }),
        ),
        event(
            "content_block_start",
            json!({"type": "content_block_start", "index": 0,
                   "content_block": {"type": "text", "text": ""}}),
        ),
        event(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "hello"}}),
        ),
        event(
            "content_block_stop",
            json!({"type": "content_block_stop", "index": 0}),
        ),
        event(
            "content_block_start",
            json!({"type": "content_block_start", "index": 1,
                   "content_block": {"type": "tool_use", "id": "toolu_1",
                                      "name": "lookup", "input": {}}}),
        ),
        event(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 1,
                   "delta": {"type": "input_json_delta",
                               "partial_json": "{\"q\":"}}),
        ),
        event(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 1,
                   "delta": {"type": "input_json_delta",
                               "partial_json": "1}"}}),
        ),
        event(
            "content_block_stop",
            json!({"type": "content_block_stop", "index": 1}),
        ),
        event(
            "message_delta",
            json!({"type": "message_delta",
                   "delta": {"stop_reason": "end_turn", "stop_sequence": null},
                   "usage": {"output_tokens": 2}}),
        ),
        event("message_stop", json!({"type": "message_stop"})),
    ]
}

fn image_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/responses",
        &json!({
            "model": "responses-model",
            "stream": true,
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "call_id": "c1", "name": "shot",
                 "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1",
                 "output": [{"type": "input_text", "text": "shot"},
                             {"type": "input_image",
                              "image_url": "data:image/png;base64,AAAA"}]},
            ],
            "tools": [{"type": "function", "name": "shot",
                        "parameters": {"type": "object"}}],
            "max_output_tokens": 64,
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

#[test]
fn messages_stream_events_follow_the_python_shape_and_order() {
    let temp = TempDir::new("um-rust-responses-messages-stream-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: anthropic_events().into_iter().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &image_request()).expect("stream request");
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    assert_eq!(
        event_names(&raw),
        [
            "response.created",
            "response.output_item.added",
            "response.output_text.delta",
            "response.output_item.done",
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.delta",
            "response.output_item.done",
            "response.completed",
        ]
    );
    let events = event_payloads(&raw);
    let completed = &payload_for(&events, "response.completed")["response"];
    assert_eq!(completed["status"], json!("completed"));
    assert_eq!(
        completed["output"]
            .as_array()
            .expect("output array")
            .iter()
            .map(|item| item["type"].clone())
            .collect::<Vec<_>>(),
        [json!("message"), json!("function_call")]
    );
    assert_eq!(completed["output"][1]["arguments"], json!("{\"q\":1}"));
    assert_eq!(
        completed["usage"],
        json!({"input_tokens": 7, "output_tokens": 2, "total_tokens": 9})
    );
    assert_eq!(stub.seen()[0].path, "/api/v1/messages");

    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["protocol"], json!("responses"));
    assert_eq!(rows[0]["status"], json!("ok"));
    assert_eq!(rows[0]["input_tokens"], json!(7));
    assert_eq!(rows[0]["output_tokens"], json!(2));
    assert!(
        rows[0]["first_token_ms"].as_u64().is_some(),
        "the first substantive text delta records the first token time"
    );
}

#[test]
fn a_missing_message_stop_yields_a_failed_response() {
    let temp = TempDir::new("um-rust-responses-no-stop-");
    let stub = ChatStub::start();
    let chunks: Vec<ChatChunk> = anthropic_events()
        .into_iter()
        .take(3)
        .map(ChatChunk::now)
        .collect();
    stub.push(ChatAttempt::Sse {
        chunks,
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &image_request()).expect("stream request");
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let events = event_payloads(&body);
    let last = events.last().expect("a terminal event");
    assert_eq!(last.0, "response.failed");
    assert_eq!(
        last.1["response"]["error"]["code"],
        json!("upstream_eof_without_message_stop")
    );
    assert_eq!(
        last.1["response"]["error"]["detail"]["reason"],
        json!("upstream_eof_without_message_stop")
    );
    let header_id = headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("x-request-id"))
        .map(|(_, value)| value.as_str())
        .expect("X-Request-Id header");
    assert_eq!(
        last.1["response"]["error"]["detail"]["request_id"],
        json!(header_id)
    );
    assert!(!event_names(&body)
        .iter()
        .any(|name| name == "response.completed"));
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(
        rows[0]["status"],
        json!("upstream_eof_without_message_stop")
    );
}

#[test]
fn a_pre_commit_missing_message_stop_is_retried_before_the_200() {
    let temp = TempDir::new("um-rust-responses-precommit-stop-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(anthropic_events()[0].clone())],
        hold: Duration::ZERO,
    });
    stub.push(ChatAttempt::Sse {
        chunks: anthropic_events().into_iter().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.retry_delay_base = Duration::ZERO;
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
    });
    let mut client = StreamingClient::send(gateway.addr, &image_request()).expect("stream request");
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(
        stub.attempts(),
        2,
        "message_start alone never commits the 200"
    );
    let names = event_names(&body);
    assert_eq!(
        names
            .iter()
            .filter(|name| *name == "response.created")
            .count(),
        1
    );
    assert!(names.iter().any(|name| name == "response.completed"));
    assert!(!names.iter().any(|name| name == "response.failed"));
}

#[test]
fn a_pre_commit_missing_message_stop_without_retry_is_502() {
    let temp = TempDir::new("um-rust-responses-precommit-stop-502-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(anthropic_events()[0].clone())],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
    });
    let response =
        support::client_request(gateway.addr, &image_request()).expect("pre-commit message_stop");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("upstream_stream_error"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_eof_without_message_stop")
    );
    assert!(response.header("x-request-id").is_some());
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn a_post_commit_error_event_yields_a_failed_response() {
    let temp = TempDir::new("um-rust-responses-message-error-");
    let stub = ChatStub::start();
    let mut chunks: Vec<ChatChunk> = anthropic_events()
        .into_iter()
        .take(3)
        .map(ChatChunk::now)
        .collect();
    chunks.push(ChatChunk::now(event(
        "error",
        json!({"type": "error", "error": {"type": "overloaded", "message": "overloaded"}}),
    )));
    stub.push(ChatAttempt::Sse {
        chunks,
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.max_fast_retries = 5;
        config.max_slow_retries = 2;
        config.max_physical_attempts = 12;
    });
    let mut client = StreamingClient::send(gateway.addr, &image_request()).expect("stream request");
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let events = event_payloads(&body);
    let last = events.last().expect("a terminal event");
    assert_eq!(last.0, "response.failed");
    assert_eq!(
        last.1["response"]["error"]["code"],
        json!("upstream_stream_error")
    );
    assert_eq!(
        last.1["response"]["error"]["detail"]["reason"],
        json!("upstream_stream_error")
    );
    assert_eq!(
        last.1["response"]["error"]["detail"]["upstream_message"],
        json!("overloaded")
    );
    let header_id = headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("x-request-id"))
        .map(|(_, value)| value.as_str())
        .expect("X-Request-Id header");
    assert_eq!(
        last.1["response"]["error"]["detail"]["request_id"],
        json!(header_id)
    );
    assert!(!event_names(&body)
        .iter()
        .any(|name| name == "response.completed"));
    assert_eq!(stub.attempts(), 1, "a committed stream is never retried");
}

#[test]
fn an_error_event_before_the_200_is_502() {
    let temp = TempDir::new("um-rust-responses-message-error-precommit-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(event(
            "error",
            json!({"type": "error", "error": {"type": "overloaded", "message": "overloaded"}}),
        ))],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
    });
    let response = support::client_request(gateway.addr, &image_request())
        .expect("pre-commit upstream error event");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("upstream_stream_error"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_stream_error")
    );
    assert_eq!(
        body["error"]["detail"]["upstream_message"],
        json!("overloaded")
    );
    assert!(response.header("x-request-id").is_some());
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn the_stream_starts_with_created_and_a_json_response_has_the_same_shape() {
    let temp = TempDir::new("um-rust-responses-stream-created-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":\"stop\"}]}\n\n".to_vec()),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let raw = start_stream(&gateway);
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let content_type = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("content-type"))
        .map(|(_, value)| value.as_str());
    assert_eq!(content_type, Some("text/event-stream"));
    let names = event_names(&body);
    assert_eq!(names.first().map(String::as_str), Some("response.created"));
    assert!(names
        .iter()
        .any(|name| name == "response.output_text.delta"));
    assert!(names.iter().any(|name| name == "response.completed"));
    assert!(sse_payloads(&body)
        .iter()
        .any(|payload| { payload["response"]["status"] == json!("in_progress") }));
}

#[test]
fn a_terminal_only_chat_stream_does_not_record_a_first_token() {
    let temp = TempDir::new("um-rust-responses-terminal-only-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":0,\"total_tokens\":10}}\n\n"
                    .to_vec(),
            ),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let raw = start_stream(&gateway);
    assert!(event_names(&raw)
        .iter()
        .any(|name| name == "response.completed"));
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert!(
        rows[0].get("first_token_ms").is_none(),
        "a terminal-only stream never produced substantive output"
    );
}

fn terminal_only_anthropic_events() -> Vec<Vec<u8>> {
    vec![
        event(
            "message_start",
            json!({
                "type": "message_start",
                "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "m", "content": [], "stop_reason": null,
                    "stop_sequence": null,
                    "usage": {"input_tokens": 7, "output_tokens": 0},
                },
            }),
        ),
        event(
            "message_delta",
            json!({"type": "message_delta",
                   "delta": {"stop_reason": "end_turn", "stop_sequence": null},
                   "usage": {"output_tokens": 0}}),
        ),
        event("message_stop", json!({"type": "message_stop"})),
    ]
}

#[test]
fn a_terminal_only_messages_stream_does_not_record_a_first_token() {
    let temp = TempDir::new("um-rust-responses-messages-terminal-only-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: terminal_only_anthropic_events()
            .into_iter()
            .map(ChatChunk::now)
            .collect(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &image_request()).expect("stream request");
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    let names = event_names(&raw);
    assert!(names.iter().any(|name| name == "response.completed"));
    assert!(!names
        .iter()
        .any(|name| name == "response.output_text.delta"));
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("ok"));
    assert!(
        rows[0].get("first_token_ms").is_none(),
        "a terminal-only messages stream never produced substantive output"
    );
}
