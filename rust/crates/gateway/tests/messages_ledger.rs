//! S2a acceptance 5: every logical Messages request appends exactly one share
//! ledger row with the Python `record_usage` field set and status semantics.
//!
//! Python source: `chat2api/chat2api.py:3316-3401` (`record_usage`) and the
//! Anthropic usage translation at `chat2api/chat2api.py:3420-3543`. A stream
//! records the accumulated Anthropic usage; an interrupted stream records its
//! known lower bound with a non-"ok" status, never a fabricated success.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, read_jsonl, start_chat_gateway, wait_jsonl, ChatAttempt, ChatChunk, ChatStub,
    ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
};

fn request(stream: bool) -> ClientRequest {
    ClientRequest::post(
        "/v1/messages",
        &json!({
            "model": "claude-x",
            "max_tokens": 16,
            "stream": stream,
            "messages": [{"role": "user", "content": "hi"}],
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn message_response(usage: Option<Value>) -> Value {
    let mut body = json!({
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": "claude-x",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": null,
    });
    if let Some(usage) = usage {
        body["usage"] = usage;
    }
    body
}

fn sse_event(event_type: &str, payload: Value) -> Vec<u8> {
    format!(
        "event: {event_type}\ndata: {}\n\n",
        serde_json::to_string(&payload).unwrap()
    )
    .into_bytes()
}

fn official_events() -> Vec<Vec<u8>> {
    vec![
        sse_event(
            "message_start",
            json!({
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [],
                    "stop_reason": null,
                    "stop_sequence": null,
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                    },
                },
            }),
        ),
        sse_event(
            "content_block_delta",
            json!({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            }),
        ),
        sse_event(
            "message_delta",
            json!({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": null},
                "usage": {"output_tokens": 16},
            }),
        ),
        sse_event("message_stop", json!({"type": "message_stop"})),
    ]
}

fn assert_time(value: &str) {
    let bytes = value.as_bytes();
    assert_eq!(bytes.len(), 25, "ISO-8601 local seconds: {value}");
    assert_eq!(bytes[4], b'-');
    assert_eq!(bytes[7], b'-');
    assert_eq!(bytes[10], b'T');
    assert_eq!(bytes[13], b':');
    assert_eq!(bytes[16], b':');
    assert!(bytes[19] == b'+' || bytes[19] == b'-', "{value}");
    assert_eq!(bytes[22], b':');
}

fn assert_verified_attribution(row: &Value) {
    assert_eq!(row["key_id"], json!("friend"));
    let request_id = row["request_id"].as_str().expect("request id");
    assert_eq!(request_id.len(), 32);
    assert!(request_id.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_eq!(row["attribution"], json!("verified"));
}

#[test]
fn a_non_stream_row_translates_the_anthropic_usage_block() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: message_response(Some(json!({
            "input_tokens": 3,
            "output_tokens": 2,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 4,
        }))),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(false)).expect("non-stream messages");
    assert_eq!(response.status, 200);
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert_time(row["time"].as_str().expect("time"));
    assert_eq!(row["service"], json!("chat2api"));
    assert_eq!(row["model"], json!("claude-x"));
    assert_eq!(row["stream"], json!(false));
    assert_eq!(row["status"], json!("ok"));
    assert_eq!(row["input_tokens"], json!(8));
    assert_eq!(row["output_tokens"], json!(2));
    assert_eq!(row["cached_input_tokens"], json!(1));
    assert_eq!(row["cache_report_status"], json!("reported"));
    assert_eq!(row["reasoning_tokens"], json!(0));
    assert_eq!(row["total_tokens"], json!(10));
    assert_eq!(row["estimated"], json!(false));
    assert!(row["duration_ms"].as_u64().is_some());
    assert_eq!(row["usage_source"], json!("upstream"));
    assert_eq!(row["protocol"], json!("messages"));
    assert_verified_attribution(row);
}

#[test]
fn a_non_stream_row_without_usage_keeps_the_estimate_and_coverage() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: message_response(None),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(false)).expect("non-stream messages");
    assert_eq!(response.status, 200);
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    assert_eq!(row["input_tokens"], json!(5));
    assert_eq!(row["output_tokens"], json!(1));
    assert_eq!(row["total_tokens"], json!(6));
    assert_eq!(row["cached_input_tokens"], json!(0));
    assert_eq!(row["cache_report_status"], json!("unreported"));
    assert_eq!(row["reasoning_tokens"], json!(0));
    assert_eq!(row["estimated"], json!(true));
    assert_eq!(row["usage_source"], json!("estimated"));
}

#[test]
fn a_stream_row_translates_the_anthropic_usage_events() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let stub = ChatStub::start();
    let events = official_events();
    stub.push(ChatAttempt::Sse {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream messages");
    let _ = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    assert_eq!(row["stream"], json!(true));
    assert_eq!(row["status"], json!("ok"));
    assert_eq!(row["input_tokens"], json!(12));
    assert_eq!(row["output_tokens"], json!(16));
    assert_eq!(row["cached_input_tokens"], json!(2));
    assert_eq!(row["cache_report_status"], json!("reported"));
    assert_eq!(row["total_tokens"], json!(28));
    assert_eq!(row["estimated"], json!(false));
    assert_eq!(row["usage_source"], json!("upstream"));
    assert_eq!(row["protocol"], json!("messages"));
    assert_verified_attribution(row);
}

#[test]
fn a_stream_without_message_stop_is_recorded_as_incomplete() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let stub = ChatStub::start();
    let mut events = official_events();
    events.pop();
    stub.push(ChatAttempt::Sse {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream messages");
    let _ = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    assert_eq!(row["status"], json!("upstream_eof_without_event"));
    assert_eq!(row["stream"], json!(true));
    assert_eq!(row["protocol"], json!("messages"));
}

#[test]
fn an_error_event_is_recorded_as_an_upstream_stream_error() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let stub = ChatStub::start();
    let mut events = official_events();
    events.truncate(2);
    events.push(sse_event(
        "error",
        json!({
            "type": "error",
            "error": {"type": "overloaded_error", "message": "overloaded"},
        }),
    ));
    stub.push(ChatAttempt::Sse {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream messages");
    let _ = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    assert_eq!(row["status"], json!("upstream_stream_error"));
    assert_eq!(row["usage_source"], json!("partial"));
    assert_eq!(row["input_tokens"], json!(12));
    assert_eq!(row["output_tokens"], json!(1));
    assert_eq!(row["total_tokens"], json!(13));
    assert_eq!(row["estimated"], json!(false));
    assert_eq!(row["protocol"], json!("messages"));
}

#[test]
fn a_stream_without_usage_keeps_the_local_estimate() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let stub = ChatStub::start();
    let events = vec![
        sse_event(
            "message_start",
            json!({
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [],
                    "stop_reason": null,
                    "stop_sequence": null,
                },
            }),
        ),
        sse_event(
            "content_block_delta",
            json!({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            }),
        ),
        sse_event("message_stop", json!({"type": "message_stop"})),
    ];
    stub.push(ChatAttempt::Sse {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream messages");
    let _ = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    assert_eq!(row["status"], json!("ok"));
    assert_eq!(row["input_tokens"], json!(5));
    assert_eq!(row["output_tokens"], json!(1));
    assert_eq!(row["total_tokens"], json!(6));
    assert_eq!(row["estimated"], json!(true));
    assert_eq!(row["usage_source"], json!("estimated"));
}

#[test]
fn a_non_200_row_keeps_the_upstream_status_and_no_fabricated_usage() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 400,
        body: "{\"type\":\"error\"}".to_string(),
        retry_after: None,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(false)).expect("messages request");
    assert_eq!(response.status, 400);
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    assert_eq!(row["status"], json!("HTTP400"));
    assert_eq!(row["input_tokens"], json!(5));
    assert_eq!(row["output_tokens"], json!(0));
    assert_eq!(row["estimated"], json!(true));
    assert_eq!(row["usage_source"], json!("estimated"));
    assert_eq!(row["protocol"], json!("messages"));
}

#[test]
fn a_ledger_write_failure_never_blocks_the_proxy() {
    let temp = TempDir::new("um-rust-messages-ledger-");
    let blocker = temp.path().join("blocker");
    std::fs::write(&blocker, b"not a directory").expect("blocker file");
    let ledger = blocker.join("usage.jsonl");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: message_response(Some(json!({"input_tokens": 3, "output_tokens": 2}))),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger);
        config.request_total = Duration::from_secs(5);
    });
    let response =
        client_request(gateway.addr, &request(false)).expect("messages with broken ledger");
    assert_eq!(response.status, 200);
    assert_eq!(response.json()["type"], json!("message"));
    assert!(gateway.server.state().ledger.disabled());
    assert!(read_jsonl(&blocker.join("usage.jsonl")).is_empty());
}
