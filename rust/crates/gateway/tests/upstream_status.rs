//! Ticket #60: preserved upstream statuses and protocol-shaped error bodies.
//!
//! The fake UM answers with a scripted 4xx/5xx sequence; Chat and Responses
//! rebuild the OpenAI envelope, `/v1/messages` rebuilds the Anthropic envelope,
//! and `detail` carries `upstream_status` plus a bounded `upstream_message`.
//! Transport failures stay 502; the gateway's own waits are 504.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, client_request_with_timeout, start_chat_gateway, ChatAttempt, ChatStub,
    ClientRequest, TempDir, SHARE_SECRET,
};

fn chat_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn chat_stream_request() -> ClientRequest {
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

fn responses_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/responses",
        &json!({"model": "responses-model", "input": "hi"}).to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn messages_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/messages",
        &json!({
            "model": "claude-x",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
    .header("Anthropic-Version", "2023-06-01")
}

fn messages_stream_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/messages",
        &json!({
            "model": "claude-x",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": true,
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
    .header("Anthropic-Version", "2023-06-01")
}

fn expected_status_shape(status: u16) -> (&'static str, &'static str) {
    match status {
        402 => ("rate_limit_error", "upstream_quota_exhausted"),
        403 => ("permission_error", "upstream_forbidden"),
        429 => ("rate_limit_error", "upstream_rate_limited"),
        504 => ("upstream_error", "upstream_timeout"),
        400..=499 => ("invalid_request_error", "upstream_request_rejected"),
        _ => ("upstream_error", "upstream_unavailable"),
    }
}

fn no_retries(config: &mut um_gateway::config::GatewayConfig) {
    config.max_fast_retries = 0;
    config.max_slow_retries = 0;
    config.max_physical_attempts = 1;
    config.request_total = Duration::from_secs(5);
}

fn assert_preserved_status(
    response: &support::ClientResponse,
    status: u16,
    upstream_body: &str,
    anthropic: bool,
) {
    assert_eq!(
        response.status,
        status,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        response.header("content-type"),
        Some("application/json; charset=utf-8")
    );
    let body = response.json();
    let error = if anthropic {
        assert_eq!(body["type"], json!("error"), "{body}");
        &body["error"]
    } else {
        body.get("error")
            .unwrap_or_else(|| panic!("missing error object: {body}"))
    };
    let (category, code) = expected_status_shape(status);
    assert_eq!(error["type"], json!(category), "{body}");
    assert_eq!(error["code"], json!(code), "{body}");
    assert_eq!(error["param"], Value::Null, "{body}");
    assert_eq!(error["detail"]["upstream_status"], json!(status), "{body}");
    assert_eq!(
        error["detail"]["upstream_message"],
        json!(upstream_body),
        "{body}"
    );
    assert_eq!(error["upstream_status"], json!(status), "{body}");
}

#[test]
fn chat_preserves_each_upstream_4xx_and_5xx_status() {
    let temp = TempDir::new("um-rust-status-chat-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, no_retries);
    for status in [403, 404, 409, 413, 415, 429, 500, 502, 503, 504] {
        let upstream_body = format!("upstream status {status}");
        stub.push(ChatAttempt::HttpStatus {
            status,
            body: upstream_body.clone(),
            retry_after: None,
        });
        let response = client_request(gateway.addr, &chat_request()).expect("chat request");
        assert_preserved_status(&response, status, &upstream_body, false);
    }
    assert_eq!(stub.attempts(), 10);
}

#[test]
fn responses_preserves_each_upstream_4xx_and_5xx_status() {
    let temp = TempDir::new("um-rust-status-responses-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, no_retries);
    for status in [403, 404, 409, 413, 415, 429, 500, 502, 503, 504] {
        let upstream_body = format!("upstream status {status}");
        stub.push(ChatAttempt::HttpStatus {
            status,
            body: upstream_body.clone(),
            retry_after: None,
        });
        let response =
            client_request(gateway.addr, &responses_request()).expect("responses request");
        assert_preserved_status(&response, status, &upstream_body, false);
    }
    assert_eq!(stub.attempts(), 10);
}

#[test]
fn messages_preserves_each_upstream_4xx_and_5xx_status_in_the_anthropic_shape() {
    let temp = TempDir::new("um-rust-status-messages-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, no_retries);
    for status in [403, 404, 409, 413, 415, 429, 500, 502, 503, 504] {
        let upstream_body = format!("upstream status {status}");
        stub.push(ChatAttempt::HttpStatus {
            status,
            body: upstream_body.clone(),
            retry_after: None,
        });
        let response = client_request(gateway.addr, &messages_request()).expect("messages request");
        assert_preserved_status(&response, status, &upstream_body, true);
    }
    assert_eq!(stub.attempts(), 10);
}

#[test]
fn upstream_401_is_a_503_reauthentication_error_with_retry_after() {
    let temp = TempDir::new("um-rust-status-401-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, no_retries);
    for (request, anthropic) in [
        (chat_request(), false),
        (responses_request(), false),
        (messages_request(), true),
    ] {
        stub.push(ChatAttempt::HttpStatus {
            status: 401,
            body: "{\"detail\":\"expired\"}".to_string(),
            retry_after: Some("17".to_string()),
        });
        let response = client_request(gateway.addr, &request).expect("upstream 401");
        assert_eq!(response.status, 503);
        assert_eq!(response.header("retry-after"), Some("17"));
        let body = response.json();
        let error = if anthropic {
            assert_eq!(body["type"], json!("error"), "{body}");
            &body["error"]
        } else {
            &body["error"]
        };
        assert_eq!(error["type"], json!("authentication_error"), "{body}");
        assert_eq!(error["code"], json!("reauthentication_required"), "{body}");
        assert_eq!(error["detail"], json!("upstream_rejected"), "{body}");
        assert!(
            error.get("upstream_status").is_none(),
            "401 stays the gateway's own state: {body}"
        );
    }
    assert_eq!(stub.attempts(), 3);
}

#[test]
fn a_connection_failure_is_a_502_for_every_client_protocol() {
    let temp = TempDir::new("um-rust-status-transport-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, no_retries);
    drop(stub);
    for request in [chat_request(), responses_request(), messages_request()] {
        let response = client_request(gateway.addr, &request).expect("connection failure");
        assert_eq!(response.status, 502);
        let body = response.json();
        let error = body.get("error").unwrap_or(&Value::Null);
        assert_eq!(error["type"], json!("upstream_error"), "{body}");
        assert_eq!(error["code"], json!("upstream_unavailable"), "{body}");
        assert_eq!(
            error["detail"]["reason"],
            json!("ConnectionError"),
            "{body}"
        );
        assert!(error.get("upstream_status").is_none(), "{body}");
    }
}

#[test]
fn a_response_head_wait_is_a_504_upstream_timeout() {
    let temp = TempDir::new("um-rust-status-head-timeout-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HeadSilent {
        hold: Duration::from_secs(2),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        no_retries(config);
        config.request_total = Duration::from_millis(150);
    });
    let response =
        client_request_with_timeout(gateway.addr, &chat_request(), Duration::from_secs(5))
            .expect("head timeout");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"), "{body}");
    assert_eq!(body["error"]["code"], json!("upstream_timeout"), "{body}");
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("Timeout"),
        "{body}"
    );
}

#[test]
fn a_first_byte_timeout_is_a_504_upstream_timeout() {
    let temp = TempDir::new("um-rust-status-first-byte-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Silent {
        hold: Duration::from_secs(2),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        no_retries(config);
        config.first_byte_timeout = Duration::from_millis(100);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let response =
        client_request_with_timeout(gateway.addr, &chat_stream_request(), Duration::from_secs(5))
            .expect("first-byte timeout");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"), "{body}");
    assert_eq!(body["error"]["code"], json!("upstream_timeout"), "{body}");
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_first_byte_timeout"),
        "{body}"
    );
}

#[test]
fn retry_exhaustion_returns_the_last_upstream_status() {
    let temp = TempDir::new("um-rust-status-retry-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 503,
        body: "first attempt".to_string(),
        retry_after: None,
    });
    stub.push(ChatAttempt::HttpStatus {
        status: 504,
        body: "last attempt".to_string(),
        retry_after: None,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
        config.retry_delay_base = Duration::ZERO;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &chat_request()).expect("retry exhaustion");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"), "{body}");
    assert_eq!(body["error"]["code"], json!("upstream_timeout"), "{body}");
    assert_eq!(
        body["error"]["detail"]["upstream_status"],
        json!(504),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["upstream_message"],
        json!("last attempt"),
        "{body}"
    );
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn transport_failure_after_retries_stays_a_502() {
    let temp = TempDir::new("um-rust-status-retry-transport-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
        config.retry_delay_base = Duration::ZERO;
        config.request_total = Duration::from_secs(5);
    });
    drop(stub);
    let response = client_request(gateway.addr, &chat_request()).expect("transport retries");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"), "{body}");
    assert_eq!(
        body["error"]["code"],
        json!("upstream_unavailable"),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("ConnectionError"),
        "{body}"
    );
}

#[test]
fn a_messages_first_byte_timeout_is_a_504_anthropic_error() {
    let temp = TempDir::new("um-rust-status-messages-timeout-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Silent {
        hold: Duration::from_secs(2),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        no_retries(config);
        config.first_byte_timeout = Duration::from_millis(100);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let response = client_request_with_timeout(
        gateway.addr,
        &messages_stream_request(),
        Duration::from_secs(5),
    )
    .expect("messages first-byte timeout");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(body["type"], json!("error"), "{body}");
    assert_eq!(body["error"]["type"], json!("upstream_error"), "{body}");
    assert_eq!(body["error"]["code"], json!("upstream_timeout"), "{body}");
    assert_eq!(
        body["error"]["detail"],
        json!("upstream_first_byte_timeout"),
        "{body}"
    );
}

#[test]
fn a_responses_head_wait_is_a_504_upstream_timeout() {
    let temp = TempDir::new("um-rust-status-responses-timeout-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HeadSilent {
        hold: Duration::from_secs(2),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        no_retries(config);
        config.request_total = Duration::from_millis(150);
    });
    let response =
        client_request_with_timeout(gateway.addr, &responses_request(), Duration::from_secs(5))
            .expect("responses head timeout");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_error"), "{body}");
    assert_eq!(body["error"]["code"], json!("upstream_timeout"), "{body}");
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("Timeout"),
        "{body}"
    );
}

#[test]
fn messages_retry_exhaustion_returns_the_last_status() {
    let temp = TempDir::new("um-rust-status-messages-retry-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 503,
        body: "first attempt".to_string(),
        retry_after: None,
    });
    stub.push(ChatAttempt::HttpStatus {
        status: 504,
        body: "last attempt".to_string(),
        retry_after: None,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 12;
        config.retry_delay_base = Duration::ZERO;
        config.request_total = Duration::from_secs(5);
    });
    let response =
        client_request(gateway.addr, &messages_request()).expect("messages retry exhaustion");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(body["type"], json!("error"), "{body}");
    assert_eq!(body["error"]["type"], json!("upstream_error"), "{body}");
    assert_eq!(body["error"]["code"], json!("upstream_timeout"), "{body}");
    assert_eq!(
        body["error"]["detail"]["upstream_status"],
        json!(504),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["upstream_message"],
        json!("last attempt"),
        "{body}"
    );
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn upstream_message_is_single_line_and_bounded() {
    let temp = TempDir::new("um-rust-status-bounded-");
    let stub = ChatStub::start();
    let long = format!("{}\nsecond line", "x".repeat(1000));
    stub.push(ChatAttempt::HttpStatus {
        status: 500,
        body: long,
        retry_after: None,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, no_retries);
    let response = client_request(gateway.addr, &chat_request()).expect("bounded message");
    assert_eq!(response.status, 500);
    let message = response.json()["error"]["detail"]["upstream_message"]
        .as_str()
        .expect("upstream_message string")
        .to_string();
    assert_eq!(message.chars().count(), 200);
    assert!(!message.contains('\n'));
    assert_eq!(message, "x".repeat(200));
}
