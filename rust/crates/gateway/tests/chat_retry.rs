//! S1b acceptance 5: fast failures at most 5, slow failures at most 2,
//! physical attempts at most 12, and retries only before any SSE event.
//!
//! Python constants and branches: `chat2api/chat2api.py:522-566,8545-8666,9146-9301`.
//! `UPSTREAM_AUTO_RETRY`/`UPSTREAM_AUTO_RETRIES`/`UPSTREAM_SLOW_AUTO_RETRIES`/
//! `MAX_TOTAL_ATTEMPTS` and `_UPSTREAM_RETRYABLE_STATUS` are the frozen values.

mod support;

use std::time::{Duration, Instant};

use serde_json::json;

use support::{
    parse_raw_response, sse_payloads, start_chat_gateway, ChatAttempt, ChatChunk, ChatStub,
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

fn metadata_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n"
        .to_vec()
}

fn content_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\n".to_vec()
}

fn finish_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec()
}

fn done_event() -> Vec<u8> {
    b"data: [DONE]\n\n".to_vec()
}

fn success_stream() -> ChatAttempt {
    ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(content_event()),
            ChatChunk::now(finish_event()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    }
}

#[test]
fn fast_failures_retry_five_times() {
    let temp = TempDir::new("um-rust-chat-retry-");
    let stub = ChatStub::start();
    stub.push_all(vec![
        ChatAttempt::HttpStatus {
            status: 502,
            body: "upstream failed".to_string(),
            retry_after: None,
        };
        8
    ]);
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.retry_delay_base = Duration::ZERO;
        config.slow_retry_threshold = Duration::from_secs(30);
        config.max_fast_retries = 5;
        config.max_slow_retries = 2;
        config.max_physical_attempts = 12;
    });
    let response = support::client_request(gateway.addr, &request(false)).expect("fast retries");
    assert_eq!(response.status, 502);
    assert_eq!(stub.attempts(), 6, "one attempt plus five fast retries");
}

#[test]
fn slow_failures_retry_twice() {
    let temp = TempDir::new("um-rust-chat-retry-");
    let stub = ChatStub::start();
    stub.push_all(vec![
        ChatAttempt::DelayedHttpStatus {
            delay: Duration::from_millis(20),
            status: 504,
            body: "slow upstream".to_string(),
        };
        5
    ]);
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.retry_delay_base = Duration::ZERO;
        config.slow_retry_threshold = Duration::from_millis(1);
        config.max_fast_retries = 5;
        config.max_slow_retries = 2;
        config.max_physical_attempts = 12;
    });
    let response = support::client_request(gateway.addr, &request(false)).expect("slow retries");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(body["error"]["code"], json!("upstream_timeout"));
    assert_eq!(body["error"]["detail"]["upstream_status"], json!(504));
    assert_eq!(stub.attempts(), 3, "one attempt plus two slow retries");
}

#[test]
fn physical_attempts_stop_at_twelve() {
    let temp = TempDir::new("um-rust-chat-retry-");
    let stub = ChatStub::start();
    stub.push_all(vec![
        ChatAttempt::HttpStatus {
            status: 502,
            body: "upstream failed".to_string(),
            retry_after: None,
        };
        20
    ]);
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.retry_delay_base = Duration::ZERO;
        config.slow_retry_threshold = Duration::from_secs(30);
        config.max_fast_retries = 20;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
    });
    let response = support::client_request(gateway.addr, &request(false)).expect("physical cap");
    assert_eq!(response.status, 502);
    assert_eq!(stub.attempts(), 12, "the physical attempt cap");
}

#[test]
fn a_pre_commit_eof_is_retried_before_any_sse_event() {
    let temp = TempDir::new("um-rust-chat-retry-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(metadata_event())],
        hold: Duration::ZERO,
    });
    stub.push(success_stream());
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.retry_delay_base = Duration::ZERO;
        config.slow_retry_threshold = Duration::from_secs(30);
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
    });
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(stub.attempts(), 2, "the zero-data attempt was retried once");
    assert_eq!(
        sse_payloads(&body)
            .into_iter()
            .filter(|payload| { payload["choices"][0]["delta"]["content"] == json!("OK") })
            .count(),
        1,
        "the retry produced one completion, not two"
    );
}

#[test]
fn a_committed_stream_is_never_retried() {
    let temp = TempDir::new("um-rust-chat-retry-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(content_event())],
        hold: Duration::ZERO,
    });
    stub.push(success_stream());
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.retry_delay_base = Duration::ZERO;
        config.max_fast_retries = 5;
        config.max_slow_retries = 2;
        config.max_physical_attempts = 12;
    });
    let mut client = StreamingClient::send(gateway.addr, &request(true)).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(
        stub.attempts(),
        1,
        "visible output was never replayed after commit"
    );
    let error = sse_payloads(&body)
        .into_iter()
        .find(|payload| payload.get("error").is_some())
        .expect("in-band error");
    assert_eq!(error["error"]["code"], json!("upstream_eof_without_done"));
    assert_eq!(
        sse_payloads(&body)
            .into_iter()
            .filter(|payload| payload.get("error").is_some())
            .count(),
        1,
        "exactly one in-band error"
    );
    let _ = Instant::now();
}
