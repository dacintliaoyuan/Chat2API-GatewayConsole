//! S2a acceptance 1: the share credential on Messages accepts either
//! `Authorization: Bearer <key>` or `X-Api-Key: <key>`, resolves both to the
//! SAME key, and fails closed with 401 `invalid_api_key` when the two headers
//! carry different values.
//!
//! Python source: `chat2api/share_auth.py:1192-1243`
//! (`resolve_share_credential`) and `chat2api/chat2api.py:7383`
//! (`allow_api_key=path.startswith("/v1/messages")`).

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, start_chat_gateway, ChatAttempt, ChatStub, ClientRequest, TempDir,
    SECOND_SECRET, SHARE_SECRET,
};

fn body() -> String {
    json!({
        "model": "claude-x",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    .to_string()
}

fn message_response() -> Value {
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

fn gateway(stub: &ChatStub, temp: &TempDir) -> support::Gateway {
    start_chat_gateway(stub, temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    })
}

fn accepted(request: ClientRequest, name: &str) {
    let temp = TempDir::new("um-rust-messages-auth-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: message_response(),
    });
    let gateway = gateway(&stub, &temp);
    let response = client_request(gateway.addr, &request).unwrap_or_else(|error| {
        panic!("{name}: {error}");
    });
    assert_eq!(
        response.status,
        200,
        "{name}: {}",
        String::from_utf8_lossy(&response.body)
    );
    let seen = stub.seen();
    assert_eq!(seen.len(), 1, "{name}");
    assert_eq!(seen[0].path, "/api/v1/messages", "{name}");
}

fn rejected(request: ClientRequest, name: &str) {
    let temp = TempDir::new("um-rust-messages-auth-");
    let stub = ChatStub::start();
    let gateway = gateway(&stub, &temp);
    let response = client_request(gateway.addr, &request).unwrap_or_else(|error| {
        panic!("{name}: {error}");
    });
    assert_eq!(response.status, 401, "{name}");
    if request.target.starts_with("/v1/messages") {
        assert_eq!(response.json()["type"], json!("error"), "{name}");
    }
    assert_eq!(
        response.json()["error"]["type"],
        json!("authentication_error"),
        "{name}"
    );
    assert_eq!(
        response.json()["error"]["code"],
        json!("invalid_api_key"),
        "{name}"
    );
    assert_eq!(stub.attempts(), 0, "{name} must not reach the upstream");
}

#[test]
fn bearer_and_api_key_are_the_same_share_credential() {
    accepted(
        ClientRequest::post("/v1/messages", &body())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json"),
        "bearer",
    );
    accepted(
        ClientRequest::post("/v1/messages", &body())
            .header("X-Api-Key", SHARE_SECRET)
            .header("Content-Type", "application/json"),
        "api-key",
    );
    accepted(
        ClientRequest::post("/v1/messages", &body())
            .bearer(SHARE_SECRET)
            .header("X-Api-Key", SHARE_SECRET)
            .header("Content-Type", "application/json"),
        "both-same",
    );
}

#[test]
fn conflicting_credential_headers_are_401_before_any_upstream_request() {
    rejected(
        ClientRequest::post("/v1/messages", &body())
            .bearer(SHARE_SECRET)
            .header("X-Api-Key", SECOND_SECRET)
            .header("Content-Type", "application/json"),
        "different-values",
    );
}

#[test]
fn malformed_or_repeated_credentials_fail_closed() {
    rejected(
        ClientRequest::post("/v1/messages", &body())
            .header("Authorization", "Bearer ")
            .header("Content-Type", "application/json"),
        "empty-bearer",
    );
    rejected(
        ClientRequest::post("/v1/messages", &body())
            .header("Authorization", "Token secret")
            .header("Content-Type", "application/json"),
        "wrong-prefix",
    );
    rejected(
        ClientRequest::post("/v1/messages", &body())
            .header("X-Api-Key", "")
            .header("Content-Type", "application/json"),
        "empty-api-key",
    );
    rejected(
        ClientRequest::post("/v1/messages", &body())
            .header("X-Api-Key", SHARE_SECRET)
            .header("X-Api-Key", SHARE_SECRET)
            .header("Content-Type", "application/json"),
        "duplicate-api-key",
    );
    rejected(
        ClientRequest::post("/v1/messages", &body())
            .header("Authorization", "Bearer secret")
            .header("Authorization", "Bearer secret")
            .header("Content-Type", "application/json"),
        "duplicate-authorization",
    );
}

#[test]
fn the_api_key_shape_stays_messages_only() {
    rejected(
        ClientRequest::post("/v1/chat/completions", &body())
            .header("X-Api-Key", SHARE_SECRET)
            .header("Content-Type", "application/json"),
        "chat-keeps-bearer-only",
    );
}
