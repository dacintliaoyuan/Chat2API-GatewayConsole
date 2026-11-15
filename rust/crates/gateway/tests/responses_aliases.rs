//! S2b acceptance 7: `/api/v1/responses` is the local compatibility alias.
//! It does not pass the share-Key gate and is not in the public Cloudflare
//! whitelist; `/v1/responses` remains the shared route.
//!
//! Python source: `chat2api/chat2api.py:7383-7391` (only `/v1/` paths enter
//! the share gate) and `um-cloud-deploy/cloudflared/check-ingress.py:26`.

mod support;

use std::time::Duration;

use serde_json::json;

use support::{
    client_request, start_chat_gateway, ChatAttempt, ChatStub, ClientRequest, TempDir, SHARE_SECRET,
};

fn body() -> String {
    json!({"model": "responses-model", "input": "hi"}).to_string()
}

fn chat_completion() -> serde_json::Value {
    json!({
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant",
                      "content": "alias"}, "finish_reason": "stop"}],
    })
}

#[test]
fn the_alias_bypasses_the_share_key_gate() {
    let temp = TempDir::new("um-rust-responses-alias-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: chat_completion(),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/api/v1/responses", &body())
            .header("Content-Type", "application/json"),
    )
    .expect("local alias");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        response.json()["output"][0]["content"][0]["text"],
        json!("alias")
    );
    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/chat/completions");
}

#[test]
fn the_shared_v1_responses_route_still_requires_its_key() {
    let temp = TempDir::new("um-rust-responses-shared-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/responses", &body()).header("Content-Type", "application/json"),
    )
    .expect("v1 without a key");
    assert_eq!(response.status, 401);
    assert_eq!(response.json()["error"]["code"], json!("invalid_api_key"));
    assert_eq!(stub.attempts(), 0);
}

#[test]
fn the_alias_is_not_in_the_public_whitelist() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    assert!(
        fixture
            .cloudflare
            .share_paths
            .iter()
            .any(|path| path == "/v1/responses"),
        "/v1/responses stays the shared route"
    );
    assert!(
        !fixture
            .cloudflare
            .share_paths
            .iter()
            .any(|path| path == "/api/v1/responses"),
        "/api/v1/responses must not be in the public whitelist"
    );
}

#[test]
fn the_share_key_gate_still_accepts_the_shared_route() {
    let temp = TempDir::new("um-rust-responses-shared-ok-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: chat_completion(),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/responses", &body())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json"),
    )
    .expect("shared route");
    assert_eq!(response.status, 200);
}
