//! Ticket #61: one retry matrix for Chat, Messages, Responses and Embeddings.
//!
//! The fake UM scripts one status per attempt. The assertions are external:
//! HTTP status, retry headers, error envelope and the number of upstream
//! attempts. No internal retry function is named here.

mod support;

use std::time::{Duration, Instant};

use serde_json::{json, Value};

use um_gateway::retry::{
    classify_status, is_reauthentication_text, is_transient_text, parse_retry_after,
    ReauthenticationTrigger, RetryClass, RETRY_AFTER_MAX,
};

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

fn responses_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/responses",
        &json!({"model": "responses-model", "input": "hi"}).to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn chat_success() -> Value {
    json!({
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
}

fn responses_success() -> Value {
    json!({
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
}

fn retry_budget(config: &mut um_gateway::config::GatewayConfig) {
    config.max_fast_retries = 5;
    config.max_slow_retries = 2;
    config.max_physical_attempts = 12;
    config.retry_delay_base = Duration::ZERO;
    config.request_total = Duration::from_secs(5);
}

fn push_status(stub: &ChatStub, status: u16, retry_after: Option<&str>) {
    stub.push(ChatAttempt::HttpStatus {
        status,
        body: format!("upstream {status}"),
        retry_after: retry_after.map(str::to_string),
    });
}

fn push_json_success(stub: &ChatStub) {
    stub.push(ChatAttempt::Json {
        status: 200,
        body: chat_success(),
    });
}

#[test]
fn full_transient_statuses_retry_once_before_the_success() {
    let temp = TempDir::new("um-rust-retry-full-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, retry_budget);
    for status in [408, 429, 500, 502, 503, 504, 529, 520, 521, 522, 523, 524] {
        push_status(&stub, status, None);
        push_json_success(&stub);
        let response = client_request(gateway.addr, &chat_request()).expect("full retry");
        assert_eq!(
            response.status,
            200,
            "{status}: {}",
            String::from_utf8_lossy(&response.body)
        );
    }
    assert_eq!(
        stub.attempts(),
        24,
        "each transient status was retried exactly once"
    );
}

#[test]
fn a_long_retry_after_is_returned_without_retrying() {
    let temp = TempDir::new("um-rust-retry-after-long-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 429,
        body: "slow down".to_string(),
        retry_after: Some("30".to_string()),
    });
    push_json_success(&stub);
    let gateway = start_chat_gateway(&stub, &temp, true, retry_budget);
    let started = Instant::now();
    let response = client_request(gateway.addr, &chat_request()).expect("long retry-after");
    assert_eq!(response.status, 429);
    assert_eq!(response.header("retry-after"), Some("30"));
    assert_eq!(
        response.json()["error"]["code"],
        json!("upstream_rate_limited")
    );
    assert_eq!(stub.attempts(), 1);
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn a_short_retry_after_waits_and_retries() {
    let temp = TempDir::new("um-rust-retry-after-short-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 429,
        body: "slow down".to_string(),
        retry_after: Some("2".to_string()),
    });
    push_json_success(&stub);
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        retry_budget(config);
        config.request_total = Duration::from_secs(6);
    });
    let started = Instant::now();
    let response =
        client_request_with_timeout(gateway.addr, &chat_request(), Duration::from_secs(10))
            .expect("short retry-after");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(stub.attempts(), 2);
    assert!(
        started.elapsed() >= Duration::from_millis(1500),
        "the Retry-After delay was not observed: {:?}",
        started.elapsed()
    );
}

#[test]
fn x_should_retry_false_stops_the_retry() {
    let temp = TempDir::new("um-rust-should-retry-false-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatusWithHeaders {
        status: 503,
        body: "stop".to_string(),
        retry_after: None,
        should_retry: Some("false".to_string()),
    });
    push_json_success(&stub);
    let gateway = start_chat_gateway(&stub, &temp, true, retry_budget);
    let response = client_request(gateway.addr, &chat_request()).expect("should-retry false");
    assert_eq!(response.status, 503);
    assert_eq!(stub.attempts(), 1);
    assert_eq!(
        response.json()["error"]["code"],
        json!("upstream_unavailable")
    );
}

#[test]
fn quota_and_permission_statuses_are_probed_once_each() {
    for (status, code) in [
        (402, "upstream_quota_exhausted"),
        (403, "upstream_forbidden"),
    ] {
        let temp = TempDir::new("um-rust-probe-");
        let stub = ChatStub::start();
        push_status(&stub, status, None);
        push_status(&stub, status, None);
        push_json_success(&stub);
        let gateway = start_chat_gateway(&stub, &temp, true, |config| {
            retry_budget(config);
            // A probe is separate from the five fast retries.
            config.max_fast_retries = 0;
        });
        let response = client_request(gateway.addr, &chat_request()).expect("probe status");
        assert_eq!(response.status, status, "{status}");
        assert_eq!(response.json()["error"]["code"], json!(code), "{status}");
        assert_eq!(stub.attempts(), 2, "{status} is probed exactly once");
    }
}

#[test]
fn forbidden_authentication_text_is_a_reauthentication_trigger() {
    assert_eq!(
        classify_status(403, Some("Not authenticated")),
        RetryClass::Reauthentication(ReauthenticationTrigger::ForbiddenAuthenticationText)
    );
    assert_eq!(
        classify_status(401, None),
        RetryClass::Reauthentication(ReauthenticationTrigger::Unauthorized)
    );
    assert!(is_reauthentication_text("please log in"));
    assert!(!is_reauthentication_text("forbidden"));
}

#[test]
fn a_400_or_422_is_probed_only_for_transient_text() {
    for status in [400, 422] {
        let temp = TempDir::new("um-rust-conditional-probe-");
        let stub = ChatStub::start();
        stub.push(ChatAttempt::HttpStatus {
            status,
            body: "temporarily unavailable".to_string(),
            retry_after: None,
        });
        push_json_success(&stub);
        let gateway = start_chat_gateway(&stub, &temp, true, |config| {
            retry_budget(config);
            config.max_fast_retries = 0;
        });
        let response = client_request(gateway.addr, &chat_request()).expect("conditional probe");
        assert_eq!(response.status, 200, "{status}");
        assert_eq!(stub.attempts(), 2, "{status}");
    }

    for status in [400, 422] {
        let temp = TempDir::new("um-rust-conditional-stop-");
        let stub = ChatStub::start();
        push_status(&stub, status, None);
        push_json_success(&stub);
        let gateway = start_chat_gateway(&stub, &temp, true, retry_budget);
        let response = client_request(gateway.addr, &chat_request()).expect("conditional stop");
        assert_eq!(response.status, status, "{status}");
        assert_eq!(stub.attempts(), 1, "{status}");
    }

    for text in [
        "rate limit",
        "too many requests",
        "overloaded",
        "try again",
        "temporarily",
        "unavailable",
        "cloudflare",
        "challenge",
    ] {
        assert!(is_transient_text(text), "{text}");
    }
    assert!(!is_transient_text("invalid request"));
}

#[test]
fn non_retryable_statuses_are_returned_immediately() {
    for status in [404, 405, 409, 413, 415] {
        let temp = TempDir::new("um-rust-no-retry-");
        let stub = ChatStub::start();
        push_status(&stub, status, None);
        push_json_success(&stub);
        let gateway = start_chat_gateway(&stub, &temp, true, retry_budget);
        let response = client_request(gateway.addr, &chat_request()).expect("no retry");
        assert_eq!(response.status, status, "{status}");
        assert_eq!(stub.attempts(), 1, "{status} must not retry");
    }
}

#[test]
fn upstream_401_is_not_a_transient_retry() {
    let temp = TempDir::new("um-rust-no-retry-401-");
    let stub = ChatStub::start();
    push_status(&stub, 401, None);
    push_json_success(&stub);
    let gateway = start_chat_gateway(&stub, &temp, true, retry_budget);
    let response = client_request(gateway.addr, &chat_request()).expect("401");
    assert_eq!(response.status, 503);
    assert_eq!(
        response.json()["error"]["code"],
        json!("reauthentication_required")
    );
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn responses_non_200_uses_the_same_retry_matrix() {
    let temp = TempDir::new("um-rust-responses-retry-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 503,
        body: json!({"detail": "overloaded"}),
    });
    stub.push(ChatAttempt::Json {
        status: 200,
        body: responses_success(),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, retry_budget);
    let response = client_request(gateway.addr, &responses_request()).expect("responses retry");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn retry_after_parsing_and_the_eight_second_bound() {
    assert_eq!(parse_retry_after(Some("2")), Some(Duration::from_secs(2)));
    assert_eq!(parse_retry_after(Some("8")), Some(RETRY_AFTER_MAX));
    assert!(parse_retry_after(Some("30")).unwrap() > RETRY_AFTER_MAX);
    assert_eq!(parse_retry_after(Some("not a date")), None);
    assert_eq!(parse_retry_after(None), None);
}
