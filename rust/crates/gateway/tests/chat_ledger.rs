//! S1b acceptance 7: the share ledger row keeps the Python
//! `chat2api.record_usage` field set and values, and a write failure never
//! blocks the proxy.
//!
//! Python row shape and normalization: `chat2api/chat2api.py:3316-3401` and
//! `_normalized_usage_fields` at `chat2api/chat2api.py:3253-3284`. The failure
//! behavior is the `_usage_disabled` fallback at `chat2api/chat2api.py:3391-3401`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, read_jsonl, start_chat_gateway, wait_jsonl, ChatAttempt, ChatChunk, ChatStub,
    ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
};

fn request(stream: bool) -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
            "max_tokens": 16,
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn usage_body(usage: Option<Value>) -> Value {
    let mut body = json!({
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "model": "fixture-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop",
        }],
    });
    if let Some(usage) = usage {
        body["usage"] = usage;
    }
    body
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
    assert_eq!(request_id.len(), 16);
    assert!(request_id.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_eq!(row["attribution"], json!("verified"));
}

#[test]
fn desktop_chat_failure_links_its_single_ledger_row_to_the_runtime_log() {
    use um_gateway::Role;

    let temp = TempDir::new("desktop-chat-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 400,
        body: json!({"error": {"message": "fixture invalid request"}}),
    });
    let ledger = temp.path().join("usage.jsonl");
    let service = temp.path().join("service.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.role = Role::Local;
        config.desktop_instance = true;
        config.usage_log = Some(ledger.clone());
        config.service_log = Some(service.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(false)).expect("desktop chat");
    assert!(response.status >= 400);
    let usage = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let failures = wait_jsonl(&service, 2, Duration::from_secs(2));
    assert_eq!(usage.len(), 1);
    assert_eq!(usage[0]["usage_source"], "none");
    let id = usage[0]["request_id"].as_str().expect("HTTP request ID");
    assert_eq!(id.len(), 32);
    let summary = failures
        .iter()
        .find(|row| row["event"] == "request_summary")
        .expect("request summary after the upstream attempt event");
    assert_eq!(summary["request_id"], id);
    assert_eq!(summary["final"], usage[0]["status"]);
}

#[test]
fn a_non_stream_row_matches_the_python_field_set_and_values() {
    let temp = TempDir::new("um-rust-chat-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: usage_body(Some(json!({
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        }))),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(false)).expect("non-stream chat");
    assert_eq!(response.status, 200);
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert_time(row["time"].as_str().expect("time"));
    assert_eq!(row["service"], json!("chat2api"));
    assert_eq!(row["model"], json!("fixture-model"));
    assert_eq!(row["stream"], json!(false));
    assert_eq!(row["status"], json!("ok"));
    assert_eq!(row["input_tokens"], json!(11));
    assert_eq!(row["output_tokens"], json!(7));
    assert_eq!(row["cached_input_tokens"], json!(3));
    assert_eq!(row["cache_report_status"], json!("reported"));
    assert_eq!(row["reasoning_tokens"], json!(2));
    assert_eq!(row["total_tokens"], json!(18));
    assert_eq!(row["estimated"], json!(false));
    assert!(row["duration_ms"].as_u64().is_some());
    assert_eq!(row["usage_source"], json!("upstream"));
    assert_eq!(row["protocol"], json!("chat"));
    assert!(
        row.get("first_token_ms").is_none(),
        "a non-stream row has no first-token time"
    );
    assert_verified_attribution(row);
}

#[test]
fn a_finish_only_stream_has_no_first_token_sample() {
    let temp = TempDir::new("um-rust-chat-ledger-finish-only-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec(),
            ),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
    });
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream chat");
    client.read_all(Duration::from_secs(5)).expect("read stream");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("ok"));
    assert!(rows[0].get("first_token_ms").is_none());
}

#[test]
fn a_stream_row_records_the_first_token_time() {
    let temp = TempDir::new("um-rust-chat-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::after(
                Duration::from_millis(120),
                b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec(),
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
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream chat");
    let _ = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    let first = row["first_token_ms"]
        .as_u64()
        .expect("the stream row records the first token time");
    let duration = row["duration_ms"].as_u64().expect("duration");
    assert!(
        first >= 80,
        "the first token waits for the delayed chunk: {first}"
    );
    assert!(
        first <= duration,
        "first token {first} <= duration {duration}"
    );
}

#[test]
fn an_unreported_usage_row_keeps_the_estimate_and_coverage() {
    let temp = TempDir::new("um-rust-chat-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: usage_body(None),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(false)).expect("non-stream chat");
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
fn a_stream_row_uses_the_upstream_usage_and_the_stream_status() {
    let temp = TempDir::new("um-rust-chat-ledger-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec(),
            ),
            ChatChunk::now(
                b"data: {\"choices\":[],\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":7,\"total_tokens\":18}}\n\n"
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
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream chat");
    let _ = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    let row = &rows[0];
    assert_eq!(row["stream"], json!(true));
    assert_eq!(row["status"], json!("ok"));
    assert_eq!(row["input_tokens"], json!(11));
    assert_eq!(row["output_tokens"], json!(7));
    assert_eq!(row["total_tokens"], json!(18));
    assert_eq!(row["usage_source"], json!("upstream"));
    assert_eq!(row["estimated"], json!(false));
    assert_eq!(row["protocol"], json!("chat"));
    assert_verified_attribution(row);
}

#[test]
fn a_ledger_write_failure_never_blocks_the_proxy() {
    let temp = TempDir::new("um-rust-chat-ledger-");
    let blocker = temp.path().join("blocker");
    std::fs::write(&blocker, b"not a directory").expect("blocker file");
    let ledger = blocker.join("usage.jsonl");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: usage_body(Some(json!({
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }))),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger);
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(false)).expect("chat with broken ledger");
    assert_eq!(response.status, 200);
    assert_eq!(
        response.json()["choices"][0]["message"]["content"],
        json!("OK")
    );
    assert!(gateway.server.state().ledger.disabled());
    assert!(read_jsonl(&temp.path().join("blocker").join("usage.jsonl")).is_empty());
}
