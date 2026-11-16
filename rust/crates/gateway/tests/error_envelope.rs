//! Ticket #59: the HTTP boundary uses one protocol-aware error envelope.
//!
//! OpenAI-shaped routes carry `message`, `type` (category), `code` (machine
//! code), `param` and `detail`; `/v1/messages` carries the Anthropic outer
//! `{"type": "error", "error": {...}}` shape with the same category/code
//! vocabulary.

mod support;

use std::thread;
use std::time::Duration;

use serde_json::{json, Value};

use um_gateway::error::ErrorProtocol;

use support::*;

fn assert_envelope(
    response: &ClientResponse,
    status: u16,
    protocol: ErrorProtocol,
    category: &str,
    code: &str,
    detail: Value,
) {
    assert_eq!(response.status, status);
    assert_eq!(
        response.header("content-type"),
        Some("application/json; charset=utf-8")
    );
    let body = response.json();
    let error = match protocol {
        ErrorProtocol::OpenAi => body
            .get("error")
            .unwrap_or_else(|| panic!("missing error object: {body}")),
        ErrorProtocol::Anthropic => {
            assert_eq!(body["type"], json!("error"), "{body}");
            body.get("error")
                .unwrap_or_else(|| panic!("missing error object: {body}"))
        }
    };
    assert_eq!(error["type"], json!(category), "{body}");
    assert_eq!(error["code"], json!(code), "{body}");
    assert_eq!(error["param"], Value::Null, "{body}");
    assert_eq!(error["detail"], detail, "{body}");
}

fn chat_body() -> String {
    json!({
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    })
    .to_string()
}

#[test]
fn a_400_invalid_json_is_an_invalid_request_error() {
    let temp = TempDir::new("um-rust-envelope-400-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", "{").bearer(SHARE_SECRET),
    )
    .expect("invalid json");
    assert_envelope(
        &response,
        400,
        ErrorProtocol::OpenAi,
        "invalid_request_error",
        "invalid_json",
        Value::Null,
    );
}

#[test]
fn a_401_invalid_share_key_is_an_authentication_error() {
    let temp = TempDir::new("um-rust-envelope-401-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body())
            .header("Content-Type", "application/json"),
    )
    .expect("missing share key");
    assert_envelope(
        &response,
        401,
        ErrorProtocol::OpenAi,
        "authentication_error",
        "invalid_api_key",
        Value::Null,
    );
}

#[test]
fn a_403_denied_origin_is_a_permission_error() {
    let temp = TempDir::new("um-rust-envelope-403-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json")
            .header("Origin", DENIED_ORIGIN),
    )
    .expect("denied origin");
    assert_envelope(
        &response,
        403,
        ErrorProtocol::OpenAi,
        "permission_error",
        "origin_denied",
        Value::Null,
    );
}

#[test]
fn a_404_unshared_route_is_a_not_found_error() {
    let temp = TempDir::new("um-rust-envelope-404-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/version").bearer(SHARE_SECRET),
    )
    .expect("unshared route");
    assert_envelope(
        &response,
        404,
        ErrorProtocol::OpenAi,
        "not_found_error",
        "route_not_shared",
        Value::Null,
    );
}

#[test]
fn a_413_oversize_body_is_an_invalid_request_error() {
    let temp = TempDir::new("um-rust-envelope-413-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.share_max_request_bytes = 10;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body()).bearer(SHARE_SECRET),
    )
    .expect("oversize body");
    assert_envelope(
        &response,
        413,
        ErrorProtocol::OpenAi,
        "invalid_request_error",
        "payload_too_large",
        Value::Null,
    );
}

#[test]
fn a_429_share_admission_is_a_rate_limit_error() {
    let temp = TempDir::new("um-rust-envelope-429-");
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        delay: Duration::from_millis(400),
        ..StubBehaviour::default()
    });
    let gateway = start_projection_gateway(&stub, &temp);
    let first_addr = gateway.addr;
    let first = thread::spawn(move || {
        client_request(
            first_addr,
            &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
        )
    });
    wait_until(Duration::from_secs(5), || {
        stub.seen()
            .iter()
            .any(|request| request.path == "/api/models")
    });
    let second = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SECOND_SECRET),
    )
    .expect("second request");
    assert_envelope(
        &second,
        429,
        ErrorProtocol::OpenAi,
        "rate_limit_error",
        "sharing_limit_reached",
        json!("global share concurrency limit reached (max 1 in-flight requests)"),
    );
    assert_eq!(second.json()["error"]["reason"], json!("share_concurrency"));
    assert_eq!(first.join().unwrap().unwrap().status, 200);
}

#[test]
fn a_503_reauthentication_required_is_an_authentication_error() {
    let temp = TempDir::new("um-rust-envelope-503-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 401,
        body: "{\"error\":\"upstream_rejected\"}".to_string(),
        retry_after: None,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json"),
    )
    .expect("upstream 401");
    assert_envelope(
        &response,
        503,
        ErrorProtocol::OpenAi,
        "authentication_error",
        "reauthentication_required",
        json!("upstream_rejected"),
    );
}

#[test]
fn messages_local_errors_use_the_anthropic_shape() {
    let temp = TempDir::new("um-rust-envelope-messages-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let invalid = client_request(
        gateway.addr,
        &ClientRequest::post(
            "/v1/messages",
            &json!({
                "model": "claude-x",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
                "unknown": true,
            })
            .to_string(),
        )
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json"),
    )
    .expect("invalid messages body");
    assert_envelope(
        &invalid,
        400,
        ErrorProtocol::Anthropic,
        "invalid_request_error",
        "unsupported_messages_request",
        Value::Null,
    );

    let missing_key = client_request(
        gateway.addr,
        &ClientRequest::post(
            "/v1/messages",
            &json!({
                "model": "claude-x",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            })
            .to_string(),
        )
        .header("Content-Type", "application/json"),
    )
    .expect("missing messages key");
    assert_envelope(
        &missing_key,
        401,
        ErrorProtocol::Anthropic,
        "authentication_error",
        "invalid_api_key",
        Value::Null,
    );
    assert_eq!(stub.attempts(), 0);
}
