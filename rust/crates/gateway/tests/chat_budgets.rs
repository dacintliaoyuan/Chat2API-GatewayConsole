//! S1b acceptance 4: first-byte 120 s, one upstream socket silence 180 s and
//! the total request budget 540 s (environment override clamped to 10..3600).
//! A first-byte timeout answers 504 `upstream_timeout`; a retry wait that
//! crosses the total budget answers 504 `request_deadline_exceeded` with
//! `detail.last_upstream_status`.
//!
//! Python constants: `chat2api/chat2api.py:400-402,457-462`. The behavior is
//! `_chat`'s `first_data_deadline`, `chat_call_timeout()` and the producer's
//! `STREAM_SOCKET_TIMEOUT_S` read cap.

mod support;

use std::time::{Duration, Instant};

use serde_json::json;

use support::{
    parse_raw_response, sse_payloads, start_chat_gateway, wait_until, ChatAttempt, ChatChunk,
    ChatStub, ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
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

fn content_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\n".to_vec()
}

#[test]
fn the_timeout_defaults_match_the_contract_fixture() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    let timeout = |name: &str| {
        fixture
            .boundaries
            .timeouts
            .iter()
            .find(|timeout| timeout.surface == "gateway" && timeout.name == name)
            .unwrap_or_else(|| panic!("missing gateway timeout {name}"))
            .value
    };
    let config = um_gateway::config::GatewayConfig::default();
    assert_eq!(
        config.first_byte_timeout.as_secs_f64(),
        timeout("first_byte")
    );
    assert_eq!(
        config.socket_silence_timeout.as_secs_f64(),
        timeout("socket_silence")
    );
    // #61 raised the total budget to 540 seconds. The frozen fixture still
    // records the pre-#61 Python baseline until the Python slice updates it.
    assert_eq!(config.request_total, Duration::from_secs(540));
    assert_eq!(um_gateway::readonly::MAX_REQUEST_SECONDS, 540);

    assert_eq!(
        um_gateway::config::parse_request_total_seconds(None),
        Duration::from_secs(540)
    );
    assert_eq!(
        um_gateway::config::parse_request_total_seconds(Some("9")),
        Duration::from_secs(10)
    );
    assert_eq!(
        um_gateway::config::parse_request_total_seconds(Some("10")),
        Duration::from_secs(10)
    );
    assert_eq!(
        um_gateway::config::parse_request_total_seconds(Some("3600")),
        Duration::from_secs(3600)
    );
    assert_eq!(
        um_gateway::config::parse_request_total_seconds(Some("3601")),
        Duration::from_secs(3600)
    );
    assert_eq!(
        um_gateway::config::parse_request_total_seconds(Some("-1")),
        Duration::from_secs(10)
    );
    assert_eq!(
        um_gateway::config::parse_request_total_seconds(Some("nope")),
        Duration::from_secs(540)
    );
}

#[test]
fn a_silent_upstream_hits_the_first_byte_budget() {
    let temp = TempDir::new("um-rust-chat-budget-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Silent {
        hold: Duration::from_secs(2),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_millis(100);
        config.socket_silence_timeout = Duration::from_millis(500);
        config.request_total = Duration::from_secs(5);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
    });
    let started = Instant::now();
    let response = support::client_request(gateway.addr, &request()).expect("first-byte timeout");
    assert_eq!(response.status, 504);
    assert!(started.elapsed() < Duration::from_secs(1));
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("upstream_timeout"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_first_byte_timeout")
    );
    wait_until(Duration::from_secs(2), || stub.closed());
}

#[test]
fn a_post_commit_silence_hits_the_socket_silence_budget() {
    let temp = TempDir::new("um-rust-chat-budget-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(content.clone())],
        hold: Duration::from_secs(2),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_millis(100);
        config.request_total = Duration::from_secs(5);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let started = Instant::now();
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    assert!(started.elapsed() < Duration::from_secs(2));
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert!(body.starts_with(&content));
    let error = sse_payloads(&body)
        .into_iter()
        .find(|payload| payload.get("error").is_some())
        .expect("in-band error");
    assert_eq!(
        error["error"]["detail"]["reason"],
        json!("upstream_read_error")
    );
    wait_until(Duration::from_secs(2), || stub.closed());
}

#[test]
fn the_total_budget_stops_retrying_before_any_upstream_data() {
    let temp = TempDir::new("um-rust-chat-budget-");
    let stub = ChatStub::start();
    stub.push_all(vec![
        ChatAttempt::HttpStatus {
            status: 502,
            body: "upstream failed".to_string(),
            retry_after: None,
        };
        5
    ]);
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_millis(100);
        config.max_fast_retries = 5;
        config.max_slow_retries = 2;
        config.retry_delay_base = Duration::from_millis(200);
    });
    let started = Instant::now();
    let response = support::client_request(gateway.addr, &request()).expect("total budget");
    assert_eq!(response.status, 504);
    assert!(started.elapsed() < Duration::from_secs(2));
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("request_deadline_exceeded"));
    assert_eq!(body["error"]["detail"]["last_upstream_status"], json!(502));
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn a_committed_stream_stops_at_the_total_budget() {
    let temp = TempDir::new("um-rust-chat-budget-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(content.clone())],
        hold: Duration::from_secs(2),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(5);
        config.request_total = Duration::from_millis(150);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let started = Instant::now();
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    assert!(started.elapsed() < Duration::from_secs(2));
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert!(body.starts_with(&content));
    let error = sse_payloads(&body)
        .into_iter()
        .find(|payload| payload.get("error").is_some())
        .expect("in-band error");
    assert_eq!(error["error"]["code"], json!("request_deadline_exceeded"));
    wait_until(Duration::from_secs(2), || stub.closed());
}
