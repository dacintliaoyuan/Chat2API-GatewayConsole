//! S1b acceptance 6: a client disconnect is checked at every byte, closes the
//! upstream and writes `client_disconnected` to the ledger.
//!
//! Python behavior: `_client_disconnected` is probed on every producer byte and
//! every consumer poll (`chat2api/chat2api.py:5621-5640,6748-6750,9536-9544`);
//! the producer closes the upstream in its own `finally`
//! (`chat2api/chat2api.py:6874-6893`); the ledger row is
//! `record_usage(..., status="client_disconnected")` (`chat2api/chat2api.py:9173-9181`).
//! ADR 0004 removed the reader cap, so a disconnect never frees a permit that
//! another request could reuse.

mod support;

use std::time::Duration;

use serde_json::json;

use support::{
    parse_raw_response, read_jsonl, start_chat_gateway, wait_jsonl, wait_until, ChatAttempt,
    ChatChunk, ChatStub, ClientRequest, StreamingClient, TempDir, SECOND_SECRET, SHARE_SECRET,
};

fn request() -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": true,
            "max_tokens": 16,
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn metadata_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n"
        .to_vec()
}

fn content_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\n".to_vec()
}

fn success_stream() -> ChatAttempt {
    ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(content_event()),
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec(),
            ),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    }
}

#[test]
fn a_post_commit_disconnect_closes_the_upstream() {
    let temp = TempDir::new("um-rust-chat-disconnect-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(content.clone())],
        hold: Duration::from_secs(2),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_millis(300);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let raw = client
        .read_until(b"\"content\":\"OK\"", Duration::from_secs(2))
        .expect("read content");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert!(body.starts_with(&content));
    drop(client);

    wait_until(Duration::from_secs(2), || stub.closed());
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows.len(), 1, "exactly one ledger row");
    assert_eq!(rows[0]["status"], json!("client_disconnected"));
    assert_eq!(rows[0]["stream"], json!(true));
    assert_eq!(rows[0]["protocol"], json!("chat"));
    assert_eq!(rows[0]["attribution"], json!("verified"));
    assert_eq!(rows[0]["key_id"], json!("friend"));
    let request_id = rows[0]["request_id"].as_str().expect("request id");
    assert_eq!(request_id.len(), 16);
    assert!(request_id.bytes().all(|byte| byte.is_ascii_hexdigit()));

    // There is no reader cap: a second stream is admitted even if the first
    // upstream has not closed yet.
    stub.push(success_stream());
    let second = StreamingClient::send(gateway.addr, &request()).expect("second stream");
    let mut second = second;
    let raw = second
        .read_all(Duration::from_secs(5))
        .expect("second stream body");
    let (status, _, _) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn a_second_stream_is_admitted_while_the_first_upstream_is_still_open() {
    let temp = TempDir::new("um-rust-chat-disconnect-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(content.clone())],
        hold: Duration::from_secs(2),
    });
    stub.push(success_stream());
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_millis(300);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
    });
    let mut first = StreamingClient::send(gateway.addr, &request()).expect("first stream");
    first
        .read_until(b"\"content\":\"OK\"", Duration::from_secs(2))
        .expect("first content");
    assert!(!stub.closed(), "the first upstream is still open");

    let mut second_request = request();
    second_request
        .headers
        .retain(|(name, _)| name != "Authorization");
    second_request.headers.push((
        "Authorization".to_string(),
        format!("Bearer {SECOND_SECRET}"),
    ));
    let mut second = StreamingClient::send(gateway.addr, &second_request).expect("second stream");
    let raw = second
        .read_until(b"\"content\":\"OK\"", Duration::from_secs(2))
        .expect("second stream must not be refused by a reader cap");
    let (status, _, _) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(stub.attempts(), 2);
    drop(first);
}

#[test]
fn a_pre_commit_disconnect_records_client_disconnected_without_a_200() {
    let temp = TempDir::new("um-rust-chat-disconnect-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(metadata_event())],
        hold: Duration::from_secs(2),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_millis(300);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    assert_eq!(
        client
            .read_some(Duration::from_millis(100))
            .expect("first read"),
        None,
        "no 200 was committed before the disconnect"
    );
    drop(client);

    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows.len(), 1, "exactly one ledger row");
    assert_eq!(rows[0]["status"], json!("client_disconnected"));
    assert_eq!(rows[0]["stream"], json!(true));
    assert_eq!(rows[0]["estimated"], json!(true));
    assert_eq!(read_jsonl(&ledger).len(), 1);
    wait_until(Duration::from_secs(2), || stub.closed());
}
