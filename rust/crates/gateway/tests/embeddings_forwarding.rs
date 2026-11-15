//! S2b acceptance 5/6: embeddings are relayed unchanged and write no ledger
//! row; an upstream non-200 answer is mapped like `_compat_proxy` (401 -> 503
//! reauthentication, 400/422 -> the same status, others -> 502); the shared
//! `/v1/embeddings` route is `404 route_not_shared`, while the local
//! `/api/v1/embeddings` alias bypasses the share-Key gate and is not in the
//! public Cloudflare whitelist.
//!
//! Python source: `chat2api/chat2api.py:7460-7464` (route) and `_compat_proxy`
//! at `chat2api/chat2api.py:7990-8465`; the public whitelist is
//! `um-cloud-deploy/cloudflared/check-ingress.py:26`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{client_request, start_chat_gateway, ChatAttempt, ChatStub, ClientRequest, TempDir};

fn request(path: &str, body: &Value) -> ClientRequest {
    ClientRequest::post(path, &body.to_string()).header("Content-Type", "application/json")
}

fn embedding_body() -> Value {
    json!({"model": "embedding-model",
           "input": ["first", "second"],
           "encoding_format": "float"})
}

#[test]
fn embeddings_are_relayed_unchanged_and_write_no_ledger_row() {
    let temp = TempDir::new("um-rust-embeddings-");
    let stub = ChatStub::start();
    let upstream_body = json!({
        "object": "list",
        "data": [{"object": "embedding", "index": 0,
                   "embedding": [0.1, 0.2]}],
        "model": "embedding-model",
        "usage": {"prompt_tokens": 2, "total_tokens": 2},
    });
    stub.push(ChatAttempt::Json {
        status: 200,
        body: upstream_body.clone(),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let body = embedding_body();
    let response = client_request(gateway.addr, &request("/v1/embeddings", &body))
        .expect("embeddings request");
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
        serde_json::to_vec(&upstream_body).expect("serialize fixture"),
        "the upstream body is relayed byte for byte"
    );

    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/v1/embeddings");
    assert_eq!(
        seen[0].authorization.as_deref(),
        Some("Bearer fixture-upstream-token")
    );
    assert_eq!(seen[0].body, body);
    assert!(
        !ledger.exists(),
        "embeddings must not append a usage ledger row"
    );
}

#[test]
fn a_400_embedding_response_keeps_the_status_without_a_ledger_row() {
    let temp = TempDir::new("um-rust-embeddings-status-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 400,
        body: "{\"error\":\"bad input\"}".to_string(),
        retry_after: None,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.usage_log = Some(ledger.clone());
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request("/v1/embeddings", &embedding_body()))
        .expect("embeddings request");
    assert_eq!(response.status, 400);
    assert_eq!(
        response.json(),
        json!({"error": {
            "message": "Upstream rejected the request; the server log has structured diagnostics",
            "type": "upstream_request_rejected",
            "detail": null,
        }})
    );
    assert!(
        !ledger.exists(),
        "a failed embeddings call writes no ledger"
    );
}

#[test]
fn a_401_embedding_response_is_a_503_reauthentication_error() {
    let temp = TempDir::new("um-rust-embeddings-status-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 401,
        body: "{\"error\":\"upstream_rejected\"}".to_string(),
        retry_after: None,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.usage_log = Some(ledger.clone());
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request("/v1/embeddings", &embedding_body()))
        .expect("embeddings request");
    assert_eq!(response.status, 503);
    assert_eq!(
        response.json(),
        json!({"error": {
            "message": "Open WebUI authentication requires an interactive sign-in",
            "type": "reauthentication_required",
            "detail": "upstream_rejected",
        }})
    );
    assert!(!ledger.exists());
}

#[test]
fn a_500_embedding_response_is_a_502_with_the_bounded_text() {
    let temp = TempDir::new("um-rust-embeddings-status-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 500,
        body: "upstream exploded".to_string(),
        retry_after: None,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.usage_log = Some(ledger.clone());
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request("/v1/embeddings", &embedding_body()))
        .expect("embeddings request");
    assert_eq!(response.status, 502);
    assert_eq!(
        response.json(),
        json!({"error": {
            "message": "Upstream request failed",
            "type": "chat2api_error",
            "detail": "HTTP 500: upstream exploded",
        }})
    );
    assert!(!ledger.exists());
}
