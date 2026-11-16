//! S1b acceptance 3: pre-commit failures answer 502; post-commit failures
//! write exactly one in-band `error` event and end the stream.
//!
//! Python behavior: `Handler._chat` maps a zero-data failure to HTTP 502
//! (`chat2api/chat2api.py:9646-9660`) and a committed failure to
//! `_stream_error_event` (`chat2api/chat2api.py:9680-9690`). A partial
//! completion is never reported as a success.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    parse_raw_response, sse_payloads, start_chat_gateway, ChatAttempt, ChatChunk, ChatStub,
    ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
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
    b"data: {\"choices\":[{\"delta\":{\"content\":\"partial\"},\"finish_reason\":null}]}\n\n"
        .to_vec()
}

fn finish_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec()
}

fn done_event() -> Vec<u8> {
    b"data: [DONE]\n\n".to_vec()
}

fn explicit_error_event() -> Vec<u8> {
    b"data: {\"error\":{\"message\":\"upstream boom\"}}\n\n".to_vec()
}

fn gateway(stub: &ChatStub, temp: &TempDir, retries: u32) -> support::Gateway {
    start_chat_gateway(stub, temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
        config.max_fast_retries = retries;
        config.max_slow_retries = 0;
        config.retry_delay_base = Duration::ZERO;
    })
}

fn in_band_error(body: &[u8]) -> Value {
    sse_payloads(body)
        .into_iter()
        .find(|payload| payload.get("error").is_some())
        .expect("one in-band error event")
}

#[test]
fn a_zero_data_eof_before_the_200_is_502() {
    let temp = TempDir::new("um-rust-chat-errors-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(metadata_event())],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, 0);
    let response = support::client_request(gateway.addr, &request()).expect("pre-commit EOF");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_eof_without_done")
    );
    assert_eq!(
        body["error"]["message"],
        json!("Upstream closed the stream before the completion finished (no [DONE] received).")
    );
}

#[test]
fn an_explicit_error_event_before_the_200_is_502() {
    let temp = TempDir::new("um-rust-chat-errors-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(explicit_error_event()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, 0);
    let response = support::client_request(gateway.addr, &request()).expect("pre-commit error");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_stream_error")
    );
    assert_eq!(
        body["error"]["detail"]["upstream_message"],
        json!("upstream boom")
    );
}

#[test]
fn a_post_commit_eof_writes_one_in_band_error_and_no_done() {
    let temp = TempDir::new("um-rust-chat-errors-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(content.clone())],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, 0);
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert!(body.starts_with(&content), "content was forwarded");
    assert!(
        !body.windows(12).any(|window| window == b"data: [DONE]"),
        "a failed stream must not forward [DONE]"
    );
    let error = in_band_error(&body);
    assert_eq!(error["error"]["type"], json!("upstream_error"));
    assert_eq!(error["error"]["code"], json!("upstream_eof_without_done"));
    assert_eq!(
        error["error"]["detail"]["reason"],
        json!("upstream_eof_without_done")
    );
    let header_id = headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("x-request-id"))
        .map(|(_, value)| value.as_str())
        .expect("X-Request-Id header");
    assert_eq!(error["error"]["detail"]["request_id"], json!(header_id));
}

#[test]
fn a_post_commit_explicit_error_keeps_its_sanitized_message() {
    let temp = TempDir::new("um-rust-chat-errors-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(content_event()),
            ChatChunk::now(explicit_error_event()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, 0);
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let error = in_band_error(&body);
    assert_eq!(error["error"]["type"], json!("upstream_error"));
    assert_eq!(error["error"]["code"], json!("upstream_stream_error"));
    assert_eq!(error["error"]["param"], Value::Null);
    assert_eq!(
        error["error"]["detail"]["upstream_message"],
        json!("upstream boom")
    );
    assert!(
        !body.windows(12).any(|window| window == b"data: [DONE]"),
        "an in-band error must not be followed by [DONE]"
    );
    let header_id = headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("x-request-id"))
        .map(|(_, value)| value.as_str())
        .expect("X-Request-Id header");
    assert_eq!(error["error"]["detail"]["request_id"], json!(header_id));
    assert_eq!(
        sse_payloads(&body)
            .into_iter()
            .filter(|payload| payload.get("error").is_some())
            .count(),
        1,
        "exactly one in-band error event"
    );
}

#[test]
fn a_done_without_finish_reason_is_an_incomplete_stream() {
    let temp = TempDir::new("um-rust-chat-errors-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(content.clone()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, 0);
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert!(body.starts_with(&content));
    assert!(!body.windows(12).any(|window| window == b"data: [DONE]"));
    let error = in_band_error(&body);
    assert_eq!(
        error["error"]["code"],
        json!("upstream_missing_finish_reason")
    );
}

#[test]
fn an_empty_finish_reason_is_not_a_finish_reason() {
    let temp = TempDir::new("um-rust-chat-errors-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(content.clone()),
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"oops\"},\"finish_reason\":\"\"}]}

"
                .to_vec(),
            ),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, 0);
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let error = in_band_error(&body);
    assert_eq!(
        error["error"]["code"],
        json!("upstream_missing_finish_reason")
    );
}

#[test]
fn a_successful_stream_never_writes_an_error_event() {
    let temp = TempDir::new("um-rust-chat-errors-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(content.clone()),
            ChatChunk::now(finish_event()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, 0);
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert!(!sse_payloads(&body)
        .iter()
        .any(|payload| payload.get("error").is_some()));
}
