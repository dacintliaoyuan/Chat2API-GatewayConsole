//! S1b acceptance 8: `/api/chat/completions` and
//! `/api/v1/chat/completions` are local compatibility aliases. They are not
//! share-Key gated and are not in the public Cloudflare whitelist.
//!
//! Python routing: `chat2api/chat2api.py:7380-7384` only admits `/v1/` paths
//! through the share gate; the aliases fall through to `_api_allowed` /
//! `_origin_allowed` (`chat2api/chat2api.py:7454-7459`). The public whitelist
//! is frozen in `um-cloud-deploy/cloudflared/check-ingress.py:26`.

mod support;

use std::time::Duration;

use serde_json::json;

use support::{
    client_request, read_jsonl, start_chat_gateway, wait_jsonl, ChatAttempt, ChatStub,
    ClientRequest, TempDir,
};

fn chat_body() -> String {
    json!({
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": false,
        "max_tokens": 16,
    })
    .to_string()
}

fn success_body() -> serde_json::Value {
    json!({
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
}

#[test]
fn the_aliases_bypass_the_share_key_gate_and_record_no_attribution() {
    for target in ["/api/chat/completions", "/api/v1/chat/completions"] {
        let temp = TempDir::new("um-rust-chat-alias-");
        let stub = ChatStub::start();
        stub.push(ChatAttempt::Json {
            status: 200,
            body: success_body(),
        });
        let ledger = temp.path().join("usage.jsonl");
        let gateway = start_chat_gateway(&stub, &temp, true, |config| {
            config.usage_log = Some(ledger.clone());
            config.request_total = Duration::from_secs(5);
        });
        let response = client_request(
            gateway.addr,
            &ClientRequest::post(target, &chat_body()).header("Content-Type", "application/json"),
        )
        .unwrap_or_else(|error| panic!("{target}: {error}"));
        assert_eq!(response.status, 200, "{target} is not share-Key gated");
        assert_eq!(response.json(), success_body());
        let seen = stub.seen();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].path, "/api/chat/completions");
        assert_eq!(
            seen[0].authorization.as_deref(),
            Some("Bearer fixture-upstream-token")
        );
        let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["key_id"], json!(null), "{target}");
        assert_eq!(rows[0]["request_id"], json!(null), "{target}");
        assert_eq!(rows[0]["attribution"], json!("absent"), "{target}");
        assert_eq!(rows[0]["protocol"], json!("chat"), "{target}");
        assert!(read_jsonl(&ledger).len() == 1);
    }
}

#[test]
fn the_share_v1_route_still_requires_its_key() {
    let temp = TempDir::new("um-rust-chat-alias-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body())
            .header("Content-Type", "application/json"),
    )
    .expect("v1 without a key");
    assert_eq!(response.status, 401);
    assert_eq!(response.json()["error"]["code"], json!("invalid_api_key"));
    assert_eq!(stub.attempts(), 0);
}

#[test]
fn the_aliases_are_not_in_the_public_share_whitelist() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    assert_eq!(
        fixture.cloudflare.share_paths,
        vec![
            "/v1/models",
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/responses",
        ]
    );
    for alias in ["/api/chat/completions", "/api/v1/chat/completions"] {
        assert!(
            !fixture
                .cloudflare
                .share_paths
                .iter()
                .any(|path| path == alias),
            "{alias} must not be in the public whitelist"
        );
    }
}
