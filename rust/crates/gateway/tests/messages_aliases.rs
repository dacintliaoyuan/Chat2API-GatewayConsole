//! S2a acceptance 6: `/api/v1/messages` is the local compatibility alias.
//! It does not pass the share-Key gate and is not in the public Cloudflare
//! whitelist; `/v1/messages` remains the shared route.
//!
//! Python source: `chat2api/chat2api.py:7383-7391` only admits `/v1/` paths
//! through the share gate; the alias falls through to `_api_allowed` /
//! `_origin_allowed` at `chat2api/chat2api.py:7467-7473`. The public
//! whitelist is frozen in `um-cloud-deploy/cloudflared/check-ingress.py:26`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, start_chat_gateway, wait_jsonl, ChatAttempt, ChatStub, ClientRequest, TempDir,
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

#[test]
fn the_alias_bypasses_the_share_key_gate_and_records_no_attribution() {
    let temp = TempDir::new("um-rust-messages-alias-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: message_response(),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/api/v1/messages", &body())
            .header("Content-Type", "application/json"),
    )
    .expect("local alias");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(response.json(), message_response());
    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/v1/messages");
    assert_eq!(
        seen[0].authorization.as_deref(),
        Some("Bearer fixture-upstream-token")
    );
    assert_eq!(seen[0].body["model"], json!("claude-x"));

    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["key_id"], json!(null));
    assert_eq!(rows[0]["request_id"], json!(null));
    assert_eq!(rows[0]["attribution"], json!("absent"));
    assert_eq!(rows[0]["protocol"], json!("messages"));
}

#[test]
fn the_share_v1_messages_route_still_requires_its_key() {
    let temp = TempDir::new("um-rust-messages-alias-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/messages", &body()).header("Content-Type", "application/json"),
    )
    .expect("v1 without a key");
    assert_eq!(response.status, 401);
    assert_eq!(response.json()["error"]["code"], json!("invalid_api_key"));
    assert_eq!(stub.attempts(), 0);
}

#[test]
fn the_alias_is_not_in_the_public_share_whitelist() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    assert!(
        fixture
            .cloudflare
            .share_paths
            .iter()
            .any(|path| path == "/v1/messages"),
        "/v1/messages stays the shared route"
    );
    assert!(
        !fixture
            .cloudflare
            .share_paths
            .iter()
            .any(|path| path == "/api/v1/messages"),
        "/api/v1/messages must not be in the public whitelist"
    );
}

#[test]
fn the_alias_uses_the_local_key_when_the_local_key_is_required() {
    let temp = TempDir::new("um-rust-messages-alias-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: message_response(),
    });
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.local_api_key = "local-secret".to_string();
        config.request_total = Duration::from_secs(5);
    });
    let missing = client_request(
        gateway.addr,
        &ClientRequest::post("/api/v1/messages", &body())
            .header("Content-Type", "application/json"),
    )
    .expect("alias without a local key");
    assert_eq!(missing.status, 401);
    assert_eq!(
        missing.json()["error"]["type"],
        json!("authentication_error")
    );
    assert_eq!(missing.json()["error"]["code"], json!("local_auth_error"));
    assert_eq!(stub.attempts(), 0);

    let accepted = client_request(
        gateway.addr,
        &ClientRequest::post("/api/v1/messages", &body())
            .header("Content-Type", "application/json")
            .header("X-Api-Key", "local-secret"),
    )
    .expect("alias with the local key");
    assert_eq!(accepted.status, 200);
    assert_eq!(stub.attempts(), 1);
}
