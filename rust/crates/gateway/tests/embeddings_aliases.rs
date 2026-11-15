//! S2b acceptance 6: the shared `/v1/embeddings` route is not shared
//! (`404 route_not_shared`), the local `/api/v1/embeddings` alias bypasses the
//! share-Key gate, and neither path is in the public Cloudflare whitelist.
//!
//! Python source: `chat2api/chat2api.py:7383-7391` (only `/v1/` paths enter the
//! share gate), `7460-7464` (the alias route) and
//! `um-cloud-deploy/cloudflared/check-ingress.py:26`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, start_chat_gateway, ChatAttempt, ChatStub, ClientRequest, TempDir, SHARE_SECRET,
};

fn request(path: &str, body: &Value) -> ClientRequest {
    ClientRequest::post(path, &body.to_string())
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
}

fn embedding_body() -> Value {
    json!({"model": "embedding-model", "input": ["first", "second"]})
}

#[test]
fn the_shared_embeddings_route_is_not_shared() {
    let temp = TempDir::new("um-rust-embeddings-shared-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request("/v1/embeddings", &embedding_body()))
        .expect("embeddings request");
    assert_eq!(response.status, 404);
    assert_eq!(
        response.json()["error"]["message"],
        json!("route_not_shared")
    );
    assert_eq!(response.json()["error"]["type"], json!("not_found_error"));
    assert_eq!(response.json()["error"]["code"], json!("route_not_shared"));
    assert_eq!(stub.attempts(), 0);
}

#[test]
fn the_local_alias_bypasses_the_share_key_gate() {
    let temp = TempDir::new("um-rust-embeddings-alias-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({"object": "list", "data": []}),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &request("/api/v1/embeddings", &embedding_body()),
    )
    .expect("embeddings alias");
    assert_eq!(response.status, 200);
    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/v1/embeddings");
    assert_eq!(seen[0].body, embedding_body());
}

#[test]
fn the_embeddings_paths_are_not_in_the_public_whitelist() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    for path in ["/v1/embeddings", "/api/v1/embeddings"] {
        assert!(
            !fixture
                .cloudflare
                .share_paths
                .iter()
                .any(|shared| shared == path),
            "{path} must not be in the public whitelist"
        );
    }
}
