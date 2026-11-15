//! S2a acceptance 4: the non-stream Anthropic JSON body is relayed unchanged
//! through the Python compatibility path (`POST /api/v1/messages`), with the
//! stored upstream bearer and the client's `Anthropic-Version`/`Anthropic-Beta`
//! headers. An upstream non-200 answer is mapped like `_compat_proxy`:
//! 401 -> 503 reauthentication, 400/422 -> the same status, others -> 502.
//!
//! Python source: `chat2api/chat2api.py:7467-7473` (route), `_compat_proxy`
//! at `chat2api/chat2api.py:7990-8470` and `_passthrough` at
//! `chat2api/chat2api.py:9670-9688`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, start_chat_gateway, ChatAttempt, ChatStub, ClientRequest, TempDir, SHARE_SECRET,
};

fn messages_body(stream: Option<bool>) -> Value {
    let mut body = json!({
        "model": "claude-x",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    });
    if let Some(stream) = stream {
        body["stream"] = json!(stream);
    }
    body
}

fn anthropic_message() -> Value {
    json!({
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": "claude-x",
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": null,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    })
}

fn request(body: &Value) -> ClientRequest {
    ClientRequest::post("/v1/messages", &body.to_string())
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
        .header("Anthropic-Version", "2023-06-01")
        .header("Anthropic-Beta", "tools-2024-04-04")
}

#[test]
fn non_stream_json_is_relayed_unchanged_with_the_anthropic_headers() {
    let temp = TempDir::new("um-rust-messages-json-");
    let stub = ChatStub::start();
    let upstream_body = anthropic_message();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: upstream_body.clone(),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let body = messages_body(None);
    let response = client_request(gateway.addr, &request(&body)).expect("messages request");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(response.header("content-type"), Some("application/json"));
    assert_eq!(response.json(), upstream_body);
    assert_eq!(
        response.body,
        serde_json::to_vec(&upstream_body).expect("serialize fixture")
    );

    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/v1/messages");
    assert_eq!(
        seen[0].authorization.as_deref(),
        Some("Bearer fixture-upstream-token")
    );
    assert_eq!(seen[0].anthropic_version.as_deref(), Some("2023-06-01"));
    assert_eq!(seen[0].anthropic_beta.as_deref(), Some("tools-2024-04-04"));
    assert_eq!(
        seen[0].body, body,
        "the accepted body is forwarded unchanged"
    );
}

#[test]
fn a_missing_stream_field_is_forwarded_unchanged() {
    let temp = TempDir::new("um-rust-messages-json-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: anthropic_message(),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let body = messages_body(None);
    let response = client_request(gateway.addr, &request(&body)).expect("messages request");
    assert_eq!(response.status, 200);
    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(
        seen[0].body.get("stream"),
        None,
        "Python forwards the body unchanged; it never adds a stream default"
    );
    assert_eq!(seen[0].body, body);
}

#[test]
fn a_400_upstream_response_keeps_the_status_without_passthrough() {
    let temp = TempDir::new("um-rust-messages-json-");
    let stub = ChatStub::start();
    let upstream_body = json!({
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "bad request"},
    });
    stub.push(ChatAttempt::HttpStatus {
        status: 400,
        body: serde_json::to_string(&upstream_body).expect("serialize body"),
        retry_after: None,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(&messages_body(Some(false))))
        .expect("messages request");
    assert_eq!(response.status, 400);
    assert_eq!(
        response.json(),
        json!({"type": "error", "error": {
            "message": "Upstream rejected the request; the server log has structured diagnostics",
            "type": "upstream_error",
            "code": "upstream_request_rejected",
            "param": null,
            "detail": null,
        }})
    );
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn a_401_upstream_response_is_a_503_reauthentication_error() {
    let temp = TempDir::new("um-rust-messages-json-");
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
    let response = client_request(gateway.addr, &request(&messages_body(Some(false))))
        .expect("messages request");
    assert_eq!(response.status, 503);
    assert_eq!(
        response.json(),
        json!({"type": "error", "error": {
            "message": "Open WebUI authentication requires an interactive sign-in",
            "type": "authentication_error",
            "code": "reauthentication_required",
            "param": null,
            "detail": "upstream_rejected",
        }})
    );
}

#[test]
fn a_500_upstream_response_is_a_502_with_the_bounded_text() {
    let temp = TempDir::new("um-rust-messages-json-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 500,
        body: "upstream exploded".to_string(),
        retry_after: None,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(&messages_body(Some(false))))
        .expect("messages request");
    assert_eq!(response.status, 502);
    assert_eq!(
        response.json(),
        json!({"type": "error", "error": {
            "message": "Upstream request failed",
            "type": "upstream_error",
            "code": "chat2api_error",
            "param": null,
            "detail": "HTTP 500: upstream exploded",
        }})
    );
}
